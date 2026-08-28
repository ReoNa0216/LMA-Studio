import copy
import io
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
    HTML,
    ProjectPaths,
    REQUIRED_INTERMEDIATE_TABLES,
    acquisition_layout_hash,
    accepted_qc_alignment_refit,
    build_raw_input_project_records,
    calibration_protocol_from_manifest,
    calibration_protocol_hash,
    draft_calibration_alignment,
    estimate_shift_alignment,
    normalize_acquisition_layout,
    normalize_calibration_protocol,
    normalize_post_qc_strategy,
    post_qc_strategy_from_manifest,
    raw_file_fingerprint,
    read_project_manifest,
    suggest_calibration_segment_windows,
    write_project_manifest,
)
from scripts.v3.lif_peak_detection import (
    adaptive_lif_peak_detection,
    lif_peak_detection_hash,
)


def lif_peak(channel: str, peak_id: str, time_sec: float, snr: float = 50.0) -> dict:
    return {
        "channel": channel,
        "peak_id": peak_id,
        "time_sec": float(time_sec),
        "time_min": float(time_sec) / 60.0,
        "snr": float(snr),
        "nearest_gap_sec": 10.0,
        "close_peak_risk": False,
        "merge_risk": False,
    }


def ms_event(event_id: str, time_sec: float) -> dict:
    return {
        "event_id": event_id,
        "time_sec": float(time_sec),
        "time_min": float(time_sec) / 60.0,
        "event_strategy": "pc34_primary",
        "primary_signal_col": "pc34_760_max_intensity",
        "pc34_760_apex": 20000.0,
        "qc_782_apex": 1000.0,
        "nearest_event_gap_sec": 10.0,
        "collision_risk_high": False,
        "low_quality_scan_window": False,
    }


