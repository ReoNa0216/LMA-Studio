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
        self.assertRegex(body, r"此项目.*只读|只读.*此项目")
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
        self.assertIn("绿色信号共享时间轴", HTML)
        self.assertIn("红色信号共享时间轴", HTML)
        self.assertIn("按信号颜色自动设置", HTML)
        self.assertNotIn('data-import-field="time_axis"', HTML)

    def test_hsc1_is_an_optional_collapsed_template_not_the_default_action(self):
        self.assertIn('<details id="importProjectTemplates"', HTML)
        self.assertRegex(HTML, r"<summary>[^<]*可选[^<]*实验[^<]*模板[^<]*</summary>")
        self.assertIn('id="applyHsc1Preset"', HTML)
        self.assertNotIn('<div class="preset-box">', HTML)

    def test_event_coordinate_picker_explains_required_columns_and_ignored_extras(self):
        self.assertIn("必须包含 scan_start_time、UMAP1、UMAP2", HTML)
        self.assertRegex(HTML, r"CellNumber.*batch.*其他列.*保留.*忽略")

    def test_export_copy_explains_full_roster_and_unknown_rows(self):
        self.assertIn("全部事件均导出", HTML)
        self.assertIn("未标注为 unknown", HTML)
        self.assertIn("前段 QC anchor 留在审计库", HTML)

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

    def test_import_sections_have_side_insets_and_time_fields_use_visible_labels(self):
        section_rule = re.search(r"\.import-section\s*\{(?P<body>[^}]*)\}", HTML)
        self.assertIsNotNone(section_rule)
        self.assertRegex(
            section_rule.group("body"),
            r"padding\s*:\s*\d+px\s+(?:1[4-9]|[2-9]\d)px(?:\s+\d+px)?",
        )

        body = javascript_function_body(
            "renderImportSegments",
            "renderImportScheduledQcWindows",
        )
        self.assertIn('class="protocol-time-field"', body)
        self.assertIn("开始时间 (min)", body)
        self.assertIn("结束时间 (min)", body)
        self.assertNotIn('placeholder="开始 min"', body)
        self.assertNotIn('placeholder="结束 min"', body)

    def test_protocol_row_controls_share_one_input_baseline(self):
        row_rule = re.search(r"\.protocol-row\s*\{(?P<body>[^}]*)\}", HTML)
        self.assertIsNotNone(row_rule)
        self.assertRegex(row_rule.group("body"), r"align-items\s*:\s*end")

        number_rule = re.search(
            r"\.protocol-row\s*>\s*strong:first-child\s*\{(?P<body>[^}]*)\}",
            HTML,
        )
        self.assertIsNotNone(number_rule)
        self.assertRegex(number_rule.group("body"), r"align-self\s*:\s*center")

        channels_rule = re.search(r"\.protocol-channel-options\s*\{(?P<body>[^}]*)\}", HTML)
        self.assertIsNotNone(channels_rule)
        self.assertRegex(channels_rule.group("body"), r"min-height\s*:\s*34px")
        self.assertRegex(channels_rule.group("body"), r"align-items\s*:\s*center")

    def test_project_creation_has_persistent_busy_and_elapsed_feedback(self):
        body = javascript_function_body("importProject", "openExistingProject")
        self.assertIn("button.disabled = true", body)
        self.assertIn("aria-busy", body)
        self.assertIn("setInterval", body)
        self.assertIn("已等待", body)
        self.assertIn("requestAnimationFrame", body)
        self.assertRegex(body, r"clearInterval\(")

        render = javascript_function_body(
            "renderImportSegments",
            "renderImportScheduledQcWindows",
        )
        self.assertRegex(render, r"(?s)state\.importCreating.*正在创建")

    def test_dense_peak_time_labels_default_to_adaptive_decluttering(self):
        self.assertIn('id="peakLabelMode"', HTML)
        self.assertIn('value="auto"', HTML)
        self.assertIn('value="all"', HTML)
        self.assertIn('value="hidden"', HTML)
        self.assertIn("peakLabelMode: 'auto'", HTML)
        self.assertIn("function automaticPeakLabelIds", HTML)

        draw = javascript_function_body("draw", "trackShiftSec")
        self.assertIn("automaticPeakLabelIds", draw)
        self.assertRegex(draw, r"labelIds\.has\(")
        self.assertRegex(HTML, r"悬停.*精确.*时间|精确.*时间.*悬停")

    def test_weak_peak_hit_target_selects_manual_cells_and_explains_other_stages(self):
        selection = javascript_function_body("selectManualPeak", "createManualTriplet")
        draw = javascript_function_body("draw", "trackShiftSec")

        self.assertIn("仅在事件标注段生效", selection)
        self.assertLess(
            selection.index("仅在事件标注段生效"),
            selection.index("if (!state.manualMode)"),
            "A weak peak outside Events must explain its stage gate before the manual-mode gate",
        )
        self.assertIn("showInteractionHint", selection)
        self.assertIn("该通道未启用 Cell pair", selection)
        self.assertRegex(
            selection,
            r"renderManualSelection\(\);\s*draw\(\);",
            "Selecting a weak peak must repaint its visible selected outline immediately",
        )
        self.assertIn("weak-peak-hit-target", draw)
        hit_radius = re.search(
            r"weak-peak-hit-target[\s\S]{0,500}?r:\s*(?P<radius>\d+(?:\.\d+)?)",
            draw,
        )
        self.assertIsNotNone(hit_radius)
        self.assertGreaterEqual(float(hit_radius.group("radius")), 8.0)
        self.assertRegex(draw, r"weakPeak[\s\S]{0,900}showInteractionHint")

    def test_manual_save_button_names_the_selected_relation(self):
        panels = javascript_function_body("renderStagePanels", "setConfigSaveStatus")
        self.assertRegex(
            panels,
            r"createManual['\"]?\)\.textContent\s*=\s*cellMode\s*\?\s*'Save pair'\s*:\s*'Save anchor'",
        )

    def test_cross_channel_ambiguities_are_hidden_by_default_and_grouped_on_request(self):
        self.assertIn('id="showCrossChannelConflicts"', HTML)
        self.assertIn('id="crossChannelConflictHint"', HTML)
        self.assertIn("showCrossChannelConflicts: false", HTML)
        self.assertIn("function pendingCrossChannelConflictGroups", HTML)
        self.assertIn("function visibleCellCandidates", HTML)

        candidates = javascript_function_body("candidateRows", "manualBelongsToStage")
        self.assertIn("visibleCellCandidates", candidates)
        self.assertNotIn("选择此通道", HTML)

        render = javascript_function_body("renderCandidateList", "confirmQcEvidenceInvalidation")
        self.assertIn("pendingCrossChannelConflictGroups", render)
        self.assertRegex(render, r"Use\s+\$\{[^}]*lif_channel")
        self.assertRegex(render, r"(?i)ambiguous event")

        draw = javascript_function_body("drawCellCandidates", "drawManualAnnotations")
        self.assertIn("visibleCellCandidates", draw)

        peak_visibility = javascript_function_body(
            "visibleThirdStageLifPeakIds",
            "automaticPeakLabelIds",
        )
        self.assertIn("visibleCellCandidates", peak_visibility)

    def test_candidate_actions_wrap_inside_the_sidebar(self):
        actions_rule = re.search(r"\.row-actions\s*\{(?P<body>[^}]*)\}", HTML)
        self.assertIsNotNone(actions_rule)
        self.assertRegex(actions_rule.group("body"), r"flex-wrap\s*:\s*wrap")

        buttons_rule = re.search(
            r"\.row-actions\s+button,\s*\.small-button\s*\{(?P<body>[^}]*)\}",
            HTML,
        )
        self.assertIsNotNone(buttons_rule)
        self.assertRegex(buttons_rule.group("body"), r"max-width\s*:\s*100%")
        self.assertRegex(buttons_rule.group("body"), r"overflow-wrap\s*:\s*anywhere")

    def test_review_actions_show_feedback_before_waiting_for_the_server(self):
        body = javascript_function_body("reviewCandidate", "clearManualAnnotation")
        busy_index = body.index("state.actionBusy = true")
        feedback_index = body.index("showInteractionHint", busy_index)
        request_index = body.index("await postJson('/api/review'", busy_index)
        self.assertLess(feedback_index, request_index)
        self.assertIn("Accepting", body)
        self.assertIn("Rejecting", body)

    def test_stage_switch_clears_unsaved_manual_selection(self):
        stage_handler = HTML.rsplit(
            "document.querySelectorAll('.stage-tab').forEach", 1
        )[1].split("el('showRejected')", 1)[0]
        self.assertIn("resetManualSelection();", stage_handler)
        self.assertIn("state.manualMode = false;", stage_handler)

    def test_dense_controls_use_compact_scientific_english(self):
        visible_markup = HTML.split("</style>", 1)[1].split("<script>", 1)[0]
        for label in (
            "Start",
            "Window",
            "Time",
            "Y",
            "Labels",
            "Weak peaks",
            "Show",
            "Calibration",
            "MS Δt",
            "Events / QC",
            "QC anchor",
            "Cell pair",
        ):
            self.assertRegex(visible_markup, rf">\s*{re.escape(label)}\s*<")
        for verbose in (
            "图窗起点",
            "窗口宽度 (min)",
            "峰时间标签",
            "显示弱候选峰（仅人工细胞配对）",
            "事件标注 / 质控巡检",
        ):
            self.assertNotIn(verbose, visible_markup)

    def test_local_delta_estimator_uses_a_compact_non_wrapping_label(self):
        button = re.search(r'<button[^>]*id="estimateDelta"(?P<attrs>[^>]*)>(?P<label>[^<]+)</button>', HTML)
        self.assertIsNotNone(button)
        self.assertEqual(button.group("label").strip(), "Estimate MS Δt")
        self.assertRegex(button.group("attrs"), r'title="[^"]+"')
        self.assertNotIn("用无需身份标签的峰估计 MS 时间差", HTML)

        self.assertRegex(
            HTML,
            r"\.small-button\s*\{[^}]*white-space\s*:\s*nowrap",
        )

    def test_manual_cell_selection_unlocks_all_main_window_core_lif_peaks(self):
        draw = javascript_function_body("draw", "trackShiftSec")
        self.assertIn("manualCellSelectionActive", draw)
        self.assertIn("peakInsideMainWindow", draw)
        self.assertRegex(
            draw,
            r"manualCellSelectionActive[\s\S]{0,700}peakInsideMainWindow\(p\)[\s\S]{0,700}thirdStagePeakIds\.has",
            "Manual Cell-pair mode must not be limited to peaks already present in automatic candidates",
        )

        manual_mode_handler = HTML.rsplit("el('manualMode').addEventListener", 1)[1].split(
            "el('clearManual')", 1
        )[0]
        self.assertIn("draw();", manual_mode_handler)

    def test_unmapped_ms_peaks_remain_explainable_but_not_selectable(self):
        draw = javascript_function_body("draw", "trackShiftSec")
        self.assertIn("MS 760（未在事件坐标 CSV）", draw)
        self.assertIn("不在事件坐标 CSV，不能用于 Cell pair", draw)
        self.assertRegex(
            draw,
            r"in_cell_event_map[\s\S]{0,1800}attachHover\(c\)",
            "A pale MS marker must still explain why it cannot be selected",
        )
        self.assertNotIn("MS weak", draw)

    def test_saved_boundary_cell_pair_moves_to_its_ms_owner_window(self):
        create_pair = javascript_function_body("createManualTriplet", "setAttrs")
        self.assertRegex(
            create_pair,
            r"const\s+response\s*=\s*await\s+postJson\('/api/manual-cell-pair'",
        )
        self.assertIn("focusSavedCellRelation(response.annotation)", create_pair)

        focus = javascript_function_body("focusSavedCellRelation", "createManualTriplet")
        self.assertIn("ms_plot_time_min", focus)
        self.assertIn("eventGridWindowStart", focus)
        self.assertIn("已保存；已转到包含完整关系的窗口", focus)

    def test_post_qc_modes_explain_when_each_mode_is_appropriate(self):
        visible_markup = HTML.split("</style>", 1)[1].split("<script>", 1)[0]
        for label in ("Off", "QC signature", "Scheduled windows"):
            self.assertGreaterEqual(visible_markup.count(f">{label}</option>"), 2)
        self.assertIn('id="importPostQcHint"', visible_markup)
        self.assertIn('id="cfgPostQcHint"', visible_markup)
        self.assertIn("function postQcModeHelp", HTML)
        self.assertRegex(HTML, r"QC 时间未知.*整个事件标注段")
        self.assertRegex(HTML, r"QC 时间已知.*仅在.*时间窗口")

        import_render = javascript_function_body(
            "renderImportPostQcControls",
            "calibrationProtocolPayload",
        )
        config_render = javascript_function_body(
            "renderConfigPostQcEditor",
            "renderLocalDeltaPanel",
        )
        self.assertRegex(import_render, r"importPostQcHint.*postQcModeHelp")
        self.assertRegex(config_render, r"cfgPostQcHint.*postQcModeHelp")
        self.assertNotIn('data-scheduled-field="window_id"', HTML)
        self.assertNotIn('data-cfg-scheduled-field="window_id"', HTML)
        self.assertNotIn('placeholder="窗口 ID"', HTML)
        self.assertIn("前段参考结束 (min)", visible_markup)
        self.assertNotIn("前段协议结束(min)", visible_markup)

    def test_confirming_calibration_draft_switches_to_aligned_qc_candidates(self):
        save = javascript_function_body("saveProjectConfig", "previewQcAlignmentRefit")
        self.assertIn(
            "const calibrationWasReady = calibrationBoundariesConfirmed()",
            save,
        )
        self.assertIn(
            "const calibrationBecameReady = !calibrationWasReady && calibrationBoundariesConfirmed()",
            save,
        )
        self.assertRegex(
            save,
            r"calibrationBecameReady[\s\S]{0,700}?state\.stage\s*=\s*'qc_calibration'"
            r"[\s\S]{0,300}?state\.timeMode\s*=\s*'aligned'",
        )
        self.assertIn("QC anchor 候选已生成", save)

    def test_new_project_explains_qc_and_cell_roles_are_independent(self):
        visible_markup = HTML.split("</style>", 1)[1].split("<script>", 1)[0]
        self.assertIn('id="importDualRoleHelp"', visible_markup)
        self.assertRegex(
            visible_markup,
            r"同一通道[^<]{0,100}QC anchor[^<]{0,100}Cell pair[^<]{0,100}同时",
        )

    def test_main_plot_window_width_is_user_editable_and_not_reset_by_stage(self):
        control = re.search(r'<input id="widthDisplay"[^>]*>', HTML)
        self.assertIsNotNone(control)
        self.assertIn('type="number"', control.group(0))
        self.assertIn('min="0.25"', control.group(0))
        self.assertIn('max="15"', control.group(0))
        self.assertNotIn("readonly", control.group(0))
        self.assertIn("function syncWindowWidthFromControl", HTML)
        self.assertRegex(
            HTML,
            r"(?s)el\('go'\).*?syncWindowWidthFromControl\(\)",
        )

        stage_listener_start = HTML.index("document.querySelectorAll('.stage-tab')")
        stage_listener_end = HTML.index("el('manualAnnotationKind')", stage_listener_start)
        stage_listener = HTML[stage_listener_start:stage_listener_end]
        self.assertNotIn("state.width = stageWindowWidth()", stage_listener)
        self.assertRegex(HTML, r"0\.25.*15.*min|0\.25–15 min")

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
        self.assertRegex(HTML, r"可以先创建草稿.*所有边界确认前.*时间校准")
        self.assertRegex(HTML, r"所有边界确认前.*后续阶段.*锁定")

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
