"""Contract tests for the project-owned detector-v2-only semantics.

The intended boundary is deliberately narrow:

* ``lif_peak_detection`` is a project-level, hash-bound configuration.
* Existing projects without that key or with detector v1 are rejected without
  rewriting their manifest or peak parquet.
* Detector v2 calls both ``core`` and ``weak`` peaks. Weak peaks are optional
  manual-review evidence: they do not train alignment/delta or create automatic
  candidates, but a user-confirmed manual pair remains exportable.
* Detector configuration is part of the immutable preprocessing-table binding.
  An existing project's config page is read-only; switching v1/v2 requires a
  new project or project copy and a fresh intermediate-table build.

These tests intentionally precede the implementation.
"""

from __future__ import annotations

import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from annotation_app import app as app_module
from annotation_app.app import (
    AnnotationStore,
    BadRequest,
    HTML,
    high_confidence_cell_pairs,
    read_project_manifest,
    write_project_manifest,
)
from scripts.v3 import project_protocol
from scripts.v3 import run_v3_01_lif_trace_physical_qc as lif_qc
from tests.test_v04_ui_csv_regressions import make_export_app


LEGACY_V1_CONFIG = {
    "detector_version": 1,
    "profile": "legacy_v3_fixed",
    "core": {"prominence_snr_min": 10.0},
    "weak": {"enabled": False, "prominence_snr_min": None},
    "geometry": {
        "min_distance_sec": 0.02,
        "merge_gap_sec": 0.12,
        "min_width_sec": 0.02,
        "max_width_sec": 1.0,
    },
    "weak_usage": "disabled",
}

DETECTOR_V2_CONFIG = {
    "detector_version": 2,
    "profile": "core_weak",
    "core": {"prominence_snr_min": 10.0},
    "weak": {"enabled": True, "prominence_snr_min": 3.5},
    "geometry": {
        "min_distance_sec": 0.02,
        "merge_gap_sec": 0.12,
        "min_width_sec": 0.02,
        "max_width_sec": 1.0,
    },
    "weak_usage": "manual_review_only",
}


def require_callable(testcase: unittest.TestCase, module: object, name: str):
    value = getattr(module, name, None)
    testcase.assertTrue(callable(value), f"missing recommended detector-v2 API: {name}()")
    return value


def synthetic_detector_trace() -> tuple[pd.DataFrame, dict]:
    time_sec = np.linspace(0.0, 1.0, 1001)
    strong = sum(
        15.0 * np.exp(-0.5 * ((time_sec - center) / 0.015) ** 2)
        for center in (0.15, 0.30, 0.45)
    )
    weak = 6.0 * np.exp(-0.5 * ((time_sec - 0.72) / 0.015) ** 2)
    signal = strong + weak
    trace = pd.DataFrame(
        {
            "channel": "G1",
            "label": "LSK",
            "detector": "green",
            "phase": "annotation_region",
            "time_min": time_sec / 60.0,
            "time_sec": time_sec,
            "raw": signal,
            "baseline": np.zeros_like(signal),
            "signal": signal,
        }
    )
    meta = {
        "dt_sec": float(time_sec[1] - time_sec[0]),
        "noise": 1.0,
        # Detector-v1 callers use this field. Detector v2 must derive the weak
        # and core thresholds from the project configuration and the same noise.
        "prom_threshold": 10.0,
    }
    return trace, meta


def peak_rows() -> pd.DataFrame:
    common = {
        "peak_stage": "merged",
        "raw_peak_count_merged": 1,
        "channel": "G1",
        "label": "LSK",
        "detector": "green",
        "phase": "annotation_region",
        "raw": 20.0,
        "baseline": 0.0,
        "height": 20.0,
        "prominence": 20.0,
        "snr": 25.0,
        "width_sec": 0.10,
        "area": 2.0,
        "nearest_gap_sec": 30.0,
        "close_peak_risk": False,
        "merge_risk": False,
        "detector_version": 2,
        "detector_config_hash": "detector-v2-hash",
    }
    return pd.DataFrame(
        [
            {
                **common,
                "peak_id": "g1-core",
                "parent_raw_peak_ids": "g1-core-raw",
                "peak_index": 1,
                "peak_tier": "core",
                "time_min": 24.5,
                "time_sec": 1470.0,
            },
            {
                **common,
                "peak_id": "g1-weak",
                "parent_raw_peak_ids": "g1-weak-raw",
                "peak_index": 2,
                "peak_tier": "weak",
                "time_min": 25.0,
                "time_sec": 1500.0,
            },
        ]
    )


