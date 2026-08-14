import hashlib
import gc
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import annotation_app.app as app_module
from annotation_app.cell_event_map import CellEventMapError, match_source_to_events
from annotation_app.app import (
    AppData,
    AnnotationStore,
    BadRequest,
    ProjectPaths,
    RAW_INPUT_MODE_COPY,
    read_project_manifest,
    read_sqlite_project_binding,
    sqlite_annotation_count,
    validate_project_manifest_against_files,
)
from scripts.v3.lif_peak_detection import (
    adaptive_lif_peak_detection,
    lif_peak_detection_hash,
)
from scripts.v3.project_storage import (
    canonical_storage_layout_manifest_entry,
    project_uses_canonical_storage,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPOSITORY_ROOT.parent
REAL_V04_PROJECT = next(
    (
        candidate
        for candidate in (
            WORKSPACE_ROOT / "Lin-_LSK_LMA",
            WORKSPACE_ROOT / "Lin-_LSK",
        )
        if (candidate / "lifms_project.json").is_file()
    ),
    WORKSPACE_ROOT / "Lin-_LSK_LMA",
)

CANONICAL_TABLE_PATHS = {
    "lif_traces": Path("data/lif_traces.parquet"),
    "lif_peaks": Path("data/lif_peaks.parquet"),
    "ms_events": Path("data/ms_events.parquet"),
    "ms_scan_summary": Path("data/ms_scan_summary.parquet"),
}
CANONICAL_EVENT_MAP_PATH = Path("data/cell_event_map.csv")
CANONICAL_ANNOTATION_DB_PATH = Path("annotations/annotation.sqlite")
LEGACY_TABLE_PATHS = {
    "lif_traces": Path(
        "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet"
    ),
    "lif_peaks": Path(
        "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet"
    ),
    "ms_events": Path(
        "data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet"
    ),
    "ms_scan_summary": Path(
        "data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet"
    ),
}


def _peak_row(channel: str, peak_id: str, time_min: float) -> dict:
    detector = adaptive_lif_peak_detection()
    return {
        "channel": channel,
        "peak_id": peak_id,
        "time_sec": time_min * 60.0,
        "time_min": time_min,
        "snr": 50.0,
        "nearest_gap_sec": 10.0,
        "close_peak_risk": False,
        "merge_risk": False,
        "peak_stage": "merged",
        "peak_tier": "core",
        "detector_version": detector["detector_version"],
        "detector_config_hash": lif_peak_detection_hash(detector),
        "detector_profile": detector["profile"],
        "weak_usage": detector["weak_usage"],
    }


def _write_synthetic_preprocessor_outputs(script_name: str, project_dir: Path) -> str:
    """Stand in for raw parsing while retaining the new-project storage contract."""

    project_dir = Path(project_dir)
    if "01_lif" in script_name:
        frames = {
            "lif_traces": pd.DataFrame(
                [
                    {
                        "channel": "G1",
                        "label": "LSK",
                        "detector": "green",
                        "time_min": 2.0,
                        "time_sec": 120.0,
                        "rfu": 1.0,
                    },
                    {
                        "channel": "G2",
                        "label": "Lin-",
                        "detector": "green",
                        "time_min": 14.0,
                        "time_sec": 840.0,
                        "rfu": 1.0,
                    },
                ]
            ),
            "lif_peaks": pd.DataFrame(
                [
                    _peak_row("G1", "g1_core_1", 2.0),
                    _peak_row("G2", "g2_core_1", 14.0),
                ]
            ),
        }
    elif "02_ms" in script_name:
        frames = {
            "ms_events": pd.DataFrame(
                [
                    {
                        "event_id": "ms760_24_001",
                        "scan_id": "scan_1",
                        "time_sec": 24.001 * 60.0,
                        "time_min": 24.001,
                        "event_strategy": "pc34_primary",
                        "primary_signal_col": "pc34_760_max_intensity",
                        "pc34_760_apex": 20000.0,
                        "qc_782_apex": 1000.0,
                        "nearest_event_gap_sec": 10.0,
                        "collision_risk_high": False,
                        "low_quality_scan_window": False,
                    }
                ]
            ),
            "ms_scan_summary": pd.DataFrame(
                [
                    {
                        "scan_id": "scan_1",
                        "scan_start_time_min": 24.001,
                        "tic": 1.0,
                    }
                ]
            ),
        }
    else:  # pragma: no cover - guards the preprocessing call contract
        raise AssertionError(f"unexpected preprocessing script: {script_name}")

    for key, frame in frames.items():
        destination = project_dir / CANONICAL_TABLE_PATHS[key]
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(destination, index=False)
    return f"synthetic {script_name} complete"


def _write_dual_layout_preprocessor_outputs(script_name: str, project_dir: Path) -> str:
    """Let atomic-publication tests reach the DB gate before either layout wins."""

    message = _write_synthetic_preprocessor_outputs(script_name, project_dir)
    project_dir = Path(project_dir)
    keys = (
        ("lif_traces", "lif_peaks")
        if "01_lif" in script_name
        else ("ms_events", "ms_scan_summary")
    )
    for key in keys:
        legacy_path = project_dir / LEGACY_TABLE_PATHS[key]
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project_dir / CANONICAL_TABLE_PATHS[key], legacy_path)
    return message


