import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from tests.test_calibration_protocol import ms_event
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


if __name__ == "__main__":
    unittest.main()