class DetectorV2ManifestContractTest(unittest.TestCase):
    def test_legacy_manifest_is_rejected_without_migration(self):
        adapter = require_callable(self, app_module, "lif_peak_detection_from_manifest")
        legacy_manifest = {
            "project_id": "legacy-project",
            "project_schema_version": 3,
            "created_by_app_version": "lma_studio_v0.3.0",
        }
        before = copy.deepcopy(legacy_manifest)

        with self.assertRaisesRegex(BadRequest, "V3|v1|v2|重新"):
            adapter(legacy_manifest)

        self.assertEqual(legacy_manifest, before)
        self.assertNotIn("lif_peak_detection", legacy_manifest)

    def test_missing_preprocessing_protocol_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            with self.assertRaisesRegex(ValueError, "缺少预处理设置|新的空目录"):
                project_protocol.load_project_protocol(root)

            after = sorted(path.relative_to(root) for path in root.rglob("*"))

        self.assertEqual(after, before)

    def test_preprocessing_protocol_exposes_project_detector_config(self):
        payload = {
            "schema_version": 4,
            "calibration_protocol": {
                "segments": [
                    {
                        "segment_id": "reference",
                        "order": 1,
                        "start_min": 0.0,
                        "end_min": 5.0,
                        "reference_channels": ["G1"],
                        "boundaries_confirmed": True,
                    }
                ]
            },
            "post_qc_strategy": {"mode": "disabled"},
            "annotation_config": {"annotation_start_min": 24.0},
            "lif_peak_detection": app_module.normalize_lif_peak_detection(
                DETECTOR_V2_CONFIG
            ),
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            path = root / project_protocol.PROTOCOL_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")

            policy = project_protocol.load_project_protocol(root)

        self.assertEqual(
            policy["lif_peak_detection"],
            app_module.normalize_lif_peak_detection(DETECTOR_V2_CONFIG),
        )

    def test_new_manifest_round_trips_detector_v2_and_scientific_hash(self):
        normalizer = require_callable(self, app_module, "normalize_lif_peak_detection")
        hasher = require_callable(self, app_module, "lif_peak_detection_hash")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            write_project_manifest(
                project_dir=root,
                raw_input_mode="external_reference",
                raw_inputs={},
                channel_identity_prior={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
                lif_peak_detection=copy.deepcopy(DETECTOR_V2_CONFIG),
            )
            loaded = read_project_manifest(root)

        self.assertIsNotNone(loaded)
        stored = loaded["lif_peak_detection"]
        self.assertEqual(stored, normalizer(DETECTOR_V2_CONFIG))
        self.assertEqual(loaded["lif_peak_detection_hash"], hasher(stored))
        self.assertEqual(stored["detector_version"], 2)
        self.assertEqual(stored["weak_usage"], "manual_review_only")
        self.assertLess(
            float(stored["weak"]["prominence_snr_min"]),
            float(stored["core"]["prominence_snr_min"]),
        )


class DetectorV2PeakSemanticsTest(unittest.TestCase):
    def test_v2_detector_emits_auditable_core_and_weak_tiers(self):
        trace, meta = synthetic_detector_trace()
        hasher = require_callable(self, app_module, "lif_peak_detection_hash")

        raw = lif_qc.call_raw_peaks(
            trace,
            meta,
            detection_config=copy.deepcopy(DETECTOR_V2_CONFIG),
        )
        merged = lif_qc.merge_close_raw_peaks(raw)

        self.assertEqual(set(raw["peak_tier"]), {"core", "weak"})
        self.assertEqual(set(merged["peak_tier"]), {"core", "weak"})
        self.assertEqual(set(raw["detector_version"]), {2})
        self.assertEqual(set(merged["detector_version"]), {2})
        expected_hash = hasher(DETECTOR_V2_CONFIG)
        self.assertEqual(set(raw["detector_config_hash"]), {expected_hash})
        self.assertEqual(set(merged["detector_config_hash"]), {expected_hash})
        tier_by_time = {
            round(float(row.time_sec), 2): str(row.peak_tier)
            for row in merged.itertuples()
        }
        self.assertEqual(
            tier_by_time,
            {0.15: "core", 0.30: "core", 0.45: "core", 0.72: "weak"},
        )

    def test_weak_peaks_never_generate_automatic_cell_candidates(self):
        weak_lif = peak_rows().query("peak_tier == 'weak'").copy()
        core_lif = peak_rows().query("peak_tier == 'core'").copy()
        for frame in (weak_lif, core_lif):
            frame.loc[:, "time_min"] = 40.5
            frame.loc[:, "time_sec"] = 2430.0
        ms_events = pd.DataFrame(
            [
                {
                    "event_id": "ms-cell",
                    "scan_id": 10,
                    "time_min": 40.5,
                    "time_sec": 2430.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                    "pc34_760_apex": 20000.0,
                    "nearest_event_gap_sec": 10.0,
                    "collision_risk_high": False,
                    "low_quality_scan_window": False,
                }
            ]
        )

        weak_candidates = high_confidence_cell_pairs(
            weak_lif,
            ms_events,
            channel="G1",
            context_start_min=40.0,
            context_end_min=41.0,
            shift_sec=0.0,
            ms_shift_sec=0.0,
            annotation_start_min=40.0,
        )
        core_candidates = high_confidence_cell_pairs(
            core_lif,
            ms_events,
            channel="G1",
            context_start_min=40.0,
            context_end_min=41.0,
            shift_sec=0.0,
            ms_shift_sec=0.0,
            annotation_start_min=40.0,
        )

        self.assertEqual(weak_candidates, [])
        self.assertEqual([row["lif_peak_id"] for row in core_candidates], ["g1-core"])


class DetectorV2UiAndExportContractTest(unittest.TestCase):
    def prepare_app(self, root: Path):
        app = make_export_app(root)
        object.__setattr__(app, "lif_peaks", peak_rows())
        object.__setattr__(
            app,
            "lif_traces",
            pd.DataFrame(
                [
                    {
                        "channel": "G1",
                        "label": "LSK",
                        "detector": "green",
                        "phase": "annotation_region",
                        "time_min": value,
                        "time_sec": value * 60.0,
                        "raw": 1.0,
                        "baseline": 0.0,
                        "signal": 1.0,
                    }
                    for value in (24.0, 24.5, 25.0, 25.5)
                ]
            ),
        )
        scan = app.ms_scan.copy()
        scan["scan_start_time_min"] = [1.5, 24.5, 25.0]
        scan["pc34_760_max_intensity"] = [100.0, 200.0, 300.0]
        scan["qc_782_max_intensity"] = [50.0, 60.0, 70.0]
        object.__setattr__(app, "ms_scan", scan)
        return app

    def test_window_api_defaults_to_core_and_explicitly_opt_in_to_weak(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.prepare_app(Path(tmp))

            core_view = app.window(
                24.0,
                1.25,
                time_mode="raw",
                include_weak_lif_peaks=False,
            )
            all_view = app.window(
                24.0,
                1.25,
                time_mode="raw",
                include_weak_lif_peaks=True,
            )

        self.assertEqual([row["peak_id"] for row in core_view["lif_peaks"]], ["g1-core"])
        self.assertEqual(
            [row["peak_id"] for row in all_view["lif_peaks"]],
            ["g1-core", "g1-weak"],
        )
        self.assertEqual(
            [row["peak_tier"] for row in all_view["lif_peaks"]],
            ["core", "weak"],
        )
        self.assertFalse(core_view["display_options"]["include_weak_lif_peaks"])
        self.assertTrue(all_view["display_options"]["include_weak_lif_peaks"])

    def test_in_memory_peak_table_without_tier_uses_active_v2_without_mutation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.prepare_app(Path(tmp))
            legacy_table = peak_rows().query("peak_tier == 'core'").drop(
                columns=["peak_tier", "detector_version", "detector_config_hash"]
            )
            before = legacy_table.copy(deep=True)
            object.__setattr__(app, "lif_peaks", legacy_table)

            view = app.window(
                24.0,
                1.25,
                time_mode="raw",
                include_weak_lif_peaks=False,
            )
            meta = app.meta()

        self.assertEqual([row["peak_id"] for row in view["lif_peaks"]], ["g1-core"])
        self.assertEqual(view["lif_peaks"][0]["peak_tier"], "core")
        self.assertEqual(meta["lif_peak_detection"]["detector_version"], 2)
        self.assertEqual(
            meta["project_config"]["lif_peak_detection"]["profile"],
            "core_weak",
        )
        pd.testing.assert_frame_equal(legacy_table, before)

    def test_user_accepted_weak_peak_is_exported_as_manual_evidence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.prepare_app(Path(tmp))
            app.store.upsert_review(
                annotation_id="cell-core",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "cell_annotation",
                    "candidate_type": "manual_cell_pair",
                    "label": "LSK cell",
                    "lif_channel": "G1",
                    "lif_peak_id": "g1-core",
                    "lif_peak_tier": "core",
                    "lif_peak_detection_hash": "detector-v2-hash",
                    "ms_event_id": "ms-cell-1",
                    "ms_time_min": 24.5,
                    "time_model_version": "tm-current",
                    "residual_sec": 0.0,
                },
                action="test_detector_v2_export",
            )
            events = app.ms_events.copy()
            events["time_sec"] = events["time_min"] * 60.0
            events["nearest_event_gap_sec"] = 30.0
            events["collision_risk_high"] = False
            events["low_quality_scan_window"] = False
            object.__setattr__(app, "ms_events", events)

            weak_review = app.create_manual_cell_pair(
                "G1",
                "g1-weak",
                "ms-cell-2",
                window_start_min=24.0,
                window_end_min=25.25,
                time_mode="aligned",
            )

            exported = app.export_accepted_annotations_csv()
            frame = pd.read_csv(io.StringIO(exported["csv_text"]))
            preserved = app.store.get(str(weak_review["annotation_id"]))

        self.assertEqual(exported["row_count"], 2)
        self.assertEqual(
            frame.columns.tolist(),
            [
                "CellNumber",
                "scan_Id",
                "scan_start_time",
                "TIC",
                "PC(34:1)_mz",
                "PC(34:1)_intensity",
                "UMAP1",
                "UMAP2",
                "Type",
                "annotation_kind",
                "review_stage",
                "LIF_channel",
                "LIF_peak_id",
                "MS_event_id",
                "residual_sec",
                "annotation_id",
            ],
        )
        self.assertEqual(len(frame.columns), 16)
        self.assertEqual(frame["LIF_peak_id"].tolist(), ["g1-core", "g1-weak"])
        self.assertEqual(frame["Type"].tolist(), ["LSK", "LSK"])
        self.assertNotIn(
            weak_review["annotation_id"],
            {row.get("annotation_id") for row in exported["skipped"]},
        )
        self.assertEqual(preserved["review_status"], "accepted")
        self.assertEqual(preserved["lif_peak_tier"], "weak")

    def test_ui_names_detector_profile_and_weak_display_semantics(self):
        visible_markup = HTML.split("</style>", 1)[1].split("<script>", 1)[0]
        self.assertNotRegex(HTML, r'id="importLifPeakDetectorVersion"')
        self.assertRegex(HTML, r'id="importLifPeakDetectorStandard"')
        config_control = re.search(
            r'<(?P<tag>select|input|output|span)[^>]*id="cfgLifPeakStandard"[^>]*>',
            HTML,
        )
        self.assertIsNotNone(config_control)
        if config_control.group("tag") in {"select", "input"}:
            self.assertRegex(config_control.group(0), r"disabled|readonly")
        self.assertRegex(HTML, r'id="showWeakLifPeaks"')
        self.assertIn('id="cfgLifPeakDetectorDetails"', HTML)
        self.assertNotIn('id="cfgLifPeakDetectorHash"', HTML)
        self.assertIn("weakToggle.disabled = !weakAvailable", HTML)
        self.assertIn("weak-peak-hit-target", HTML)
        self.assertIn("仅在事件标注段生效", HTML)
        self.assertIn("showInteractionHint", HTML)
        self.assertIn("state.stage === 'event_annotation'", HTML)
        self.assertIn("state.manualAnnotationKind === 'cell'", HTML)
        self.assertNotIn("legacy_v3_fixed", HTML)
        self.assertNotRegex(HTML, r"detector_version\s*:\s*1")
        self.assertNotRegex(
            visible_markup,
            r">[^<]*(?:\bv1\b|\bv2\b|detector|配置 hash|检测器版本)[^<]*<",
        )
        self.assertIn("自适应双层峰识别", visible_markup)
        self.assertRegex(
            visible_markup,
            r'弱候选峰[^<]{0,180}人工[^<]{0,180}不参与自动',
        )
        self.assertRegex(visible_markup, r'弱候选峰[^<]{0,220}人工[^<]{0,220}(?:接受|确认)[^<]{0,220}导出')
        self.assertRegex(visible_markup, r'新的空目录[^<]{0,180}(?:重跑|重建)[^<]{0,180}预处理')
        self.assertIn("include_weak_lif_peaks", HTML)
        self.assertIn("lif_peak_detection", HTML)
        self.assertRegex(HTML, r"peak_tier[^\n]{0,120}weak|weak[^\n]{0,120}peak_tier")


class DetectorV2ImmutableBindingContractTest(unittest.TestCase):
    def test_existing_project_rejects_detector_change_and_preserves_all_state(self):
        defaults = {
            "qc_calibration_end_min": 5.0,
            "annotation_start_min": 24.0,
            "local_delta_seed_window_min": 2.5,
            "lif_peak_detection": copy.deepcopy(LEGACY_V1_CONFIG),
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "annotation.sqlite"
            store = AnnotationStore(db_path, default_project_config=defaults)
            store.save_qc_alignment_model(
                {
                    "model_id": "qca-before-detector-change",
                    "preview_hash": "a" * 64,
                    "axis_shifts_sec": {"green_axis": 3.0},
                }
            )
            store.upsert_time_model(
                {
                    "time_model_version": "tm-frozen-detector-v1",
                    "status": "frozen",
                    "base_model_name": "legacy-v1",
                    "qc_calibration_end_min": 5.0,
                    "sample_valve_switch_min": 20.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.0,
                    "contains_cell_labels": False,
                    "max_training_time_min": 26.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                    "lif_peak_detection_hash": "legacy-v1-hash",
                },
                action="test_freeze_detector_v1",
            )
            store.upsert_review(
                annotation_id="manual-cell-before-detector-change",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "cell_annotation",
                    "candidate_type": "manual_cell_pair",
                    "lif_channel": "G1",
                    "lif_peak_id": "legacy-peak-id",
                    "lif_peak_tier": "core",
                    "lif_peak_detection_hash": "legacy-v1-hash",
                    "ms_event_id": "ms-cell",
                    "time_model_version": "tm-frozen-detector-v1",
                },
                action="test_manual_before_detector_change",
            )

            config_before = store.project_config()
            time_model_before = store.active_time_model()
            qc_alignment_before = store.qc_alignment_model()
            annotation_before = store.get("manual-cell-before-detector-change")

            with self.assertRaisesRegex(BadRequest, r"预处理|中间表|只读|新项目|副本|detector"):
                store.update_project_config(
                    {"lif_peak_detection": copy.deepcopy(DETECTOR_V2_CONFIG)}
                )

            config_after = store.project_config()
            time_model_after = store.active_time_model()
            qc_alignment_after = store.qc_alignment_model()
            annotation_after = store.get("manual-cell-before-detector-change")

        self.assertEqual(config_after, config_before)
        self.assertEqual(time_model_after, time_model_before)
        self.assertEqual(qc_alignment_after, qc_alignment_before)
        self.assertEqual(annotation_after, annotation_before)
        self.assertEqual(annotation_after["review_status"], "accepted")
        self.assertEqual(annotation_after["time_model_version"], "tm-frozen-detector-v1")
        self.assertEqual(annotation_after["lif_peak_detection_hash"], "legacy-v1-hash")


if __name__ == "__main__":
    unittest.main()
