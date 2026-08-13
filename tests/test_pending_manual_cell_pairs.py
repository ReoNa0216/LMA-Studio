from __future__ import annotations

import copy
import io
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import AnnotationStore, BadRequest, HTML
from tests.test_lif_detector_v2_contract import peak_rows
from tests.test_v04_ui_csv_regressions import make_export_app


class PendingManualCellPairContractTest(unittest.TestCase):
    def make_app(self, root: Path):
        app = make_export_app(root)
        ms_events = app.ms_events.copy()
        ms_events["time_sec"] = ms_events["time_min"].astype(float) * 60.0
        ms_events["nearest_event_gap_sec"] = 30.0
        ms_events["collision_risk_high"] = False
        ms_events["low_quality_scan_window"] = False
        object.__setattr__(app, "ms_events", ms_events)
        ms_scan = app.ms_scan.copy()
        event_times = dict(zip(ms_events["scan_id"], ms_events["time_min"]))
        ms_scan["scan_start_time_min"] = ms_scan["scan_id"].map(event_times)
        ms_scan["pc34_760_max_intensity"] = ms_scan["tic"].astype(float)
        ms_scan["qc_782_max_intensity"] = ms_scan["tic"].astype(float) / 2.0
        object.__setattr__(app, "ms_scan", ms_scan)
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
                        "raw": 0.0,
                        "signal": 0.0,
                    }
                    for channel, label in (("G1", "LSK"), ("G2", "Lin-"))
                    for time_min in (24.0, 24.5, 25.0)
                ]
            ),
        )
        peaks = peak_rows()
        g2_core = peaks.iloc[0].copy()
        g2_core["channel"] = "G2"
        g2_core["label"] = "Lin-"
        g2_core["peak_id"] = "g2-core"
        g2_core["parent_raw_peak_ids"] = "g2-core-raw"
        object.__setattr__(
            app,
            "lif_peaks",
            pd.concat([peaks, pd.DataFrame([g2_core])], ignore_index=True),
        )
        return app

    def test_pending_pair_persists_in_track_but_not_scientific_outputs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_app(Path(tmp))
            manifest_before = copy.deepcopy(app.manifest)
            config_before = copy.deepcopy(app.project_config())
            time_model_before = copy.deepcopy(app.active_time_model())
            pending = app.create_manual_cell_pair(
                "G1",
                "g1-core",
                "ms-cell-1",
                window_start_min=24.0,
                window_end_min=25.0,
                time_mode="aligned",
                review_status="pending",
            )

            stored = app.store.get(pending["annotation_id"])
            reopened = AnnotationStore(app.store.db_path).get(pending["annotation_id"])
            config_after_pending = copy.deepcopy(app.project_config())
            time_model_after_pending = copy.deepcopy(app.active_time_model())
            visible = app.window_annotations(24.0, 25.0)
            window = app.window(24.0, 1.0, time_mode="aligned")
            exported = app.export_accepted_annotations_csv()
            export_frame = pd.read_csv(io.StringIO(exported["csv_text"]))
            projected = app.projected_cell_event_map_state()
            projected_by_event = {
                str(point["ms_event_id"]): point for point in projected["points"]
            }
            accepted_event_ids = app.accepted_cell_annotation_ms_event_ids()

        self.assertEqual(pending["review_status"], "pending")
        self.assertEqual(pending["source"], "manual_created")
        self.assertFalse(pending["exportable"])
        self.assertEqual(stored["review_status"], "pending")
        self.assertEqual(reopened["review_status"], "pending")
        self.assertEqual(app.manifest, manifest_before)
        self.assertEqual(config_after_pending, config_before)
        self.assertEqual(time_model_after_pending, time_model_before)
        self.assertIn(pending["annotation_id"], {row["annotation_id"] for row in visible})
        self.assertIn(
            pending["annotation_id"],
            {row["annotation_id"] for row in window["annotations"]},
        )
        self.assertEqual(window["cell_counts"]["pending"], 1)
        self.assertNotIn("ms-cell-1", accepted_event_ids)
        self.assertNotIn(pending["annotation_id"], set(export_frame["annotation_id"].dropna()))
        self.assertEqual(
            export_frame.loc[export_frame["MS_event_id"] == "ms-cell-1", "Type"].iloc[0],
            "unknown",
        )
        self.assertEqual(projected_by_event["ms-cell-1"]["classification"], "unknown")

    def test_pending_does_not_reserve_event_and_acceptance_rechecks_conflicts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_app(Path(tmp))
            pending = app.create_manual_cell_pair(
                "G1",
                "g1-core",
                "ms-cell-1",
                review_status="pending",
            )
            accepted_other = app.create_manual_cell_pair(
                "G2",
                "g2-core",
                "ms-cell-1",
            )

            with self.assertRaisesRegex(BadRequest, "只能有一个|冲突"):
                app.review_annotation(pending["annotation_id"], "accepted")

            still_pending = app.store.get(pending["annotation_id"])

        self.assertEqual(accepted_other["review_status"], "accepted")
        self.assertEqual(still_pending["review_status"], "pending")

    def test_default_save_pair_remains_immediately_accepted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_app(Path(tmp))
            saved = app.create_manual_cell_pair("G1", "g1-core", "ms-cell-1")

        self.assertEqual(saved["review_status"], "accepted")
        self.assertTrue(saved["exportable"])

    def test_roster_supported_ms_event_is_manual_cell_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_app(Path(tmp))
            ms_events = app.ms_events.copy()
            ms_events["event_strategy"] = "pc34_primary"
            ms_events["primary_signal_col"] = "pc34_760_max_intensity"
            ms_events.loc[
                ms_events["event_id"].eq("ms-cell-1"),
                "event_strategy",
            ] = "pc34_roster_supported"
            object.__setattr__(app, "ms_events", ms_events)

            automatic = app.build_cell_candidates(24.0, 25.0, "aligned")
            saved = app.create_manual_cell_pair(
                "G1",
                "g1-core",
                "ms-cell-1",
                window_start_min=24.0,
                window_end_min=25.0,
                time_mode="aligned",
            )

        self.assertNotIn(
            "ms-cell-1",
            {str(row.get("ms_event_id")) for row in automatic},
        )
        self.assertEqual(saved["review_status"], "accepted")
        self.assertEqual(saved["ms_event_id"], "ms-cell-1")
        self.assertTrue(saved["exportable"])

    def test_roster_supported_manual_cell_exports_the_selected_mass_lane_mz(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_app(Path(tmp))
            ms_events = app.ms_events.copy()
            ms_events["event_strategy"] = "pc34_primary"
            ms_events["primary_signal_col"] = "pc34_760_max_intensity"
            target = ms_events["event_id"].eq("ms-cell-1")
            ms_events.loc[target, "event_strategy"] = "pc34_roster_supported"
            ms_events.loc[target, "pc34_760_mz_at_apex"] = 760.575748358
            object.__setattr__(app, "ms_events", ms_events)
            ms_scan = app.ms_scan.copy()
            target_scan = str(ms_events.loc[target, "scan_id"].iloc[0])
            ms_scan.loc[
                ms_scan["scan_id"].astype(str).eq(target_scan),
                "pc34_760_mz_at_max_intensity",
            ] = float("nan")
            object.__setattr__(app, "ms_scan", ms_scan)

            app.create_manual_cell_pair(
                "G1",
                "g1-core",
                "ms-cell-1",
                window_start_min=24.0,
                window_end_min=25.0,
                time_mode="aligned",
            )
            exported = pd.read_csv(
                io.StringIO(app.export_accepted_annotations_csv()["csv_text"])
            )

        row = exported.loc[exported["MS_event_id"].eq("ms-cell-1")].iloc[0]
        self.assertAlmostEqual(float(row["PC(34:1)_mz"]), 760.575748358)

    def test_pending_pair_can_be_accepted_later_and_then_reaches_csv_and_umap(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = self.make_app(Path(tmp))
            pending = app.create_manual_cell_pair(
                "G1",
                "g1-core",
                "ms-cell-1",
                review_status="pending",
            )
            accepted = app.review_annotation(pending["annotation_id"], "accepted")
            exported = pd.read_csv(
                io.StringIO(app.export_accepted_annotations_csv()["csv_text"])
            )
            projected = {
                str(point["ms_event_id"]): point
                for point in app.projected_cell_event_map_state()["points"]
            }

        self.assertEqual(accepted["review_status"], "accepted")
        self.assertTrue(accepted["exportable"])
        exported_cell = exported.loc[exported["MS_event_id"] == "ms-cell-1"].iloc[0]
        self.assertEqual(exported_cell["Type"], "LSK")
        self.assertEqual(projected["ms-cell-1"]["classification"], "cell")
        self.assertEqual(projected["ms-cell-1"]["lif_channel"], "G1")

    def test_events_ui_offers_separate_pending_save_without_changing_qc_anchor(self):
        self.assertIn('id="createManualPending"', HTML)
        self.assertIn("Save pending", HTML)
        self.assertRegex(
            HTML,
            r"createManualTriplet\([^)]*reviewStatus[^)]*\)[\s\S]*"
            r"review_status:\s*reviewStatus",
        )
        self.assertRegex(
            HTML,
            r"createManualPending[\s\S]*createManualTriplet\(['\"]pending['\"]\)",
        )
        self.assertRegex(
            HTML,
            r"createManualPending['\"]?\)\.style\.display\s*=\s*cellMode",
        )


if __name__ == "__main__":
    unittest.main()
