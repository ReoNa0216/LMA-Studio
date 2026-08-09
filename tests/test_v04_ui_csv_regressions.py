from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import (
    HTML,
    AppData,
    AnnotationStore,
    ProjectPaths,
    acquisition_layout_hash,
    calibration_protocol_hash,
)


def javascript_function_body(name: str, next_name: str) -> str:
    start_token = f"function {name}"
    end_token = f"function {next_name}"
    start = HTML.index(start_token)
    end = HTML.index(end_token, start + len(start_token))
    return HTML[start:end]


class V04UiRegressionTest(unittest.TestCase):
    def test_legacy_protocol_editor_is_explicitly_read_only(self):
        body = javascript_function_body(
            "renderConfigProtocolEditor",
            "configChannelCheckboxes",
        )

        self.assertIn("compatibility_mode", body)
        self.assertRegex(body, r"旧项目.*只读|只读.*旧项目")
        for field in ("start_min", "end_min", "boundaries_confirmed"):
            control = re.search(
                rf'<(?:input|select)[^>]*data-cfg-segment-field="{field}"[^>]*>',
                body,
            )
            self.assertIsNotNone(control, field)
            self.assertIn("disabled", control.group(0), field)

    def test_lif_input_changes_revoke_confirmation_and_stale_suggestions(self):
        self.assertIn("importSuggestionRevision", HTML)
        self.assertIn("function invalidateImportCalibrationConfirmations", HTML)

        listener_start = HTML.index("el('importLifRows').addEventListener('input'")
        listener_end = HTML.index("el('importCalibrationSegments').addEventListener", listener_start)
        listener = HTML[listener_start:listener_end]
        for field in ("path", "channel", "detector"):
            self.assertIn(f"'{field}'", listener)
        self.assertIn("invalidateImportCalibrationConfirmations", listener)
        self.assertRegex(listener, r"importSuggestionRevision\s*\+=\s*1")
        self.assertIn("automaticTimeAxisForDetector", listener)

        suggestion = javascript_function_body(
            "suggestImportCalibrationWindows",
            "postQcStrategyPayload",
        )
        self.assertRegex(
            suggestion,
            r"const\s+\w*[Rr]evision\s*=\s*state\.importSuggestionRevision",
        )
        self.assertRegex(
            suggestion,
            r"\w*[Rr]evision\s*!==\s*state\.importSuggestionRevision",
        )
        self.assertIn("segment.boundaries_confirmed = false", suggestion)

    def test_new_project_uses_automatic_plain_language_physical_axes(self):
        self.assertIn("function automaticTimeAxisForDetector", HTML)
        self.assertIn("function physicalTimeAxisLabel", HTML)
        self.assertIn("Green 共享时间轴", HTML)
        self.assertIn("Red 共享时间轴", HTML)
        self.assertIn("由检测器自动设置", HTML)
        self.assertNotIn('data-import-field="time_axis"', HTML)

    def test_hsc1_is_an_optional_collapsed_template_not_the_default_action(self):
        self.assertIn('<details id="importProjectTemplates"', HTML)
        self.assertRegex(HTML, r"<summary>[^<]*可选[^<]*实验[^<]*模板[^<]*</summary>")
        self.assertIn('id="applyHsc1Preset"', HTML)
        self.assertNotIn('<div class="preset-box">', HTML)

    def test_event_coordinate_picker_explains_required_columns_and_ignored_extras(self):
        self.assertIn("必须包含 scan_start_time、UMAP1、UMAP2", HTML)
        self.assertRegex(HTML, r"CellNumber.*batch.*其他列.*保留.*忽略")

    def test_import_segment_status_uses_a_full_width_grid_row(self):
        body = javascript_function_body(
            "renderImportSegments",
            "renderImportScheduledQcWindows",
        )
        self.assertIn('class="protocol-segment-status"', body)
        self.assertNotRegex(
            body,
            r"<span>.*suggestion_status.*</span>",
            "长失效提示不能放进 34px 的参考段序号列",
        )
        rule = re.search(r"\.protocol-segment-status\s*\{(?P<body>[^}]*)\}", HTML)
        self.assertIsNotNone(rule)
        self.assertRegex(rule.group("body"), r"grid-column\s*:\s*1\s*/\s*-1")
        self.assertRegex(rule.group("body"), r"overflow-wrap\s*:\s*(?:anywhere|break-word)")

    def test_post_qc_policy_change_explains_historical_staleness(self):
        body = javascript_function_body("saveProjectConfig", "previewQcAlignmentRefit")

        self.assertRegex(body, r"postQcStrategyChanged|postQcChanged")
        self.assertRegex(body, r"后段 QC.*保留.*历史.*失效|保留.*历史.*后段 QC.*失效")

    def test_axis_shift_metrics_are_driven_by_configured_physical_axes(self):
        self.assertIn('id="axisShiftMetrics"', HTML)
        self.assertIn("function configuredPhysicalAxes()", HTML)
        self.assertIn("function renderAxisShiftMetrics", HTML)

        body = javascript_function_body("updateTimeModelPanel", "stageCounts")
        self.assertIn("configuredPhysicalAxes()", body)
        self.assertIn("renderAxisShiftMetrics", body)

    def test_new_project_allows_unconfirmed_draft_but_explains_calibration_gate(self):
        import_body = javascript_function_body("importProject", "openExistingProject")

        self.assertNotIn("的边界尚未由用户确认", import_body)
        self.assertRegex(
            HTML,
            r"可先创建项目.*边界.*待确认.*前段校准|边界.*待确认.*可先创建项目.*前段校准",
        )
        self.assertRegex(HTML, r"确认边界前.*校准|校准.*确认边界前")

    def test_post_qc_policy_fields_wrap_and_hide_irrelevant_signature_picker(self):
        rule = re.search(r"\.policy-fields\s*\{(?P<body>[^}]*)\}", HTML)
        self.assertIsNotNone(rule)
        self.assertRegex(rule.group("body"), r"repeat\(auto-(?:fit|fill)")
        self.assertRegex(rule.group("body"), r"minmax\(")

        render = javascript_function_body(
            "renderImportPostQcControls",
            "calibrationProtocolPayload",
        )
        self.assertRegex(
            render,
            r"importPostQcChannelsLabel.*style\.display.*mode\s*===\s*'signature'",
        )

    def test_saving_or_navigating_a_draft_returns_to_raw_front_stage(self):
        load = javascript_function_body("loadWindow", "updateMetrics")
        self.assertIn("!calibrationBoundariesConfirmed()", load)
        self.assertIn("state.stage = 'qc_calibration'", load)
        self.assertIn("state.timeMode = 'raw'", load)
        self.assertRegex(
            load,
            r"state\.stage\s*===\s*'local_calibration'.*calibrationBoundariesConfirmed\(\)",
        )

        save = javascript_function_body("saveProjectConfig", "previewQcAlignmentRefit")
        self.assertIn("!calibrationBoundariesConfirmed()", save)
        self.assertIn("state.stage = 'qc_calibration'", save)
        self.assertIn("state.timeMode = 'raw'", save)

        actions = javascript_function_body("contextActions", "hideLineContextMenu")
        self.assertRegex(actions, r"qc_calibration.*!calibrationBoundariesConfirmed\(\).*return \[\]")
        self.assertRegex(HTML, r"focus-event[\s\S]*!calibrationBoundariesConfirmed\(\)[\s\S]*UMAP")


