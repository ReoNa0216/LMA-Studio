from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from annotation_app.app import APP_VERSION, HTML, normalize_post_qc_strategy
from tests.test_v050_timeline_adjustment import make_app


class V051ReviewStateProjectionContractTest(unittest.TestCase):
    def test_version_is_v051(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.5.1")

    def test_reviewed_auto_cell_remains_accepted_in_events_counts_after_rebuild(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            ms_events = app.ms_events.copy()
            ms_events.loc[
                ms_events["event_id"].isin(["ms-cell-1", "ms-cell-2"]),
                "pc34_760_apex",
            ] = 20000.0
            object.__setattr__(app, "ms_events", ms_events)
            candidate = app.build_cell_candidates(24.0, 25.0, "aligned")[0]
            accepted = app.review_auto_candidate(
                candidate["candidate_id"],
                "accepted",
                window_start_min=24.0,
                window_end_min=25.0,
                time_mode="aligned",
            )

            preview = app.timeline_adjustment_preview(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
            )
            app.apply_timeline_adjustment(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
                expected_preview_hash=preview["preview_hash"],
            )
            window = app.window(24.0, 1.0, time_mode="aligned")
            projected = next(
                row
                for row in window["annotations"]
                if row["annotation_id"] == accepted["annotation_id"]
            )

        self.assertEqual(projected["review_status"], "accepted")
        self.assertTrue(projected["needs_review"])
        self.assertEqual(window["cell_counts"]["accepted"], 1)
        self.assertEqual(window["cell_counts"]["pending"], 0)

    def test_reviewed_auto_qc_is_counted_and_available_to_candidate_list(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            strategy = normalize_post_qc_strategy(
                {"mode": "signature", "reference_channels": ["G1"]},
                app.acquisition_layout,
            )
            app.store.update_project_config({"post_qc_strategy": strategy})
            object.__setattr__(app, "post_qc_strategy", strategy)
            candidate = app.build_post_qc_candidates(24.0, 25.0, "aligned")[0]
            payload = app.payload_from_post_qc_group(candidate)
            accepted = app.store.upsert_review(
                annotation_id=candidate["annotation_id"],
                source="auto_candidate",
                review_status="accepted",
                payload=payload,
                action="test_accept_post_qc",
            )

            preview = app.timeline_adjustment_preview(
                {"green_axis": 5.0},
                ms_local_delta_sec=0.0,
            )
            app.apply_timeline_adjustment(
                {"green_axis": 5.0},
                ms_local_delta_sec=0.0,
                expected_preview_hash=preview["preview_hash"],
            )
            window = app.window(24.0, 1.0, time_mode="aligned")
            projected = next(
                row
                for row in window["cell_qc_anchors"]
                if row["annotation_id"] == accepted["annotation_id"]
            )

        self.assertEqual(projected["review_status"], "accepted")
        self.assertTrue(projected["needs_review"])
        self.assertEqual(window["post_qc_counts"]["accepted"], 1)
        self.assertIn("...(state.current?.cell_qc_anchors || [])", HTML)
        self.assertIn("原状态保留", HTML)


if __name__ == "__main__":
    unittest.main()
