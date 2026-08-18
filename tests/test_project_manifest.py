import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path
import sqlite3

import pandas as pd

from annotation_app.app import (
    AppData,
    AnnotationStore,
    RAW_INPUT_MODE_COPY,
    RAW_INPUT_MODE_EXTERNAL,
    BadRequest,
    BootstrapAppData,
    HTML,
    ProjectPaths,
    acquisition_layout_hash,
    acquisition_layout_from_manifest,
    accepted_qc_alignment_refit,
    apply_qc_alignment_model,
    build_axis_peak_clusters,
    build_qc_alignment_groups,
    build_raw_input_project_records,
    candidate_id_for_group,
    channel_identity_prior_from_manifest,
    commit_staging_project,
    export_filename_for_project,
    estimate_local_delta_shift,
    estimate_axis_shift,
    estimate_shift_alignment,
    normalize_acquisition_layout,
    qc_anchor_peak_id_map,
    qc_relation_key,
    reconcile_qc_calibration_groups,
    qc_group_auto_accept_block_reason,
    qc_group_batch_accept_block_reason,
    unique_file_path,
    assert_new_project_target_is_clean,
    project_table_binding,
    read_project_manifest,
    validate_sqlite_project_binding,
    validate_annotation_db_against_tables,
    validate_project_manifest_against_files,
    validate_distinct_lif_input_files,
    write_project_manifest,
)