def make_export_app(root: Path) -> AppData:
    layout = {
        "layout_version": 4,
        "lif_channels": [
            {
                "input_id": "g1",
                "channel": "G1",
                "identity_prior": "LSK",
                "time_axis": "green_axis",
                "detector": "green",
                "use_for_cell_annotation": True,
            },
            {
                "input_id": "g2",
                "channel": "G2",
                "identity_prior": "Lin−",
                "time_axis": "green_axis",
                "detector": "green",
                "use_for_cell_annotation": True,
            },
        ],
    }
    protocol = {
        "protocol_version": 1,
        "segments": [
            {
                "segment_id": "lsk_reference",
                "order": 1,
                "start_min": 1.0,
                "end_min": 2.0,
                "reference_channels": ["G1"],
                "population_label": "LSK",
                "boundaries_confirmed": True,
            },
            {
                "segment_id": "lin_reference",
                "order": 2,
                "start_min": 3.0,
                "end_min": 4.0,
                "reference_channels": ["G2"],
                "population_label": "Lin−",
                "boundaries_confirmed": True,
            },
        ],
    }
    manifest = {
        "project_id": "export-project",
        "project_schema_version": 3,
        "acquisition_layout": layout,
        "channel_identity_prior": {"G1": "LSK", "G2": "Lin−"},
        "calibration_protocol": protocol,
        "post_qc_strategy": {"mode": "disabled"},
        "intermediate_tables": {},
    }
    (root / "lifms_project.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    store = AnnotationStore(root / "annotation.sqlite")
    store.upsert_time_model(
        {
            "time_model_version": "tm-current",
            "status": "frozen",
            "base_model_name": "test",
            "qc_calibration_end_min": 4.0,
            "sample_valve_switch_min": 20.0,
            "annotation_start_min": 24.0,
            "local_delta_seed_window_min": 2.5,
            "ms_local_delta_sec": 0.0,
            "contains_cell_labels": False,
            "max_training_time_min": 26.5,
            "evidence_count": 2,
            "unique_match_count": 2,
            "conflict_count": 0,
            "median_abs_residual_sec": 0.0,
            "p90_abs_residual_sec": 0.0,
            "acquisition_layout_hash": acquisition_layout_hash(layout),
            "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
        },
        action="test_frozen",
    )
    event_map = pd.DataFrame(
        [
            {
                "ms_event_id": "ms-cell-1",
                "scan_id": 101,
                "scan_start_time": 24.5,
                "UMAP1": 1.0,
                "UMAP2": 2.0,
            },
            {
                "ms_event_id": "ms-cell-2",
                "scan_id": 102,
                "scan_start_time": 25.0,
                "UMAP1": 3.0,
                "UMAP2": 4.0,
            },
        ]
    )
    ms_events = pd.DataFrame(
        [
            {
                "event_id": "ms-calibration",
                "scan_id": 10,
                "time_min": 1.5,
                "tic_apex": 1000.0,
                "pc34_760_apex": 100.0,
            },
            {
                "event_id": "ms-cell-1",
                "scan_id": 101,
                "time_min": 24.5,
                "tic_apex": 2000.0,
                "pc34_760_apex": 200.0,
            },
            {
                "event_id": "ms-cell-2",
                "scan_id": 102,
                "time_min": 25.0,
                "tic_apex": 3000.0,
                "pc34_760_apex": 300.0,
            },
        ]
    )
    ms_scan = pd.DataFrame(
        [
            {"scan_id": 10, "tic": 1000.0, "pc34_760_mz_at_max_intensity": 760.58},
            {"scan_id": 101, "tic": 2000.0, "pc34_760_mz_at_max_intensity": 760.59},
            {"scan_id": 102, "tic": 3000.0, "pc34_760_mz_at_max_intensity": 760.60},
        ]
    )
    return AppData(
        project=ProjectPaths.from_args(
            project_dir=str(root),
            annotation_db=str(root / "annotation.sqlite"),
        ),
        lif_traces=pd.DataFrame(),
        lif_peaks=pd.DataFrame(),
        ms_events=ms_events,
        ms_scan=ms_scan,
        alignment={
            "model": "test",
            "axis_shifts_sec": {"green_axis": 0.0},
            "green_to_ms_shift_sec": 0.0,
            "red_to_ms_shift_sec": 0.0,
            "qc_groups": {"groups": []},
            "acquisition_layout_hash": acquisition_layout_hash(layout),
            "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
        },
        store=store,
        channel_identity_prior={
            "G1": {"identity_prior": "LSK", "identity_prior_source": "test"},
            "G2": {"identity_prior": "Lin−", "identity_prior_source": "test"},
        },
        acquisition_layout=layout,
        manifest=manifest,
        calibration_protocol=protocol,
        post_qc_strategy={"mode": "disabled"},
        cell_event_map=event_map,
        cell_event_map_info={"sha256": "map-sha", "row_count": 2},
    )


class V04CsvRegressionTest(unittest.TestCase):
    def test_main_cell_csv_excludes_front_calibration_and_numbers_every_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_export_app(Path(tmp))
            app.store.upsert_review(
                annotation_id="front-anchor",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "qc_calibration",
                    "candidate_type": "qc_calibration_segment_anchor",
                    "label": "QC",
                    "ms_event_id": "ms-calibration",
                    "ms_time_min": 1.5,
                    "lif_anchor_peak_ids": {"G1": "g1-reference"},
                    "anchor_channels": ["G1"],
                    "residual_sec": 0.1,
                },
                action="test_accept_front_anchor",
            )
            for index, (channel, event_id, peak_id, time_min) in enumerate(
                [
                    ("G1", "ms-cell-1", "g1-cell", 24.5),
                    ("G2", "ms-cell-2", "g2-cell", 25.0),
                ],
                start=1,
            ):
                app.store.upsert_review(
                    annotation_id=f"cell-{index}",
                    source="manual_created",
                    review_status="accepted",
                    payload={
                        "review_stage": "cell_annotation",
                        "candidate_type": "manual_cell_pair",
                        "label": app.cell_label_for_channel(channel),
                        "lif_channel": channel,
                        "lif_peak_id": peak_id,
                        "ms_event_id": event_id,
                        "ms_time_min": time_min,
                        "time_model_version": "tm-current",
                        "residual_sec": 0.2,
                    },
                    action="test_accept_cell",
                )

            exported = app.export_accepted_annotations_csv()
            frame = pd.read_csv(io.StringIO(exported["csv_text"]), dtype={"CellNumber": "string"})
            preserved_front_anchor = app.store.get("front-anchor")

        self.assertEqual(exported["row_count"], 2)
        self.assertEqual(preserved_front_anchor["review_status"], "accepted")
        self.assertEqual(set(frame["review_stage"]), {"cell_annotation"})
        self.assertNotIn("qc_calibration", set(frame["review_stage"]))
        self.assertFalse(frame["CellNumber"].isna().any())
        self.assertTrue(frame["CellNumber"].str.len().gt(0).all())
        self.assertTrue(frame["CellNumber"].is_unique)
        self.assertEqual(frame["CellNumber"].tolist(), ["Cell00001", "Cell00002"])
        self.assertEqual(frame["Type"].tolist(), ["LSK", "Lin−"])


if __name__ == "__main__":
    unittest.main()
