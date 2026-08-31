"""Focused contracts for v0.4.12 physical-axis fine tuning."""

import copy
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import (
    APP_VERSION,
    HTML,
    AnnotationStore,
    AppData,
    BadRequest,
    ProjectPaths,
    acquisition_layout_hash,
    apply_qc_alignment_model,
    calibration_protocol_hash,
    estimate_shift_alignment,
    live_preview_context_margin_min,
    manual_qc_alignment_preview_model,
    normalize_acquisition_layout,
    normalize_calibration_protocol,
    normalize_post_qc_strategy,
)


class V0412AxisFineTuneContractTest(unittest.TestCase):
    def setUp(self):
        self.layout = normalize_acquisition_layout(
            {
                "lif_channels": [
                    {"input_id": "g1", "channel": "G1", "detector": "green", "time_axis": "green_axis"},
                    {"input_id": "g2", "channel": "G2", "detector": "green", "time_axis": "green_axis"},
                    {"input_id": "r1", "channel": "R1", "detector": "red", "time_axis": "red_axis"},
                ],
                "qc_anchor_channels": ["G1", "G2", "R1"],
            }
        )
        self.protocol = normalize_calibration_protocol(
            {
                "protocol_version": 1,
                "segments": [
                    {
                        "segment_id": "reference",
                        "order": 1,
                        "start_min": 0.0,
                        "end_min": 10.0,
                        "reference_channels": ["G1", "G2", "R1"],
                        "reference_mode": "red_green",
                        "population_label": "QC",
                        "boundaries_confirmed": True,
                    }
                ],
            },
            self.layout,
        )
        self.current_alignment = {
            "model": "automatic",
            "status": "auto",
            "axis_shifts_sec": {"green_axis": 3.0, "red_axis": -7.0},
            "green_to_ms_shift_sec": 3.0,
            "red_to_ms_shift_sec": -7.0,
            "channel_time_axes": dict(self.layout["channel_time_axes"]),
            "qc_anchor_channels": ["G1", "G2", "R1"],
            "acquisition_layout_hash": acquisition_layout_hash(self.layout),
            "calibration_protocol_hash": calibration_protocol_hash(self.protocol, self.layout),
            "channels": {channel: {} for channel in ("G1", "G2", "R1")},
            "axes": {"green_axis": {}, "red_axis": {}},
            "qc_groups": {"groups": []},
        }

    def test_version_and_compact_user_controls_are_present(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.5.1")
        self.assertIn('id="axisFineTuneToggle"', HTML)
        self.assertIn('id="axisFineTunePanel"', HTML)
        self.assertIn('id="axisFineTuneRows"', HTML)
        self.assertIn('id="discardAxisFineTune"', HTML)
        self.assertIn('id="applyAxisFineTune"', HTML)
        self.assertIn("MS 不移动", HTML)

    def test_axis_controls_use_full_width_sliders_without_crowded_dot_labels(self):
        self.assertIn('class="axis-fine-tune-slider"', HTML)
        self.assertIn('class="axis-fine-tune-nudges"', HTML)
        self.assertIn('data-axis-readout', HTML)
        render_body = HTML.split("function renderAxisFineTunePanel", 1)[1].split(
            "async function toggleAxisFineTune", 1
        )[0]
        self.assertNotIn(" · ", render_body)
        self.assertIn("`${compactName} ${channels.join('/')}`", render_body)

    def test_axis_and_ms_sliders_redraw_locally_without_window_requests(self):
        axis_update = HTML.split("function updateAxisFineTuneValue", 1)[1].split(
            "async function discardAxisFineTune", 1
        )[0]
        self.assertIn("scheduleLivePreviewDraw()", axis_update)
        self.assertNotIn("loadWindow", axis_update)
        delta_update = HTML.split("function updateDeltaPreview", 1)[1].split(
            "async function freezeLocalDelta", 1
        )[0]
        self.assertIn("scheduleLivePreviewDraw()", delta_update)
        self.assertNotIn("loadWindow", delta_update)
        draw_body = HTML.split("function draw()", 1)[1].split("function trackShiftSec", 1)[0]
        self.assertIn("livePreviewDeltaSecForTrack(track)", draw_body)
        self.assertIn("trackXScale", draw_body)

    def test_live_preview_moves_track_time_ticks_with_the_signal(self):
        draw_body = HTML.split("function draw()", 1)[1].split(
            "function trackShiftSec", 1
        )[0]
        self.assertIn(
            "drawTrackTimeAxis(svg, signalLayer, xScale",
            draw_body,
        )
        axis_body = HTML.split("function drawTrackTimeAxis", 1)[1].split(
            "function boxesOverlap", 1
        )[0]
        self.assertIn("movingLayer.appendChild", axis_body)
        self.assertIn("svg.appendChild", axis_body)

    def test_live_preview_context_covers_full_slider_travel(self):
        self.assertGreaterEqual(live_preview_context_margin_min("lif"), 2.08)
        self.assertGreaterEqual(live_preview_context_margin_min("ms"), 0.74)
        self.assertEqual(live_preview_context_margin_min(""), 0.08)

    def test_manual_model_is_per_physical_axis_and_keeps_schema_v1(self):
        preview = manual_qc_alignment_preview_model(
            acquisition_layout=self.layout,
            calibration_protocol=self.protocol,
            qc_calibration_end_min=10.0,
            current_alignment=self.current_alignment,
            requested_axis_shifts_sec={"green_axis": 4.25, "red_axis": -6.5},
        )
        self.assertEqual(preview["model_version"], 1)
        self.assertEqual(preview["method"], "manual_physical_axis_shift")
        self.assertEqual(preview["axis_shifts_sec"], {"green_axis": 4.25, "red_axis": -6.5})
        self.assertEqual(preview["previous_axis_shifts_sec"], {"green_axis": 3.0, "red_axis": -7.0})
        self.assertEqual(len(preview["preview_hash"]), 64)

    def test_manual_model_requires_all_and_only_configured_axes(self):
        for shifts in (
            {"green_axis": 4.0},
            {"green_axis": 4.0, "red_axis": -6.0, "other_axis": 1.0},
            {"green_axis": float("inf"), "red_axis": -6.0},
        ):
            with self.subTest(shifts=shifts), self.assertRaises(BadRequest):
                manual_qc_alignment_preview_model(
                    acquisition_layout=self.layout,
                    calibration_protocol=self.protocol,
                    qc_calibration_end_min=10.0,
                    current_alignment=self.current_alignment,
                    requested_axis_shifts_sec=shifts,
                )

    def test_applying_manual_model_moves_shared_axes_not_individual_channels(self):
        preview = manual_qc_alignment_preview_model(
            acquisition_layout=self.layout,
            calibration_protocol=self.protocol,
            qc_calibration_end_min=10.0,
            current_alignment=self.current_alignment,
            requested_axis_shifts_sec={"green_axis": 4.25, "red_axis": -6.5},
        )
        lif = pd.DataFrame(
            [
                {"peak_id": "g1", "channel": "G1", "time_min": 1.0, "time_sec": 60.0, "snr": 20.0, "peak_tier": "core"},
                {"peak_id": "g2", "channel": "G2", "time_min": 1.0, "time_sec": 60.0, "snr": 20.0, "peak_tier": "core"},
                {"peak_id": "r1", "channel": "R1", "time_min": 1.0, "time_sec": 60.0, "snr": 20.0, "peak_tier": "core"},
            ]
        )
        ms = pd.DataFrame(
            [
                {
                    "event_id": "ms",
                    "time_min": 1.0,
                    "time_sec": 60.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                }
            ]
        )
        applied = apply_qc_alignment_model(
            self.current_alignment,
            lif,
            ms,
            qc_calibration_end_min=10.0,
            acquisition_layout=self.layout,
            calibration_protocol=self.protocol,
            model=preview,
        )
        self.assertEqual(applied["status"], "manual_axis_shift_preview")
        self.assertEqual(applied["channels"]["G1"]["shift_sec"], 4.25)
        self.assertEqual(applied["channels"]["G2"]["shift_sec"], 4.25)
        self.assertEqual(applied["channels"]["R1"]["shift_sec"], -6.5)

    def test_preview_is_runtime_only_and_apply_preserves_annotations(self):
        lif_rows = []
        ms_rows = []
        for index, ms_time in enumerate((60.0, 120.0, 180.0), start=1):
            lif_rows.extend(
                [
                    {"peak_id": f"g1_{index}", "channel": "G1", "time_min": (ms_time - 3.0) / 60.0, "time_sec": ms_time - 3.0, "snr": 20.0, "peak_tier": "core"},
                    {"peak_id": f"g2_{index}", "channel": "G2", "time_min": (ms_time - 3.0) / 60.0, "time_sec": ms_time - 3.0, "snr": 20.0, "peak_tier": "core"},
                    {"peak_id": f"r1_{index}", "channel": "R1", "time_min": (ms_time + 7.0) / 60.0, "time_sec": ms_time + 7.0, "snr": 20.0, "peak_tier": "core"},
                ]
            )
            ms_rows.append(
                {
                    "event_id": f"ms_{index}",
                    "time_min": ms_time / 60.0,
                    "time_sec": ms_time,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                }
            )
        lif = pd.DataFrame(lif_rows)
        ms = pd.DataFrame(ms_rows)
        alignment = estimate_shift_alignment(
            lif,
            ms,
            10.0,
            acquisition_layout=self.layout,
            calibration_protocol=self.protocol,
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            strategy = normalize_post_qc_strategy({"mode": "disabled"}, self.layout)
            store = AnnotationStore(
                root / "annotation.sqlite",
                default_project_config={
                    "qc_calibration_end_min": 10.0,
                    "annotation_start_min": 20.0,
                    "local_delta_seed_window_min": 2.5,
                    "calibration_protocol": self.protocol,
                    "post_qc_strategy": strategy,
                },
            )
            store.upsert_review(
                annotation_id="manual_history",
                source="manual_created",
                review_status="pending",
                payload={"label": "QC"},
                action="test_history",
            )
            store.upsert_time_model(
                {
                    "time_model_version": "tm_frozen",
                    "status": "frozen",
                    "base_model_name": str(alignment["model"]),
                    "qc_calibration_end_min": 10.0,
                    "sample_valve_switch_min": 10.0,
                    "annotation_start_min": 20.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.5,
                    "contains_cell_labels": False,
                    "max_training_time_min": 22.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                    "acquisition_layout_hash": acquisition_layout_hash(self.layout),
                    "calibration_protocol_hash": calibration_protocol_hash(self.protocol, self.layout),
                },
                action="test_frozen",
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=root),
                lif_traces=pd.DataFrame(),
                lif_peaks=lif,
                ms_events=ms,
                ms_scan=pd.DataFrame(),
                alignment=alignment,
                store=store,
                channel_identity_prior={},
                acquisition_layout=self.layout,
                calibration_protocol=self.protocol,
                post_qc_strategy=strategy,
            )
            original_alignment = copy.deepcopy(app.alignment)
            shifts = {"green_axis": 4.25, "red_axis": -6.5}
            preview = app.qc_axis_manual_preview(shifts)
            self.assertEqual(app.alignment, original_alignment)
            preview_alignment = app.alignment_with_axis_shift_preview(shifts)
            self.assertEqual(preview_alignment["status"], "manual_axis_shift_preview")
            self.assertEqual(preview_alignment["axis_shifts_sec"], shifts)
            self.assertEqual(app.alignment, original_alignment)
            result = app.save_qc_axis_manual_adjustment(
                shifts,
                preview["preview_hash"],
                clear_frozen_time_model=True,
            )
            self.assertEqual(result["alignment"]["status"], "manual_axis_shift_active")
            self.assertEqual(result["time_model"]["status"], "draft")
            self.assertEqual(result["time_model"]["ms_local_delta_sec"], 0.0)
            self.assertEqual(store.get("manual_history")["review_status"], "pending")

    def test_live_preview_reuses_window_and_never_moves_ms(self):
        load_body = HTML.split("async function loadWindow()", 1)[1].split("function updateMetrics", 1)[0]
        self.assertIn("preview_axis_shifts_sec", load_body)
        self.assertIn("state.axisFineTuneShifts", load_body)
        calibration_branch = load_body.split("state.stage === 'qc_calibration'", 1)[1].split(
            "state.stage === 'event_annotation'", 1
        )[0]
        self.assertNotIn("preview_ms_delta_sec=", calibration_branch)
        self.assertIn("state.timelineAxisShifts", load_body)
        self.assertIn("state.timelineDeltaSec", load_body)
        self.assertIn("'/api/qc-axis-preview'", HTML)
        self.assertIn("'/api/qc-axis-apply'", HTML)

    def test_ui_exposes_axes_from_layout_not_channel_specific_sliders(self):
        render_body = HTML.split("function renderAxisFineTunePanel", 1)[1].split(
            "async function previewQcAlignmentRefit", 1
        )[0]
        self.assertIn("configuredPhysicalAxes()", render_body)
        self.assertIn("physicalAxisName(axis)", render_body)
        self.assertNotIn("G1", render_body)
        self.assertNotIn("G2", render_body)
        self.assertNotIn("R1", render_body)
        self.assertNotIn("R2", render_body)

    def test_apply_warning_preserves_annotations_but_resets_downstream_time_model(self):
        apply_body = HTML.split("async function applyAxisFineTune", 1)[1].split(
            "async function previewQcAlignmentRefit", 1
        )[0]
        self.assertIn("已有人工标注", apply_body)
        self.assertIn("重新进行后段时间差校正", apply_body)
        self.assertIn("clear_frozen_time_model", apply_body)


if __name__ == "__main__":
    unittest.main()