class ProjectManifestTest(unittest.TestCase):
    def test_lif_inputs_reject_duplicate_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared.csv"
            other = root / "other.csv"
            shared.write_bytes(b"shared")
            other.write_bytes(b"other")

            with self.assertRaisesRegex(BadRequest, "路径不能重复"):
                validate_distinct_lif_input_files(
                    [
                        {"channel": "R1", "path": shared},
                        {"channel": "G1", "path": shared},
                        {"channel": "G2", "path": other},
                    ]
                )

    def test_lif_inputs_reject_duplicate_sha256_at_different_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / f"lif_{index}.csv" for index in range(3)]
            paths[0].write_bytes(b"duplicate-content")
            paths[1].write_bytes(b"duplicate-content")
            paths[2].write_bytes(b"unique-content")

            with self.assertRaisesRegex(BadRequest, "SHA256 相同"):
                validate_distinct_lif_input_files(
                    [
                        {"channel": "R1", "path": paths[0]},
                        {"channel": "G1", "path": paths[1]},
                        {"channel": "G2", "path": paths[2]},
                    ]
                )

    def test_lif_inputs_accept_distinct_paths_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / f"lif_{index}.csv" for index in range(3)]
            for index, path in enumerate(paths):
                path.write_bytes(f"content-{index}".encode("ascii"))

            validate_distinct_lif_input_files(
                [
                    {"channel": "R1", "path": paths[0]},
                    {"channel": "G1", "path": paths[1]},
                    {"channel": "G2", "path": paths[2]},
                ]
            )

    def test_project_creation_rejects_duplicate_lif_content_before_creating_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "new-project"
            lif_paths = [root / f"lif_{index}.csv" for index in range(3)]
            lif_paths[0].write_bytes(b"same-lif-content")
            lif_paths[1].write_bytes(b"same-lif-content")
            lif_paths[2].write_bytes(b"different-lif-content")
            ms_path = root / "ms.txt"
            ms_path.write_bytes(b"ms")

            with self.assertRaisesRegex(BadRequest, "SHA256 相同"):
                AppData.create_project_from_raw_inputs(
                    project_dir=target,
                    ms_path=ms_path,
                    lif_inputs=[
                        {"key": "lif_1", "channel": "R1", "identity_prior": "CLP", "path": lif_paths[0]},
                        {"key": "lif_2", "channel": "G1", "identity_prior": "LK", "path": lif_paths[1]},
                        {"key": "lif_3", "channel": "G2", "identity_prior": "LSK", "path": lif_paths[2]},
                    ],
                    qc_anchor_channels=["R1", "G1"],
                )

            self.assertFalse(target.exists())

    def test_project_creation_rejects_duplicate_lif_keys_before_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "new-project"
            lif_paths = [root / f"lif_{index}.csv" for index in range(3)]
            for index, path in enumerate(lif_paths):
                path.write_bytes(f"distinct-lif-{index}".encode("ascii"))
            ms_path = root / "ms.txt"
            ms_path.write_bytes(b"ms")

            with self.assertRaisesRegex(BadRequest, "key 不能重复"):
                AppData.create_project_from_raw_inputs(
                    project_dir=target,
                    ms_path=ms_path,
                    raw_input_mode=RAW_INPUT_MODE_COPY,
                    lif_inputs=[
                        {"key": "lif_same", "channel": "R1", "identity_prior": "CLP", "path": lif_paths[0]},
                        {"key": "lif_same", "channel": "G1", "identity_prior": "LK", "path": lif_paths[1]},
                        {"key": "lif_3", "channel": "G2", "identity_prior": "LSK", "path": lif_paths[2]},
                    ],
                    qc_anchor_channels=["R1", "G1"],
                )

            self.assertFalse(target.exists())

    def test_external_reference_records_absolute_paths_without_raw_inputs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir = Path(tmp) / "project"
            raw_dir = Path(tmp) / "raw"
            raw_paths = {
                "lif_g2": raw_dir / "g2.csv",
                "lif_r1": raw_dir / "r1.csv",
                "lif_r2": raw_dir / "r2.csv",
                "ms": raw_dir / "ms.txt",
            }

            rows, manifest_inputs, _layout = build_raw_input_project_records(
                project_dir=project_dir,
                raw_paths=raw_paths,
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                identities={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
            )

            self.assertFalse((project_dir / "raw_inputs").exists())
            self.assertEqual(rows[0]["path"], str(raw_paths["lif_g2"].resolve()))
            self.assertEqual(rows[3]["path"], str(raw_paths["ms"].resolve()))
            self.assertEqual(manifest_inputs["ms"]["path_mode"], RAW_INPUT_MODE_EXTERNAL)
            self.assertEqual(manifest_inputs["ms"]["path"], str(raw_paths["ms"].resolve()))

    def test_copy_records_use_project_relative_raw_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            raw_dir = Path(tmp) / "raw"
            raw_paths = {
                "lif_g2": raw_dir / "g2.csv",
                "lif_r1": raw_dir / "r1.csv",
                "lif_r2": raw_dir / "r2.csv",
                "ms": raw_dir / "ms.txt",
            }

            rows, manifest_inputs, _layout = build_raw_input_project_records(
                project_dir=project_dir,
                raw_paths=raw_paths,
                raw_input_mode=RAW_INPUT_MODE_COPY,
                identities={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
            )

            self.assertEqual(rows[0]["path"], "raw_inputs/lif_g2.csv")
            self.assertEqual(rows[3]["path"], "raw_inputs/ms.txt")
            self.assertEqual(manifest_inputs["lif_g2"]["path_mode"], RAW_INPUT_MODE_COPY)
            self.assertEqual(manifest_inputs["lif_g2"]["path"], "raw_inputs/lif_g2.csv")

    def test_manifest_round_trip_and_channel_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)

            write_project_manifest(
                project_dir=project_dir,
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                raw_inputs={
                    "lif_g2": {
                        "path": str(Path(tmp) / "raw" / "g2.csv"),
                        "path_mode": RAW_INPUT_MODE_EXTERNAL,
                    }
                },
                channel_identity_prior={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
            )

            manifest = read_project_manifest(project_dir)
            self.assertEqual(manifest["raw_input_mode"], RAW_INPUT_MODE_EXTERNAL)
            self.assertEqual(manifest["annotation_db"]["path"], "annotation_app/annotations/annotation.sqlite")

            prior = channel_identity_prior_from_manifest(manifest)
            self.assertEqual(prior["G2"]["identity_prior"], "Day0")
            self.assertEqual(prior["G2"]["identity_prior_source"], "project_manifest")

    def test_manifest_round_trip_supports_configurable_lif_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            layout = {
                "lif_channels": [
                    {"input_id": "lif_1_raw", "channel": "G1", "identity_prior": "Day1", "time_axis": "green_axis"},
                    {"input_id": "lif_2_raw", "channel": "G2", "identity_prior": "Day0", "time_axis": "green_axis"},
                    {"input_id": "lif_3_raw", "channel": "R1", "identity_prior": "Day3", "time_axis": "red_axis"},
                ],
                "qc_anchor_channels": ["G2", "R1"],
            }

            manifest = write_project_manifest(
                project_dir=project_dir,
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                raw_inputs={},
                channel_identity_prior={"G1": "Day1", "G2": "Day0", "R1": "Day3"},
                acquisition_layout=layout,
            )

            loaded = acquisition_layout_from_manifest(manifest)
            self.assertEqual([row["channel"] for row in loaded["lif_channels"]], ["G1", "G2", "R1"])
            self.assertEqual(loaded["qc_anchor_channels"], ["G2", "R1"])
            prior = channel_identity_prior_from_manifest(manifest)
            self.assertEqual(prior["G1"]["identity_prior"], "Day1")
            self.assertEqual(prior["R1"]["identity_prior"], "Day3")

    def test_qc_anchor_set_is_canonical_and_must_cover_every_time_axis(self):
        layout = {
            "lif_channels": [
                {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "green_axis"},
                {"input_id": "lif_2_raw", "channel": "G2", "time_axis": "green_axis"},
                {"input_id": "lif_3_raw", "channel": "R1", "time_axis": "red_axis"},
            ],
            "qc_anchor_channels": ["R1", "G2", "G1"],
        }

        normalized = normalize_acquisition_layout(layout)

        self.assertEqual(normalized["qc_anchor_channels"], ["G1", "G2", "R1"])
        self.assertEqual(normalized["qc_anchor_time_axes"], ["green_axis", "red_axis"])
        with self.assertRaises(BadRequest):
            normalize_acquisition_layout({**layout, "qc_anchor_channels": ["G1", "G2"]})

        reversed_legacy = normalize_acquisition_layout(
            {
                "lif_channels": [
                    {"input_id": "lif_r1_raw", "channel": "R1", "time_axis": "red_axis"},
                    {"input_id": "lif_g2_raw", "channel": "G2", "time_axis": "green_axis"},
                    {"input_id": "lif_r2_raw", "channel": "R2", "time_axis": "red_axis"},
                ],
                "qc_anchor_channels": ["R1", "G2"],
            }
        )
        self.assertEqual(reversed_legacy["qc_anchor_channels"], ["G2", "R1"])

    def test_qc_anchor_set_requires_explicit_red_and_green_detectors(self):
        all_green_layout = {
            "lif_channels": [
                {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "green_axis"},
                {"input_id": "lif_2_raw", "channel": "G2", "time_axis": "green_axis"},
                {"input_id": "lif_3_raw", "channel": "G3", "time_axis": "green_axis"},
            ],
            "qc_anchor_channels": ["G1", "G2"],
        }

        with self.assertRaises(BadRequest):
            normalize_acquisition_layout(all_green_layout)
        with self.assertRaises(BadRequest):
            normalize_acquisition_layout({**all_green_layout, "qc_anchor_channels": []})

    def test_acquisition_layout_hash_tracks_physical_layout_not_schema_version(self):
        physical = {
            "lif_channels": [
                {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "green_axis"},
                {"input_id": "lif_2_raw", "channel": "G2", "time_axis": "green_axis"},
                {"input_id": "lif_3_raw", "channel": "R1", "time_axis": "red_axis"},
            ],
            "qc_anchor_channels": ["G1", "G2", "R1"],
        }

        self.assertEqual(
            acquisition_layout_hash({**physical, "layout_version": 1}),
            acquisition_layout_hash({**physical, "layout_version": 999}),
        )

    def test_two_to_four_lif_layouts_support_all_channel_roles(self):
        two = normalize_acquisition_layout(
            {
                "lif_channels": [
                    {"input_id": "g1", "channel": "G1", "time_axis": "green_axis", "use_for_cell_annotation": True},
                    {"input_id": "r1", "channel": "R1", "time_axis": "red_axis", "use_for_cell_annotation": True},
                ],
                "qc_anchor_channels": ["G1", "R1"],
            }
        )
        self.assertEqual([row["channel"] for row in two["lif_channels"]], ["G1", "R1"])

        four_source = {
            "lif_channels": [
                {"input_id": "g1", "channel": "G1", "time_axis": "green_axis", "use_for_cell_annotation": True},
                {"input_id": "g2", "channel": "G2", "time_axis": "green_axis", "use_for_cell_annotation": True},
                {"input_id": "r1", "channel": "R1", "time_axis": "red_axis", "use_for_cell_annotation": False},
                {"input_id": "r2", "channel": "R2", "time_axis": "red_axis", "use_for_cell_annotation": False},
            ],
            "qc_anchor_channels": ["G1", "R1"],
        }
        four = normalize_acquisition_layout(four_source)
        roles = {
            row["channel"]: (
                row["channel"] in four["qc_anchor_channels"],
                row["use_for_cell_annotation"],
            )
            for row in four["lif_channels"]
        }
        self.assertEqual(
            roles,
            {
                "G1": (True, True),
                "G2": (False, True),
                "R1": (True, False),
                "R2": (False, False),
            },
        )

        changed_cell_role = {
            **four_source,
            "lif_channels": [
                *four_source["lif_channels"][:-1],
                {**four_source["lif_channels"][-1], "use_for_cell_annotation": True},
            ],
        }
        changed_qc_role = {**four_source, "qc_anchor_channels": ["G1", "G2", "R1", "R2"]}
        self.assertNotEqual(acquisition_layout_hash(four_source), acquisition_layout_hash(changed_cell_role))
        self.assertNotEqual(acquisition_layout_hash(four_source), acquisition_layout_hash(changed_qc_role))

        with self.assertRaisesRegex(BadRequest, "至少一个 LIF"):
            normalize_acquisition_layout(
                {
                    **four_source,
                    "lif_channels": [
                        {**row, "use_for_cell_annotation": False}
                        for row in four_source["lif_channels"]
                    ],
                }
            )

    def test_build_raw_input_records_accepts_configured_lif_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            raw_dir = Path(tmp) / "raw"
            lif_inputs = [
                {"key": "lif_1", "path": raw_dir / "g1.csv", "channel": "G1", "identity_prior": "Day1"},
                {"key": "lif_2", "path": raw_dir / "g2.csv", "channel": "G2", "identity_prior": "Day0"},
                {"key": "lif_3", "path": raw_dir / "r1.csv", "channel": "R1", "identity_prior": "Day3"},
            ]

            rows, manifest_inputs, layout = build_raw_input_project_records(
                project_dir=project_dir,
                raw_paths={"ms": raw_dir / "ms.txt"},
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                identities={},
                lif_inputs=lif_inputs,
                qc_anchor_channels=["G1", "G2", "R1"],
            )

            lif_rows = [row for row in rows if row["input_class"] == "raw_lif_trace"]
            self.assertEqual([row["input_id"] for row in lif_rows], ["lif_1_raw", "lif_2_raw", "lif_3_raw"])
            self.assertEqual([row["channel"] for row in lif_rows], ["G1", "G2", "R1"])
            self.assertEqual([row["detector"] for row in lif_rows], ["green", "green", "red"])
            self.assertEqual(manifest_inputs["lif_1"]["path"], str((raw_dir / "g1.csv").resolve()))
            self.assertEqual(layout["qc_anchor_channels"], ["G1", "G2", "R1"])

            with self.assertRaises(BadRequest):
                build_raw_input_project_records(
                    project_dir=project_dir,
                    raw_paths={"ms": raw_dir / "ms.txt"},
                    raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                    identities={},
                    lif_inputs=lif_inputs,
                    qc_anchor_channels=[],
                )

    def test_build_raw_records_preserves_qc_only_cell_only_both_and_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lif_inputs = [
                {"key": "g1", "path": root / "g1.csv", "channel": "G1", "identity_prior": "both", "use_for_qc": True, "use_for_cell_annotation": True},
                {"key": "g2", "path": root / "g2.csv", "channel": "G2", "identity_prior": "cell", "use_for_qc": False, "use_for_cell_annotation": True},
                {"key": "r1", "path": root / "r1.csv", "channel": "R1", "identity_prior": "", "use_for_qc": True, "use_for_cell_annotation": False},
                {"key": "r2", "path": root / "r2.csv", "channel": "R2", "identity_prior": "", "use_for_qc": False, "use_for_cell_annotation": False},
            ]

            rows, _manifest, layout = build_raw_input_project_records(
                project_dir=root / "project",
                raw_paths={"ms": root / "ms.txt"},
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                identities={},
                lif_inputs=lif_inputs,
                qc_anchor_channels=["G1", "R1"],
            )

        lif_rows = [row for row in rows if row["input_class"] == "raw_lif_trace"]
        self.assertEqual(
            [row["use_for_cell_annotation"] for row in lif_rows],
            [True, True, False, False],
        )
        self.assertEqual(layout["qc_anchor_channels"], ["G1", "R1"])

    def test_backend_rejects_cell_annotation_on_qc_only_channel(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            layout = normalize_acquisition_layout(
                {
                    "lif_channels": [
                        {
                            "input_id": "lif_g1_raw",
                            "channel": "G1",
                            "identity_prior": "cell",
                            "time_axis": "green_axis",
                            "detector": "green",
                            "use_for_cell_annotation": True,
                        },
                        {
                            "input_id": "lif_r1_raw",
                            "channel": "R1",
                            "identity_prior": "QC-only",
                            "time_axis": "red_axis",
                            "detector": "red",
                            "use_for_cell_annotation": False,
                        },
                    ],
                    "qc_anchor_channels": ["G1", "R1"],
                }
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(
                    [{"peak_id": "r1", "channel": "R1", "time_min": 41.0, "time_sec": 2460.0}]
                ),
                ms_events=pd.DataFrame(
                    [
                        {
                            "event_id": "ms",
                            "event_strategy": "pc34_primary",
                            "primary_signal_col": "pc34_760_max_intensity",
                            "scan_id": 1,
                            "time_min": 41.0,
                            "time_sec": 2460.0,
                        }
                    ]
                ),
                ms_scan=pd.DataFrame(),
                alignment={"model": "test", "qc_groups": {"groups": []}},
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={"R1": {"identity_prior": "QC-only"}},
                acquisition_layout=layout,
            )

            with self.assertRaisesRegex(BadRequest, "未配置为细胞标注通道"):
                app.payload_from_cell_ids("R1", "r1", "ms")

    def test_project_dir_defaults_raw_data_dir_to_project_raw_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "Batch03"
            project = ProjectPaths.from_args(project_dir=project_dir)

            self.assertEqual(project.raw_data_dir, project_dir.resolve() / "raw_inputs")

    def test_bootstrap_meta_hides_project_when_none_selected(self):
        data = BootstrapAppData(project=ProjectPaths.from_args(), load_error="", project_selected=False)

        meta = data.meta()

        self.assertTrue(meta["bootstrap"])
        self.assertIsNone(meta["project"])
        self.assertEqual(meta["root"], "")

    def test_export_filename_uses_safe_project_name_and_timestamp(self):
        filename = export_filename_for_project(
            Path("projects") / "Batch:03 Test",
            datetime(2026, 7, 1, 22, 5, 6),
        )

        self.assertEqual(filename, "Batch_03 Test_20260701_220506_accepted_annotations.csv")
        self.assertNotIn(":", filename)

    def test_unique_file_path_appends_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "Batch03_20260701_220506_accepted_annotations.csv"
            first.write_text("old", encoding="utf-8")

            self.assertEqual(
                unique_file_path(first).name,
                "Batch03_20260701_220506_accepted_annotations_2.csv",
            )

    def test_qc_alignment_groups_use_configurable_qc_end_boundary(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g2_11", "channel": "G2", "time_min": 11.0, "time_sec": 660.0, "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "r1_11", "channel": "R1", "time_min": 11.0, "time_sec": 660.0, "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {
                    "event_id": "ms_11",
                    "time_min": 11.0,
                    "time_sec": 660.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                    "pc34_760_apex": 1000.0,
                    "nearest_event_gap_sec": 10,
                    "collision_risk_high": False,
                    "low_quality_scan_window": False,
                }
            ]
        )

        groups = build_qc_alignment_groups(
            lif_peaks,
            ms_events,
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            qc_calibration_end_min=12.0,
        )

        self.assertEqual([row["ms_event_id"] for row in groups["groups"]], ["ms_11"])

    def test_qc_alignment_groups_use_configured_anchor_channels(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g1_11", "channel": "G1", "time_min": 11.0, "time_sec": 660.0, "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "r1_11", "channel": "R1", "time_min": 11.0, "time_sec": 660.0, "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "g2_11", "channel": "G2", "time_min": 11.2, "time_sec": 672.0, "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {
                    "event_id": "ms_11",
                    "time_min": 11.0,
                    "time_sec": 660.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                    "pc34_760_apex": 1000.0,
                    "nearest_event_gap_sec": 10,
                    "collision_risk_high": False,
                    "low_quality_scan_window": False,
                }
            ]
        )

        groups = build_qc_alignment_groups(
            lif_peaks,
            ms_events,
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
            channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
            qc_anchor_channels=["G1", "R1"],
            qc_calibration_end_min=12.0,
        )

        self.assertEqual(groups["anchor_channels"], ["G1", "R1"])
        self.assertEqual(groups["groups"][0]["anchor_a_peak_id"], "g1_11")
        self.assertEqual(groups["groups"][0]["anchor_b_peak_id"], "r1_11")
        self.assertEqual(groups["groups"][0]["ms_event_id"], "ms_11")
        self.assertEqual(groups["groups"][0]["lif_anchor_peak_ids"], {"G1": "g1_11", "R1": "r1_11"})
        self.assertEqual(groups["groups"][0]["g1_peak_id"], "g1_11")
        self.assertIsNone(groups["groups"][0]["g2_peak_id"])
        self.assertTrue(candidate_id_for_group(groups["groups"][0]).startswith("auto_qc:v2:"))

    def test_three_channel_anchor_groups_allow_missing_same_axis_support(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g1_1", "channel": "G1", "time_min": 1.0000, "time_sec": 60.00, "snr": 30},
                {"peak_id": "g2_1", "channel": "G2", "time_min": 1.0017, "time_sec": 60.10, "snr": 40},
                {"peak_id": "r1_1", "channel": "R1", "time_min": 1.1175, "time_sec": 67.05, "snr": 50},
                {"peak_id": "g2_2", "channel": "G2", "time_min": 2.0000, "time_sec": 120.00, "snr": 40},
                {"peak_id": "r1_2", "channel": "R1", "time_min": 2.1167, "time_sec": 127.00, "snr": 50},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": "ms_1", "time_min": 1.0008, "time_sec": 60.05, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
                {"event_id": "ms_2", "time_min": 2.0008, "time_sec": 120.05, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
            ]
        )

        result = build_qc_alignment_groups(
            lif_peaks,
            ms_events,
            qc_calibration_end_min=3.0,
            axis_shifts_sec={"green_axis": 0.0, "red_axis": -7.0},
            channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
            qc_anchor_channels=["G1", "G2", "R1"],
        )

        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(result["groups"][0]["complete_anchor_set"])
        self.assertEqual(result["groups"][0]["lif_anchor_count"], 3)
        self.assertFalse(result["groups"][1]["complete_anchor_set"])
        self.assertEqual(result["groups"][1]["missing_lif_channels"], ["G1"])
        self.assertEqual(result["groups"][1]["covered_time_axes"], ["green_axis", "red_axis"])

    def test_three_channel_anchor_group_does_not_merge_incoherent_same_axis_peaks(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g1", "channel": "G1", "time_min": 1.0000, "time_sec": 60.0, "snr": 100},
                {"peak_id": "g2", "channel": "G2", "time_min": 1.0583, "time_sec": 63.5, "snr": 10},
                {"peak_id": "r1", "channel": "R1", "time_min": 1.1167, "time_sec": 67.0, "snr": 50},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": "ms", "time_min": 1.0292, "time_sec": 61.75, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
            ]
        )

        result = build_qc_alignment_groups(
            lif_peaks,
            ms_events,
            qc_calibration_end_min=2.0,
            axis_shifts_sec={"green_axis": 0.0, "red_axis": -7.0},
            channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
            qc_anchor_channels=["G1", "G2", "R1"],
        )

        group = result["groups"][0]
        self.assertEqual(group["lif_anchor_peak_ids"], {"G1": "g1", "G2": None, "R1": "r1"})
        self.assertEqual(group["same_axis_dropped_channels"], ["G2"])
        self.assertEqual(group["same_axis_conflict_count"], 1)
        self.assertFalse(group["complete_anchor_set"])

    def test_cross_axis_residuals_cannot_cancel_into_a_complete_anchor_set(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g1", "channel": "G1", "time_min": 56.0 / 60.0, "time_sec": 56.0, "snr": 50},
                {"peak_id": "g2", "channel": "G2", "time_min": 56.1 / 60.0, "time_sec": 56.1, "snr": 50},
                {"peak_id": "r1", "channel": "R1", "time_min": 64.0 / 60.0, "time_sec": 64.0, "snr": 50},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": "ms", "time_min": 1.0, "time_sec": 60.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
            ]
        )

        result = build_qc_alignment_groups(
            lif_peaks,
            ms_events,
            qc_calibration_end_min=2.0,
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
            channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
            qc_anchor_channels=["G1", "G2", "R1"],
        )

        group = result["groups"][0]
        self.assertAlmostEqual(group["composite_to_ms_residual_sec"], -0.025, places=3)
        self.assertGreater(group["axis_span_sec"], 7.0)
        self.assertFalse(group["axis_coherent"])
        self.assertFalse(group["complete_anchor_set"])
        self.assertGreater(group["conflict_count"], 0)
        self.assertEqual(qc_group_auto_accept_block_reason(group), "axis_incoherent")

    def test_conflicting_complete_anchor_set_is_not_batch_acceptable(self):
        group = {
            "source": "auto_candidate",
            "review_status": "pending",
            "review_enabled": True,
            "complete_anchor_set": True,
            "axis_coherent": True,
            "conflict_count": 3,
            "composite_to_ms_residual_sec": 0.0,
            "max_abs_axis_to_ms_residual_sec": 0.1,
            "match_tolerance_sec": 4.0,
        }

        self.assertEqual(qc_group_auto_accept_block_reason(group), "conflicting_anchor_set")
        self.assertEqual(
            qc_group_batch_accept_block_reason(group, window_start_min=0.0, window_end_min=2.0),
            "conflicting_anchor_set",
        )

    def test_three_channel_axis_alignment_estimates_one_shift_per_axis(self):
        lif_rows = []
        ms_rows = []
        for index, center in enumerate([60.0, 180.0, 300.0, 420.0], start=1):
            lif_rows.extend(
                [
                    {"peak_id": f"g1_{index}", "channel": "G1", "time_min": (center - 3.1) / 60.0, "time_sec": center - 3.1, "snr": 40},
                    {"peak_id": f"g2_{index}", "channel": "G2", "time_min": (center - 2.9) / 60.0, "time_sec": center - 2.9, "snr": 40},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_min": (center + 7.0) / 60.0, "time_sec": center + 7.0, "snr": 40},
                ]
            )
            ms_rows.append(
                {"event_id": f"ms_{index}", "time_min": center / 60.0, "time_sec": center, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
            )
        layout = {
            "lif_channels": [
                {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "green_axis"},
                {"input_id": "lif_2_raw", "channel": "G2", "time_axis": "green_axis"},
                {"input_id": "lif_3_raw", "channel": "R1", "time_axis": "red_axis"},
            ],
            "qc_anchor_channels": ["R1", "G2", "G1"],
        }

        alignment = estimate_shift_alignment(
            pd.DataFrame(lif_rows),
            pd.DataFrame(ms_rows),
            qc_calibration_end_min=8.0,
            acquisition_layout=layout,
        )

        self.assertEqual(alignment["qc_anchor_channels"], ["G1", "G2", "R1"])
        self.assertAlmostEqual(alignment["axis_shifts_sec"]["green_axis"], 3.0, delta=0.3)
        self.assertAlmostEqual(alignment["axis_shifts_sec"]["red_axis"], -7.0, delta=0.3)
        self.assertEqual(alignment["channels"]["G1"]["shift_sec"], alignment["channels"]["G2"]["shift_sec"])
        self.assertEqual(len(alignment["qc_groups"]["groups"]), 4)
        self.assertTrue(all(row["complete_anchor_set"] for row in alignment["qc_groups"]["groups"]))

    def test_four_channel_synthetic_alignment_supports_four_qc_anchors(self):
        lif_rows = []
        ms_rows = []
        for index, center in enumerate([60.0, 180.0, 300.0, 420.0], start=1):
            lif_rows.extend(
                [
                    {"peak_id": f"g1_{index}", "channel": "G1", "time_min": (center - 4.1) / 60.0, "time_sec": center - 4.1, "snr": 45},
                    {"peak_id": f"g2_{index}", "channel": "G2", "time_min": (center - 3.9) / 60.0, "time_sec": center - 3.9, "snr": 45},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_min": (center + 6.9) / 60.0, "time_sec": center + 6.9, "snr": 45},
                    {"peak_id": f"r2_{index}", "channel": "R2", "time_min": (center + 7.1) / 60.0, "time_sec": center + 7.1, "snr": 45},
                ]
            )
            ms_rows.append(
                {"event_id": f"ms_{index}", "time_min": center / 60.0, "time_sec": center, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
            )
        layout = {
            "lif_channels": [
                {"input_id": "g1", "channel": "G1", "time_axis": "green_axis", "use_for_cell_annotation": True},
                {"input_id": "g2", "channel": "G2", "time_axis": "green_axis", "use_for_cell_annotation": True},
                {"input_id": "r1", "channel": "R1", "time_axis": "red_axis", "use_for_cell_annotation": False},
                {"input_id": "r2", "channel": "R2", "time_axis": "red_axis", "use_for_cell_annotation": True},
            ],
            "qc_anchor_channels": ["G1", "G2", "R1", "R2"],
        }

        alignment = estimate_shift_alignment(
            pd.DataFrame(lif_rows),
            pd.DataFrame(ms_rows),
            qc_calibration_end_min=8.0,
            acquisition_layout=layout,
        )

        self.assertEqual(alignment["qc_anchor_channels"], ["G1", "G2", "R1", "R2"])
        self.assertAlmostEqual(alignment["axis_shifts_sec"]["green_axis"], 4.0, delta=0.3)
        self.assertAlmostEqual(alignment["axis_shifts_sec"]["red_axis"], -7.0, delta=0.3)
        self.assertEqual(alignment["channels"]["G1"]["shift_sec"], alignment["channels"]["G2"]["shift_sec"])
        self.assertEqual(alignment["channels"]["R1"]["shift_sec"], alignment["channels"]["R2"]["shift_sec"])
        self.assertEqual(len(alignment["qc_groups"]["groups"]), 4)
        self.assertTrue(all(row["lif_anchor_count"] == 4 for row in alignment["qc_groups"]["groups"]))

    def test_axis_shift_prioritizes_independent_events_over_one_multi_channel_coincidence(self):
        lif_rows = [
            {"peak_id": "g1_decoy", "channel": "G1", "time_min": 80.0 / 60.0, "time_sec": 80.0, "snr": 50},
            {"peak_id": "g2_decoy", "channel": "G2", "time_min": 80.1 / 60.0, "time_sec": 80.1, "snr": 50},
        ]
        lif_rows.extend(
            {"peak_id": f"g1_{time}", "channel": "G1", "time_min": time / 60.0, "time_sec": float(time), "snr": 30}
            for time in [200, 300, 400, 500, 600]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": f"ms_{time}", "time_min": time / 60.0, "time_sec": float(time), "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
                for time in [100, 200, 300, 400, 500, 600]
            ]
        )

        estimate = estimate_axis_shift(
            pd.DataFrame(lif_rows),
            ms_events,
            time_axis="green_axis",
            channels=["G1", "G2"],
            qc_calibration_end_min=11.0,
        )

        self.assertAlmostEqual(estimate["shift_sec"], 0.0, places=6)
        self.assertEqual(estimate["match_count"], 5)

    def test_nonlegacy_two_anchor_layout_estimates_one_shift_per_physical_axis(self):
        lif_rows = []
        ms_rows = []
        for index, center in enumerate([60.0, 180.0, 300.0], start=1):
            lif_rows.extend(
                [
                    {"peak_id": f"g1_{index}", "channel": "G1", "time_min": (center - 3.1) / 60.0, "time_sec": center - 3.1, "snr": 40},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_min": (center - 2.9) / 60.0, "time_sec": center - 2.9, "snr": 40},
                ]
            )
            ms_rows.append(
                {"event_id": f"ms_{index}", "time_min": center / 60.0, "time_sec": center, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
            )
        layout = {
            "lif_channels": [
                {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "shared_axis", "detector": "green"},
                {"input_id": "lif_2_raw", "channel": "R1", "time_axis": "shared_axis", "detector": "red"},
                {"input_id": "lif_3_raw", "channel": "R2", "time_axis": "shared_axis", "detector": "red"},
            ],
            "qc_anchor_channels": ["G1", "R1"],
        }

        alignment = estimate_shift_alignment(
            pd.DataFrame(lif_rows),
            pd.DataFrame(ms_rows),
            qc_calibration_end_min=6.0,
            acquisition_layout=layout,
        )

        self.assertEqual(set(alignment["axes"]), {"shared_axis"})
        self.assertAlmostEqual(alignment["axis_shifts_sec"]["shared_axis"], 3.0, delta=0.3)
        self.assertEqual(alignment["channels"]["G1"]["shift_sec"], alignment["channels"]["R1"]["shift_sec"])

    def test_axis_peak_clustering_cannot_bridge_through_duplicate_channel_peaks(self):
        peaks = pd.DataFrame(
            [
                {"peak_id": "g1_high", "channel": "G1", "time_min": 0.0, "time_sec": 0.0, "snr": 100},
                {"peak_id": "g1_bridge", "channel": "G1", "time_min": 1.9 / 60.0, "time_sec": 1.9, "snr": 10},
                {"peak_id": "g2_far", "channel": "G2", "time_min": 2.9 / 60.0, "time_sec": 2.9, "snr": 100},
            ]
        )

        clusters = build_axis_peak_clusters(
            peaks,
            ["G1", "G2"],
            start_min=0.0,
            end_min=1.0,
            tolerance_sec=2.0,
        )

        self.assertFalse(any(cluster["support_count"] > 1 for cluster in clusters))

    def test_dynamic_qc_candidate_id_is_independent_of_mapping_order(self):
        left = {"lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"}, "ms_event_id": "ms"}
        right = {"lif_anchor_peak_ids": {"R1": "r1", "G2": "g2", "G1": "g1"}, "ms_event_id": "ms"}

        self.assertEqual(candidate_id_for_group(left), candidate_id_for_group(right))

    def test_qc_relation_key_ignores_manual_vs_auto_id_namespace(self):
        auto = {
            "annotation_id": "auto_qc:v2:same",
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"},
            "ms_event_id": "ms",
        }
        manual = {
            "annotation_id": "manual_qc:v2:same",
            "lif_anchor_peak_ids": {"R1": "r1", "G2": "g2", "G1": "g1"},
            "ms_event_id": "ms",
        }

        self.assertEqual(qc_relation_key(auto), qc_relation_key(manual))

    def test_qc_reconciliation_reuses_exact_manual_review_and_suppresses_accepted_conflicts(self):
        accepted = {
            "annotation_id": "manual_qc:v2:accepted",
            "source": "manual_created",
            "review_status": "accepted",
            "updated_at": "2026-07-15T17:00:00Z",
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"},
            "ms_event_id": "ms_1",
        }
        exact = {
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"},
            "ms_event_id": "ms_1",
        }
        shares_ms = {
            "lif_anchor_peak_ids": {"G1": "g1_other", "G2": "g2_other", "R1": "r1_other"},
            "ms_event_id": "ms_1",
        }
        shares_lif = {
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2_other", "R1": "r1_other"},
            "ms_event_id": "ms_2",
        }
        independent = {
            "lif_anchor_peak_ids": {"G1": "g1_2", "G2": "g2_2", "R1": "r1_2"},
            "ms_event_id": "ms_2",
        }

        reconciled = reconcile_qc_calibration_groups(
            [exact, shares_ms, shares_lif, independent],
            [accepted],
        )

        self.assertEqual([group for group, _ in reconciled], [exact, independent])
        self.assertIs(reconciled[0][1], accepted)
        self.assertIsNone(reconciled[1][1])

    def test_rejected_qc_relation_only_suppresses_its_exact_relation(self):
        rejected = {
            "annotation_id": "manual_qc:v2:rejected",
            "source": "manual_created",
            "review_status": "rejected",
            "updated_at": "2026-07-15T17:00:00Z",
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"},
            "ms_event_id": "ms_1",
        }
        exact = {
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"},
            "ms_event_id": "ms_1",
        }
        alternative = {
            "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2_alt", "R1": "r1_alt"},
            "ms_event_id": "ms_2",
        }

        reconciled = reconcile_qc_calibration_groups([exact, alternative], [rejected])

        self.assertEqual([group for group, _ in reconciled], [exact, alternative])
        self.assertIs(reconciled[0][1], rejected)
        self.assertIsNone(reconciled[1][1])

    def test_legacy_pair_synthesizes_anchor_map_without_changing_candidate_id(self):
        group = {
            "anchor_a_channel": "G2",
            "anchor_b_channel": "R1",
            "anchor_a_peak_id": "g2",
            "anchor_b_peak_id": "r1",
            "g2_peak_id": "g2",
            "r1_peak_id": "r1",
            "ms_event_id": "ms",
        }

        self.assertEqual(qc_anchor_peak_id_map(group), {"G2": "g2", "R1": "r1"})
        self.assertEqual(candidate_id_for_group(group), "auto_qc:g2:r1:ms")

    def test_dynamic_manual_map_reuses_legacy_auto_candidate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            group = {
                "anchor_a_channel": "G2",
                "anchor_b_channel": "R1",
                "anchor_a_peak_id": "g2",
                "anchor_b_peak_id": "r1",
                "g2_peak_id": "g2",
                "r1_peak_id": "r1",
                "ms_event_id": "ms",
                "g2_raw_time_min": 1.0,
                "r1_raw_time_min": 1.0,
                "ms_time_min": 1.0,
                "g2_plot_time_min": 1.0,
                "r1_plot_time_min": 1.0,
                "ms_plot_time_min": 1.0,
                "composite_to_ms_residual_sec": 0.0,
                "abs_composite_to_ms_residual_sec": 0.0,
            }
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(
                    [
                        {"peak_id": "g2", "channel": "G2", "time_min": 1.0, "time_sec": 60.0},
                        {"peak_id": "r1", "channel": "R1", "time_min": 1.0, "time_sec": 60.0},
                    ]
                ),
                ms_events=pd.DataFrame(
                    [
                        {"event_id": "ms", "time_min": 1.0, "time_sec": 60.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
                    ]
                ),
                ms_scan=pd.DataFrame(),
                alignment={
                    "model": "legacy",
                    "green_to_ms_shift_sec": 0.0,
                    "red_to_ms_shift_sec": 0.0,
                    "axis_shifts_sec": {"green_axis": 0.0, "red_axis": 0.0},
                    "channel_time_axes": {"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"},
                    "qc_anchor_channels": ["G2", "R1"],
                    "qc_groups": {"groups": [group]},
                    "acquisition_layout_hash": acquisition_layout_hash(None),
                },
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={},
                acquisition_layout=None,
            )

            row = app.create_manual_triplet(
                None,
                None,
                "ms",
                lif_anchor_peak_ids={"G2": "g2", "R1": "r1"},
            )

            self.assertEqual(row["annotation_id"], "auto_qc:g2:r1:ms")
            self.assertEqual(len(app.store.records()), 1)

    def test_explicit_manual_anchor_does_not_reuse_ambiguous_auto_candidate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            group = {
                "anchor_a_channel": "G2",
                "anchor_b_channel": "R1",
                "anchor_a_peak_id": "g2",
                "anchor_b_peak_id": "r1",
                "g2_peak_id": "g2",
                "r1_peak_id": "r1",
                "ms_event_id": "ms",
                "g2_raw_time_min": 1.0,
                "r1_raw_time_min": 1.0,
                "ms_time_min": 1.0,
                "g2_plot_time_min": 1.0,
                "r1_plot_time_min": 1.0,
                "ms_plot_time_min": 1.0,
                "composite_to_ms_residual_sec": 0.0,
                "abs_composite_to_ms_residual_sec": 0.0,
                "component_ambiguous": True,
                "alternative_ms_event_ids": ["ms_alternative"],
            }
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(
                    [
                        {"peak_id": "g2", "channel": "G2", "time_min": 1.0, "time_sec": 60.0},
                        {"peak_id": "r1", "channel": "R1", "time_min": 1.0, "time_sec": 60.0},
                    ]
                ),
                ms_events=pd.DataFrame(
                    [
                        {"event_id": "ms", "time_min": 1.0, "time_sec": 60.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
                        {"event_id": "ms_alternative", "time_min": 1.01, "time_sec": 60.6, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
                    ]
                ),
                ms_scan=pd.DataFrame(),
                alignment={
                    "model": "legacy",
                    "green_to_ms_shift_sec": 0.0,
                    "red_to_ms_shift_sec": 0.0,
                    "axis_shifts_sec": {"green_axis": 0.0, "red_axis": 0.0},
                    "channel_time_axes": {"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"},
                    "qc_anchor_channels": ["G2", "R1"],
                    "qc_groups": {"groups": [group]},
                    "acquisition_layout_hash": acquisition_layout_hash(None),
                },
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={},
                acquisition_layout=None,
            )

            row = app.create_manual_triplet(
                None,
                None,
                "ms",
                lif_anchor_peak_ids={"G2": "g2", "R1": "r1"},
            )

            self.assertEqual(row["source"], "manual_created")
            self.assertEqual(row["review_status"], "accepted")
            self.assertNotEqual(row["annotation_id"], "auto_qc:g2:r1:ms")
            self.assertEqual(len(app.store.records()), 1)

    def test_qc_survey_manual_map_without_window_reuses_dynamic_auto_candidate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            layout = normalize_acquisition_layout(
                {
                    "lif_channels": [
                        {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "green_axis"},
                        {"input_id": "lif_2_raw", "channel": "G2", "time_axis": "green_axis"},
                        {"input_id": "lif_3_raw", "channel": "R1", "time_axis": "red_axis"},
                    ],
                    "qc_anchor_channels": ["G1", "G2", "R1"],
                }
            )
            lif_peaks = pd.DataFrame(
                [
                    {"peak_id": "g1", "channel": "G1", "time_min": 41.0, "time_sec": 2460.0, "snr": 50},
                    {"peak_id": "g2", "channel": "G2", "time_min": 41.0, "time_sec": 2460.0, "snr": 50},
                    {"peak_id": "r1", "channel": "R1", "time_min": 41.0, "time_sec": 2460.0, "snr": 50},
                ]
            )
            ms_events = pd.DataFrame(
                [
                    {"event_id": "ms", "time_min": 41.0, "time_sec": 2460.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
                ]
            )
            alignment = estimate_shift_alignment(
                lif_peaks,
                ms_events,
                qc_calibration_end_min=10.5,
                acquisition_layout=layout,
            )
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            store.upsert_time_model(
                {
                    "time_model_version": "tm_frozen",
                    "status": "frozen",
                    "base_model_name": alignment["model"],
                    "qc_calibration_end_min": 10.5,
                    "sample_valve_switch_min": 36.0,
                    "annotation_start_min": 40.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 0.0,
                    "contains_cell_labels": False,
                    "max_training_time_min": 42.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.0,
                    "p90_abs_residual_sec": 0.0,
                    "acquisition_layout_hash": alignment["acquisition_layout_hash"],
                },
                action="test_frozen",
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=lif_peaks,
                ms_events=ms_events,
                ms_scan=pd.DataFrame(),
                alignment=alignment,
                store=store,
                channel_identity_prior={},
                acquisition_layout=layout,
            )

            row = app.create_manual_triplet(
                None,
                None,
                "ms",
                stage="qc_survey",
                lif_anchor_peak_ids={"G1": "g1", "G2": "g2", "R1": "r1"},
            )

            self.assertTrue(row["annotation_id"].startswith("post_qc:v2:"))
            self.assertEqual(row["source"], "auto_candidate")
            self.assertEqual(len(app.store.records()), 1)

    def test_app_channel_shift_uses_configured_time_axes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(),
                ms_scan=pd.DataFrame(),
                alignment={
                    "model": "v3_base",
                    "axis_shifts_sec": {"green_axis": -2.5, "red_axis": 9.0},
                    "green_to_ms_shift_sec": -2.5,
                    "red_to_ms_shift_sec": 9.0,
                    "qc_groups": {"groups": []},
                },
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={},
                acquisition_layout={
                    "lif_channels": [
                        {"input_id": "lif_1_raw", "channel": "G1", "identity_prior": "Day1", "time_axis": "green_axis"},
                        {"input_id": "lif_2_raw", "channel": "G2", "identity_prior": "Day0", "time_axis": "green_axis"},
                        {"input_id": "lif_3_raw", "channel": "R1", "identity_prior": "Day3", "time_axis": "red_axis"},
                    ],
                    "qc_anchor_channels": ["G2", "R1"],
                },
            )

            self.assertEqual(app.channel_shift_sec("G1", "aligned"), -2.5)
            self.assertEqual(app.channel_shift_sec("G2", "aligned"), -2.5)
            self.assertEqual(app.channel_shift_sec("R1", "aligned"), 9.0)
            self.assertEqual(app.cell_annotation_channels(), ["G1", "G2", "R1"])
            self.assertEqual(app.cell_label_for_channel("G1"), "Day1 cell")
            self.assertEqual(app.cell_label_for_channel("G2"), "Day0 cell")
            self.assertEqual(app.cell_label_for_channel("R1"), "Day3 cell")

    def test_cell_candidates_cannot_be_batch_accepted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(),
                ms_scan=pd.DataFrame(),
                alignment={"model": "v3_base", "qc_groups": {"groups": []}},
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={},
                acquisition_layout=None,
            )

            with self.assertRaisesRegex(BadRequest, "individual review"):
                app.accept_pending_auto_candidates_in_window(
                    start_min=40.0,
                    window_min=2.5,
                    time_mode="aligned",
                    stage="cell_annotation",
                )

    def test_window_lif_display_mode_changes_trace_y_without_changing_peak_identity(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(
                    [
                        {"channel": "G2", "label": "Day0", "detector": "green", "phase": "qc", "time_min": 1.00, "time_sec": 60.0, "raw": 0.050, "baseline": 0.040, "signal": 0.010},
                        {"channel": "G2", "label": "Day0", "detector": "green", "phase": "qc", "time_min": 1.01, "time_sec": 60.6, "raw": 0.060, "baseline": 0.040, "signal": 0.020},
                        {"channel": "G2", "label": "Day0", "detector": "green", "phase": "qc", "time_min": 1.27, "time_sec": 76.2, "raw": 0.051, "baseline": 0.040, "signal": 0.011},
                    ]
                ),
                lif_peaks=pd.DataFrame(
                    [
                        {
                            "peak_id": "g2_peak_1",
                            "peak_stage": "merged",
                            "parent_raw_peak_ids": "g2_raw_1",
                            "raw_peak_count_merged": 1,
                            "channel": "G2",
                            "label": "Day0",
                            "detector": "green",
                            "phase": "qc",
                            "peak_index": 1,
                            "time_min": 1.01,
                            "time_sec": 60.6,
                            "raw": 0.060,
                            "baseline": 0.040,
                            "height": 0.020,
                            "prominence": 0.019,
                            "snr": 20.0,
                            "width_sec": 0.2,
                            "area": 0.004,
                            "nearest_gap_sec": 10.0,
                            "close_peak_risk": False,
                            "merge_risk": False,
                        },
                        {
                            "peak_id": "g2_context_peak",
                            "peak_stage": "merged",
                            "parent_raw_peak_ids": "g2_raw_context",
                            "raw_peak_count_merged": 1,
                            "channel": "G2",
                            "label": "Day0",
                            "detector": "green",
                            "phase": "qc",
                            "peak_index": 2,
                            "time_min": 1.27,
                            "time_sec": 76.2,
                            "raw": 0.061,
                            "baseline": 0.040,
                            "height": 0.021,
                            "prominence": 0.019,
                            "snr": 20.0,
                            "width_sec": 0.2,
                            "area": 0.004,
                            "nearest_gap_sec": 10.0,
                            "close_peak_risk": False,
                            "merge_risk": False,
                        },
                    ]
                ),
                ms_events=pd.DataFrame(
                    [
                        {
                            "event_id": "ms_1",
                            "time_min": 1.01,
                            "time_sec": 60.6,
                            "event_strategy": "pc34_primary",
                            "primary_signal_col": "pc34_760_max_intensity",
                            "pc34_760_apex": 1000.0,
                            "qc_782_apex": 500.0,
                            "nearest_event_gap_sec": 10.0,
                            "collision_risk_high": False,
                            "low_quality_scan_window": False,
                        },
                        {
                            "event_id": "ms_context",
                            "time_min": 1.27,
                            "time_sec": 76.2,
                            "event_strategy": "pc34_primary",
                            "primary_signal_col": "pc34_760_max_intensity",
                            "pc34_760_apex": 900.0,
                            "qc_782_apex": 450.0,
                            "nearest_event_gap_sec": 10.0,
                            "collision_risk_high": False,
                            "low_quality_scan_window": False,
                        },
                    ]
                ),
                ms_scan=pd.DataFrame(
                    [
                        {
                            "scan_start_time_min": 1.0,
                            "pc34_760_max_intensity": 1000.0,
                            "qc_782_max_intensity": 500.0,
                        },
                        {
                            "scan_start_time_min": 1.27,
                            "pc34_760_max_intensity": 900.0,
                            "qc_782_max_intensity": 450.0,
                        },
                    ]
                ),
                alignment={"model": "test_alignment", "green_to_ms_shift_sec": 0.0, "red_to_ms_shift_sec": 0.0, "qc_groups": {"groups": []}},
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={},
                acquisition_layout=None,
            )

            raw_window = app.window(1.0, 0.25, time_mode="raw", lif_signal_mode="raw")
            signal_window = app.window(1.0, 0.25, time_mode="raw", lif_signal_mode="signal")

            self.assertEqual(raw_window["display_options"]["lif_signal_mode"], "raw")
            self.assertEqual(signal_window["display_options"]["lif_signal_mode"], "signal")
            self.assertEqual([row["peak_id"] for row in raw_window["lif_peaks"]], ["g2_peak_1", "g2_context_peak"])
            self.assertEqual([row["peak_id"] for row in signal_window["lif_peaks"]], ["g2_peak_1", "g2_context_peak"])
            self.assertEqual(raw_window["lif_peaks"][0]["display_y"], 0.060)
            self.assertEqual(signal_window["lif_peaks"][0]["display_y"], 0.020)
            self.assertEqual(raw_window["lif_traces"]["G2"][0]["y"], 0.050)
            self.assertEqual(signal_window["lif_traces"]["G2"][0]["y"], 0.010)
            self.assertEqual(
                raw_window["counts"],
                {
                    "lif_trace_points_returned": 2,
                    "lif_peaks": 1,
                    "ms_scan_points_returned": 1,
                    "ms_events": 1,
                },
            )

    def test_window_rejects_unknown_lif_display_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(
                    [
                        {"channel": "G2", "label": "Day0", "detector": "green", "time_min": 1.0, "time_sec": 60.0, "raw": 0.05, "signal": 0.01},
                    ]
                ),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(
                    [
                        {"event_id": "ms_1", "time_min": 1.0, "time_sec": 60.0, "pc34_760_apex": 1000.0, "qc_782_apex": 500.0},
                    ]
                ),
                ms_scan=pd.DataFrame(
                    [
                        {"scan_start_time_min": 1.0, "pc34_760_max_intensity": 1000.0, "qc_782_max_intensity": 500.0},
                    ]
                ),
                alignment={"model": "test_alignment", "green_to_ms_shift_sec": 0.0, "red_to_ms_shift_sec": 0.0, "qc_groups": {"groups": []}},
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={},
                acquisition_layout=None,
            )

            with self.assertRaises(BadRequest):
                app.window(1.0, 0.25, time_mode="raw", lif_signal_mode="masshunter")

    def test_local_delta_evidence_window_must_be_positive(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")

            with self.assertRaises(BadRequest):
                store.update_project_config({"local_delta_seed_window_min": 0.0})

    def test_config_change_requires_confirmation_before_clearing_frozen_time_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            frozen = store.upsert_time_model(
                {
                    "time_model_version": "tm_frozen",
                    "status": "frozen",
                    "base_model_name": "v3_base",
                    "qc_calibration_end_min": 10.5,
                    "sample_valve_switch_min": 36.0,
                    "annotation_start_min": 40.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.25,
                    "contains_cell_labels": False,
                    "max_training_time_min": 42.5,
                    "evidence_count": 5,
                    "unique_match_count": 5,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                },
                action="test_freeze",
            )

            with self.assertRaises(BadRequest):
                store.update_project_config({"annotation_start_min": 41.0})

            self.assertEqual(store.active_time_model()["time_model_version"], frozen["time_model_version"])

            store.update_project_config(
                {"annotation_start_min": 41.0},
                clear_frozen_time_model=True,
            )

            self.assertIsNone(store.active_time_model())

    def test_unhashed_time_model_only_binds_through_explicit_legacy_migration(self):
        payload = {
            "time_model_version": "tm_unhashed",
            "status": "frozen",
            "base_model_name": "legacy",
            "qc_calibration_end_min": 10.5,
            "sample_valve_switch_min": 36.0,
            "annotation_start_min": 40.0,
            "local_delta_seed_window_min": 2.5,
            "ms_local_delta_sec": 1.25,
            "contains_cell_labels": False,
            "max_training_time_min": 42.5,
            "evidence_count": 5,
            "unique_match_count": 5,
            "conflict_count": 0,
            "median_abs_residual_sec": 0.1,
            "p90_abs_residual_sec": 0.2,
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            blocked_store = AnnotationStore(Path(tmp) / "blocked.sqlite")
            blocked_store.upsert_time_model(payload, action="test_unhashed")
            with self.assertRaises(BadRequest):
                blocked_store.ensure_draft_time_model(
                    "axis_aware",
                    "new_layout_hash",
                    allow_unhashed_legacy_binding=False,
                )
            self.assertIsNone(blocked_store.active_time_model().get("acquisition_layout_hash"))

            legacy_store = AnnotationStore(Path(tmp) / "legacy.sqlite")
            legacy_store.upsert_time_model(payload, action="test_unhashed")
            bound = legacy_store.ensure_draft_time_model(
                "legacy",
                "legacy_layout_hash",
                allow_unhashed_legacy_binding=True,
            )
            self.assertEqual(bound["acquisition_layout_hash"], "legacy_layout_hash")

    def test_app_config_update_recomputes_alignment_with_new_qc_end(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            lif_peaks = pd.DataFrame(
                [
                    {"peak_id": "g2_11", "channel": "G2", "time_min": 11.0, "time_sec": 660.0, "peak_stage": "merged", "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                    {"peak_id": "r1_11", "channel": "R1", "time_min": 11.0, "time_sec": 660.0, "peak_stage": "merged", "snr": 10, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                ]
            )
            ms_events = pd.DataFrame(
                [
                    {
                        "event_id": "ms_11",
                        "time_min": 11.0,
                        "time_sec": 660.0,
                        "event_strategy": "pc34_primary",
                        "primary_signal_col": "pc34_760_max_intensity",
                        "pc34_760_apex": 1000.0,
                        "nearest_event_gap_sec": 10,
                        "collision_risk_high": False,
                        "low_quality_scan_window": False,
                    }
                ]
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=lif_peaks,
                ms_events=ms_events,
                ms_scan=pd.DataFrame(),
                alignment=estimate_shift_alignment(lif_peaks, ms_events, 10.5),
                store=store,
                channel_identity_prior={},
                acquisition_layout=None,
            )

            self.assertEqual(app.alignment["qc_groups"]["groups"], [])

            app.update_project_config({"qc_calibration_end_min": 12.0})

            self.assertEqual(
                [row["ms_event_id"] for row in app.alignment["qc_groups"]["groups"]],
                ["ms_11"],
            )

            with mock.patch.object(AppData, "active_time_model", side_effect=RuntimeError("sync unavailable")):
                saved_config = app.update_project_config({"annotation_start_min": 41.25})

            self.assertEqual(saved_config["annotation_start_min"], 41.25)
            self.assertIn("已保存", app._project_config_update_warning)
            self.assertEqual(app._project_config_update_time_model["status"], "unavailable")

    def test_window_annotations_hide_stale_post_qc_manual_records(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            store.upsert_time_model(
                {
                    "time_model_version": "tm_current",
                    "status": "frozen",
                    "base_model_name": "v3_base",
                    "qc_calibration_end_min": 10.5,
                    "sample_valve_switch_min": 36.0,
                    "annotation_start_min": 40.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.25,
                    "contains_cell_labels": False,
                    "max_training_time_min": 42.5,
                    "evidence_count": 5,
                    "unique_match_count": 5,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                },
                action="test_freeze",
            )
            store.upsert_review(
                annotation_id="manual_qc:old",
                source="manual_created",
                review_status="accepted",
                action="test_old_manual",
                payload={
                    "candidate_type": "manual_qc_anchor_partial",
                    "review_stage": "qc_survey",
                    "ms_event_id": "ms_old",
                    "ms_time_min": 41.0,
                    "ms_plot_time_min": 41.0,
                    "time_model_version": "tm_old",
                },
            )
            store.upsert_review(
                annotation_id="manual_qc:current",
                source="manual_created",
                review_status="accepted",
                action="test_current_manual",
                payload={
                    "candidate_type": "manual_qc_anchor_partial",
                    "review_stage": "qc_survey",
                    "ms_event_id": "ms_current",
                    "ms_time_min": 41.5,
                    "ms_plot_time_min": 41.5,
                    "time_model_version": "tm_current",
                },
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(),
                ms_scan=pd.DataFrame(),
                alignment={"model": "v3_base", "qc_groups": {"groups": []}},
                store=store,
                channel_identity_prior={},
                acquisition_layout=None,
            )

            rows = app.window_annotations(40.0, 42.0)

        self.assertEqual([row["annotation_id"] for row in rows], ["manual_qc:current"])

    def test_html_hides_raw_rfu_lif_display_control(self):
        self.assertIn("LMA Studio", HTML)
        self.assertNotIn('id="lifSignalMode"', HTML)
        self.assertIn('id="yAxisMode"', HTML)
        self.assertIn('id="windowPolicy"', HTML)
        self.assertIn('function applyStageWindowWidth()', HTML)
        self.assertIn('function eventGridWindowStart(eventTime)', HTML)
        self.assertIn('state.start = eventGridWindowStart(eventTime);', HTML)
        self.assertNotIn('eventTime - state.width / 2', HTML)
        self.assertIn('自动估计时间差的范围(min)', HTML)
        self.assertNotIn('主窗口固定 2.5 min', HTML)
        self.assertIn('id="importLifRows"', HTML)
        self.assertIn('id="addImportLif"', HTML)
        self.assertIn('id="importRoleSummary"', HTML)
        self.assertNotIn('id="importQcAnchorA"', HTML)
        self.assertNotIn('id="importQcAnchorB"', HTML)
        self.assertIn('data-import-field="channel"', HTML)
        self.assertNotIn('data-import-field="use_for_qc"', HTML)
        self.assertIn('id="importCalibrationSegments"', HTML)
        self.assertIn('data-segment-field="boundaries_confirmed"', HTML)
        self.assertIn('data-import-field="use_for_cell_annotation"', HTML)
        self.assertIn('placeholder="${row.use_for_cell_annotation ? \'细胞用途必填\' : \'可留空\'}"', HTML)
        self.assertNotIn('如 LK / LSK / CLP', HTML)
        self.assertIn('id="importCellEventMap"', HTML)
        self.assertIn('data-picker-role="cell_event_map"', HTML)
        self.assertIn('id="attachMapPanel" class="attach-map-panel"', HTML)
        self.assertIn('UMAP coordinates', HTML)
        self.assertIn('选择与当前 MS 对应的 .csv', HTML)
        self.assertIn('Validate &amp; enable', HTML)
        self.assertIn('Validate & switch', HTML)
        self.assertIn('软件保存项目内副本，不依赖原 CSV 路径', HTML)
        self.assertIn("activeSourceName = String(mapInfo.source_name || '').trim()", HTML)
        self.assertIn('/api/replace-cell-event-map', HTML)
        self.assertIn("notifyStateChannel(replacing ? 'map-replaced' : 'map-attached')", HTML)
        self.assertNotIn('仅可附加一次', HTML)
        self.assertNotIn('绑定后不能直接替换', HTML)
        self.assertIn('.path-picker-row input {\n      width: 100%;', HTML)
        self.assertIn('UMAP（未配置）', HTML)
        self.assertNotIn('id="openUmap" class="header-secondary-button" disabled', HTML)
        self.assertNotIn('id="openUmap" class="header-secondary-button" aria-disabled', HTML)
        self.assertIn('data-unavailable="true"', HTML)
        self.assertIn('事件时间表已启用，但尚未附加二维 UMAP 坐标', HTML)
        self.assertIn('当前项目尚未附加事件表；点击查看配置说明', HTML)
        self.assertIn('function syncUmapButtonState()', HTML)
        self.assertIn('function updateAttachMapControls()', HTML)
        self.assertIn('已切换 ${rowCount.toLocaleString()} 个 UMAP 坐标点', HTML)
        self.assertIn('function setModalVisibility(', HTML)
        self.assertIn('function modalFocusableElements(', HTML)
        self.assertNotIn("if (ev.target === el('importModal')) setImportModal(false);", HTML)
        self.assertIn('id="configSaveStatus"', HTML)
        self.assertIn('正在保存项目时间节点...', HTML)
        self.assertIn('已保存：前段参考结束', HTML)
        self.assertIn('图窗刷新失败：', HTML)
        self.assertIn('configSaveBusy', HTML)
        self.assertIn('function duplicateImportLifPathMessage()', HTML)
        self.assertIn('id="trackLegend"', HTML)
        self.assertIn('function renderTrackLegend()', HTML)
        self.assertIn("if (state.meta?.bootstrap || !state.meta?.project) return [];", HTML)
        self.assertIn("if (group.review_status === 'rejected') return;", HTML)
        self.assertNotIn("dash: '3 5'", HTML)
        self.assertIn('function appendQcConnectorPolyline(', HTML)
        self.assertIn("const line = svgEl('polyline'", HTML)
        self.assertNotIn('function appendQcConnectorLines(', HTML)
        self.assertIn('response.result?.accepted_count', HTML)
        self.assertIn('response.result?.skipped_count', HTML)
        self.assertIn('function batchAcceptableAutoCandidatesInMainWindow()', HTML)
        self.assertIn('需逐条审核', HTML)
        self.assertIn('附近有多个可匹配峰', HTML)
        self.assertNotIn('LIF G2 / Day0</div>', HTML)
        self.assertNotIn("'annotation_id', 'candidate_id', 'source', 'review_status', 'exportable'", HTML)
        self.assertIn('<option value="robust">Zoom</option>', HTML)

    def test_accepted_qc_refit_estimates_each_physical_axis_and_rejects_outlier(self):
        layout = normalize_acquisition_layout(
            {
                "lif_channels": [
                    {"input_id": "lif_1_raw", "channel": "G1", "time_axis": "green_axis"},
                    {"input_id": "lif_2_raw", "channel": "G2", "time_axis": "green_axis"},
                    {"input_id": "lif_3_raw", "channel": "R1", "time_axis": "red_axis"},
                ],
                "qc_anchor_channels": ["G1", "G2", "R1"],
            }
        )
        lif_rows = []
        ms_rows = []
        annotations = []
        for index, ms_time in enumerate([60.0, 120.0, 180.0, 240.0], start=1):
            green_shift = 20.0 if index == 4 else 3.0
            lif_rows.extend(
                [
                    {"peak_id": f"g1_{index}", "channel": "G1", "time_sec": ms_time - green_shift - 0.1, "time_min": (ms_time - green_shift - 0.1) / 60.0},
                    {"peak_id": f"g2_{index}", "channel": "G2", "time_sec": ms_time - green_shift + 0.1, "time_min": (ms_time - green_shift + 0.1) / 60.0},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_sec": ms_time + 7.0, "time_min": (ms_time + 7.0) / 60.0},
                ]
            )
            ms_rows.append(
                {"event_id": f"ms_{index}", "time_sec": ms_time, "time_min": ms_time / 60.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
            )
            annotations.append(
                {
                    "annotation_id": f"qc_{index}",
                    "review_status": "accepted",
                    "review_stage": "qc_calibration",
                    "candidate_type": "manual_qc_anchor_set",
                    "label": "QC",
                    "lif_anchor_peak_ids": {"G1": f"g1_{index}", "G2": f"g2_{index}", "R1": f"r1_{index}"},
                    "ms_event_id": f"ms_{index}",
                    "acquisition_layout_hash": acquisition_layout_hash(layout),
                }
            )
        lif_peaks = pd.DataFrame(lif_rows)
        ms_events = pd.DataFrame(ms_rows)

        baseline_preview = accepted_qc_alignment_refit(
            lif_peaks,
            ms_events,
            annotations[:3],
            acquisition_layout=layout,
            qc_calibration_end_min=10.5,
            current_axis_shifts_sec={"green_axis": 1.0, "red_axis": -5.0},
        )
        preview = accepted_qc_alignment_refit(
            lif_peaks,
            ms_events,
            annotations,
            acquisition_layout=layout,
            qc_calibration_end_min=10.5,
            current_axis_shifts_sec={"green_axis": 1.0, "red_axis": -5.0},
        )

        self.assertAlmostEqual(preview["axis_shifts_sec"]["green_axis"], 3.0, places=6)
        self.assertAlmostEqual(preview["axis_shifts_sec"]["red_axis"], -7.0, places=6)
        self.assertEqual(preview["axes"]["green_axis"]["outlier_count"], 1)
        self.assertEqual(preview["axes"]["green_axis"]["inlier_count"], 3)
        self.assertEqual(preview["accepted_annotation_count"], 4)
        self.assertEqual(len(preview["preview_hash"]), 64)
        self.assertNotEqual(preview["preview_hash"], baseline_preview["preview_hash"])

        automatic = estimate_shift_alignment(lif_peaks, ms_events, 10.5, acquisition_layout=layout)
        applied = apply_qc_alignment_model(
            automatic,
            lif_peaks,
            ms_events,
            qc_calibration_end_min=10.5,
            acquisition_layout=layout,
            model=preview,
        )
        self.assertEqual(applied["status"], "accepted_anchor_refit_active")
        self.assertAlmostEqual(applied["channels"]["G1"]["shift_sec"], 3.0, places=6)
        self.assertAlmostEqual(applied["channels"]["G2"]["shift_sec"], 3.0, places=6)
        self.assertAlmostEqual(applied["channels"]["R1"]["shift_sec"], -7.0, places=6)

    def test_accepted_qc_refit_requires_two_independent_anchors_per_axis(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g2", "channel": "G2", "time_sec": 57.0, "time_min": 0.95},
                {"peak_id": "r1", "channel": "R1", "time_sec": 67.0, "time_min": 67.0 / 60.0},
            ]
        )
        ms_events = pd.DataFrame(
            [{"event_id": "ms", "time_sec": 60.0, "time_min": 1.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}]
        )
        annotations = [
            {
                "annotation_id": "qc",
                "review_status": "accepted",
                "candidate_type": "qc_calibration_anchor_0_10p5",
                "label": "QC",
                "g2_peak_id": "g2",
                "r1_peak_id": "r1",
                "ms_event_id": "ms",
            }
        ]

        with self.assertRaisesRegex(BadRequest, "2 个独立"):
            accepted_qc_alignment_refit(
                lif_peaks,
                ms_events,
                annotations,
                acquisition_layout=None,
                qc_calibration_end_min=10.5,
            )

    def test_accepted_qc_refit_rejects_globally_dispersed_shifts(self):
        lif_rows = []
        ms_rows = []
        annotations = []
        for index, (ms_time, shift) in enumerate(zip([120.0, 240.0, 360.0], [-30.0, 0.0, 30.0]), start=1):
            lif_time = ms_time - shift
            lif_rows.extend(
                [
                    {"peak_id": f"g2_{index}", "channel": "G2", "time_sec": lif_time, "time_min": lif_time / 60.0},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_sec": lif_time, "time_min": lif_time / 60.0},
                ]
            )
            ms_rows.append(
                {"event_id": f"ms_{index}", "time_sec": ms_time, "time_min": ms_time / 60.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
            )
            annotations.append(
                {
                    "annotation_id": f"auto_qc:g2_{index}:r1_{index}:ms_{index}",
                    "review_status": "accepted",
                    "label": "QC",
                    "g2_peak_id": f"g2_{index}",
                    "r1_peak_id": f"r1_{index}",
                    "ms_event_id": f"ms_{index}",
                }
            )

        with self.assertRaisesRegex(BadRequest, "稳健内点|P90"):
            accepted_qc_alignment_refit(
                pd.DataFrame(lif_rows),
                pd.DataFrame(ms_rows),
                annotations,
                acquisition_layout=None,
                qc_calibration_end_min=10.5,
            )

    def test_qc_alignment_persistence_requires_explicit_invalidation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            frozen = store.upsert_time_model(
                {
                    "time_model_version": "tm_frozen",
                    "status": "frozen",
                    "base_model_name": "auto",
                    "qc_calibration_end_min": 10.5,
                    "sample_valve_switch_min": 36.0,
                    "annotation_start_min": 40.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.5,
                    "contains_cell_labels": False,
                    "max_training_time_min": 42.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                },
                action="test_freeze",
            )
            model = {
                "model_version": 1,
                "model_id": "qca_test",
                "status": "preview",
                "preview_hash": "a" * 64,
                "qc_calibration_end_min": 10.5,
                "acquisition_layout_hash": acquisition_layout_hash(None),
                "axis_shifts_sec": {"green_axis": 3.0, "red_axis": -7.0},
            }

            with self.assertRaises(BadRequest):
                store.save_qc_alignment_model(model)
            self.assertEqual(store.active_time_model()["time_model_version"], frozen["time_model_version"])

            stored = store.save_qc_alignment_model(model, clear_frozen_time_model=True)
            self.assertEqual(stored["status"], "active")
            self.assertIsNone(store.active_time_model())
            self.assertEqual(store.qc_alignment_model()["model_id"], "qca_test")

            with self.assertRaises(BadRequest):
                store.update_project_config({"qc_calibration_end_min": 11.0})
            store.update_project_config(
                {"qc_calibration_end_min": 11.0},
                clear_qc_alignment_model=True,
            )
            self.assertIsNone(store.qc_alignment_model())

    def test_qc_alignment_save_creates_draft_atomically_and_evidence_change_invalidates_both(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            store.upsert_time_model(
                {
                    "time_model_version": "tm_frozen",
                    "status": "frozen",
                    "base_model_name": "auto",
                    "qc_calibration_end_min": 10.5,
                    "sample_valve_switch_min": 36.0,
                    "annotation_start_min": 40.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 2.0,
                    "contains_cell_labels": False,
                    "max_training_time_min": 42.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                },
                action="test_freeze",
            )
            qc_model = {
                "model_version": 1,
                "model_id": "qca_atomic",
                "status": "preview",
                "preview_hash": "b" * 64,
                "qc_calibration_end_min": 10.5,
                "acquisition_layout_hash": acquisition_layout_hash(None),
                "axis_shifts_sec": {"green_axis": 3.0, "red_axis": -7.0},
            }
            draft = {
                "time_model_version": "tm_after_qc",
                "status": "draft",
                "base_model_name": "accepted_anchor_refit_0_10.5min_qc",
                "qc_calibration_end_min": 10.5,
                "sample_valve_switch_min": 36.0,
                "annotation_start_min": 40.0,
                "local_delta_seed_window_min": 2.5,
                "ms_local_delta_sec": 0.0,
                "contains_cell_labels": False,
                "max_training_time_min": 42.5,
                "evidence_count": 0,
                "unique_match_count": 0,
                "conflict_count": 0,
                "median_abs_residual_sec": None,
                "p90_abs_residual_sec": None,
            }

            store.save_qc_alignment_model(
                qc_model,
                clear_frozen_time_model=True,
                draft_time_model_payload=draft,
            )

            self.assertEqual(store.qc_alignment_model()["model_id"], "qca_atomic")
            self.assertEqual(store.active_time_model()["time_model_version"], "tm_after_qc")
            self.assertEqual(store.active_time_model()["ms_local_delta_sec"], 0.0)

            store.upsert_review(
                annotation_id="auto_qc:g2:r1:ms",
                source="auto_candidate",
                review_status="accepted",
                payload={"label": "QC", "g2_peak_id": "g2", "r1_peak_id": "r1", "ms_event_id": "ms"},
                action="test_qc_evidence_change",
                invalidate_qc_alignment_model=True,
            )

            self.assertIsNone(store.qc_alignment_model())
            self.assertIsNone(store.active_time_model())
            self.assertEqual(store.get("auto_qc:g2:r1:ms")["review_status"], "accepted")

    def test_qc_model_invalidation_only_tracks_accepted_evidence_membership(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            store.save_qc_alignment_model(
                {
                    "model_version": 1,
                    "model_id": "qca_membership",
                    "status": "preview",
                    "preview_hash": "c" * 64,
                    "qc_calibration_end_min": 10.5,
                    "acquisition_layout_hash": acquisition_layout_hash(None),
                    "axis_shifts_sec": {"green_axis": 3.0, "red_axis": 14.0},
                }
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(),
                ms_scan=pd.DataFrame(),
                alignment={"model": "test", "qc_groups": {"groups": []}},
                store=store,
                channel_identity_prior={},
                acquisition_layout=None,
            )
            row = {"review_stage": "qc_calibration", "label": "QC"}

            self.assertFalse(
                app.require_qc_evidence_invalidation_confirmation(
                    row,
                    clear_qc_alignment_model=False,
                    previous_review_status="pending",
                    new_review_status="rejected",
                )
            )
            self.assertFalse(
                app.require_qc_evidence_invalidation_confirmation(
                    row,
                    clear_qc_alignment_model=False,
                    previous_review_status="accepted",
                    new_review_status="accepted",
                )
            )
            with self.assertRaises(BadRequest):
                app.require_qc_evidence_invalidation_confirmation(
                    row,
                    clear_qc_alignment_model=False,
                    previous_review_status="pending",
                    new_review_status="accepted",
                )
            self.assertTrue(
                app.require_qc_evidence_invalidation_confirmation(
                    row,
                    clear_qc_alignment_model=True,
                    previous_review_status="accepted",
                    new_review_status="rejected",
                )
            )

    def test_qc_alignment_refit_controls_are_present(self):
        self.assertIn('id="qcRefitPanel"', HTML)
        self.assertIn('id="previewQcRefit"', HTML)
        self.assertIn('id="applyQcRefit"', HTML)
        self.assertIn("'/api/qc-alignment-refit-preview'", HTML)
        self.assertIn("'/api/qc-alignment-refit'", HTML)

    def test_local_delta_estimator_prefers_qc_pair_topology_over_single_lif_ambiguity(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g2_1", "channel": "G2", "time_min": 40.000, "time_sec": 2400.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "r1_1", "channel": "R1", "time_min": 40.167, "time_sec": 2410.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "g2_2", "channel": "G2", "time_min": 40.500, "time_sec": 2430.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "r1_2", "channel": "R1", "time_min": 40.667, "time_sec": 2440.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "g2_3", "channel": "G2", "time_min": 41.000, "time_sec": 2460.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "r1_3", "channel": "R1", "time_min": 41.167, "time_sec": 2470.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
                {"peak_id": "r2_decoy", "channel": "R2", "time_min": 40.050, "time_sec": 2403.0, "snr": 50, "nearest_gap_sec": 10, "close_peak_risk": False, "merge_risk": False},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": "ms_1", "time_min": 40.050, "time_sec": 2403.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity", "pc34_760_apex": 10000, "nearest_event_gap_sec": 10, "collision_risk_high": False, "low_quality_scan_window": False},
                {"event_id": "ms_2", "time_min": 40.550, "time_sec": 2433.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity", "pc34_760_apex": 10000, "nearest_event_gap_sec": 10, "collision_risk_high": False, "low_quality_scan_window": False},
                {"event_id": "ms_3", "time_min": 41.050, "time_sec": 2463.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity", "pc34_760_apex": 10000, "nearest_event_gap_sec": 10, "collision_risk_high": False, "low_quality_scan_window": False},
            ]
        )

        result = estimate_local_delta_shift(
            lif_peaks,
            ms_events,
            annotation_start_min=40.0,
            seed_window_min=2.5,
            green_shift_sec=5.0,
            red_shift_sec=-5.0,
            qc_calibration_end_min=10.5,
            pair_offset_sec=10.0,
            axis_shifts_sec={"green_axis": 5.0, "red_axis": -5.0},
            channel_time_axes={"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"},
            qc_anchor_channels=["G2", "R1"],
        )

        self.assertEqual(result["method"], "qc_pair_seed_window_shift_grid_search")
        self.assertAlmostEqual(result["delta_sec"], 2.0, places=6)
        self.assertEqual(result["unique_match_count"], 3)
        self.assertEqual(result["recommendation_status"], "recommended")

    def test_pair_local_delta_refuses_single_event_as_recommendation(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g2", "channel": "G2", "time_min": 40.0, "time_sec": 2400.0, "snr": 50},
                {"peak_id": "r1", "channel": "R1", "time_min": 2410.0 / 60.0, "time_sec": 2410.0, "snr": 50},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": "ms", "time_min": 2403.0 / 60.0, "time_sec": 2403.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
            ]
        )

        result = estimate_local_delta_shift(
            lif_peaks,
            ms_events,
            annotation_start_min=40.0,
            seed_window_min=2.5,
            green_shift_sec=5.0,
            red_shift_sec=-5.0,
            qc_calibration_end_min=10.5,
            pair_offset_sec=10.0,
            axis_shifts_sec={"green_axis": 5.0, "red_axis": -5.0},
            channel_time_axes={"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"},
            qc_anchor_channels=["G2", "R1"],
        )

        self.assertAlmostEqual(result["delta_sec"], 2.0, places=6)
        self.assertEqual(result["unique_match_count"], 1)
        self.assertEqual(result["recommendation_status"], "insufficient_evidence")

    def test_pair_local_delta_marks_separated_equal_optima_ambiguous(self):
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g2_1", "channel": "G2", "time_min": 2400.0 / 60.0, "time_sec": 2400.0, "snr": 50},
                {"peak_id": "r1_1", "channel": "R1", "time_min": 2410.0 / 60.0, "time_sec": 2410.0, "snr": 50},
                {"peak_id": "g2_2", "channel": "G2", "time_min": 2460.0 / 60.0, "time_sec": 2460.0, "snr": 50},
                {"peak_id": "r1_2", "channel": "R1", "time_min": 2470.0 / 60.0, "time_sec": 2470.0, "snr": 50},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": f"ms_{time}", "time_min": time / 60.0, "time_sec": float(time), "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
                for time in [2400, 2410, 2460, 2470]
            ]
        )

        result = estimate_local_delta_shift(
            lif_peaks,
            ms_events,
            annotation_start_min=40.0,
            seed_window_min=2.5,
            green_shift_sec=5.0,
            red_shift_sec=-5.0,
            qc_calibration_end_min=10.5,
            pair_offset_sec=10.0,
            axis_shifts_sec={"green_axis": 5.0, "red_axis": -5.0},
            channel_time_axes={"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"},
            qc_anchor_channels=["G2", "R1"],
        )

        self.assertEqual(result["unique_match_count"], 2)
        self.assertEqual(result["recommendation_status"], "ambiguous")
        self.assertIsNotNone(result["runner_up"])

    def test_local_delta_estimator_uses_three_channel_axis_complete_evidence(self):
        lif_rows = []
        ms_rows = []
        for index, center in enumerate([2410.0, 2440.0, 2470.0], start=1):
            lif_rows.extend(
                [
                    {"peak_id": f"g1_{index}", "channel": "G1", "time_min": (center - 3.1) / 60.0, "time_sec": center - 3.1, "snr": 40},
                    {"peak_id": f"g2_{index}", "channel": "G2", "time_min": (center - 2.9) / 60.0, "time_sec": center - 2.9, "snr": 40},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_min": (center + 7.0) / 60.0, "time_sec": center + 7.0, "snr": 40},
                ]
            )
            ms_rows.append(
                {"event_id": f"ms_{index}", "time_min": (center - 2.0) / 60.0, "time_sec": center - 2.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"}
            )

        result = estimate_local_delta_shift(
            pd.DataFrame(lif_rows),
            pd.DataFrame(ms_rows),
            annotation_start_min=40.0,
            seed_window_min=2.5,
            green_shift_sec=3.0,
            red_shift_sec=-7.0,
            qc_calibration_end_min=10.5,
            axis_shifts_sec={"green_axis": 3.0, "red_axis": -7.0},
            channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
            qc_anchor_channels=["G1", "G2", "R1"],
        )

        self.assertEqual(result["method"], "qc_anchor_set_seed_window_shift_grid_search")
        self.assertEqual(result["recommendation_status"], "recommended")
        self.assertAlmostEqual(result["delta_sec"], 2.0, places=6)
        self.assertEqual(result["unique_match_count"], 3)
        self.assertEqual(result["complete_anchor_set_count"], 3)

    def test_multi_anchor_local_delta_does_not_let_one_fewer_conflict_override_fit(self):
        def synthetic_evidence(*args, ms_delta_sec, **kwargs):
            result = {
                "delta_sec": float(ms_delta_sec),
                "evidence": [],
                "evidence_count": 0,
                "unique_match_count": 0,
                "complete_anchor_set_count": 0,
                "total_anchor_support": 0,
                "conflict_count": 0,
                "median_abs_residual_sec": None,
                "p90_abs_residual_sec": None,
            }
            if abs(float(ms_delta_sec) + 18.25) < 1e-9:
                result.update(
                    evidence_count=3,
                    unique_match_count=3,
                    total_anchor_support=6,
                    conflict_count=1,
                    median_abs_residual_sec=1.479,
                    p90_abs_residual_sec=1.4854,
                )
            elif abs(float(ms_delta_sec) - 5.0) < 1e-9:
                result.update(
                    evidence_count=3,
                    unique_match_count=3,
                    total_anchor_support=6,
                    conflict_count=2,
                    median_abs_residual_sec=0.393,
                    p90_abs_residual_sec=0.538,
                )
            return result

        with mock.patch("annotation_app.app.local_delta_qc_anchor_set_evidence", side_effect=synthetic_evidence):
            result = estimate_local_delta_shift(
                pd.DataFrame(),
                pd.DataFrame(),
                annotation_start_min=54.0,
                seed_window_min=6.0,
                green_shift_sec=0.0,
                red_shift_sec=0.0,
                qc_calibration_end_min=21.5,
                axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
                channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
                qc_anchor_channels=["R1", "G1", "G2"],
            )

        self.assertAlmostEqual(result["delta_sec"], 5.0, places=6)
        self.assertEqual(result["recommendation_status"], "recommended")
        self.assertEqual(result["runner_up"]["conflict_count"], 1)

    def test_three_channel_local_delta_refuses_single_event_as_recommendation(self):
        center = 2410.0
        lif_peaks = pd.DataFrame(
            [
                {"peak_id": "g1", "channel": "G1", "time_min": (center - 3.1) / 60.0, "time_sec": center - 3.1, "snr": 40},
                {"peak_id": "g2", "channel": "G2", "time_min": (center - 2.9) / 60.0, "time_sec": center - 2.9, "snr": 40},
                {"peak_id": "r1", "channel": "R1", "time_min": (center + 7.0) / 60.0, "time_sec": center + 7.0, "snr": 40},
            ]
        )
        ms_events = pd.DataFrame(
            [
                {"event_id": "ms", "time_min": (center - 2.0) / 60.0, "time_sec": center - 2.0, "event_strategy": "pc34_primary", "primary_signal_col": "pc34_760_max_intensity"},
            ]
        )

        result = estimate_local_delta_shift(
            lif_peaks,
            ms_events,
            annotation_start_min=40.0,
            seed_window_min=2.5,
            green_shift_sec=3.0,
            red_shift_sec=-7.0,
            qc_calibration_end_min=10.5,
            axis_shifts_sec={"green_axis": 3.0, "red_axis": -7.0},
            channel_time_axes={"G1": "green_axis", "G2": "green_axis", "R1": "red_axis"},
            qc_anchor_channels=["G1", "G2", "R1"],
        )

        self.assertEqual(result["recommendation_status"], "insufficient_evidence")
        self.assertEqual(result["unique_match_count"], 1)

    def test_export_preserves_dynamic_anchor_channels_and_g1_cell_fields(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(),
                ms_scan=pd.DataFrame(),
                alignment={"model": "test", "qc_groups": {"groups": []}},
                store=AnnotationStore(Path(tmp) / "annotation.sqlite"),
                channel_identity_prior={"G1": {"identity_prior": "Day0"}},
                acquisition_layout=None,
            )
            qc_export = app.export_row(
                {
                    "annotation_id": "qc",
                    "candidate_type": "qc_calibration_anchor_0_10p5",
                    "source": "auto_candidate",
                    "review_status": "accepted",
                    "anchor_channels": ["G1", "G2", "R1"],
                    "lif_anchor_peak_ids": {"G1": "g1", "G2": "g2", "R1": "r1"},
                    "lif_anchor_raw_times_min": {"G1": 1.0, "G2": 1.001, "R1": 1.1},
                    "lif_anchor_plot_times_min": {"G1": 1.05, "G2": 1.051, "R1": 1.05},
                    "required_time_axes": ["green_axis", "red_axis"],
                    "ms_event_id": "ms",
                    "ms_time_min": 1.05,
                    "ms_plot_time_min": 1.05,
                },
                stage="qc_calibration",
                export_id="export",
                exported_at="2026-07-14T00:00:00Z",
            )
            cell_export = app.export_row(
                {
                    "annotation_id": "cell",
                    "candidate_type": "manual_cell_pair",
                    "lif_channel": "G1",
                    "lif_peak_id": "g1_cell",
                    "lif_raw_time_min": 41.0,
                    "lif_plot_time_min": 41.1,
                    "ms_event_id": "ms_cell",
                    "ms_time_min": 41.1,
                    "ms_plot_time_min": 41.1,
                },
                stage="cell_annotation",
                export_id="export",
                exported_at="2026-07-14T00:00:00Z",
            )

        self.assertEqual(qc_export["g1_peak_id"], "g1")
        self.assertEqual(qc_export["g2_peak_id"], "g2")
        self.assertEqual(qc_export["r1_peak_id"], "r1")
        self.assertEqual(json.loads(qc_export["qc_anchor_peak_ids_json"]), {"G1": "g1", "G2": "g2", "R1": "r1"})
        self.assertEqual(cell_export["g1_peak_id"], "g1_cell")
        self.assertEqual(cell_export["g1_raw_time_min"], 41.0)

    def test_new_project_rejects_existing_annotation_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            sqlite_path = project_dir / "annotation_app/annotations/annotation.sqlite"
            sqlite_path.parent.mkdir(parents=True)
            sqlite_path.write_bytes(b"not empty")

            with self.assertRaises(BadRequest):
                assert_new_project_target_is_clean(project_dir, [])

    def test_new_project_rejects_existing_legacy_annotation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            state_path = project_dir / "annotation_app/annotations/annotation_state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text('{"annotations": {}}', encoding="utf-8")

            with self.assertRaises(BadRequest):
                assert_new_project_target_is_clean(project_dir, [])

    def test_staging_project_replaces_preexisting_empty_target_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            target = parent / "new-project"
            staging = parent / ".new-project.lma-building-test"
            target.mkdir()
            staging.mkdir()
            (staging / "complete.marker").write_text("complete", encoding="utf-8")

            commit_staging_project(staging, target, target_preexisted=True)

            self.assertFalse(staging.exists())
            self.assertEqual((target / "complete.marker").read_text(encoding="utf-8"), "complete")

    def test_staging_publish_failure_restores_preexisting_empty_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            target = parent / "new-project"
            staging = parent / ".new-project.lma-building-test"
            target.mkdir()
            staging.mkdir()
            (staging / "complete.marker").write_text("complete", encoding="utf-8")

            with mock.patch("annotation_app.app.os.replace", side_effect=OSError("publish failed")):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    commit_staging_project(staging, target, target_preexisted=True)

            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertTrue(staging.is_dir())

    def test_staging_publish_refuses_target_that_became_nonempty(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            target = parent / "new-project"
            staging = parent / ".new-project.lma-building-test"
            target.mkdir()
            staging.mkdir()
            (target / "user-file.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(BadRequest, "不再为空"):
                commit_staging_project(staging, target, target_preexisted=True)

            self.assertEqual((target / "user-file.txt").read_text(encoding="utf-8"), "keep")
            self.assertTrue(staging.is_dir())

    def test_manifest_validation_requires_all_intermediate_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            manifest = {"intermediate_tables": {"lif_traces": {"path": "x.parquet"}}}

            with self.assertRaises(BadRequest):
                validate_project_manifest_against_files(project_dir, manifest)

    def test_manifest_validation_rejects_intermediate_size_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            table_path = project_dir / "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet"
            table_path.parent.mkdir(parents=True)
            table_path.write_bytes(b"abc")
            manifest = {
                "intermediate_tables": {
                    "lif_traces": {
                        "path": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet",
                        "size_bytes": 999,
                    }
                }
            }

            with self.assertRaises(BadRequest):
                validate_project_manifest_against_files(project_dir, manifest)

    def test_annotation_db_validation_rejects_missing_peak_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "annotation.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE annotations (
                        annotation_id TEXT PRIMARY KEY,
                        g2_peak_id TEXT,
                        r1_peak_id TEXT,
                        ms_event_id TEXT,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO annotations (
                        annotation_id, g2_peak_id, r1_peak_id, ms_event_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("bad", "missing_g2", None, "ms1", "{}"),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(BadRequest):
                validate_annotation_db_against_tables(db_path, {"g2_ok"}, {"ms1"})

    def test_sqlite_project_binding_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "annotation.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE project_config (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        app_version TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO project_config (key, value_json, updated_at, app_version)
                    VALUES ('project_table_binding', ?, '2026-07-01T00:00:00Z', 'test')
                    """,
                    ('{"binding_sha256": "old"}',),
                )
                conn.commit()
            finally:
                conn.close()

            binding = project_table_binding(
                {
                    "lif_traces": {"path": "a", "size_bytes": 1, "sha256": "new"},
                    "lif_peaks": {"path": "b", "size_bytes": 2, "sha256": "new"},
                    "ms_events": {"path": "c", "size_bytes": 3, "sha256": "new"},
                    "ms_scan_summary": {"path": "d", "size_bytes": 4, "sha256": "new"},
                }
            )

            with self.assertRaises(BadRequest):
                validate_sqlite_project_binding(db_path, binding, allow_adopt=False)


if __name__ == "__main__":
    unittest.main()
