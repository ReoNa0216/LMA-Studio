import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import (
    AppData,
    AnnotationStore,
    BadRequest,
    ProjectPaths,
    acquisition_layout_hash,
    calibration_protocol_from_manifest,
    calibration_protocol_hash,
    normalize_acquisition_layout,
    normalize_calibration_protocol,
    normalize_post_qc_strategy,
    post_qc_strategy_hash,
    raw_file_fingerprint,
)
from scripts.v3.lif_peak_detection import (
    adaptive_lif_peak_detection,
    lif_peak_detection_hash,
)
from tests.test_calibration_protocol import lif_peak, ms_event


def create_legacy_project(root: Path, *, qc_end_min: float = 12.0) -> tuple[Path, Path]:
    project_dir = root / "legacy_project"
    table_dir = project_dir / "results" / "tables" / "v3"
    db_path = project_dir / "annotation_app" / "annotations" / "annotation.sqlite"
    table_dir.mkdir(parents=True)

    lif_traces = pd.DataFrame(
        [
            {"channel": "G2", "label": "Day0", "detector": "green", "time_min": 1.0, "time_sec": 60.0, "rfu": 1.0},
            {"channel": "R1", "label": "Day9", "detector": "red", "time_min": 1.0, "time_sec": 60.0, "rfu": 1.0},
            {"channel": "R2", "label": "Day3", "detector": "red", "time_min": 20.0, "time_sec": 1200.0, "rfu": 1.0},
        ]
    )
    lif_peaks = pd.DataFrame(
        [
            lif_peak("G2", "g2_1", 60.0),
            lif_peak("R1", "r1_1", 60.0),
            lif_peak("G2", "g2_late", 1200.0),
            lif_peak("R2", "r2_1", 1200.0),
        ]
    )
    lif_peaks["peak_stage"] = "merged"
    detector_config = adaptive_lif_peak_detection()
    detector_hash = lif_peak_detection_hash(detector_config)
    lif_peaks["peak_tier"] = "core"
    lif_peaks["detector_version"] = 2
    lif_peaks["detector_config_hash"] = detector_hash
    ms_events = pd.DataFrame(
        [
            ms_event("ms_1", 65.0),
            ms_event("ms_2", 1205.0),
        ]
    )
    ms_scan = pd.DataFrame(
        [{"scan_id": "scan_1", "scan_start_time_min": 65.0 / 60.0, "tic": 1.0}]
    )
    table_paths = {
        "lif_traces": table_dir / "01_lif_traces_qc.parquet",
        "lif_peaks": table_dir / "01_lif_peaks_qc.parquet",
        "ms_events": table_dir / "02_ms_events_qc.parquet",
        "ms_scan_summary": table_dir / "02_ms_scan_summary_qc.parquet",
    }
    for frame, key in [
        (lif_traces, "lif_traces"),
        (lif_peaks, "lif_peaks"),
        (ms_events, "ms_events"),
        (ms_scan, "ms_scan_summary"),
    ]:
        frame.to_parquet(table_paths[key], index=False)

    manifest = {
        "project_schema_version": 2,
        "acquisition_layout": {
            "layout_version": 3,
            "lif_channels": [
                {
                    "input_id": "lif_g2_raw",
                    "channel": "G2",
                    "identity_prior": "Day0",
                    "time_axis": "green_axis",
                    "detector": "green",
                    "use_for_cell_annotation": True,
                },
                {
                    "input_id": "lif_r1_raw",
                    "channel": "R1",
                    "identity_prior": "Day9",
                    "time_axis": "red_axis",
                    "detector": "red",
                    "use_for_cell_annotation": True,
                },
                {
                    "input_id": "lif_r2_raw",
                    "channel": "R2",
                    "identity_prior": "Day3",
                    "time_axis": "red_axis",
                    "detector": "red",
                    "use_for_cell_annotation": True,
                },
            ],
            "qc_anchor_channels": ["G2", "R1"],
        },
        "channel_identity_prior": {"G2": "Day0", "R1": "Day9", "R2": "Day3"},
        "lif_peak_detection": detector_config,
        "lif_peak_detection_hash": detector_hash,
        "intermediate_tables": {},
        "annotation_db": {
            "path": "annotation_app/annotations/annotation.sqlite",
            "schema_version": 2,
        },
    }
    for key, path in table_paths.items():
        manifest["intermediate_tables"][key] = {
            "path": path.relative_to(project_dir).as_posix(),
            **raw_file_fingerprint(path, full_hash_limit_bytes=None),
        }
    (project_dir / "lifms_project.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    store = AnnotationStore(db_path)
    store.update_project_config({"qc_calibration_end_min": qc_end_min})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM project_config WHERE key IN ('calibration_protocol', 'post_qc_strategy')"
        )
    return project_dir, db_path


