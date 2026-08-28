from __future__ import annotations

import copy
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import (
    APP_VERSION,
    BadRequest,
    HTML,
    normalize_acquisition_layout,
)
from tests.test_pending_manual_cell_pairs import PendingManualCellPairContractTest


EXPECTED_EXPORT_COLUMNS = [
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
]


def make_app(root: Path):
    return PendingManualCellPairContractTest().make_app(root)


def sqlite_state(db_path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "annotations",
                "audit_events",
                "project_config",
                "time_models",
                "time_model_audit_events",
            )
        }


class V050TimelineAdjustmentContractTest(unittest.TestCase):
    def test_version_is_v050(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.5.0")

    def test_preview_is_read_only_and_reports_candidate_rebuild(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            app.create_manual_cell_pair(
                "G1",
                "g1-core",
                "ms-cell-1",
                review_status="pending",
            )
            before = sqlite_state(app.store.db_path)

            preview = app.timeline_adjustment_preview(
                {"green_axis": 1.25},
                ms_local_delta_sec=-0.5,
            )

            after = sqlite_state(app.store.db_path)

        self.assertEqual(after, before)
        self.assertEqual(preview["status"], "preview")
        self.assertTrue(preview["preview_hash"])
        self.assertEqual(preview["physical_axis"]["axis_shifts_sec"], {"green_axis": 1.25})
        self.assertEqual(preview["post_ms_delta"]["ms_local_delta_sec"], -0.5)
        self.assertEqual(preview["impact"]["reviewed_relationships_preserved"], 1)
        self.assertEqual(preview["impact"]["unreviewed_auto_candidates"], "rebuild")

    def test_apply_creates_frozen_revision_and_reprojects_reviewed_relation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            relation = app.create_manual_cell_pair("G1", "g1-core", "ms-cell-1")
            stored_before = copy.deepcopy(app.store.get(relation["annotation_id"]))
            old_model = copy.deepcopy(app.frozen_time_model())
            preview = app.timeline_adjustment_preview(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
            )

            applied = app.apply_timeline_adjustment(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
                expected_preview_hash=preview["preview_hash"],
            )
            stored_after = app.store.get(relation["annotation_id"])
            visible = {
                row["annotation_id"]: row
                for row in app.window_annotations(24.0, 25.0)
            }
            with sqlite3.connect(app.store.db_path) as conn:
                audit = conn.execute(
                    "SELECT action, payload_json FROM time_model_audit_events "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()

        new_model = applied["time_model"]
        self.assertEqual(new_model["status"], "frozen")
        self.assertNotEqual(new_model["time_model_version"], old_model["time_model_version"])
        self.assertEqual(new_model["previous_time_model_version"], old_model["time_model_version"])
        self.assertEqual(new_model["physical_axis_shifts_sec"], {"green_axis": 2.0})
        self.assertEqual(new_model["ms_local_delta_sec"], 0.0)
        self.assertEqual(
            new_model["downstream_invalidation"]["unreviewed_auto_candidates"],
            "invalidate_and_rebuild",
        )
        self.assertEqual(
            new_model["downstream_invalidation"]["reviewed_relationships"],
            "preserve_and_reproject",
        )
        self.assertEqual(stored_after["annotation_id"], stored_before["annotation_id"])
        self.assertEqual(stored_after["review_status"], "accepted")
        self.assertEqual(
            stored_after["time_model_version"],
            stored_before["time_model_version"],
            "decision provenance must not be rewritten during reprojection",
        )
        projected = visible[relation["annotation_id"]]
        self.assertEqual(projected["projection_time_model_version"], new_model["time_model_version"])
        self.assertAlmostEqual(projected["lif_plot_time_min"], 24.5 + 2.0 / 60.0)
        self.assertAlmostEqual(projected["residual_sec"], -2.0)
        self.assertTrue(projected["needs_review"])
        self.assertEqual(projected["review_status"], "accepted")
        self.assertEqual(audit[0], "apply_timeline_adjustment")

    def test_events_preview_moves_tracks_ticks_and_relation_endpoints_without_writing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            relation = app.create_manual_cell_pair("G1", "g1-core", "ms-cell-1")
            before_state = sqlite_state(app.store.db_path)
            preview = app.window(
                24.0,
                1.5,
                time_mode="aligned",
                preview_axis_shifts_sec={"green_axis": 1.0},
                preview_ms_delta_sec=0.5,
                live_preview_context="lif",
            )
            cancelled = app.window(24.0, 1.5, time_mode="aligned")
            after_state = sqlite_state(app.store.db_path)

        preview_peak = next(row for row in preview["lif_peaks"] if row["peak_id"] == "g1-core")
        preview_event = next(row for row in preview["ms_events"] if row["event_id"] == "ms-cell-1")
        preview_relation = next(
            row for row in preview["annotations"] if row["annotation_id"] == relation["annotation_id"]
        )
        cancelled_relation = next(
            row for row in cancelled["annotations"] if row["annotation_id"] == relation["annotation_id"]
        )
        self.assertAlmostEqual(preview_peak["plot_time_min"], 24.5 + 1.0 / 60.0)
        self.assertAlmostEqual(preview_event["plot_time_min"], 24.5 + 0.5 / 60.0)
        self.assertAlmostEqual(preview_relation["lif_plot_time_min"], preview_peak["plot_time_min"])
        self.assertAlmostEqual(preview_relation["ms_plot_time_min"], preview_event["plot_time_min"])
        self.assertAlmostEqual(preview_relation["residual_sec"], -0.5)
        self.assertAlmostEqual(cancelled_relation["residual_sec"], 0.0)
        self.assertEqual(after_state, before_state)

    def test_post_ms_delta_is_a_separate_projection_parameter(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            relation = app.create_manual_cell_pair("G1", "g1-core", "ms-cell-1")
            preview = app.timeline_adjustment_preview(
                {"green_axis": 0.0},
                ms_local_delta_sec=1.5,
            )
            app.apply_timeline_adjustment(
                {"green_axis": 0.0},
                ms_local_delta_sec=1.5,
                expected_preview_hash=preview["preview_hash"],
            )
            projected = {
                row["annotation_id"]: row
                for row in app.window_annotations(24.0, 25.0)
            }[relation["annotation_id"]]

        self.assertAlmostEqual(projected["lif_plot_time_min"], 24.5)
        self.assertAlmostEqual(projected["ms_plot_time_min"], 24.5 + 1.5 / 60.0)
        self.assertAlmostEqual(projected["residual_sec"], 1.5)
        self.assertEqual(app.alignment["axis_shifts_sec"], {"green_axis": 0.0})

    def test_accepted_pending_rejected_manual_and_reviewed_auto_are_never_deleted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            payload = app.payload_from_cell_ids(
                "G1", "g1-core", "ms-cell-1", enforce_acceptance_conflicts=False
            )
            decisions = {
                "accepted-manual": ("manual_created", "accepted"),
                "pending-manual": ("manual_created", "pending"),
                "rejected-manual": ("manual_created", "rejected"),
                "pending-auto-reviewed": ("auto_candidate", "pending"),
                "rejected-auto-reviewed": ("auto_candidate", "rejected"),
            }
            for annotation_id, (source, status) in decisions.items():
                app.store.upsert_review(
                    annotation_id=annotation_id,
                    source=source,
                    review_status=status,
                    payload=payload,
                    action="test_review_decision",
                )
            preview = app.timeline_adjustment_preview(
                {"green_axis": 0.4},
                ms_local_delta_sec=0.1,
            )
            app.apply_timeline_adjustment(
                {"green_axis": 0.4},
                ms_local_delta_sec=0.1,
                expected_preview_hash=preview["preview_hash"],
            )
            stored = {row["annotation_id"]: row for row in app.store.records()}

        self.assertEqual(set(stored), set(decisions))
        self.assertEqual(
            {key: stored[key]["review_status"] for key in decisions},
            {key: value[1] for key, value in decisions.items()},
        )

    def test_unreviewed_candidates_rebuild_but_accepted_identity_survives(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            ms_events = app.ms_events.copy()
            ms_events.loc[
                ms_events["event_id"].isin(["ms-cell-1", "ms-cell-2"]),
                "pc34_760_apex",
            ] = 20000.0
            object.__setattr__(app, "ms_events", ms_events)
            before = app.build_cell_candidates(24.0, 25.0, "aligned")
            before_ids = {row["candidate_id"] for row in before}
            self.assertTrue(before_ids)

            preview = app.timeline_adjustment_preview(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
            )
            app.apply_timeline_adjustment(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
                expected_preview_hash=preview["preview_hash"],
            )
            after_ids = {
                row["candidate_id"]
                for row in app.build_cell_candidates(24.0, 25.0, "aligned")
            }

        self.assertTrue(before_ids.isdisjoint(after_ids))

    def test_track_umap_and_csv_keep_raw_identity_after_adjustment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            relation = app.create_manual_cell_pair("G1", "g1-core", "ms-cell-1")
            before_window = app.window(24.0, 1.5, time_mode="aligned")
            before_peak_ids = {row["peak_id"] for row in before_window["lif_peaks"]}
            preview = app.timeline_adjustment_preview(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
            )
            app.apply_timeline_adjustment(
                {"green_axis": 2.0},
                ms_local_delta_sec=0.0,
                expected_preview_hash=preview["preview_hash"],
            )
            after_window = app.window(24.0, 1.5, time_mode="aligned")
            after_peak_ids = {row["peak_id"] for row in after_window["lif_peaks"]}
            projected = {
                point["ms_event_id"]: point
                for point in app.projected_cell_event_map_state()["points"]
            }
            exported = pd.read_csv(
                io.StringIO(app.export_accepted_annotations_csv()["csv_text"])
            )
            row = exported.loc[exported["MS_event_id"].eq("ms-cell-1")].iloc[0]

        self.assertEqual(after_peak_ids, before_peak_ids)
        self.assertEqual(projected["ms-cell-1"]["classification"], "cell")
        self.assertEqual(projected["ms-cell-1"]["lif_channel"], "G1")
        self.assertEqual(row["annotation_id"], relation["annotation_id"])
        self.assertEqual(row["LIF_peak_id"], "g1-core")
        self.assertEqual(row["MS_event_id"], "ms-cell-1")
        self.assertEqual(exported.columns.tolist(), EXPECTED_EXPORT_COLUMNS)

    def test_green_red_axes_are_shared_by_detector_family(self):
        valid = normalize_acquisition_layout(
            {
                "layout_version": 4,
                "lif_channels": [
                    {"channel": "G1", "time_axis": "green_axis"},
                    {"channel": "G2", "time_axis": "green_axis"},
                    {"channel": "R1", "time_axis": "red_axis"},
                    {"channel": "R2", "time_axis": "red_axis"},
                ],
            }
        )
        self.assertEqual(valid["channel_time_axes"]["G1"], "green_axis")
        self.assertEqual(valid["channel_time_axes"]["G2"], "green_axis")
        self.assertEqual(valid["channel_time_axes"]["R1"], "red_axis")
        self.assertEqual(valid["channel_time_axes"]["R2"], "red_axis")
        with self.assertRaisesRegex(BadRequest, "G1.*green_axis"):
            normalize_acquisition_layout(
                {
                    "layout_version": 4,
                    "lif_channels": [
                        {"channel": "G1", "time_axis": "red_axis"},
                        {"channel": "R1", "time_axis": "red_axis"},
                    ],
                }
            )

    def test_dual_axis_projection_moves_red_independently_but_keeps_r1_r2_shared(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp))
            layout = copy.deepcopy(app.acquisition_layout)
            layout["lif_channels"].extend(
                [
                    {
                        "input_id": "r1",
                        "channel": "R1",
                        "identity_prior": "R1",
                        "time_axis": "red_axis",
                        "detector": "red",
                        "use_for_cell_annotation": True,
                    },
                    {
                        "input_id": "r2",
                        "channel": "R2",
                        "identity_prior": "R2",
                        "time_axis": "red_axis",
                        "detector": "red",
                        "use_for_cell_annotation": True,
                    },
                ]
            )
            object.__setattr__(app, "acquisition_layout", layout)
            red_peaks = []
            for channel in ("R1", "R2"):
                peak = app.lif_peaks.iloc[0].copy()
                peak["channel"] = channel
                peak["peak_id"] = f"{channel.lower()}-core"
                red_peaks.append(peak)
            object.__setattr__(
                app,
                "lif_peaks",
                pd.concat([app.lif_peaks, pd.DataFrame(red_peaks)], ignore_index=True),
            )
            projection_alignment = {
                **copy.deepcopy(app.alignment),
                "axis_shifts_sec": {"green_axis": 1.0, "red_axis": -3.0},
                "green_to_ms_shift_sec": 1.0,
                "red_to_ms_shift_sec": -3.0,
                "channel_time_axes": {
                    "G1": "green_axis",
                    "G2": "green_axis",
                    "R1": "red_axis",
                    "R2": "red_axis",
                },
            }
            projection_model = {
                **app.frozen_time_model(),
                "time_model_version": "tm-dual-preview",
                "ms_local_delta_sec": 2.0,
            }
            r1 = app.project_saved_relation(
                {
                    "annotation_id": "r1-relation",
                    "source": "manual_created",
                    "review_status": "accepted",
                    "review_stage": "cell_annotation",
                    "candidate_type": "manual_cell_pair",
                    "lif_channel": "R1",
                    "lif_peak_id": "r1-core",
                    "ms_event_id": "ms-cell-1",
                    "time_model_version": "tm-current",
                },
                alignment=projection_alignment,
                time_model=projection_model,
            )
            r2 = app.project_saved_relation(
                {
                    **r1,
                    "annotation_id": "r2-relation",
                    "lif_channel": "R2",
                    "lif_peak_id": "r2-core",
                    "time_model_version": "tm-current",
                },
                alignment=projection_alignment,
                time_model=projection_model,
            )

        self.assertAlmostEqual(r1["lif_plot_time_min"], 24.5 - 3.0 / 60.0)
        self.assertAlmostEqual(r2["lif_plot_time_min"], r1["lif_plot_time_min"])
        self.assertAlmostEqual(r1["ms_plot_time_min"], 24.5 + 2.0 / 60.0)
        self.assertAlmostEqual(r1["residual_sec"], 5.0)

    def test_events_qc_ui_exposes_compact_preview_apply_cancel_flow(self):
        self.assertIn('id="timelineAdjustToggle"', HTML)
        self.assertIn("调整时间轴", HTML)
        self.assertIn('id="timelineAdjustCancel"', HTML)
        self.assertIn('id="timelineAdjustApply"', HTML)
        self.assertIn("人工关系会保留", HTML)
        self.assertIn("未审核候选会重算", HTML)
        self.assertIn("偏差过大的旧关系会标记需复核", HTML)
        self.assertNotIn("人工关系会保留 ·", HTML)
        self.assertIn("'/api/timeline-adjustment-preview'", HTML)
        self.assertIn("'/api/timeline-adjustment-apply'", HTML)
        self.assertIn("scheduleTimelinePreviewRefresh()", HTML)


if __name__ == "__main__":
    unittest.main()
