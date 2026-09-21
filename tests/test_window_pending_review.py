"""Human window review: scope, real conflicts, durable decisions and atomicity."""
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from annotation_app.app import AppData, AnnotationStore, BadRequest, HTML, build_segmented_calibration_groups
from tests.test_protocol_regressions import create_hsc_app
from tests import test_pending_manual_cell_pairs as manual_pairs
from tests import test_saved_auto_review as saved_auto


class WindowPendingReviewTest(unittest.TestCase):
    def front_app(self, root):
        app = create_hsc_app(root)
        peaks = app.lif_peaks.copy()
        peaks["peak_tier"] = "core"
        peaks["height"] = peaks["raw"] = 1.0
        peaks["detector_version"] = 2
        peaks["detector_config_hash"] = "synthetic-current-standard"
        object.__setattr__(app, "lif_peaks", peaks)
        object.__setattr__(app, "lif_traces", pd.DataFrame([
            {"channel": channel, "label": channel, "detector": "green",
             "time_min": t, "raw": 0.0, "signal": 0.0}
            for channel in ("G1", "G2") for t in (0.0, 5.0)
        ]))
        object.__setattr__(app, "ms_scan", pd.DataFrame([
            {"scan_start_time_min": t, "pc34_760_max_intensity": 0.0,
             "qc_782_max_intensity": 0.0} for t in (0.0, 5.0)
        ]))
        app.alignment["qc_groups"] = build_segmented_calibration_groups(
            peaks, app.ms_events, calibration_protocol=app.calibration_protocol,
            channel_time_axes={"G1": "green_axis", "G2": "green_axis"},
            axis_shifts_sec={"green_axis": 5.0},
        )
        return app

    def test_front_qc_score_warnings_can_be_batch_accepted_without_changing_peaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.front_app(Path(tmp))
            for group in app.alignment["qc_groups"]["groups"]:
                group.update(conflict_count=3, axis_coherent=False, complete_anchor_set=False)
            before_peaks, before_ms = app.lif_peaks.copy(), app.ms_events.copy()
            result = app.accept_pending_auto_candidates_in_window(0, 2, "aligned")
            self.assertEqual(result["accepted_count"], 2)
            self.assertEqual(result["skipped_count"], 0)
            self.assertEqual(len(app.store.records()), 2)
            pd.testing.assert_frame_equal(before_peaks, app.lif_peaks)
            pd.testing.assert_frame_equal(before_ms, app.ms_events)
            self.assertTrue(all(row["review_status"] == "accepted" for row in app.store.records()))

    def test_front_qc_unresolved_ms_choice_remains_unaccepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.front_app(Path(tmp))
            app.alignment["qc_groups"]["groups"][0]["component_ambiguous"] = True
            result = app.accept_pending_auto_candidates_in_window(0, 2, "aligned")
            self.assertEqual(result["accepted_count"], 1)
            self.assertEqual(result["skipped"][0]["reason"], "ambiguous_ms_choice")

    def make_app(self, root):
        return manual_pairs.PendingManualCellPairContractTest().make_app(root)

    def pending(self, app, channel="G1", peak="g1-core", event="ms-cell-1"):
        return app.create_manual_cell_pair(channel, peak, event, review_status="pending")

    def accept(self, app, ids=None):
        return app.accept_pending_auto_candidates_in_window(
            24.0, 1.0, "aligned", "event_annotation", annotation_ids=ids,
        )

    def test_conflicting_relations_stay_pending_until_wrong_one_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            first = self.pending(app)
            second = self.pending(app, "G2", "g2-core")
            result = self.accept(app)
            self.assertEqual(result["accepted_count"], 0)
            self.assertEqual(result["skipped_count"], 2)
            self.assertTrue(all(row["reason"] == "competing_relation" for row in result["skipped"]))
            rejected = app.review_annotation(second["annotation_id"], "rejected")
            result = self.accept(app)
            self.assertEqual(result["accepted_annotation_ids"], [first["annotation_id"]])
            self.assertEqual(app.store.get(second["annotation_id"]), rejected)
            reopened = AnnotationStore(app.store.db_path)
            self.assertEqual(reopened.get(first["annotation_id"])["review_status"], "accepted")
            exported = pd.read_csv(io.StringIO(app.export_accepted_annotations_csv()["csv_text"]))
            self.assertNotEqual(exported.loc[exported.MS_event_id.eq("ms-cell-1"), "Type"].iloc[0], "unknown")

    def test_selected_visible_ids_only_and_repeated_click_preserves_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            first = self.pending(app)
            other = self.pending(app, "G1", "g1-weak", "ms-cell-2")
            result = self.accept(app, [first["annotation_id"]])
            self.assertEqual(result["accepted_count"], 1)
            self.assertEqual(app.store.get(other["annotation_id"])["review_status"], "pending")
            before = app.store.records()
            self.assertEqual(self.accept(app, [first["annotation_id"]])["accepted_count"], 0)
            self.assertEqual(before, app.store.records())
            with self.assertRaises(BadRequest):
                self.accept(app, ["not-on-this-screen"])
            self.assertEqual(before, app.store.records())

    def test_same_lif_peak_cannot_be_batch_assigned_to_two_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            self.pending(app)
            self.pending(app, event="ms-cell-2")
            result = self.accept(app)
            self.assertEqual(result["accepted_count"], 0)
            self.assertEqual(result["skipped_count"], 2)

    def test_accepted_competitor_outside_requested_ids_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            first = self.pending(app)
            accepted = app.create_manual_cell_pair("G2", "g2-core", "ms-cell-1")
            result = self.accept(app, [first["annotation_id"]])
            self.assertEqual(result["accepted_count"], 0)
            self.assertEqual(app.store.get(accepted["annotation_id"]), accepted)

    def test_batch_failure_rolls_back_every_annotation_and_audit_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            self.pending(app)
            self.pending(app, "G1", "g1-weak", "ms-cell-2")
            before = app.store.db_path.read_bytes()
            write = app.store._insert_audit_row
            calls = []
            def fail_second(conn, audit):
                calls.append(audit)
                if len(calls) == 2:
                    raise RuntimeError("simulated disk failure")
                return write(conn, audit)
            with patch.object(app.store, "_insert_audit_row", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "disk failure"):
                    self.accept(app)
            self.assertEqual(len(calls), 2)
            self.assertEqual(before, app.store.db_path.read_bytes())

    def test_concurrent_rejection_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            first = self.pending(app)
            save = app.store.accept_pending_batch
            def changed(*args, **kwargs):
                app.review_annotation(first["annotation_id"], "rejected")
                return save(*args, **kwargs)
            with patch.object(app.store, "accept_pending_batch", side_effect=changed):
                with self.assertRaisesRegex(BadRequest, "状态已改变"):
                    self.accept(app)
            self.assertEqual(app.store.get(first["annotation_id"])["review_status"], "rejected")

    def test_saved_auto_after_time_adjustment_keeps_physical_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, saved = saved_auto.SavedAutoReviewTest().prepare(Path(tmp), "pending")
            result = self.accept(app, [saved["annotation_id"]])
            self.assertEqual(result["accepted_count"], 1)
            actual = app.store.get(saved["annotation_id"])
            for key in ("annotation_id", "source", "lif_peak_id", "ms_event_id", "created_at"):
                self.assertEqual(actual[key], saved[key])
            self.assertEqual(actual["time_model_version"], app.frozen_time_model()["time_model_version"])

    def test_raw_view_and_local_calibration_cannot_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            self.pending(app)
            before = app.store.db_path.read_bytes()
            for mode, stage in (("raw", "event_annotation"), ("aligned", "local_calibration")):
                with self.assertRaises(BadRequest):
                    app.accept_pending_auto_candidates_in_window(24, 1, mode, stage)
            self.assertEqual(before, app.store.db_path.read_bytes())

    @unittest.skipUnless(shutil.which("node"), "Node required for production UI logic")
    def test_stage_render_keeps_batch_button_visible_and_updates_its_state(self):
        def fn(name):
            start = HTML.index("    function " + name + "(")
            return HTML[start:HTML.index("\n    function ", start + 15)]
        script = "\n".join([
            "const assert = require('node:assert/strict');",
            """
            const nodes = new Map();
            const el = id => {
              if (!nodes.has(id)) nodes.set(id, {style:{}, setAttribute(){}, parentNode:{insertBefore(){}}});
              return nodes.get(id);
            };
            const document = {querySelectorAll: () => []};
            const state = {stage:'event_annotation', manualAnnotationKind:'cell', eventFilter:'all',
              axisFineTuneShifts:null, timelineAdjustOpen:false, actionBusy:false,
              current:{time_model:{status:'frozen'}, project_config:{post_qc_strategy:{mode:'disabled'}}}};
            let boundariesReady = true;
            let pending = [{}, {}, {}];
            const calibrationBoundariesConfirmed = () => boundariesReady;
            const batchAcceptableAutoCandidatesInMainWindow = () => pending;
            const renderTimelineAdjustmentPanel = () => {};
            const qcAnchorChannels = () => ['G1', 'R1'];
            const postQcModeLabel = mode => mode;
            """,
            fn("renderStagePanels"), fn("updateAcceptWindowButton"),
            """
            function render() { renderStagePanels(); updateAcceptWindowButton(); }
            function checkReviewVisible() {
              assert.equal(el('reviewPanel').style.display, 'block');
              assert.notEqual(el('acceptWindow').style.display, 'none');
            }
            render();
            checkReviewVisible();
            assert.equal(el('acceptWindow').textContent, '接受本屏待审（3）');
            assert.equal(el('acceptWindow').disabled, false);
            pending = [];
            render();
            checkReviewVisible();
            assert.equal(el('acceptWindow').disabled, true);
            state.stage = 'qc_calibration'; pending = [{}];
            render(); checkReviewVisible();
            assert.equal(el('acceptWindow').disabled, false);
            state.stage = 'event_annotation';
            state.current.project_config.post_qc_strategy.mode = 'manual';
            state.manualAnnotationKind = 'qc'; state.eventFilter = 'qc';
            render(); checkReviewVisible();
            assert.equal(el('acceptWindow').disabled, false);
            state.current.time_model.status = 'draft';
            render();
            assert.equal(el('reviewPanel').style.display, 'none');
            assert.equal(el('acceptWindow').disabled, true);
            state.current.time_model.status = 'frozen';
            render(); checkReviewVisible();
            state.stage = 'local_calibration';
            render();
            assert.equal(el('reviewPanel').style.display, 'none');
            boundariesReady = false; state.stage = 'qc_calibration';
            render();
            assert.equal(el('reviewPanel').style.display, 'none');
            assert.equal(el('acceptWindow').disabled, true);
            """,
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for production UI logic")
    def test_ui_hides_conflicts_and_sends_only_screen_pending_ids(self):
        def fn(name):
            start = HTML.index("    function " + name + "(")
            return HTML[start:HTML.index("\n    function ", start + 15)]
        script = "\n".join([
            "const assert = require('node:assert/strict');",
            "const state = {stage:'event_annotation', eventFilter:'all', showRejected:true, showCrossChannelConflicts:false, current:{}};",
            "const rowId = r => r.annotation_id;",
            fn("isPendingRelationConflict"), fn("relationVisibleForReview"),
            fn("candidateRows"), fn("manualBelongsToStage"), fn("eventRowKind"), fn("eventRowMatchesFilter"),
            fn("batchAcceptableAutoCandidatesInMainWindow"), fn("stageCounts"),
            """
            const base = {source:'auto_candidate', review_stage:'cell_annotation', candidate_type:'cell_high_confidence', review_status:'pending'};
            const a = {...base, annotation_id:'a', batch_accept_eligible:true};
            const b = {...base, annotation_id:'b', batch_accept_eligible:false, batch_accept_block_reason:'competing_relation'};
            const c = {...a, annotation_id:'c', review_status:'rejected'};
            state.current.cell_candidates = [a,b,c];
            assert.deepEqual(candidateRows().map(rowId), ['a','c']);
            assert.deepEqual(batchAcceptableAutoCandidatesInMainWindow().map(rowId), ['a']);
            assert.equal(stageCounts().pending, 1);
            state.showCrossChannelConflicts = true;
            assert.deepEqual(candidateRows().map(rowId), ['a','b','c']);
            assert.deepEqual(batchAcceptableAutoCandidatesInMainWindow().map(rowId), ['a']);
            assert.equal(stageCounts().pending, 1);
            state.eventFilter = 'qc';
            assert.equal(batchAcceptableAutoCandidatesInMainWindow().length, 0);
            """,
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("annotation_ids: rows.map(row => String(rowId(row)))", HTML)
        # Compile the entire production script, not only the extracted helpers.
        scripts = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'));"],
                                input=scripts, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
