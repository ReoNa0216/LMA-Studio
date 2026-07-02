import tempfile
import unittest
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
    acquisition_layout_from_manifest,
    build_qc_alignment_groups,
    build_raw_input_project_records,
    channel_identity_prior_from_manifest,
    export_filename_for_project,
    estimate_local_delta_shift,
    estimate_shift_alignment,
    unique_file_path,
    assert_new_project_target_is_clean,
    project_table_binding,
    read_project_manifest,
    validate_sqlite_project_binding,
    validate_annotation_db_against_tables,
    validate_project_manifest_against_files,
    write_project_manifest,
)


class ProjectManifestTest(unittest.TestCase):
    def test_external_reference_records_absolute_paths_without_raw_inputs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir = Path(tmp) / "project"
            raw_paths = {
                "lif_g2": Path(r"E:\raw\g2.csv"),
                "lif_r1": Path(r"E:\raw\r1.csv"),
                "lif_r2": Path(r"E:\raw\r2.csv"),
                "ms": Path(r"E:\raw\ms.txt"),
            }

            rows, manifest_inputs, _layout = build_raw_input_project_records(
                project_dir=project_dir,
                raw_paths=raw_paths,
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                identities={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
            )

            self.assertFalse((project_dir / "raw_inputs").exists())
            self.assertEqual(rows[0]["path"], str(raw_paths["lif_g2"]))
            self.assertEqual(rows[3]["path"], str(raw_paths["ms"]))
            self.assertEqual(manifest_inputs["ms"]["path_mode"], RAW_INPUT_MODE_EXTERNAL)
            self.assertEqual(manifest_inputs["ms"]["path"], str(raw_paths["ms"]))

    def test_copy_records_use_project_relative_raw_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            raw_paths = {
                "lif_g2": Path(r"E:\raw\g2.csv"),
                "lif_r1": Path(r"E:\raw\r1.csv"),
                "lif_r2": Path(r"E:\raw\r2.csv"),
                "ms": Path(r"E:\raw\ms.txt"),
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
                raw_inputs={"lif_g2": {"path": r"E:\raw\g2.csv", "path_mode": RAW_INPUT_MODE_EXTERNAL}},
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

    def test_build_raw_input_records_accepts_configured_lif_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            lif_inputs = [
                {"key": "lif_1", "path": Path(r"E:\raw\g1.csv"), "channel": "G1", "identity_prior": "Day1"},
                {"key": "lif_2", "path": Path(r"E:\raw\g2.csv"), "channel": "G2", "identity_prior": "Day0"},
                {"key": "lif_3", "path": Path(r"E:\raw\r1.csv"), "channel": "R1", "identity_prior": "Day3"},
            ]

            rows, manifest_inputs, layout = build_raw_input_project_records(
                project_dir=project_dir,
                raw_paths={"ms": Path(r"E:\raw\ms.txt")},
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                identities={},
                lif_inputs=lif_inputs,
                qc_anchor_channels=["G2", "R1"],
            )

            lif_rows = [row for row in rows if row["input_class"] == "raw_lif_trace"]
            self.assertEqual([row["input_id"] for row in lif_rows], ["lif_1_raw", "lif_2_raw", "lif_3_raw"])
            self.assertEqual([row["channel"] for row in lif_rows], ["G1", "G2", "R1"])
            self.assertEqual([row["detector"] for row in lif_rows], ["green", "green", "red"])
            self.assertEqual(manifest_inputs["lif_1"]["path"], str(Path(r"E:\raw\g1.csv")))
            self.assertEqual(layout["qc_anchor_channels"], ["G2", "R1"])

    def test_project_dir_defaults_raw_data_dir_to_project_raw_inputs(self):
        project = ProjectPaths.from_args(project_dir=r"D:\LIFMSProjects\Batch03")

        self.assertEqual(project.raw_data_dir, Path(r"D:\LIFMSProjects\Batch03\raw_inputs"))

    def test_bootstrap_meta_hides_project_when_none_selected(self):
        data = BootstrapAppData(project=ProjectPaths.from_args(), load_error="", project_selected=False)

        meta = data.meta()

        self.assertTrue(meta["bootstrap"])
        self.assertIsNone(meta["project"])
        self.assertEqual(meta["root"], "")

    def test_export_filename_uses_safe_project_name_and_timestamp(self):
        filename = export_filename_for_project(
            Path(r"D:\LIFMSProjects\Batch:03 Test"),
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

    def test_window_lif_display_mode_changes_trace_y_without_changing_peak_identity(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(
                    [
                        {"channel": "G2", "label": "Day0", "detector": "green", "phase": "qc", "time_min": 1.00, "time_sec": 60.0, "raw": 0.050, "baseline": 0.040, "signal": 0.010},
                        {"channel": "G2", "label": "Day0", "detector": "green", "phase": "qc", "time_min": 1.01, "time_sec": 60.6, "raw": 0.060, "baseline": 0.040, "signal": 0.020},
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
                        }
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
                        }
                    ]
                ),
                ms_scan=pd.DataFrame(
                    [
                        {
                            "scan_start_time_min": 1.0,
                            "pc34_760_max_intensity": 1000.0,
                            "qc_782_max_intensity": 500.0,
                        }
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
            self.assertEqual([row["peak_id"] for row in raw_window["lif_peaks"]], ["g2_peak_1"])
            self.assertEqual([row["peak_id"] for row in signal_window["lif_peaks"]], ["g2_peak_1"])
            self.assertEqual(raw_window["lif_peaks"][0]["display_y"], 0.060)
            self.assertEqual(signal_window["lif_peaks"][0]["display_y"], 0.020)
            self.assertEqual(raw_window["lif_traces"]["G2"][0]["y"], 0.050)
            self.assertEqual(signal_window["lif_traces"]["G2"][0]["y"], 0.010)

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
        self.assertIn('稳健放大', HTML)

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
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            qc_calibration_end_min=10.5,
            pair_offset_sec=10.0,
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
            channel_time_axes={"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"},
            qc_anchor_channels=["G2", "R1"],
        )

        self.assertEqual(result["method"], "qc_pair_seed_window_shift_grid_search")
        self.assertAlmostEqual(result["delta_sec"], 2.0, places=6)
        self.assertEqual(result["unique_match_count"], 3)

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
