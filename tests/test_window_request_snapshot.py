import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from annotation_app.app import AppData, acquisition_layout_hash
from tests.test_calibration_protocol import (
    CalibrationProtocolSchemaTest,
    lif_peak,
    ms_event,
)
from tests.test_lif_detector_v2_contract import peak_rows
from tests.test_v04_ui_csv_regressions import make_export_app


class WindowRequestSnapshotRegressionTest(unittest.TestCase):
    def prepare_app(self, root: Path):
        app = make_export_app(root)
        object.__setattr__(app, "lif_peaks", peak_rows())
        object.__setattr__(
            app,
            "lif_traces",
            pd.DataFrame(
                [
                    {
                        "channel": channel,
                        "label": label,
                        "detector": "green",
                        "time_min": time_min,
                        "time_sec": time_min * 60.0,
                        "raw": 1.0,
                        "signal": 1.0,
                    }
                    for channel, label in (("G1", "LSK"), ("G2", "Lin−"))
                    for time_min in (24.0, 24.5, 25.0, 25.5)
                ]
            ),
        )
        object.__setattr__(
            app,
            "ms_events",
            pd.DataFrame(
                [
                    ms_event("ms-cell-1", 24.5 * 60.0),
                    ms_event("ms-cell-2", 25.0 * 60.0),
                ]
            ),
        )
        object.__setattr__(
            app,
            "ms_scan",
            pd.DataFrame(
                [
                    {
                        "scan_start_time_min": time_min,
                        "pc34_760_max_intensity": 100.0,
                        "qc_782_max_intensity": 50.0,
                    }
                    for time_min in (24.0, 24.5, 25.0, 25.5)
                ]
            ),
        )
        for index in range(48):
            time_min = 24.1 + index * 0.015
            app.store.upsert_review(
                annotation_id=f"stored-cell-{index:03d}",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "cell_annotation",
                    "candidate_type": "manual_cell_pair",
                    "lif_channel": "G1",
                    "lif_peak_id": f"stored-lif-{index:03d}",
                    "lif_plot_time_min": time_min,
                    "ms_event_id": f"stored-ms-{index:03d}",
                    "ms_time_min": time_min,
                    "ms_plot_time_min": time_min,
                    "time_model_version": "tm-current",
                },
                action="test_seed_window_snapshot",
            )
        return app

    def test_window_reads_sqlite_state_once_per_request_not_once_per_annotation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.prepare_app(Path(tmp))
            with mock.patch.object(
                app.store,
                "_connect",
                wraps=app.store._connect,
            ) as connect:
                payload = app.window(
                    24.0,
                    1.0,
                    time_mode="aligned",
                    include_weak_lif_peaks=True,
                )

            accepted_ids = {
                str(row.get("annotation_id"))
                for row in payload["annotations"]
                if row.get("review_status") == "accepted"
            }
            self.assertEqual(len(accepted_ids), 48)
            self.assertLessEqual(
                connect.call_count,
                10,
                "A window request must use one request-level SQLite snapshot; "
                "connection count must not scale with annotation rows.",
            )

    def test_save_pair_reuses_one_validation_snapshot_before_the_write(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.prepare_app(Path(tmp))
            with mock.patch.object(
                app.store,
                "_connect",
                wraps=app.store._connect,
            ) as connect:
                saved = app.create_manual_cell_pair(
                    "G1",
                    "g1-core",
                    "ms-cell-1",
                    window_start_min=24.0,
                    window_end_min=25.0,
                    time_mode="aligned",
                )

            self.assertEqual(saved["review_status"], "accepted")
            self.assertEqual(saved["ms_event_id"], "ms-cell-1")
            self.assertLessEqual(
                connect.call_count,
                12,
                "Save pair validation must not reopen SQLite for every existing annotation.",
            )

    def test_accept_and_reject_reuse_one_validation_snapshot_before_the_write(self):
        for review_status in ("accepted", "rejected"):
            with self.subTest(review_status=review_status):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                    app = self.prepare_app(Path(tmp))
                    candidate = next(
                        row
                        for row in app.build_cell_candidates(24.0, 25.0, "aligned")
                        if row["lif_channel"] == "G1"
                        and row["ms_event_id"] == "ms-cell-1"
                    )
                    with mock.patch.object(
                        app.store,
                        "_connect",
                        wraps=app.store._connect,
                    ) as connect:
                        saved = app.review_annotation(
                            candidate["candidate_id"],
                            review_status,
                            window_start_min=24.0,
                            window_end_min=25.0,
                            time_mode="aligned",
                        )

                    self.assertEqual(saved["review_status"], review_status)
                    self.assertEqual(
                        app.store.get(candidate["candidate_id"])["review_status"],
                        review_status,
                    )
                    self.assertLessEqual(
                        connect.call_count,
                        15,
                        "Accept/reject validation must not reopen SQLite for every "
                        "existing annotation or reconstructed candidate.",
                    )

    def test_request_snapshot_expires_before_the_next_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.prepare_app(Path(tmp))
            first = app.window(24.0, 1.0, time_mode="aligned")
            self.assertEqual(len(first["annotations"]), 48)

            app.store.upsert_time_model(
                {
                    **app.active_time_model(),
                    "time_model_version": "tm-next",
                    "status": "frozen",
                },
                action="test_advance_time_model_between_windows",
            )
            second = app.window(24.0, 1.0, time_mode="aligned")

            self.assertEqual(second["time_model"]["time_model_version"], "tm-next")
            self.assertEqual(second["annotations"], [])

    def test_qc_review_drops_invalidated_models_from_the_same_request_snapshot(self):
        case = CalibrationProtocolSchemaTest()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            lif = pd.DataFrame(
                [
                    lif_peak("G1", "g1-front-1", 60.0),
                    lif_peak("G1", "g1-front-2", 90.0),
                    lif_peak("G2", "g2-front-1", 210.0),
                    lif_peak("G2", "g2-front-2", 240.0),
                ]
            )
            ms = pd.DataFrame(
                [
                    ms_event("ms-front-1", 65.0),
                    ms_event("ms-front-2", 95.0),
                    ms_event("ms-front-3", 215.0),
                    ms_event("ms-front-4", 245.0),
                ]
            )
            app = case.make_hsc_app(
                Path(tmp),
                lif,
                ms,
                strategy={"mode": "disabled"},
                frozen=False,
            )
            app.reset_to_automatic_qc_alignment()
            candidate = app.enrich_qc_candidate(
                app.alignment["qc_groups"]["groups"][0],
                post_qc=False,
            )
            config = app.project_config()
            app.store.save_qc_alignment_model(
                {
                    "model_version": 1,
                    "model_id": "qca-before-review",
                    "status": "preview",
                    "preview_hash": "d" * 64,
                    "qc_calibration_end_min": 5.0,
                    "acquisition_layout_hash": acquisition_layout_hash(
                        app.acquisition_layout
                    ),
                    "axis_shifts_sec": {"green_axis": 5.0},
                },
                draft_time_model_payload={
                    "time_model_version": "tm-before-review",
                    "status": "draft",
                    "base_model_name": "segmented",
                    "qc_calibration_end_min": 5.0,
                    "sample_valve_switch_min": 20.0,
                    "annotation_start_min": 24.0,
                    "local_delta_seed_window_min": 2.5,
                    "ms_local_delta_sec": 0.0,
                    "contains_cell_labels": False,
                    "max_training_time_min": 24.0,
                    "evidence_count": 0,
                    "unique_match_count": 0,
                    "conflict_count": 0,
                    "median_abs_residual_sec": None,
                    "p90_abs_residual_sec": None,
                    "acquisition_layout_hash": acquisition_layout_hash(
                        app.acquisition_layout
                    ),
                    "calibration_protocol_hash": config[
                        "calibration_protocol_hash"
                    ],
                },
            )

            observed = {}
            original_reset = AppData.reset_to_automatic_qc_alignment

            def inspect_reset(current_app):
                observed["qc_model"] = current_app.project_config().get(
                    "qc_alignment_model"
                )
                observed["time_model_version"] = current_app.active_time_model().get(
                    "time_model_version"
                )
                return original_reset(current_app)

            with mock.patch.object(
                AppData,
                "reset_to_automatic_qc_alignment",
                new=inspect_reset,
            ):
                app.review_annotation(
                    candidate["annotation_id"],
                    "accepted",
                    window_start_min=0.0,
                    window_end_min=2.0,
                    time_mode="aligned",
                    clear_qc_alignment_model=True,
                )

            self.assertIsNone(observed["qc_model"])
            self.assertNotEqual(
                observed["time_model_version"],
                "tm-before-review",
            )


if __name__ == "__main__":
    unittest.main()