class CalibrationProtocolSchemaTest(unittest.TestCase):
    def hsc_layout(self) -> dict:
        return normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {
                        "input_id": "lif_g1_raw",
                        "channel": "G1",
                        "identity_prior": "LSK",
                        "detector": "green",
                        "time_axis": "green_axis",
                        "use_for_cell_annotation": True,
                    },
                    {
                        "input_id": "lif_g2_raw",
                        "channel": "G2",
                        "identity_prior": "Lin-",
                        "detector": "green",
                        "time_axis": "green_axis",
                        "use_for_cell_annotation": True,
                    },
                ],
            }
        )

    def test_peak_shape_window_suggestions_are_ordered_and_never_confirmed(self):
        peaks = pd.DataFrame(
            [
                lif_peak("G1", f"g1_{index}", minute * 60.0, snr=80.0)
                for index, minute in enumerate([2.0, 2.6, 3.2, 3.8, 4.4, 5.0, 5.6, 6.2, 6.8, 7.4])
            ]
            + [
                lif_peak("G2", f"g2_{index}", minute * 60.0, snr=70.0)
                for index, minute in enumerate([13.0, 13.7, 14.4, 15.1, 15.8, 16.5, 17.2, 17.9, 18.6, 19.3])
            ]
        )
        segments = [
            {"segment_id": "lsk_reference", "order": 1, "reference_channels": ["G1"]},
            {"segment_id": "lin_reference", "order": 2, "reference_channels": ["G2"]},
        ]

        result = suggest_calibration_segment_windows(peaks, segments, annotation_start_min=24.0)

        self.assertTrue(result["can_apply_suggestions"])
        self.assertTrue(result["requires_user_confirmation"])
        self.assertEqual([row["status"] for row in result["segments"]], ["suggested", "suggested"])
        self.assertLess(result["segments"][0]["suggested_end_min"], result["segments"][1]["suggested_start_min"])
        self.assertTrue(all(row["boundaries_confirmed"] is False for row in result["segments"]))

    def test_window_suggestions_surface_missing_wrong_order_overlap_and_ambiguity(self):
        segments = [
            {"segment_id": "first", "order": 1, "reference_channels": ["G1"]},
            {"segment_id": "second", "order": 2, "reference_channels": ["G2"]},
        ]
        missing = suggest_calibration_segment_windows(
            pd.DataFrame([lif_peak("G1", "only", 2.0 * 60.0)]),
            segments,
            annotation_start_min=24.0,
        )
        self.assertEqual(missing["segments"][1]["status"], "missing_evidence")
        self.assertFalse(missing["can_apply_suggestions"])

        wrong_order = suggest_calibration_segment_windows(
            pd.DataFrame(
                [
                    lif_peak("G1", "late_a", 15.0 * 60.0),
                    lif_peak("G1", "late_b", 15.4 * 60.0),
                    lif_peak("G2", "early_a", 5.0 * 60.0),
                    lif_peak("G2", "early_b", 5.4 * 60.0),
                ]
            ),
            segments,
            annotation_start_min=24.0,
        )
        self.assertEqual({row["status"] for row in wrong_order["segments"]}, {"order_conflict"})
        self.assertFalse(wrong_order["can_apply_suggestions"])

        overlap = suggest_calibration_segment_windows(
            pd.DataFrame(
                [
                    lif_peak("G1", "g1_a", 9.8 * 60.0),
                    lif_peak("G1", "g1_b", 10.2 * 60.0),
                    lif_peak("G2", "g2_a", 10.0 * 60.0),
                    lif_peak("G2", "g2_b", 10.4 * 60.0),
                ]
            ),
            segments,
            annotation_start_min=24.0,
        )
        self.assertEqual({row["status"] for row in overlap["segments"]}, {"order_conflict"})

        ambiguous = suggest_calibration_segment_windows(
            pd.DataFrame(
                [
                    lif_peak("G1", "a1", 2.0 * 60.0, snr=50.0),
                    lif_peak("G1", "a2", 2.4 * 60.0, snr=50.0),
                    lif_peak("G1", "b1", 6.0 * 60.0, snr=50.0),
                    lif_peak("G1", "b2", 6.4 * 60.0, snr=50.0),
                ]
            ),
            [segments[0]],
            annotation_start_min=24.0,
        )
        self.assertEqual(ambiguous["segments"][0]["status"], "ambiguous")
        self.assertGreaterEqual(ambiguous["segments"][0]["alternative_count"], 2)
        self.assertFalse(ambiguous["segments"][0]["boundaries_confirmed"])

    def hsc_protocol(self) -> dict:
        return {
            "protocol_version": 1,
            "segments": [
                {
                    "segment_id": "lsk_reference",
                    "order": 1,
                    "start_min": 0.0,
                    "end_min": 2.0,
                    "reference_channels": ["G1"],
                    "reference_mode": "green_only",
                    "population_label": "LSK",
                    "boundaries_confirmed": True,
                },
                {
                    "segment_id": "lin_reference",
                    "order": 2,
                    "start_min": 3.0,
                    "end_min": 5.0,
                    "reference_channels": ["G2"],
                    "reference_mode": "green_only",
                    "population_label": "Lin-",
                    "boundaries_confirmed": True,
                },
            ],
        }

    def make_hsc_app(
        self,
        root: Path,
        lif: pd.DataFrame,
        ms: pd.DataFrame,
        *,
        strategy: dict,
        frozen: bool = True,
    ) -> AppData:
        layout = self.hsc_layout()
        protocol = normalize_calibration_protocol(self.hsc_protocol(), layout)
        normalized_strategy = normalize_post_qc_strategy(strategy, layout)
        store = AnnotationStore(
            root / "annotation.sqlite",
            default_project_config={
                "qc_calibration_end_min": 5.0,
                "annotation_start_min": 24.0,
                "local_delta_seed_window_min": 2.5,
                "calibration_protocol": protocol,
                "post_qc_strategy": normalized_strategy,
            },
        )
        if frozen:
            store.upsert_time_model(
                {
                    "time_model_version": "tm_hsc_current",
                    "status": "frozen",
                    "base_model_name": "segmented",
                    "qc_calibration_end_min": 5.0,
                    "sample_valve_switch_min": 20.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 0.0,
                    "contains_cell_labels": False,
                    "max_training_time_min": 26.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                    "acquisition_layout_hash": acquisition_layout_hash(layout),
                    "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
                },
                action="test_hsc_frozen",
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
            post_qc_strategy=normalized_strategy,
        )

    def test_green_only_sequential_protocol_shares_one_physical_axis(self):
        layout = self.hsc_layout()
        protocol = normalize_calibration_protocol(self.hsc_protocol(), layout)

        self.assertEqual(layout["channel_time_axes"], {"G1": "green_axis", "G2": "green_axis"})
        self.assertEqual(protocol["calibration_time_axes"], ["green_axis"])
        self.assertEqual(protocol["reference_channels"], ["G1", "G2"])
        self.assertEqual([row["segment_id"] for row in protocol["segments"]], ["lsk_reference", "lin_reference"])
        self.assertNotEqual(calibration_protocol_hash(protocol, layout), acquisition_layout_hash(layout))

    def test_red_only_and_red_green_protocols_are_valid(self):
        red_layout = normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {"channel": "R1", "detector": "red", "time_axis": "red_axis", "use_for_cell_annotation": True},
                    {"channel": "R2", "detector": "red", "time_axis": "red_axis", "use_for_cell_annotation": True},
                ],
            }
        )
        red_protocol = normalize_calibration_protocol(
            {
                "segments": [
                    {
                        "segment_id": "red_reference",
                        "order": 1,
                        "start_min": 0,
                        "end_min": 2,
                        "reference_channels": ["R1"],
                        "reference_mode": "red_only",
                        "boundaries_confirmed": True,
                    }
                ]
            },
            red_layout,
        )
        self.assertEqual(red_protocol["calibration_time_axes"], ["red_axis"])

        mixed_layout = normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {"channel": "G1", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                    {"channel": "R1", "detector": "red", "time_axis": "red_axis", "use_for_cell_annotation": True},
                ],
            }
        )
        mixed_protocol = normalize_calibration_protocol(
            {
                "segments": [
                    {
                        "segment_id": "mixed_reference",
                        "order": 1,
                        "start_min": 0,
                        "end_min": 2,
                        "reference_channels": ["G1", "R1"],
                        "reference_mode": "red_green",
                        "boundaries_confirmed": True,
                    }
                ]
            },
            mixed_layout,
        )
        self.assertEqual(mixed_protocol["calibration_time_axes"], ["green_axis", "red_axis"])

    def test_protocol_rejects_wrong_order_overlap_unconfirmed_and_missing_axis(self):
        layout = self.hsc_layout()
        cases = []

        wrong_order = self.hsc_protocol()
        wrong_order["segments"][0]["order"] = 2
        wrong_order["segments"][1]["order"] = 1
        cases.append((wrong_order, "顺序"))

        overlap = self.hsc_protocol()
        overlap["segments"][1]["start_min"] = 1.5
        cases.append((overlap, "重叠"))

        unconfirmed = self.hsc_protocol()
        unconfirmed["segments"][0]["boundaries_confirmed"] = False
        cases.append((unconfirmed, "确认"))

        for protocol, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(BadRequest, message):
                normalize_calibration_protocol(protocol, layout)

        mixed_layout = normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {"channel": "G1", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                    {"channel": "R1", "detector": "red", "time_axis": "red_axis", "use_for_cell_annotation": True},
                ],
            }
        )
        with self.assertRaisesRegex(BadRequest, "red_axis"):
            normalize_calibration_protocol(
                {
                    "segments": [
                        {
                            "segment_id": "green_only",
                            "order": 1,
                            "start_min": 0,
                            "end_min": 2,
                            "reference_channels": ["G1"],
                            "reference_mode": "green_only",
                            "boundaries_confirmed": True,
                        }
                    ]
                },
                mixed_layout,
            )

    def test_unconfirmed_protocol_is_persistable_as_project_draft_but_not_usable_for_alignment(self):
        layout = self.hsc_layout()
        draft_input = self.hsc_protocol()
        for segment in draft_input["segments"]:
            segment["boundaries_confirmed"] = False

        draft = normalize_calibration_protocol(
            draft_input,
            layout,
            require_confirmed=False,
        )
        self.assertFalse(draft["boundaries_confirmed"])

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manifest = write_project_manifest(
                project_dir=root,
                raw_input_mode="external_reference",
                raw_inputs={},
                channel_identity_prior={"G1": "LSK", "G2": "Lin-"},
                acquisition_layout=layout,
                calibration_protocol=draft,
                post_qc_strategy={"mode": "disabled"},
                annotation_config={
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                },
            )
            loaded = calibration_protocol_from_manifest(manifest, {})

        self.assertFalse(loaded["boundaries_confirmed"])
        with self.assertRaisesRegex(BadRequest, "确认"):
            estimate_shift_alignment(
                pd.DataFrame(),
                pd.DataFrame(),
                acquisition_layout=layout,
                calibration_protocol=loaded,
            )

    def test_unconfirmed_draft_cannot_create_or_freeze_downstream_time_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            app = self.make_hsc_app(
                root,
                pd.DataFrame(),
                pd.DataFrame(),
                strategy={"mode": "disabled"},
                frozen=False,
            )
            draft_input = self.hsc_protocol()
            for segment in draft_input["segments"]:
                segment["boundaries_confirmed"] = False
            draft = normalize_calibration_protocol(
                draft_input,
                self.hsc_layout(),
                require_confirmed=False,
            )
            app.store.update_project_config({"calibration_protocol": draft})
            object.__setattr__(app, "calibration_protocol", draft)
            object.__setattr__(
                app,
                "alignment",
                draft_calibration_alignment(
                    acquisition_layout=self.hsc_layout(),
                    calibration_protocol=draft,
                ),
            )

            actions = (
                lambda: app.local_delta_preview(0.0),
                app.estimate_local_delta_preview,
                lambda: app.update_local_delta_draft(0.0),
                app.freeze_local_delta_model,
            )
            for action in actions:
                with self.subTest(action=action), self.assertRaisesRegex(BadRequest, "确认"):
                    action()
                self.assertIsNone(app.store.active_time_model())

            app.store.upsert_review(
                annotation_id="manual_front_history",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "qc_calibration",
                    "candidate_type": "manual_qc_anchor_partial",
                },
                action="test_seed_manual_front_history",
            )
            with self.assertRaisesRegex(BadRequest, "确认"):
                app.review_annotation("manual_front_history", "rejected")
            with self.assertRaisesRegex(BadRequest, "确认"):
                app.clear_manual_annotation("manual_front_history")
            self.assertIsNotNone(app.store.get("manual_front_history"))

            with self.assertRaisesRegex(BadRequest, "确认"):
                app.create_manual_triplet(None, None, "missing", stage="qc_calibration")

    def test_v03_manifest_adapts_front_and_post_qc_without_mutation(self):
        manifest = {
            "project_schema_version": 2,
            "acquisition_layout": {
                "layout_version": 3,
                "lif_channels": [
                    {"channel": "G2", "time_axis": "green_axis", "detector": "green", "use_for_cell_annotation": True},
                    {"channel": "R1", "time_axis": "red_axis", "detector": "red", "use_for_cell_annotation": True},
                    {"channel": "R2", "time_axis": "red_axis", "detector": "red", "use_for_cell_annotation": True},
                ],
                "qc_anchor_channels": ["G2", "R1"],
            },
        }
        before = copy.deepcopy(manifest)
        config = {"qc_calibration_end_min": 10.5}

        protocol = calibration_protocol_from_manifest(manifest, config)
        strategy = post_qc_strategy_from_manifest(manifest, config)

        self.assertEqual(manifest, before)
        self.assertEqual(protocol["compatibility_mode"], "v0.3_qc_anchor_channels")
        self.assertEqual(protocol["segments"][0]["reference_channels"], ["G2", "R1"])
        self.assertEqual(protocol["segments"][0]["end_min"], 10.5)
        self.assertEqual(strategy["mode"], "signature")
        self.assertEqual(strategy["reference_channels"], ["G2", "R1"])

    def test_v03_app_load_uses_existing_qc_end_without_persisting_compatibility_projection(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            paths = {key: root / value for key, value in REQUIRED_INTERMEDIATE_TABLES.items()}
            for path in paths.values():
                path.parent.mkdir(parents=True, exist_ok=True)
            channels = ["G2", "R1", "R2"]
            pd.DataFrame(
                [
                    {
                        "channel": channel,
                        "time_min": 1.0,
                        "time_sec": 60.0,
                        "raw": 1.0,
                        "baseline": 0.0,
                        "signal": 1.0,
                        "signal_pos": 1.0,
                        "snr_trace": 10.0,
                    }
                    for channel in channels
                ]
            ).to_parquet(paths["lif_traces"], index=False)
            detector_config = adaptive_lif_peak_detection()
            detector_hash = lif_peak_detection_hash(detector_config)
            pd.DataFrame(
                [
                    {
                        **lif_peak(channel, f"{channel.lower()}-1", 60.0),
                        "peak_stage": "merged",
                        "peak_tier": "core",
                        "detector_version": 2,
                        "detector_config_hash": detector_hash,
                    }
                    for channel in channels
                ]
            ).to_parquet(paths["lif_peaks"], index=False)
            pd.DataFrame([ms_event("ms-1", 60.0)]).to_parquet(paths["ms_events"], index=False)
            pd.DataFrame(
                [
                    {
                        "scan_id": "scan-1",
                        "scan_start_time_min": 1.0,
                        "scan_start_time_sec": 60.0,
                        "pc34_760_max_intensity": 20000.0,
                        "qc_782_max_intensity": 1000.0,
                        "tic": 50000.0,
                    }
                ]
            ).to_parquet(paths["ms_scan_summary"], index=False)
            manifest = {
                "project_schema_version": 2,
                "acquisition_layout": {
                    "layout_version": 3,
                    "lif_channels": [
                        {
                            "input_id": f"lif_{channel.lower()}_raw",
                            "channel": channel,
                            "identity_prior": channel,
                            "time_axis": "green_axis" if channel == "G2" else "red_axis",
                            "detector": "green" if channel == "G2" else "red",
                            "use_for_cell_annotation": True,
                        }
                        for channel in channels
                    ],
                    "qc_anchor_channels": ["G2", "R1"],
                },
                "channel_identity_prior": {channel: channel for channel in channels},
                "lif_peak_detection": detector_config,
                "lif_peak_detection_hash": detector_hash,
                "intermediate_tables": {
                    key: {
                        "path": path.relative_to(root).as_posix(),
                        **raw_file_fingerprint(path, full_hash_limit_bytes=None),
                    }
                    for key, path in paths.items()
                },
                "annotation_db": {
                    "path": "annotation_app/annotations/annotation.sqlite",
                    "schema_version": 2,
                },
            }
            (root / "lifms_project.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            db_path = root / "annotation_app/annotations/annotation.sqlite"
            legacy_store = AnnotationStore(db_path)
            legacy_store.update_project_config(
                {"qc_calibration_end_min": 12.0, "annotation_start_min": 40.0}
            )

            app = AppData.load(ProjectPaths.from_args(project_dir=str(root)))
            with sqlite3.connect(db_path) as conn:
                stored_keys = {
                    str(row[0])
                    for row in conn.execute("SELECT key FROM project_config").fetchall()
                }

            self.assertEqual(app.calibration_protocol["segments"][0]["end_min"], 12.0)
            self.assertEqual(app.post_qc_strategy["compatibility_mode"], "v0.3_qc_anchor_channels")
            self.assertNotIn("calibration_protocol", stored_keys)
            self.assertNotIn("post_qc_strategy", stored_keys)

    def test_post_qc_modes_are_independent_of_front_reference_channels(self):
        layout = self.hsc_layout()
        disabled = normalize_post_qc_strategy({"mode": "disabled"}, layout)
        self.assertEqual(disabled["mode"], "disabled")
        self.assertEqual(disabled["reference_channels"], [])

        signature = normalize_post_qc_strategy(
            {"mode": "signature", "reference_channels": ["G2"]},
            layout,
        )
        self.assertEqual(signature["reference_channels"], ["G2"])

        scheduled = normalize_post_qc_strategy(
            {
                "mode": "scheduled_windows",
                "windows": [
                    {
                        "window_id": "late_qc",
                        "start_min": 20,
                        "end_min": 21,
                        "reference_channels": ["G1"],
                    }
                ],
            },
            layout,
        )
        self.assertEqual(scheduled["windows"][0]["reference_channels"], ["G1"])

    def test_new_manifest_persists_protocol_strategy_and_project_specific_start(self):
        layout = self.hsc_layout()
        protocol = self.hsc_protocol()
        strategy = {"mode": "disabled"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            write_project_manifest(
                project_dir=Path(tmp),
                raw_input_mode="external_reference",
                raw_inputs={},
                channel_identity_prior={"G1": "LSK", "G2": "Lin-"},
                acquisition_layout=layout,
                calibration_protocol=protocol,
                post_qc_strategy=strategy,
                annotation_config={
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                },
            )
            manifest = read_project_manifest(Path(tmp))

        self.assertEqual(manifest["project_schema_version"], 3)
        self.assertEqual(manifest["acquisition_layout"]["layout_version"], 4)
        self.assertEqual(manifest["calibration_protocol"]["segments"][0]["reference_channels"], ["G1"])
        self.assertEqual(manifest["post_qc_strategy"]["mode"], "disabled")
        self.assertEqual(manifest["annotation_config"]["annotation_start_min"], 24.0)

    def test_hsc_raw_input_records_do_not_require_legacy_red_qc_channel(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            lif_inputs = [
                {"key": "lif_g1", "path": raw / "G1.CSV", "channel": "G1", "identity_prior": "LSK", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                {"key": "lif_g2", "path": raw / "G2.CSV", "channel": "G2", "identity_prior": "Lin-", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
            ]
            rows, _inputs, layout = build_raw_input_project_records(
                project_dir=root / "project",
                raw_paths={"ms": raw / "MS.txt"},
                raw_input_mode="external_reference",
                identities={"G1": "LSK", "G2": "Lin-"},
                lif_inputs=lif_inputs,
                calibration_protocol=self.hsc_protocol(),
            )

        self.assertEqual([row["channel"] for row in rows[:-1]], ["G1", "G2"])
        self.assertEqual(layout["qc_anchor_channels"], [])

    def test_protocol_change_invalidates_models_but_preserves_manual_annotations(self):
        layout = self.hsc_layout()
        protocol = normalize_calibration_protocol(self.hsc_protocol(), layout)
        defaults = {
            "qc_calibration_end_min": 5.0,
            "annotation_start_min": 24.0,
            "local_delta_seed_window_min": 2.5,
            "calibration_protocol": protocol,
            "post_qc_strategy": {"mode": "disabled"},
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(
                Path(tmp) / "annotation.sqlite",
                default_project_config=defaults,
            )
            store.save_qc_alignment_model(
                {
                    "model_id": "qca_hsc",
                    "preview_hash": "a" * 64,
                    "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
                    "axis_shifts_sec": {"green_axis": 5.0},
                }
            )
            store.upsert_time_model(
                {
                    "time_model_version": "tm_hsc_frozen",
                    "status": "frozen",
                    "base_model_name": "segmented",
                    "qc_calibration_end_min": 5.0,
                    "sample_valve_switch_min": 20.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.5,
                    "contains_cell_labels": False,
                    "max_training_time_min": 26.5,
                    "evidence_count": 4,
                    "unique_match_count": 4,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                    "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
                },
                action="test_freeze",
            )
            store.upsert_review(
                annotation_id="manual_cell_g1",
                source="manual_created",
                review_status="accepted",
                payload={
                    "candidate_type": "manual_cell_pair",
                    "lif_channel": "G1",
                    "lif_peak_id": "g1_cell",
                    "ms_event_id": "ms_cell",
                    "time_model_version": "tm_hsc_frozen",
                    "label": "LSK cell",
                },
                action="test_manual_cell",
            )
            changed = copy.deepcopy(protocol)
            changed["segments"][1]["end_min"] = 5.5

            with self.assertRaisesRegex(BadRequest, "冻结"):
                store.update_project_config({"calibration_protocol": changed})
            with self.assertRaisesRegex(BadRequest, "QC 对齐"):
                store.update_project_config(
                    {"calibration_protocol": changed},
                    clear_frozen_time_model=True,
                )

            saved = store.update_project_config(
                {"calibration_protocol": changed, "qc_calibration_end_min": 5.5},
                clear_frozen_time_model=True,
                clear_qc_alignment_model=True,
            )

            self.assertEqual(saved["qc_calibration_end_min"], 5.5)
            self.assertIsNone(store.active_time_model())
            self.assertIsNone(store.qc_alignment_model())
            preserved = store.get("manual_cell_g1")
            self.assertEqual(preserved["review_status"], "accepted")
            self.assertEqual(preserved["time_model_version"], "tm_hsc_frozen")

    def test_post_qc_strategy_change_is_independent_of_frozen_time_model(self):
        layout = self.hsc_layout()
        protocol = normalize_calibration_protocol(self.hsc_protocol(), layout)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(
                Path(tmp) / "annotation.sqlite",
                default_project_config={
                    "qc_calibration_end_min": 5.0,
                    "annotation_start_min": 24.0,
                    "calibration_protocol": protocol,
                    "post_qc_strategy": {"mode": "disabled"},
                },
            )
            store.upsert_time_model(
                {
                    "time_model_version": "tm_hsc_frozen",
                    "status": "frozen",
                    "base_model_name": "segmented",
                    "qc_calibration_end_min": 5.0,
                    "sample_valve_switch_min": 20.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 1.5,
                    "contains_cell_labels": False,
                    "max_training_time_min": 26.5,
                    "evidence_count": 4,
                    "unique_match_count": 4,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                    "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
                },
                action="test_freeze",
            )

            store.update_project_config(
                {"post_qc_strategy": {"mode": "signature", "reference_channels": ["G2"]}}
            )

            self.assertEqual(store.active_time_model()["time_model_version"], "tm_hsc_frozen")
            self.assertEqual(store.project_config()["post_qc_strategy"]["mode"], "signature")

    def test_time_model_is_bound_to_calibration_protocol_hash(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            draft = store.ensure_draft_time_model(
                "segmented",
                "layout_hash",
                calibration_protocol_hash_value="protocol_hash",
                allow_unhashed_legacy_binding=False,
            )

            self.assertEqual(draft["acquisition_layout_hash"], "layout_hash")
            self.assertEqual(draft["calibration_protocol_hash"], "protocol_hash")

    def test_preserved_old_cell_labels_do_not_make_recomputed_model_exploratory(self):
        layout = self.hsc_layout()
        protocol = normalize_calibration_protocol(self.hsc_protocol(), layout)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(
                Path(tmp) / "annotation.sqlite",
                default_project_config={
                    "qc_calibration_end_min": 5.0,
                    "annotation_start_min": 24.0,
                    "calibration_protocol": protocol,
                    "post_qc_strategy": {"mode": "disabled"},
                },
            )
            store.upsert_review(
                annotation_id="old_cell",
                source="auto_candidate",
                review_status="accepted",
                payload={
                    "candidate_type": "cell_high_confidence",
                    "lif_channel": "G1",
                    "lif_peak_id": "g1_old",
                    "ms_event_id": "ms_old",
                    "time_model_version": "tm_old",
                },
                action="test_old_cell",
            )
            store.upsert_time_model(
                {
                    "time_model_version": "tm_recomputed",
                    "status": "draft",
                    "base_model_name": "segmented",
                    "qc_calibration_end_min": 5.0,
                    "sample_valve_switch_min": 20.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 0.5,
                    "contains_cell_labels": False,
                    "max_training_time_min": 26.5,
                    "evidence_count": 3,
                    "unique_match_count": 3,
                    "conflict_count": 0,
                    "median_abs_residual_sec": 0.1,
                    "p90_abs_residual_sec": 0.2,
                    "calibration_protocol_hash": calibration_protocol_hash(protocol, layout),
                },
                action="test_recomputed",
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
                lif_traces=pd.DataFrame(),
                lif_peaks=pd.DataFrame(),
                ms_events=pd.DataFrame(),
                ms_scan=pd.DataFrame(),
                alignment={
                    "model": "segmented",
                    "green_to_ms_shift_sec": 0.0,
                    "red_to_ms_shift_sec": 0.0,
                    "acquisition_layout_hash": acquisition_layout_hash(layout),
                    "qc_groups": {"groups": []},
                },
                store=store,
                channel_identity_prior={},
                acquisition_layout=layout,
                calibration_protocol=protocol,
                post_qc_strategy={"mode": "disabled"},
            )

            frozen = app.freeze_local_delta_model()

            self.assertEqual(frozen["status"], "frozen")
            self.assertEqual(store.get("old_cell")["review_status"], "accepted")

    def test_new_protocol_local_delta_uses_generic_unlabeled_topology(self):
        layout = self.hsc_layout()
        protocol = normalize_calibration_protocol(self.hsc_protocol(), layout)
        lif = pd.DataFrame(
            [
                lif_peak("G1", "g1_late", 1440.0),
                lif_peak("G2", "g2_late", 1500.0),
            ]
        )
        ms = pd.DataFrame(
            [
                ms_event("ms_late_1", 1447.0),
                ms_event("ms_late_2", 1507.0),
            ]
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = AnnotationStore(
                Path(tmp) / "annotation.sqlite",
                default_project_config={
                    "qc_calibration_end_min": 5.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "calibration_protocol": protocol,
                    "post_qc_strategy": {"mode": "disabled"},
                },
            )
            app = AppData(
                project=ProjectPaths.from_args(project_dir=tmp),
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
                    "G1": {"identity_prior": "deliberately-wrong-label"},
                    "G2": {"identity_prior": "another-wrong-label"},
                },
                acquisition_layout=layout,
                calibration_protocol=protocol,
                post_qc_strategy={"mode": "disabled"},
            )

            preview = app.estimate_local_delta_preview()

            self.assertEqual(preview["method"], "unlabeled_seed_window_shift_only_grid_search")
            self.assertAlmostEqual(preview["delta_sec"], -2.0)
            self.assertEqual(preview["unique_match_count"], 2)
            self.assertTrue(all("label" not in row for row in preview["evidence"]))

    def test_post_qc_disabled_emits_no_candidates_even_with_frozen_model(self):
        lif = pd.DataFrame([lif_peak("G2", "g2_late", 1320.0)])
        ms = pd.DataFrame([ms_event("ms_late", 1325.0)])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(Path(tmp), lif, ms, strategy={"mode": "disabled"})

            candidates = app.build_post_qc_candidates(20.0, 24.0, "aligned")

            self.assertEqual(candidates, [])

    def test_scheduled_post_qc_only_matches_declared_windows_and_channels(self):
        lif = pd.DataFrame(
            [
                lif_peak("G1", "g1_scheduled", 20.5 * 60.0 - 5.0),
                lif_peak("G2", "g2_outside", 22.0 * 60.0 - 5.0),
            ]
        )
        ms = pd.DataFrame(
            [
                ms_event("ms_scheduled", 20.5 * 60.0),
                ms_event("ms_outside", 22.0 * 60.0),
            ]
        )
        strategy = {
            "mode": "scheduled_windows",
            "windows": [
                {
                    "window_id": "late_qc_1",
                    "start_min": 20.0,
                    "end_min": 21.0,
                    "reference_channels": ["G1"],
                }
            ],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(Path(tmp), lif, ms, strategy=strategy)

            candidates = app.build_post_qc_candidates(19.0, 23.0, "aligned")

            self.assertEqual([row["ms_event_id"] for row in candidates], ["ms_scheduled"])
            self.assertEqual(candidates[0]["post_qc_window_id"], "late_qc_1")
            self.assertEqual(candidates[0]["anchor_channels"], ["G1"])
            self.assertEqual(candidates[0]["candidate_type"], "qc_survey_scheduled_windows")

    def test_signature_post_qc_is_independent_from_front_reference_segments(self):
        lif = pd.DataFrame(
            [
                lif_peak("G1", "g1_late", 22.0 * 60.0 - 5.0),
                lif_peak("G2", "g2_late", 23.0 * 60.0 - 5.0),
            ]
        )
        ms = pd.DataFrame(
            [
                ms_event("ms_g1", 22.0 * 60.0),
                ms_event("ms_g2", 23.0 * 60.0),
            ]
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(
                Path(tmp),
                lif,
                ms,
                strategy={"mode": "signature", "reference_channels": ["G2"]},
            )

            candidates = app.build_post_qc_candidates(21.0, 24.0, "aligned")

            self.assertEqual([row["ms_event_id"] for row in candidates], ["ms_g2"])
            self.assertEqual(candidates[0]["anchor_channels"], ["G2"])
            self.assertEqual(candidates[0]["candidate_type"], "qc_survey_signature")

    def test_scheduled_qc_before_annotation_start_is_still_qc_survey(self):
        lif = pd.DataFrame([lif_peak("G1", "g1_qc", 20.5 * 60.0 - 5.0)])
        ms = pd.DataFrame([ms_event("ms_qc", 20.5 * 60.0)])
        strategy = {
            "mode": "scheduled_windows",
            "windows": [
                {
                    "window_id": "pre_annotation_qc",
                    "start_min": 20.0,
                    "end_min": 21.0,
                    "reference_channels": ["G1"],
                }
            ],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(Path(tmp), lif, ms, strategy=strategy)
            candidate = app.build_post_qc_candidates(20.0, 21.0, "aligned")[0]
            payload = app.payload_from_post_qc_group(candidate)
            stored = app.store.upsert_review(
                annotation_id=candidate["annotation_id"],
                source="auto_candidate",
                review_status="accepted",
                payload=payload,
                action="test_accept_scheduled_qc",
            )

            self.assertEqual(payload["candidate_type"], "qc_survey_scheduled_windows")
            self.assertEqual(payload["post_qc_window_id"], "pre_annotation_qc")
            self.assertTrue(app.is_qc_survey_annotation(stored))
            self.assertEqual(app.accepted_qc_survey_ms_event_ids(), {"ms_qc"})

    def test_post_qc_strategy_change_preserves_reviewed_relation_and_export(self):
        lif = pd.DataFrame([lif_peak("G2", "g2_qc", 25.0 * 60.0 - 5.0)])
        ms = pd.DataFrame([ms_event("ms_qc", 25.0 * 60.0)])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(
                Path(tmp),
                lif,
                ms,
                strategy={"mode": "signature", "reference_channels": ["G2"]},
            )
            candidate = app.build_post_qc_candidates(24.0, 26.0, "aligned")[0]
            payload = app.payload_from_post_qc_group(candidate)
            accepted = app.store.upsert_review(
                annotation_id=candidate["annotation_id"],
                source="auto_candidate",
                review_status="accepted",
                payload=payload,
                action="test_accept_signature_qc",
            )
            self.assertTrue(app.is_qc_survey_annotation(accepted))

            app.store.update_project_config(
                {"post_qc_strategy": normalize_post_qc_strategy({"mode": "disabled"}, app.acquisition_layout)}
            )

            preserved = app.store.get(candidate["annotation_id"])
            self.assertEqual(preserved["review_status"], "accepted")
            self.assertTrue(app.is_qc_survey_annotation(preserved))
            exported = app.export_accepted_annotations_csv()
            self.assertEqual(exported["row_count"], 1)
            self.assertEqual(exported["skipped"], [])

    def test_same_ms_cross_channel_cell_candidates_require_explicit_arbitration(self):
        lif = pd.DataFrame(
            [
                lif_peak("G1", "g1_cell", 24.5 * 60.0 - 5.0),
                lif_peak("G2", "g2_cell", 24.5 * 60.0 - 4.8),
            ]
        )
        ms = pd.DataFrame([ms_event("ms_cell", 24.5 * 60.0)])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(Path(tmp), lif, ms, strategy={"mode": "disabled"})

            candidates = app.build_cell_candidates(24.0, 25.0, "aligned")

            self.assertEqual(len(candidates), 2)
            self.assertEqual({row["lif_channel"] for row in candidates}, {"G1", "G2"})
            self.assertTrue(all(row["cross_channel_candidate_conflict"] for row in candidates))
            self.assertTrue(all(row["arbitration_status"] == "manual_required" for row in candidates))
            self.assertTrue(all(row["candidate_type"] == "cell_cross_channel_ambiguous" for row in candidates))

            chosen = candidates[0]
            chosen_payload = app.payload_from_auto_candidate_id(
                chosen["annotation_id"],
                window_start_min=24.0,
                window_end_min=25.0,
            )
            self.assertEqual(chosen_payload["candidate_type"], "cell_cross_channel_ambiguous")
            self.assertEqual(chosen_payload["arbitration_status"], "manual_required")
            self.assertEqual(len(chosen_payload["cross_channel_alternatives"]), 2)
            app.store.upsert_review(
                annotation_id=chosen["annotation_id"],
                source="auto_candidate",
                review_status="accepted",
                payload=chosen_payload,
                action="test_choose_channel",
            )
            exported = app.export_accepted_annotations_csv()
            exported_cells = pd.read_csv(io.StringIO(exported["csv_text"]))
            self.assertEqual(exported["row_count"], 1)
            self.assertEqual(
                exported_cells.columns.tolist(),
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
            self.assertEqual(exported_cells.loc[0, "MS_event_id"], "ms_cell")
            self.assertIn(exported_cells.loc[0, "Type"], {"LSK", "Lin-"})
            self.assertEqual(exported_cells.loc[0, "annotation_kind"], "cell_pair")
            self.assertNotIn("post_qc_strategy_hash", exported_cells.columns)
            self.assertNotIn("cross_channel_alternatives_json", exported_cells.columns)
            other = candidates[1]
            with self.assertRaisesRegex(BadRequest, "同一 MS event|冲突"):
                app.ensure_third_stage_acceptance_allowed(
                    other,
                    annotation_id=other["annotation_id"],
                )

    def test_new_project_ui_exposes_physics_protocol_start_and_post_qc_policy(self):
        required_tokens = [
            "data-import-field=\"detector\"",
            "automaticTimeAxisForDetector",
            "physicalTimeAxisLabel",
            "importCalibrationSegments",
            "addImportSegment",
            "boundaries_confirmed",
            "importAnnotationStart",
            "importPostQcMode",
            "applyLinLskExample",
            "suggestImportWindows",
            "/api/suggest-calibration-windows",
            "segment.boundaries_confirmed = false",
            "可选：实验配置模板",
            "Lin− / LSK 示例配置",
            "后段 QC 策略",
        ]
        for token in required_tokens:
            with self.subTest(token=token):
                self.assertIn(token, HTML)

        self.assertNotIn(
            'data-import-field="use_for_qc"',
            HTML,
            "前段校准参考通道必须只在分段协议中配置",
        )
        self.assertNotIn(
            'data-import-field="time_axis"',
            HTML,
            "物理时间轴应由检测器自动设置，而不是让用户填写内部名称",
        )

    def test_manual_front_anchor_uses_the_selected_calibration_segment(self):
        lif = pd.DataFrame([lif_peak("G1", "g1_front", 60.0)])
        ms = pd.DataFrame([ms_event("ms_front", 65.0)])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(Path(tmp), lif, ms, strategy={"mode": "disabled"})

            row = app.create_manual_triplet(
                None,
                None,
                "ms_front",
                stage="qc_calibration",
                lif_anchor_peak_ids={"G1": "g1_front"},
                calibration_segment_id="lsk_reference",
            )

            self.assertEqual(row["review_status"], "accepted")
            self.assertEqual(row["calibration_segment_id"], "lsk_reference")
            self.assertEqual(row["anchor_channels"], ["G1"])

    def test_manual_post_qc_is_rejected_when_policy_is_disabled(self):
        lif = pd.DataFrame([lif_peak("G2", "g2_late", 24.5 * 60.0 - 5.0)])
        ms = pd.DataFrame([ms_event("ms_late", 24.5 * 60.0)])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_hsc_app(Path(tmp), lif, ms, strategy={"mode": "disabled"})

            with self.assertRaisesRegex(BadRequest, "disabled|禁用"):
                app.create_manual_triplet(
                    None,
                    None,
                    "ms_late",
                    stage="qc_survey",
                    lif_anchor_peak_ids={"G2": "g2_late"},
                )


class SegmentedCalibrationMatchingTest(unittest.TestCase):
    def test_sequential_g1_g2_segments_pool_evidence_for_one_green_shift(self):
        layout = normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {"channel": "G1", "identity_prior": "LSK", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                    {"channel": "G2", "identity_prior": "Lin-", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                ],
            }
        )
        protocol = normalize_calibration_protocol(
            {
                "segments": [
                    {"segment_id": "lsk", "order": 1, "start_min": 0, "end_min": 2, "reference_channels": ["G1"], "reference_mode": "green_only", "boundaries_confirmed": True},
                    {"segment_id": "lin", "order": 2, "start_min": 3, "end_min": 5, "reference_channels": ["G2"], "reference_mode": "green_only", "boundaries_confirmed": True},
                ]
            },
            layout,
        )
        lif = pd.DataFrame(
            [
                lif_peak("G1", "g1-a", 30),
                lif_peak("G1", "g1-b", 60),
                lif_peak("G2", "g2-a", 210),
                lif_peak("G2", "g2-b", 240),
            ]
        )
        ms = pd.DataFrame(
            [
                ms_event("ms-a", 35),
                ms_event("ms-b", 65),
                ms_event("ms-c", 215),
                ms_event("ms-d", 245),
            ]
        )

        alignment = estimate_shift_alignment(
            lif,
            ms,
            acquisition_layout=layout,
            calibration_protocol=protocol,
        )

        self.assertAlmostEqual(alignment["axis_shifts_sec"]["green_axis"], 5.0)
        self.assertAlmostEqual(alignment["channels"]["G1"]["shift_sec"], 5.0)
        self.assertAlmostEqual(alignment["channels"]["G2"]["shift_sec"], 5.0)
        groups = alignment["qc_groups"]["groups"]
        self.assertEqual({row["calibration_segment_id"] for row in groups}, {"lsk", "lin"})
        self.assertTrue(all(row["lif_anchor_count"] == 1 for row in groups))

    def test_accepted_g1_g2_segment_anchors_refit_one_green_axis(self):
        layout = normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {"channel": "G1", "identity_prior": "LSK", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                    {"channel": "G2", "identity_prior": "Lin-", "detector": "green", "time_axis": "green_axis", "use_for_cell_annotation": True},
                ],
            }
        )
        protocol = normalize_calibration_protocol(
            {
                "segments": [
                    {"segment_id": "lsk", "order": 1, "start_min": 0, "end_min": 2, "reference_channels": ["G1"], "reference_mode": "green_only", "boundaries_confirmed": True},
                    {"segment_id": "lin", "order": 2, "start_min": 3, "end_min": 5, "reference_channels": ["G2"], "reference_mode": "green_only", "boundaries_confirmed": True},
                ]
            },
            layout,
        )
        lif = pd.DataFrame(
            [lif_peak("G1", "g1-a", 30), lif_peak("G1", "g1-b", 60), lif_peak("G2", "g2-a", 210), lif_peak("G2", "g2-b", 240)]
        )
        ms = pd.DataFrame(
            [ms_event("ms-a", 35), ms_event("ms-b", 65), ms_event("ms-c", 215), ms_event("ms-d", 245)]
        )
        annotations = []
        for annotation_id, segment_id, channel, peak_id, event_id, ms_time in [
            ("a", "lsk", "G1", "g1-a", "ms-a", 35),
            ("b", "lsk", "G1", "g1-b", "ms-b", 65),
            ("c", "lin", "G2", "g2-a", "ms-c", 215),
            ("d", "lin", "G2", "g2-b", "ms-d", 245),
        ]:
            annotations.append(
                {
                    "annotation_id": annotation_id,
                    "review_status": "accepted",
                    "review_stage": "qc_calibration",
                    "candidate_type": "qc_calibration_segment_anchor",
                    "label": "QC",
                    "calibration_segment_id": segment_id,
                    "lif_anchor_peak_ids": {channel: peak_id},
                    "ms_event_id": event_id,
                    "ms_time_min": ms_time / 60.0,
                }
            )

        preview = accepted_qc_alignment_refit(
            lif,
            ms,
            annotations,
            acquisition_layout=layout,
            calibration_protocol=protocol,
            qc_calibration_end_min=5.0,
            current_axis_shifts_sec={"green_axis": 0.0},
        )

        self.assertEqual(set(preview["axes"]), {"green_axis"})
        self.assertAlmostEqual(preview["axis_shifts_sec"]["green_axis"], 5.0)
        self.assertEqual(preview["used_annotation_count"], 4)
        self.assertEqual(preview["calibration_protocol_hash"], calibration_protocol_hash(protocol, layout))


if __name__ == "__main__":
    unittest.main()