def _unconfirmed_green_protocol() -> dict:
    return {
        "protocol_version": 1,
        "segments": [
            {
                "segment_id": "lsk_reference",
                "order": 1,
                "start_min": 1.5,
                "end_min": 3.5,
                "reference_channels": ["G1"],
                "reference_mode": "green_only",
                "population_label": "LSK",
                "boundaries_confirmed": False,
            },
            {
                "segment_id": "lin_reference",
                "order": 2,
                "start_min": 13.0,
                "end_min": 16.0,
                "reference_channels": ["G2"],
                "reference_mode": "green_only",
                "population_label": "Lin-",
                "boundaries_confirmed": False,
            },
        ],
    }


def _new_project_request(root: Path, project_name: str = "new_project") -> dict:
    g1_path = root / f"{project_name}-G1.csv"
    g2_path = root / f"{project_name}-G2.csv"
    ms_path = root / f"{project_name}-MS.txt"
    event_map_path = root / f"{project_name}-event-coordinates.csv"
    g1_path.write_bytes(b"synthetic-g1")
    g2_path.write_bytes(b"synthetic-g2")
    ms_path.write_bytes(b"synthetic-ms")
    event_map_path.write_text(
        "CellNumber,scan_start_time,UMAP1,UMAP2,batch\n"
        "Cell00001,24.001,1.25,-2.5,1\n",
        encoding="utf-8",
    )
    return {
        "project_dir": root / project_name,
        "ms_path": ms_path,
        "lif_inputs": [
            {
                "key": "lif_g1",
                "channel": "G1",
                "identity_prior": "LSK",
                "path": g1_path,
                "use_for_cell_annotation": True,
            },
            {
                "key": "lif_g2",
                "channel": "G2",
                "identity_prior": "Lin-",
                "path": g2_path,
                "use_for_cell_annotation": True,
            },
        ],
        "calibration_protocol": _unconfirmed_green_protocol(),
        "post_qc_strategy": {"mode": "disabled"},
        "annotation_start_min": 24.0,
        "cell_event_map_path": event_map_path,
    }


def _tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Capture persisted bytes, excluding SQLite's transient WAL/SHM sidecars."""

    snapshot: dict[str, tuple[int, str]] = {}
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and not item.name.endswith((".sqlite-wal", ".sqlite-shm"))
    ):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(block)
        snapshot[path.relative_to(root).as_posix()] = (
            path.stat().st_size,
            digest.hexdigest(),
        )
    return snapshot


