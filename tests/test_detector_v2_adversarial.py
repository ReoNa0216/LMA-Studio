"""Adversarial detector-v2 boundaries for automatic scientific paths."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import (
    AppData,
    BadRequest,
    ProjectPaths,
    accepted_qc_alignment_refit,
    automatic_lif_peak_evidence,
    candidate_id_for_group,
    lif_peak_detection_from_manifest,
    lif_peak_detection_hash,
    local_delta_evidence_pairs,
    normalize_acquisition_layout,
    normalize_post_qc_strategy,
    post_qc_candidate_id,
    qc_triplets_for_range,
    raw_file_fingerprint,
    suggest_calibration_segment_windows,
)
from scripts.v3.lif_peak_detection import adaptive_lif_peak_detection
from tests.test_calibration_protocol import lif_peak, ms_event
from tests.test_protocol_regressions import create_legacy_project
from tests.test_v04_ui_csv_regressions import make_export_app


class DetectorV2AutomaticEvidenceBoundaryTest(unittest.TestCase):
    @staticmethod
    def _candidate_resolution_app(root: Path) -> AppData:
        app = make_export_app(root)
        weak = lif_peak("G1", "g1-weak", 24.5 * 60.0)
        weak["peak_tier"] = "weak"
        core = lif_peak("G2", "g2-core", 24.5 * 60.0)
        core["peak_tier"] = "core"
        object.__setattr__(app, "lif_peaks", pd.DataFrame([weak, core]))
        events = app.ms_events.copy()
        events["time_sec"] = events["time_min"].astype(float) * 60.0
        events["pc34_760_apex"] = 20000.0
        events["nearest_event_gap_sec"] = 30.0
        events["collision_risk_high"] = False
        events["low_quality_scan_window"] = False
        object.__setattr__(app, "ms_events", events)
        return app

    def test_weak_peak_cannot_be_reconstructed_as_an_automatic_cell_candidate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self._candidate_resolution_app(Path(tmp))

            with self.assertRaisesRegex(BadRequest, "weak|candidate|peak"):
                app.payload_from_auto_candidate_id(
                    "cell:G1:g1-weak:ms-cell-1",
                    window_start_min=24.0,
                    window_end_min=25.0,
                )

    def test_weak_peak_cannot_enter_legacy_post_qc_id_reconstruction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self._candidate_resolution_app(Path(tmp))
            strategy = normalize_post_qc_strategy(
                {"mode": "signature", "reference_channels": ["G1", "G2"]},
                app.acquisition_layout,
            )
            app.store.update_project_config({"post_qc_strategy": strategy})

            with self.assertRaisesRegex(BadRequest, "weak|candidate|peak"):
                app.payload_from_auto_candidate_id(
                    "post_qc:g1-weak:g2-core:ms-cell-1",
                    window_start_min=24.0,
                    window_end_min=25.0,
                )

    def test_cell_auto_resolver_rejects_candidate_just_outside_strict_active_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self._candidate_resolution_app(Path(tmp))
            candidate_id = "cell:G2:g2-core:ms-cell-1"

            visible = app.payload_from_auto_candidate_id(
                candidate_id,
                window_start_min=24.0,
                window_end_min=25.0,
            )
            self.assertAlmostEqual(visible["lif_plot_time_min"], 24.5)
            self.assertAlmostEqual(visible["ms_plot_time_min"], 24.5)

            with self.assertRaisesRegex(BadRequest, "window|inactive"):
                app.payload_from_auto_candidate_id(
                    candidate_id,
                    window_start_min=24.51,
                    window_end_min=25.51,
                )

    def test_legacy_post_qc_auto_resolver_requires_strict_active_window(self):
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
                action="test_freeze_legacy_post_qc_resolver",
            )
            object.__setattr__(
                app,
                "lif_peaks",
                pd.concat(
                    [
                        app.lif_peaks,
                        pd.DataFrame([lif_peak("R1", "r1_late", 1200.0)]),
                    ],
                    ignore_index=True,
                ),
            )
            candidates = app.build_post_qc_candidates(19.0, 21.0, "aligned")
            self.assertEqual(len(candidates), 1)
            candidate_id = post_qc_candidate_id(candidates[0])
            self.assertEqual(candidate_id, "post_qc:g2_late:r1_late:ms_2")

            visible = app.payload_from_auto_candidate_id(
                candidate_id,
                window_start_min=20.0,
                window_end_min=20.5,
            )
            self.assertEqual(visible["candidate_type"], "qc_survey_post_10p5")

            with self.assertRaisesRegex(BadRequest, "window|inactive"):
                app.payload_from_auto_candidate_id(
                    candidate_id,
                    window_start_min=20.093333333333334,
                    window_end_min=20.583333333333332,
                )

    def test_explicit_post_qc_auto_resolver_requires_strict_active_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            from tests.test_protocol_regressions import create_hsc_app

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
                action="test_freeze_explicit_post_qc_resolver",
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
            candidate_id = post_qc_candidate_id(candidates[0])
            self.assertTrue(candidate_id.startswith("post_qc:v3:"))

            visible = app.payload_from_auto_candidate_id(
                candidate_id,
                window_start_min=23.5,
                window_end_min=24.5,
            )
            self.assertAlmostEqual(visible["g2_plot_time_min"], 24.0)
            self.assertAlmostEqual(visible["ms_plot_time_min"], 1442.0 / 60.0)

            with self.assertRaisesRegex(BadRequest, "window|inactive"):
                app.payload_from_auto_candidate_id(
                    candidate_id,
                    window_start_min=24.01,
                    window_end_min=24.5,
                )

    def test_legacy_front_auto_qc_resolver_requires_strict_active_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp), qc_end_min=12.0)
            app = AppData.load(
                ProjectPaths.from_args(
                    project_dir=str(project_dir),
                    annotation_db=str(db_path),
                )
            )
            groups = app.alignment["qc_groups"]["groups"]
            self.assertEqual(len(groups), 1)
            candidate_id = candidate_id_for_group(groups[0])
            self.assertEqual(candidate_id, "auto_qc:g2_1:r1_1:ms_1")

            visible = app.payload_from_auto_candidate_id(
                candidate_id,
                window_start_min=1.0,
                window_end_min=1.25,
            )
            self.assertEqual(visible["candidate_type"], "qc_calibration_anchor_0_10p5")
            self.assertEqual(visible["review_stage"], "qc_calibration")

            with self.assertRaisesRegex(BadRequest, "window|inactive"):
                app.payload_from_auto_candidate_id(
                    candidate_id,
                    window_start_min=19.0,
                    window_end_min=20.0,
                )

    def test_weak_tier_filter_uses_the_same_canonicalization_as_load_validation(self):
        weak = lif_peak("G1", "g1-padded-weak", 60.0)
        weak["peak_tier"] = " Weak "

        automatic = automatic_lif_peak_evidence(pd.DataFrame([weak]))

        self.assertTrue(automatic.empty)

    def test_accepted_anchor_refit_does_not_train_on_historical_weak_rows(self):
        peaks = []
        events = []
        annotations = []
        for rank, time_sec in enumerate((60.0, 120.0), start=1):
            g2 = lif_peak("G2", f"g2-weak-{rank}", time_sec)
            g2["peak_tier"] = "weak"
            r1 = lif_peak("R1", f"r1-core-{rank}", time_sec)
            r1["peak_tier"] = "core"
            peaks.extend([g2, r1])
            events.append(ms_event(f"ms-{rank}", time_sec + 5.0))
            annotations.append(
                {
                    "annotation_id": f"legacy-accepted-{rank}",
                    "review_status": "accepted",
                    "label": "QC",
                    "review_stage": "qc_calibration",
                    "candidate_type": "manual_qc_triplet",
                    "g2_peak_id": g2["peak_id"],
                    "r1_peak_id": r1["peak_id"],
                    "ms_event_id": f"ms-{rank}",
                }
            )
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

        with self.assertRaisesRegex(BadRequest, "green_axis|evidence|anchor"):
            accepted_qc_alignment_refit(
                pd.DataFrame(peaks),
                pd.DataFrame(events),
                annotations,
                acquisition_layout=layout,
                qc_calibration_end_min=3.0,
            )

    def test_legacy_g2_r1_post_qc_matcher_rejects_a_weak_anchor(self):
        weak_g2 = lif_peak("G2", "g2-weak", 20.0 * 60.0)
        weak_g2["peak_tier"] = "weak"
        core_r1 = lif_peak("R1", "r1-core", 20.0 * 60.0)
        core_r1["peak_tier"] = "core"

        groups = qc_triplets_for_range(
            pd.DataFrame([weak_g2, core_r1]),
            pd.DataFrame([ms_event("ms-qc", 20.0 * 60.0)]),
            context_start_min=19.5,
            context_end_min=20.5,
            qc_calibration_end_min=10.5,
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            ms_shift_sec=0.0,
            pair_offset_sec=0.0,
            tolerance_sec=2.0,
        )

        self.assertEqual(groups, [])

    def test_unlabeled_delta_primitive_rejects_weak_evidence(self):
        weak = lif_peak("G1", "g1-weak", 24.25 * 60.0)
        weak["peak_tier"] = "weak"

        result = local_delta_evidence_pairs(
            pd.DataFrame([weak]),
            pd.DataFrame([ms_event("ms-cell", 24.25 * 60.0)]),
            annotation_start_min=24.0,
            seed_window_min=1.0,
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            ms_delta_sec=0.0,
            channel_shifts_sec={"G1": 0.0},
        )

        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["unique_match_count"], 0)

    def test_calibration_window_suggestion_does_not_use_weak_evidence(self):
        weak = lif_peak("G1", "g1-weak-reference", 60.0)
        weak["peak_tier"] = "weak"

        suggestion = suggest_calibration_segment_windows(
            pd.DataFrame([weak]),
            [
                {
                    "segment_id": "g1-reference",
                    "order": 1,
                    "reference_channels": ["G1"],
                }
            ],
            annotation_start_min=24.0,
        )

        self.assertFalse(suggestion["can_apply_suggestions"])
        self.assertEqual(suggestion["segments"][0]["status"], "missing_evidence")


class DetectorV2HashBindingTest(unittest.TestCase):
    def test_peak_audit_columns_cannot_contradict_project_detector(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp))
            manifest_path = project_dir / "lifms_project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            peak_entry = manifest["intermediate_tables"]["lif_peaks"]
            peak_path = project_dir / peak_entry["path"]
            peaks = pd.read_parquet(peak_path)
            peaks["detector_profile"] = "contradictory-profile"
            peaks["weak_usage"] = "disabled"
            peaks.to_parquet(peak_path, index=False)
            manifest["intermediate_tables"]["lif_peaks"] = {
                "path": peak_entry["path"],
                **raw_file_fingerprint(peak_path, full_hash_limit_bytes=None),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BadRequest, "峰表|识别规则|不一致"):
                AppData.load(
                    ProjectPaths.from_args(
                        project_dir=str(project_dir),
                        annotation_db=str(db_path),
                    )
                )

    def test_legacy_adapter_rejects_unbound_v2_metadata_in_peak_table(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp))
            manifest_path = project_dir / "lifms_project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("lif_peak_detection", None)
            manifest.pop("lif_peak_detection_hash", None)
            peak_entry = manifest["intermediate_tables"]["lif_peaks"]
            peak_path = project_dir / peak_entry["path"]
            peaks = pd.read_parquet(peak_path)
            peaks["peak_tier"] = "weak"
            peaks["detector_version"] = 2
            peaks["detector_config_hash"] = lif_peak_detection_hash(
                adaptive_lif_peak_detection()
            )
            peaks.to_parquet(peak_path, index=False)
            manifest["intermediate_tables"]["lif_peaks"] = {
                "path": peak_entry["path"],
                **raw_file_fingerprint(peak_path, full_hash_limit_bytes=None),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BadRequest, "旧峰识别|重新|原项目"):
                AppData.load(
                    ProjectPaths.from_args(
                        project_dir=str(project_dir),
                        annotation_db=str(db_path),
                    )
                )

    def test_manifest_declared_detector_hash_must_match_its_config(self):
        config = adaptive_lif_peak_detection()
        manifest = {
            "lif_peak_detection": config,
            "lif_peak_detection_hash": "0" * 64,
        }

        with self.assertRaisesRegex(BadRequest, "峰识别|绑定|不一致"):
            lif_peak_detection_from_manifest(manifest)

    def test_v2_peak_parquet_rows_must_match_project_detector_hash(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp))
            manifest_path = project_dir / "lifms_project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config = adaptive_lif_peak_detection()
            expected_hash = lif_peak_detection_hash(config)
            manifest["lif_peak_detection"] = config
            manifest["lif_peak_detection_hash"] = expected_hash

            peak_entry = manifest["intermediate_tables"]["lif_peaks"]
            peak_path = project_dir / peak_entry["path"]
            peaks = pd.read_parquet(peak_path)
            peaks["peak_tier"] = "core"
            peaks["detector_version"] = 2
            peaks["detector_config_hash"] = "f" * 64
            peaks.to_parquet(peak_path, index=False)
            manifest["intermediate_tables"]["lif_peaks"] = {
                "path": peak_entry["path"],
                **raw_file_fingerprint(peak_path, full_hash_limit_bytes=None),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BadRequest, "峰表|识别规则|不一致"):
                AppData.load(
                    ProjectPaths.from_args(
                        project_dir=str(project_dir),
                        annotation_db=str(db_path),
                    )
                )

    def test_raw_and_merged_peak_rows_share_the_same_detector_binding(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp))
            manifest_path = project_dir / "lifms_project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config = adaptive_lif_peak_detection()
            expected_hash = lif_peak_detection_hash(config)
            manifest["lif_peak_detection"] = config
            manifest["lif_peak_detection_hash"] = expected_hash

            peak_entry = manifest["intermediate_tables"]["lif_peaks"]
            peak_path = project_dir / peak_entry["path"]
            merged = pd.read_parquet(peak_path)
            merged["peak_tier"] = "core"
            merged["detector_version"] = 2
            merged["detector_config_hash"] = expected_hash
            raw = merged.iloc[[0]].copy()
            raw["peak_id"] = "raw-wrong-detector-binding"
            raw["peak_stage"] = "raw"
            raw["detector_config_hash"] = "e" * 64
            pd.concat([raw, merged], ignore_index=True).to_parquet(
                peak_path, index=False
            )
            manifest["intermediate_tables"]["lif_peaks"] = {
                "path": peak_entry["path"],
                **raw_file_fingerprint(peak_path, full_hash_limit_bytes=None),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(BadRequest, "峰表|识别规则|不一致"):
                AppData.load(
                    ProjectPaths.from_args(
                        project_dir=str(project_dir),
                        annotation_db=str(db_path),
                    )
                )


if __name__ == "__main__":
    unittest.main()