def create_hsc_app(root: Path) -> AppData:
    layout = normalize_acquisition_layout(
        {
            "layout_version": 4,
            "lif_channels": [
                {
                    "channel": "G1",
                    "identity_prior": "LSK",
                    "detector": "green",
                    "time_axis": "green_axis",
                    "use_for_cell_annotation": True,
                },
                {
                    "channel": "G2",
                    "identity_prior": "Lin-",
                    "detector": "green",
                    "time_axis": "green_axis",
                    "use_for_cell_annotation": True,
                },
            ],
        }
    )
    protocol = normalize_calibration_protocol(
        {
            "segments": [
                {
                    "segment_id": "lsk_reference",
                    "order": 1,
                    "start_min": 0.0,
                    "end_min": 2.0,
                    "reference_channels": ["G1"],
                    "boundaries_confirmed": True,
                },
                {
                    "segment_id": "lin_reference",
                    "order": 2,
                    "start_min": 3.0,
                    "end_min": 5.0,
                    "reference_channels": ["G2"],
                    "boundaries_confirmed": True,
                },
            ]
        },
        layout,
    )
    strategy = normalize_post_qc_strategy({"mode": "disabled"}, layout)
    store = AnnotationStore(
        root / "annotation.sqlite",
        default_project_config={
            "qc_calibration_end_min": 5.0,
            "annotation_start_min": 24.0,
            "local_delta_seed_window_min": 2.5,
            "calibration_protocol": protocol,
            "post_qc_strategy": strategy,
        },
    )
    lif = pd.DataFrame(
        [
            lif_peak("G1", "g1_a", 30.0),
            lif_peak("G1", "g1_b", 60.0),
            lif_peak("G2", "g2_a", 210.0),
            lif_peak("G2", "g2_b", 240.0),
        ]
    )
    ms = pd.DataFrame(
        [
            ms_event("ms_a", 35.0),
            ms_event("ms_b", 65.0),
            ms_event("ms_c", 215.0),
            ms_event("ms_d", 245.0),
        ]
    )
    return AppData(
        project=ProjectPaths.from_args(project_dir=root),
        lif_traces=pd.DataFrame(),
        lif_peaks=lif,
        ms_events=ms,
        ms_scan=pd.DataFrame(),
        alignment={
            "model": "segmented",
            "green_to_ms_shift_sec": 5.0,
            "red_to_ms_shift_sec": 0.0,
            "axis_shifts_sec": {"green_axis": 5.0},
            "channel_time_axes": {"G1": "green_axis", "G2": "green_axis"},
            "qc_anchor_channels": ["G1", "G2"],
            "acquisition_layout_hash": acquisition_layout_hash(layout),
            "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
            "qc_groups": {"groups": []},
        },
        store=store,
        channel_identity_prior={
            "G1": {"identity_prior": "LSK"},
            "G2": {"identity_prior": "Lin-"},
        },
        acquisition_layout=layout,
        calibration_protocol=protocol,
        post_qc_strategy=strategy,
    )