class CanonicalProjectStorageTest(unittest.TestCase):
    def test_failed_event_map_import_returns_diagnostics_without_publishing_project(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "failed-map-project")
            request["cell_event_map_path"].write_text(
                "scan_start_time,UMAP1,UMAP2,Type,CellNumber\n"
                "99.0,1.0,2.0,AUTHOR_LABEL,Cell99999\n",
                encoding="utf-8",
            )
            diagnostic_source = pd.DataFrame(
                {"scan_start_time": [99.0], "UMAP1": [1.0], "UMAP2": [2.0]}
            )
            diagnostic_events = pd.DataFrame(
                [
                    {
                        "event_id": "ms760_24_001",
                        "scan_id": "scan_1",
                        "time_min": 24.001,
                        "event_strategy": "pc34_primary",
                        "primary_signal_col": "pc34_760_max_intensity",
                    }
                ]
            )
            with self.assertRaises(CellEventMapError) as diagnostic_raised:
                match_source_to_events(diagnostic_source, diagnostic_events)

            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_synthetic_preprocessor_outputs,
            ), mock.patch(
                "annotation_app.app.reconcile_event_roster_supported_ms_events",
                side_effect=diagnostic_raised.exception,
            ), self.assertRaises(BadRequest) as raised:
                AppData.create_project_from_raw_inputs(**request)

            self.assertEqual(raised.exception.code, "cell_event_map_import_failed")
            diagnostic = raised.exception.details["event_map_diagnostic"]
            self.assertEqual(diagnostic["summary"]["unmatched_rows"], 1)
            self.assertIn("outside_recognized_event_range", diagnostic["csv_text"])
            self.assertNotIn("AUTHOR_LABEL", diagnostic["csv_text"])
            self.assertNotIn("Cell99999", diagnostic["csv_text"])
            self.assertFalse(request["project_dir"].exists())
            self.assertEqual(list(root.glob(".*.lma-building-*")), [])

    def test_staging_recheck_preserves_unique_peak_support_event_map_binding(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "peak_support_staging_project")

            def write_peak_support_outputs(script_name: str, project_dir: Path) -> str:
                message = _write_synthetic_preprocessor_outputs(script_name, project_dir)
                if "02_ms" in script_name:
                    path = Path(project_dir) / CANONICAL_TABLE_PATHS["ms_events"]
                    events = pd.read_parquet(path)
                    source_time_min = 24.001
                    events["time_min"] = source_time_min + 0.207 / 60.0
                    events["time_sec"] = events["time_min"] * 60.0
                    events["left_sec"] = source_time_min * 60.0 - 0.02
                    events["right_sec"] = events["time_sec"] + 0.10
                    events.to_parquet(path, index=False)
                return message

            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=write_peak_support_outputs,
            ):
                created = AppData.create_project_from_raw_inputs(**request)

            self.assertEqual(len(created.cell_event_map), 1)
            self.assertEqual(
                created.cell_event_map_info["match_policy"],
                "apex_tolerance_then_unique_near_peak_shape_v3",
            )
            self.assertEqual(
                created.cell_event_map_info["peak_support_match_count"],
                1,
            )

    @unittest.skipUnless(shutil.which("git"), "git is required for the v0.4.0 reader probe")
    def test_released_v040_reader_opens_and_exports_a_new_canonical_project(self):
        """The additive layout marker must remain forward-readable by v0.4.0."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "forward_open_project")
            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_synthetic_preprocessor_outputs,
            ):
                created = AppData.create_project_from_raw_inputs(**request)
            del created
            gc.collect()

            archive_path = root / "v0.4.0-source.zip"
            archived = subprocess.run(
                [
                    "git",
                    "archive",
                    "--format=zip",
                    f"--output={archive_path}",
                    "v0.4.0",
                    "annotation_app",
                    "scripts",
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if archived.returncode != 0:
                self.skipTest(f"v0.4.0 source archive unavailable: {archived.stderr}")
            old_source = root / "v0.4.0-source"
            shutil.unpack_archive(archive_path, old_source)
            probe = "\n".join(
                [
                    "import json, sys",
                    f"sys.path.insert(0, {str(old_source)!r})",
                    "from annotation_app.app import AppData, ProjectPaths",
                    f"root = {str(request['project_dir'])!r}",
                    "app = AppData.load(ProjectPaths.from_args(project_dir=root))",
                    "result = app.export_accepted_annotations_csv()",
                    "print(json.dumps({",
                    "  'channels': sorted(set(app.lif_peaks['channel'].astype(str))),",
                    "  'tables': {key: value['path'] for key, value in app.manifest['intermediate_tables'].items()},",
                    "  'db': app.manifest['annotation_db']['path'],",
                    "  'map_rows': len(app.cell_event_map),",
                    "  'export_rows': result['row_count'],",
                    "}, sort_keys=True))",
                ]
            )
            opened = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=old_source,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(opened.returncode, 0, opened.stderr)
            payload = json.loads(opened.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["channels"], ["G1", "G2"])
            self.assertEqual(
                payload["tables"],
                {key: path.as_posix() for key, path in CANONICAL_TABLE_PATHS.items()},
            )
            self.assertEqual(
                payload["db"],
                CANONICAL_ANNOTATION_DB_PATH.as_posix(),
            )
            self.assertEqual(payload["map_rows"], 1)
            self.assertEqual(payload["export_rows"], 1)

    def test_canonical_layout_marker_cannot_describe_a_hybrid_runtime_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            manifest = {
                "project_schema_version": 3,
                "storage_layout": canonical_storage_layout_manifest_entry(),
                "intermediate_tables": {
                    key: {
                        "path": path.as_posix(),
                        "size_bytes": 1,
                        "sha256": f"sha-{key}",
                    }
                    for key, path in CANONICAL_TABLE_PATHS.items()
                },
                "annotation_db": {
                    "path": CANONICAL_ANNOTATION_DB_PATH.as_posix(),
                    "schema_version": 3,
                },
                "cell_event_map": {
                    "path": CANONICAL_EVENT_MAP_PATH.as_posix(),
                },
            }
            mutations = [
                ("table", LEGACY_TABLE_PATHS["lif_traces"].as_posix()),
                ("annotation_db", "annotation_app/annotations/annotation.sqlite"),
                ("cell_event_map", "data/interim/lma/cell_event_umap.csv"),
            ]
            for kind, wrong_path in mutations:
                with self.subTest(kind=kind):
                    candidate = json.loads(json.dumps(manifest))
                    if kind == "table":
                        candidate["intermediate_tables"]["lif_traces"]["path"] = wrong_path
                    elif kind == "annotation_db":
                        candidate["annotation_db"]["path"] = wrong_path
                    else:
                        candidate["cell_event_map"]["path"] = wrong_path
                    with mock.patch(
                        "annotation_app.app.require_file",
                        side_effect=AssertionError("hybrid layout reached artifact I/O"),
                    ) as require_mock:
                        with self.assertRaisesRegex(BadRequest, "目录布局"):
                            validate_project_manifest_against_files(
                                project_dir,
                                candidate,
                            )
                        require_mock.assert_not_called()

    def test_canonical_protocol_requires_its_explicit_storage_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol_path = root / "provenance/project_protocol.json"
            protocol_path.parent.mkdir(parents=True)
            for storage_layout in (None, {"name": "wrong", "layout_version": 1}):
                with self.subTest(storage_layout=storage_layout):
                    payload = {"schema_version": 3}
                    if storage_layout is not None:
                        payload["storage_layout"] = storage_layout
                    protocol_path.write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "目录布局"):
                        project_uses_canonical_storage(root)

    def test_dual_canonical_and_legacy_protocol_markers_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "provenance/project_protocol.json"
            legacy = root / "results/tables/v3/00_project_protocol.json"
            canonical.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            canonical.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "storage_layout": canonical_storage_layout_manifest_entry(),
                    }
                ),
                encoding="utf-8",
            )
            legacy.write_text(
                json.dumps({"schema_version": 3}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "同时包含"):
                project_uses_canonical_storage(root)

    def test_manifest_owned_paths_are_preflighted_before_any_artifact_io(self):
        """All mutable/runtime artifacts belong to the project portability boundary."""

        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            safe_tables = {
                key: {
                    "path": path.as_posix(),
                    "size_bytes": 1,
                    "sha256": f"sha-{key}",
                }
                for key, path in CANONICAL_TABLE_PATHS.items()
            }
            outside_absolute = str((Path(tmp) / "outside.sqlite").resolve())
            unsafe_paths = [outside_absolute, "../outside-artifact"]

            for artifact_kind in (
                "intermediate_table",
                "annotation_db",
                "cell_event_map",
            ):
                for unsafe_path in unsafe_paths:
                    with self.subTest(
                        artifact_kind=artifact_kind,
                        unsafe_path=unsafe_path,
                    ):
                        manifest = {
                            "project_schema_version": 3,
                            "intermediate_tables": json.loads(
                                json.dumps(safe_tables)
                            ),
                            "annotation_db": {
                                "path": CANONICAL_ANNOTATION_DB_PATH.as_posix(),
                                "schema_version": 3,
                            },
                            "cell_event_map": {
                                "path": CANONICAL_EVENT_MAP_PATH.as_posix(),
                            },
                        }
                        if artifact_kind == "intermediate_table":
                            manifest["intermediate_tables"]["lif_traces"][
                                "path"
                            ] = unsafe_path
                        elif artifact_kind == "annotation_db":
                            manifest["annotation_db"]["path"] = unsafe_path
                        else:
                            manifest["cell_event_map"]["path"] = unsafe_path

                        with mock.patch(
                            "annotation_app.app.require_file",
                            side_effect=AssertionError(
                                "artifact I/O occurred before path preflight"
                            ),
                        ) as require_mock, mock.patch(
                            "annotation_app.app.raw_file_fingerprint",
                            side_effect=AssertionError(
                                "artifact hashing occurred before path preflight"
                            ),
                        ) as fingerprint_mock:
                            with self.assertRaises(BadRequest):
                                validate_project_manifest_against_files(
                                    project_dir,
                                    manifest,
                                )
                            require_mock.assert_not_called()
                            fingerprint_mock.assert_not_called()

            self.assertFalse((Path(tmp) / "outside.sqlite").exists())

    def test_copy_mode_rejects_traversal_key_before_staging_or_copy(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "unsafe_key_project")
            request["raw_input_mode"] = RAW_INPUT_MODE_COPY
            request["lif_inputs"][0]["key"] = "../../outside_lif"
            original_mkdir = Path.mkdir

            def reject_staging_mkdir(path: Path, *args, **kwargs):
                if any(".lma-building-" in part for part in path.parts):
                    raise AssertionError(
                        "staging was created before validating the copy key"
                    )
                return original_mkdir(path, *args, **kwargs)

            with mock.patch.object(
                Path,
                "mkdir",
                new=reject_staging_mkdir,
            ), mock.patch(
                "annotation_app.app.shutil.copy2",
                side_effect=AssertionError(
                    "copy was attempted before validating the copy key"
                ),
            ) as copy_mock:
                with self.assertRaises(BadRequest):
                    AppData.create_project_from_raw_inputs(**request)
                copy_mock.assert_not_called()

            self.assertFalse(request["project_dir"].exists())
            self.assertFalse((root / "outside_lif.csv").exists())
            self.assertEqual(list(root.glob(".*.lma-building-*")), [])

    def test_staging_is_not_publishable_without_db_and_matching_binding(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "staging_binding_project")
            real_commit = app_module.commit_staging_project

            def checked_commit(
                staging_dir: Path,
                project_dir: Path,
                *,
                target_preexisted: bool,
            ) -> None:
                db_path = Path(staging_dir) / CANONICAL_ANNOTATION_DB_PATH
                self.assertTrue(
                    db_path.is_file(),
                    "annotation.sqlite must exist and be validated inside staging",
                )
                manifest = read_project_manifest(Path(staging_dir))
                sqlite_binding = read_sqlite_project_binding(db_path)
                self.assertIsNotNone(sqlite_binding)
                self.assertEqual(
                    sqlite_binding["binding_sha256"],
                    manifest["project_table_binding"]["binding_sha256"],
                )
                real_commit(
                    Path(staging_dir),
                    Path(project_dir),
                    target_preexisted=target_preexisted,
                )

            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_dual_layout_preprocessor_outputs,
            ), mock.patch(
                "annotation_app.app.commit_staging_project",
                side_effect=checked_commit,
            ):
                AppData.create_project_from_raw_inputs(**request)

            self.assertTrue(request["project_dir"].is_dir())

    def test_db_initialization_failure_never_publishes_target(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "db_failure_project")
            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_dual_layout_preprocessor_outputs,
            ), mock.patch.object(
                AnnotationStore,
                "_init_db",
                side_effect=RuntimeError("synthetic DB initialization failure"),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "synthetic DB initialization failure",
                ):
                    AppData.create_project_from_raw_inputs(**request)

            self.assertFalse(
                request["project_dir"].exists(),
                "a DB failure must leave the requested target unpublished",
            )
            self.assertEqual(list(root.glob(".*.lma-building-*")), [])

    def test_ms_input_change_during_preprocessing_aborts_without_publication(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "changing_ms_project")

            def mutate_ms_after_outputs(script_name: str, project_dir: Path) -> str:
                message = _write_synthetic_preprocessor_outputs(script_name, project_dir)
                if "02_ms" in script_name:
                    with Path(request["ms_path"]).open("ab") as handle:
                        handle.write(b"-changed-while-parsing")
                return message

            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=mutate_ms_after_outputs,
            ):
                with self.assertRaisesRegex(BadRequest, "MS 原始文件.*发生变化"):
                    AppData.create_project_from_raw_inputs(**request)

            self.assertFalse(request["project_dir"].exists())
            self.assertEqual(list(root.glob(".*.lma-building-*")), [])

    def test_copy_mode_ms_change_between_fingerprint_and_copy_is_rejected(self):
        """MS copy mode needs the same source-vs-copy stability gate as LIF."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "changing_ms_during_copy")
            request["raw_input_mode"] = RAW_INPUT_MODE_COPY
            source_ms = Path(request["ms_path"]).resolve()
            real_copy2 = shutil.copy2
            mutated = False

            def mutate_before_ms_copy(source, destination, *args, **kwargs):
                nonlocal mutated
                source_path = Path(source).resolve()
                if source_path == source_ms and not mutated:
                    source_path.write_bytes(
                        source_path.read_bytes() + b"-changed-before-copy-read"
                    )
                    mutated = True
                return real_copy2(source, destination, *args, **kwargs)

            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_synthetic_preprocessor_outputs,
            ), mock.patch(
                "annotation_app.app.shutil.copy2",
                side_effect=mutate_before_ms_copy,
            ):
                with self.assertRaises(BadRequest):
                    AppData.create_project_from_raw_inputs(**request)

            self.assertTrue(mutated)
            self.assertFalse(request["project_dir"].exists())
            self.assertEqual(list(root.glob(".*.lma-building-*")), [])

    def test_final_staging_load_failure_never_publishes_target(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            request = _new_project_request(root, "staging_load_failure_project")
            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_dual_layout_preprocessor_outputs,
            ), mock.patch.object(
                AppData,
                "load",
                side_effect=RuntimeError("synthetic final staging load failure"),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "synthetic final staging load failure",
                ):
                    AppData.create_project_from_raw_inputs(**request)

            self.assertFalse(
                request["project_dir"].exists(),
                "a final staging-load failure must leave the target unpublished",
            )
            self.assertEqual(list(root.glob(".*.lma-building-*")), [])

    def test_post_publish_reopen_failure_rolls_back_the_requested_target(self):
        """The final reopen is part of the atomic project-creation transaction."""

        for target_preexists in (False, True):
            with self.subTest(target_preexists=target_preexists), tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True
            ) as tmp:
                root = Path(tmp)
                request = _new_project_request(root, "post_publish_failure_project")
                if target_preexists:
                    request["project_dir"].mkdir()
                real_load = AppData.load
                load_count = 0

                def fail_only_after_publish(project):
                    nonlocal load_count
                    load_count += 1
                    if load_count == 1:
                        return real_load(project)
                    raise RuntimeError("synthetic post-publish reopen failure")

                with mock.patch(
                    "annotation_app.app.run_preprocessing_script",
                    side_effect=_write_dual_layout_preprocessor_outputs,
                ), mock.patch.object(
                    AppData,
                    "load",
                    side_effect=fail_only_after_publish,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "synthetic post-publish reopen failure",
                    ):
                        AppData.create_project_from_raw_inputs(**request)

                self.assertEqual(load_count, 2)
                if target_preexists:
                    self.assertTrue(request["project_dir"].is_dir())
                    self.assertEqual(list(request["project_dir"].iterdir()), [])
                else:
                    self.assertFalse(request["project_dir"].exists())
                self.assertEqual(list(root.glob(".*.lma-building-*")), [])
                self.assertEqual(list(root.glob(".*.lma-building-rollback-*")), [])

    def test_new_project_is_born_with_canonical_layout_and_matching_binding(self):
        """New projects must not expose historical pipeline-version directories."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            project_dir = root / "new_project"
            g1_path = root / "G1.csv"
            g2_path = root / "G2.csv"
            ms_path = root / "MS.txt"
            event_map_path = root / "event_coordinates.csv"
            g1_path.write_bytes(b"synthetic-g1")
            g2_path.write_bytes(b"synthetic-g2")
            ms_path.write_bytes(b"synthetic-ms")
            event_map_path.write_text(
                "CellNumber,scan_start_time,UMAP1,UMAP2,batch\n"
                "Cell00001,24.001,1.25,-2.5,1\n",
                encoding="utf-8",
            )
            protocol = {
                "protocol_version": 1,
                "segments": [
                    {
                        "segment_id": "lsk_reference",
                        "order": 1,
                        "start_min": 1.5,
                        "end_min": 3.5,
                        "reference_channels": ["G1"],
                        "reference_mode": "green_only",
                        "population_label": "LSK",
                        "boundaries_confirmed": False,
                    },
                    {
                        "segment_id": "lin_reference",
                        "order": 2,
                        "start_min": 13.0,
                        "end_min": 16.0,
                        "reference_channels": ["G2"],
                        "reference_mode": "green_only",
                        "population_label": "Lin-",
                        "boundaries_confirmed": False,
                    },
                ],
            }

            with mock.patch(
                "annotation_app.app.run_preprocessing_script",
                side_effect=_write_synthetic_preprocessor_outputs,
            ):
                app = AppData.create_project_from_raw_inputs(
                    project_dir=project_dir,
                    ms_path=ms_path,
                    lif_inputs=[
                        {
                            "key": "lif_g1",
                            "channel": "G1",
                            "identity_prior": "LSK",
                            "path": g1_path,
                            "use_for_cell_annotation": True,
                        },
                        {
                            "key": "lif_g2",
                            "channel": "G2",
                            "identity_prior": "Lin-",
                            "path": g2_path,
                            "use_for_cell_annotation": True,
                        },
                    ],
                    calibration_protocol=protocol,
                    post_qc_strategy={"mode": "disabled"},
                    annotation_start_min=24.0,
                    cell_event_map_path=event_map_path,
                )

            self.assertIsInstance(app, AppData)
            expected_files = [
                *CANONICAL_TABLE_PATHS.values(),
                CANONICAL_EVENT_MAP_PATH,
                CANONICAL_ANNOTATION_DB_PATH,
                Path("lifms_project.json"),
                Path("provenance/input_manifest.csv"),
                Path("provenance/project_protocol.json"),
                Path("provenance/preprocessing.log"),
                Path("provenance/preprocessing_report.md"),
                Path("README.md"),
            ]
            for relative_path in expected_files:
                self.assertTrue(
                    (project_dir / relative_path).is_file(),
                    f"missing canonical project file: {relative_path.as_posix()}",
                )
            self.assertTrue((project_dir / "annotations/exports").is_dir())
            for required_dir in (
                "data",
                "annotations",
                "provenance",
                "diagnostics/lif",
                "diagnostics/ms",
            ):
                self.assertTrue(
                    (project_dir / required_dir).is_dir(),
                    f"missing canonical project directory: {required_dir}",
                )
            project_readme = (project_dir / "README.md").read_text(encoding="utf-8")
            for required_text in (
                "data",
                "annotations",
                "provenance",
                "diagnostics",
                "可以整体重命名",
                "不要单独移动",
                "可以打开、查看、继续标注和导出",
                "不包含原始 LIF/MS 文件",
                "先关闭 LMA Studio",
            ):
                self.assertIn(required_text, project_readme)

            user_paths = [
                path.relative_to(project_dir)
                for path in project_dir.rglob("*")
            ]
            self.assertFalse(
                any("v3" in {part.lower() for part in path.parts} for path in user_paths),
                "new user projects must not expose a historical v3 directory",
            )
            for retired_root in ("annotation_app", "reports", "results"):
                self.assertFalse((project_dir / retired_root).exists())

            manifest = read_project_manifest(project_dir)
            self.assertEqual(
                manifest["annotation_db"]["path"],
                CANONICAL_ANNOTATION_DB_PATH.as_posix(),
            )
            self.assertEqual(
                {
                    key: entry["path"]
                    for key, entry in manifest["intermediate_tables"].items()
                },
                {key: path.as_posix() for key, path in CANONICAL_TABLE_PATHS.items()},
            )
            self.assertEqual(
                manifest["cell_event_map"]["path"],
                CANONICAL_EVENT_MAP_PATH.as_posix(),
            )
            sqlite_binding = read_sqlite_project_binding(
                project_dir / CANONICAL_ANNOTATION_DB_PATH
            )
            self.assertIsNotNone(sqlite_binding)
            self.assertEqual(
                sqlite_binding["binding_sha256"],
                manifest["project_table_binding"]["binding_sha256"],
            )
            with sqlite3.connect(project_dir / CANONICAL_ANNOTATION_DB_PATH) as conn:
                recorded_inputs = dict(
                    conn.execute(
                        "SELECT input_key, relative_path FROM input_manifest ORDER BY input_key"
                    ).fetchall()
                )
            self.assertEqual(
                recorded_inputs,
                {key: path.as_posix() for key, path in CANONICAL_TABLE_PATHS.items()},
            )
            self.assertFalse(
                any(".lma-building-" in path for path in recorded_inputs.values()),
                "published provenance must not retain a staging-directory path",
            )

    @unittest.skipUnless(
        (REAL_V04_PROJECT / "lifms_project.json").is_file(),
        "Lin-_LSK real-project integration fixture is not available",
    )
    def test_renamed_v04_project_copy_opens_without_writes_and_rejects_rebinding(self):
        """Legacy storage is immutable compatibility input, never an auto-migration target."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            copied_project = Path(tmp) / "arbitrarily-renamed-project"
            shutil.copytree(REAL_V04_PROJECT, copied_project)
            before_open = _tree_snapshot(copied_project)

            app = AppData.load(
                ProjectPaths.from_args(project_dir=str(copied_project))
            )
            self.assertEqual(
                {str(value) for value in app.lif_peaks["channel"].unique()},
                {"G1", "G2"},
            )
            self.assertGreater(
                sqlite_annotation_count(app.project.annotation_db_path),
                0,
            )
            del app
            gc.collect()
            self.assertEqual(_tree_snapshot(copied_project), before_open)

            original_manifest = read_project_manifest(copied_project)
            moved_manifest = json.loads(json.dumps(original_manifest))
            old_relative = Path(
                moved_manifest["intermediate_tables"]["lif_traces"]["path"]
            )
            new_relative = Path("data/lif_traces-moved-without-rebinding.parquet")
            (copied_project / new_relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(copied_project / old_relative, copied_project / new_relative)
            moved_manifest["intermediate_tables"]["lif_traces"]["path"] = (
                new_relative.as_posix()
            )
            (copied_project / "lifms_project.json").write_text(
                json.dumps(moved_manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BadRequest, "项目绑定与当前中间表不一致"):
                AppData.load(
                    ProjectPaths.from_args(project_dir=str(copied_project))
                )

            shutil.move(copied_project / new_relative, copied_project / old_relative)
            (copied_project / "lifms_project.json").write_text(
                json.dumps(original_manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            scan_relative = Path(
                original_manifest["intermediate_tables"]["ms_scan_summary"]["path"]
            )
            with (copied_project / scan_relative).open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(BadRequest, "记录不一致"):
                validate_project_manifest_against_files(
                    copied_project,
                    read_project_manifest(copied_project),
                )

    @unittest.skipUnless(
        (REAL_V04_PROJECT / "lifms_project.json").is_file(),
        "Lin-_LSK real-project integration fixture is not available",
    )
    def test_v040_project_copy_can_switch_umap_coordinates_and_reopen(self):
        """Coordinate switching is additive and keeps a formal v0.4 project operable."""

        original_before = _tree_snapshot(REAL_V04_PROJECT)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            copied_project = root / "renamed-v040-coordinate-switch"
            shutil.copytree(REAL_V04_PROJECT, copied_project)
            app = AppData.load(ProjectPaths.from_args(project_dir=str(copied_project)))
            manifest_before = read_project_manifest(copied_project)
            records_before = app.store.records()
            frozen_before = json.loads(json.dumps(app.frozen_time_model()))
            current_map = app.cell_event_map.copy()
            self.assertGreater(len(current_map), 0)

            replacement_source = root / "batch-uncorrected-coordinates.csv"
            replacement = current_map.loc[
                :, ["scan_start_time", "UMAP1", "UMAP2"]
            ].copy()
            replacement["UMAP1"] = replacement["UMAP1"] + 100.0
            replacement["UMAP2"] = replacement["UMAP2"] - 100.0
            replacement["batch"] = "uncorrected"
            replacement.to_csv(replacement_source, index=False)

            switched = app.attach_cell_event_map(replacement_source)
            self.assertEqual(switched.store.records(), records_before)
            self.assertEqual(switched.frozen_time_model(), frozen_before)
            manifest_after = read_project_manifest(copied_project)
            self.assertEqual(
                manifest_after["intermediate_tables"],
                manifest_before["intermediate_tables"],
            )
            self.assertEqual(
                manifest_after["annotation_db"],
                manifest_before["annotation_db"],
            )
            self.assertEqual(
                manifest_after["cell_event_map"]["path"],
                manifest_before["cell_event_map"]["path"],
            )
            self.assertEqual(
                manifest_after["cell_event_map_history"][-1]["sha256"],
                manifest_before["cell_event_map"]["sha256"],
            )
            self.assertNotIn(str(replacement_source.resolve()), json.dumps(manifest_after))

            target_event_id = str(current_map.iloc[0]["ms_event_id"])
            expected_umap1 = float(current_map.iloc[0]["UMAP1"]) + 100.0
            expected_umap2 = float(current_map.iloc[0]["UMAP2"]) - 100.0
            del app, switched
            gc.collect()

            reopened = AppData.load(
                ProjectPaths.from_args(project_dir=str(copied_project))
            )
            self.assertEqual(reopened.store.records(), records_before)
            self.assertEqual(reopened.frozen_time_model(), frozen_before)
            exported = reopened.export_accepted_annotations_csv()
            exported_frame = pd.read_csv(exported["csv_path"])
            exported_row = exported_frame.loc[
                exported_frame["MS_event_id"].astype(str) == target_event_id
            ]
            self.assertEqual(len(exported_row), 1)
            self.assertAlmostEqual(float(exported_row.iloc[0]["UMAP1"]), expected_umap1)
            self.assertAlmostEqual(float(exported_row.iloc[0]["UMAP2"]), expected_umap2)
            del reopened
            gc.collect()

        self.assertEqual(_tree_snapshot(REAL_V04_PROJECT), original_before)


if __name__ == "__main__":
    unittest.main()