class ProtocolRegressionTest(unittest.TestCase):
    def test_legacy_and_explicit_post_qc_matchers_have_distinct_hashes(self):
        layout = normalize_acquisition_layout(
            {
                "layout_version": 3,
                "lif_channels": [
                    {
                        "channel": "G2",
                        "detector": "green",
                        "time_axis": "green_axis",
                        "use_for_cell_annotation": True,
                    },
                    {
                        "channel": "R1",
                        "detector": "red",
                        "time_axis": "red_axis",
                        "use_for_cell_annotation": True,
                    },
                ],
                "qc_anchor_channels": ["G2", "R1"],
            }
        )
        legacy = normalize_post_qc_strategy(
            {
                "mode": "signature",
                "reference_channels": ["G2", "R1"],
                "compatibility_mode": "v0.3_qc_anchor_channels",
            },
            layout,
        )
        explicit = normalize_post_qc_strategy(
            {"mode": "signature", "reference_channels": ["G2", "R1"]},
            layout,
        )

        self.assertEqual(legacy["mode"], explicit["mode"])
        self.assertEqual(legacy["reference_channels"], explicit["reference_channels"])
        self.assertNotEqual(
            post_qc_strategy_hash(legacy, layout),
            post_qc_strategy_hash(explicit, layout),
        )

    def test_explicit_legacy_post_qc_edit_switches_to_g2_only_signature_matcher(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp), qc_end_min=12.0)
            app = AppData.load(
                ProjectPaths.from_args(
                    project_dir=str(project_dir),
                    annotation_db=str(db_path),
                )
            )
            active = app.active_time_model()
            app.store.upsert_time_model(
                {**active, "status": "frozen"},
                action="test_freeze_legacy_time_model",
            )
            edited = copy.deepcopy(app.project_config()["post_qc_strategy"])
            self.assertEqual(edited["compatibility_mode"], "v0.3_qc_anchor_channels")
            edited["reference_channels"] = ["G2"]

            updated = app.update_project_config({"post_qc_strategy": edited})
            candidates = app.build_post_qc_candidates(19.0, 21.0, "aligned")

            self.assertNotIn("compatibility_mode", updated["post_qc_strategy"])
            self.assertEqual(updated["post_qc_strategy"]["reference_channels"], ["G2"])
            self.assertEqual([row["ms_event_id"] for row in candidates], ["ms_2"])
            self.assertEqual(candidates[0]["anchor_channels"], ["G2"])
            self.assertEqual(candidates[0]["candidate_type"], "qc_survey_signature")

    def test_legacy_partial_manual_post_qc_keeps_v03_type_and_acceptance_semantics(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp), qc_end_min=12.0)
            app = AppData.load(
                ProjectPaths.from_args(
                    project_dir=str(project_dir),
                    annotation_db=str(db_path),
                )
            )
            app.update_project_config({"annotation_start_min": 15.0})
            active = app.active_time_model()
            app.store.upsert_time_model(
                {**active, "status": "frozen"},
                action="test_freeze_legacy_partial_manual_time_model",
            )

            row = app.create_manual_triplet(
                None,
                None,
                "ms_2",
                stage="qc_survey",
                window_start_min=19.0,
                window_end_min=21.0,
                time_mode="aligned",
                lif_anchor_peak_ids={"G2": "g2_late", "R1": None},
            )

            self.assertEqual(row["candidate_type"], "manual_qc_anchor_partial")
            self.assertEqual(row["review_stage"], "qc_survey")
            self.assertEqual(row["review_status"], "accepted")
            self.assertTrue(app.is_qc_survey_annotation(row))
            self.assertEqual(app.accepted_qc_survey_ms_event_ids(), {"ms_2"})

    def test_legacy_load_keeps_compatibility_projection_out_of_sqlite(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp), qc_end_min=12.0)

            app = AppData.load(
                ProjectPaths.from_args(
                    project_dir=str(project_dir),
                    annotation_db=str(db_path),
                )
            )

            with sqlite3.connect(db_path) as conn:
                persisted_keys = {
                    str(row[0])
                    for row in conn.execute("SELECT key FROM project_config").fetchall()
                }
            self.assertNotIn("calibration_protocol", persisted_keys)
            self.assertNotIn("post_qc_strategy", persisted_keys)
            self.assertEqual(
                app.project_config()["calibration_protocol"]["segments"][0]["end_min"],
                12.0,
            )

    def test_legacy_qc_end_update_refreshes_compat_protocol_and_hash_in_same_process(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp), qc_end_min=12.0)
            app = AppData.load(
                ProjectPaths.from_args(
                    project_dir=str(project_dir),
                    annotation_db=str(db_path),
                )
            )
            before_hash = app.project_config()["calibration_protocol_hash"]

            updated = app.update_project_config({"qc_calibration_end_min": 13.0})
            expected_protocol = calibration_protocol_from_manifest(
                app.manifest,
                {"qc_calibration_end_min": 13.0},
            )
            expected_hash = calibration_protocol_hash(
                expected_protocol,
                app.acquisition_layout,
            )

            self.assertEqual(updated["qc_calibration_end_min"], 13.0)
            self.assertEqual(
                updated["calibration_protocol"]["segments"][0]["end_min"],
                13.0,
            )
            self.assertEqual(updated["calibration_protocol_hash"], expected_hash)
            self.assertNotEqual(updated["calibration_protocol_hash"], before_hash)

    def test_unchanged_legacy_compatibility_objects_are_not_persisted_on_config_save(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp), qc_end_min=12.0)
            app = AppData.load(
                ProjectPaths.from_args(
                    project_dir=str(project_dir),
                    annotation_db=str(db_path),
                )
            )
            current = app.project_config()

            app.update_project_config(
                {
                    "annotation_start_min": current["annotation_start_min"],
                    "calibration_protocol": copy.deepcopy(
                        current["calibration_protocol"]
                    ),
                    "post_qc_strategy": copy.deepcopy(
                        current["post_qc_strategy"]
                    ),
                }
            )

            with sqlite3.connect(db_path) as conn:
                persisted_keys = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT key FROM project_config"
                    ).fetchall()
                }
            self.assertNotIn("calibration_protocol", persisted_keys)
            self.assertNotIn("post_qc_strategy", persisted_keys)

    def test_scheduled_windows_cannot_overlap_front_calibration(self):
        cases = [
            ("partial_overlap", 4.0, 6.0),
            ("entirely_before_front_end", 2.0, 4.0),
        ]
        for name, start_min, end_min in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True
            ) as tmp:
                app = create_hsc_app(Path(tmp))
                strategy = {
                    "mode": "scheduled_windows",
                    "windows": [
                        {
                            "window_id": "invalid_post_qc",
                            "start_min": start_min,
                            "end_min": end_min,
                            "reference_channels": ["G1"],
                        }
                    ],
                }

                with self.assertRaisesRegex(BadRequest, "scheduled|QC|前段|重叠"):
                    app.update_project_config({"post_qc_strategy": strategy})
                self.assertEqual(
                    app.store.project_config()["post_qc_strategy"]["mode"],
                    "disabled",
                )

    def test_annotation_start_exact_boundary_uses_frozen_post_delta_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = create_hsc_app(Path(tmp))
            app.update_project_config(
                {
                    "post_qc_strategy": {
                        "mode": "signature",
                        "reference_channels": ["G2"],
                    }
                }
            )
            active = app.active_time_model()
            app.store.upsert_time_model(
                {**active, "status": "frozen", "ms_local_delta_sec": 2.0},
                action="test_freeze_exact_annotation_start_delta",
            )
            object.__setattr__(
                app,
                "lif_peaks",
                pd.DataFrame([lif_peak("G2", "g2_boundary", 1435.0)]),
            )
            object.__setattr__(
                app,
                "ms_events",
                pd.DataFrame([ms_event("ms_boundary", 1440.0)]),
            )

            candidates = app.build_post_qc_candidates(23.5, 24.5, "aligned")

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["ms_event_id"], "ms_boundary")
            self.assertAlmostEqual(candidate["ms_time_min"], 24.0)
            self.assertAlmostEqual(candidate["g2_plot_time_min"], 24.0)
            self.assertAlmostEqual(candidate["ms_plot_time_min"], 1442.0 / 60.0)
            self.assertAlmostEqual(candidate["composite_to_ms_residual_sec"], 2.0)
            self.assertAlmostEqual(candidate["ms_local_delta_sec"], 2.0)


if __name__ == "__main__":
    unittest.main()
