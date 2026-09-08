"""A reviewed relation is durable even when it no longer qualifies automatically."""
import io
import shutil
import subprocess
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from annotation_app.app import AppData, AnnotationStore, BadRequest, HTML, ProjectPaths
from tests.test_protocol_regressions import create_legacy_project
from tests.test_v050_timeline_adjustment import make_app, sqlite_state


class SavedAutoReviewTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute production SVG style functions')
    def test_accepted_lines_stay_solid_when_time_projection_needs_review(self):
        def function(name, following):
            return HTML[HTML.index('    function ' + name + '('):HTML.index('    function ' + following + '(')]
        script = '\n'.join([
            "const assert = require('node:assert/strict'); const drawn = [];",
            "const state = {current: {time_mode: 'aligned'}, selectedCandidateId: null, showRejected: true};",
            "const manualBelongsToStage = () => true; const colorForChannel = () => '#008080';",
            "const visibleCellCandidates = () => state.current.cell_candidates || [];",
            "const svgEl = (tag, attrs) => ({attrs}); const appendLineWithHitTarget = (svg, line) => drawn.push(line.attrs);",
            "const qcAnchorMarkerPoints = () => ({}); const appendQcConnectorPolyline = (svg, markers, row, detail, style) => drawn.push({'stroke-dasharray': style.dash});",
            function('candidateLineStyle', 'drawTrackTimeAxis'),
            function('drawCellCandidates', 'drawManualAnnotations'),
            function('drawManualCellAnnotations', 'isAcceptedQcSurveyRow'),
            function('isAcceptedQcSurveyRow', 'drawAcceptedQcSurveyAnnotations'),
            function('drawAcceptedQcSurveyAnnotations', 'candidateLineStyle'),
            """
            for (const needs_review of [false, true]) {
              for (const source of ['manual_created', 'auto_candidate']) {
                for (const review_status of ['accepted', 'pending', 'rejected']) {
                  const row = {annotation_id: 'saved', lif_peak_id: 'lif', ms_event_id: 'ms',
                               lif_channel: 'G1', source, review_status, needs_review};
                  state.current.annotations = [row];
                  drawManualCellAnnotations({}, {'lif:lif': {x: 1, y: 2}, 'ms760:ms': {x: 3, y: 4}});
                  const line = drawn.pop();
                  assert.equal(line['stroke-dasharray'] === '', review_status === 'accepted');
                  if (review_status === 'rejected') assert.equal(line.stroke, '#98a2b3');
                  state.current.cell_candidates = [row];
                  drawCellCandidates({}, {'lif:lif': {x: 1, y: 2}, 'ms760:ms': {x: 3, y: 4}});
                  const autoLine = drawn.pop();
                  assert.equal(autoLine['stroke-dasharray'] === '', review_status === 'accepted');
                  if (review_status === 'rejected') assert.equal(autoLine.stroke, '#98a2b3');
                  if (review_status === 'accepted') {
                    state.current.annotations = [];
                    state.current.post_qc_candidates = [];
                    state.current.cell_qc_anchors = [{...row, candidate_type: 'qc_survey_signature'}];
                    drawAcceptedQcSurveyAnnotations({}, {});
                    assert.equal(drawn.pop()['stroke-dasharray'], '');
                    state.current.post_qc_candidates = [row];
                    drawAcceptedQcSurveyAnnotations({}, {});
                    assert.equal(drawn.length, 0);
                  }
                }
              }
            }
            """,
        ])
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('drawAcceptedQcSurveyAnnotations(svg, markerPositions);', function('draw', 'trackShiftSec'))

    def prepare(self, root, status='accepted'):
        app = make_app(root)
        ms = app.ms_events.copy()
        ms['pc34_760_apex'] = 20000.0
        object.__setattr__(app, 'ms_events', ms)
        candidate = next(r for r in app.build_cell_candidates(24, 25, 'aligned')
                         if r['lif_channel'] == 'G1' and r['ms_event_id'] == 'ms-cell-1')
        saved = app.review_annotation(candidate['candidate_id'], status, 24, 25, 'aligned')
        preview = app.timeline_adjustment_preview({'green_axis': 2.0}, ms_local_delta_sec=0.0)
        app.apply_timeline_adjustment({'green_axis': 2.0}, ms_local_delta_sec=0.0,
                                      expected_preview_hash=preview['preview_hash'])
        self.assertTrue(app.project_saved_relation(saved)['needs_review'])
        self.assertNotIn(saved['annotation_id'],
                         {r['candidate_id'] for r in app.build_cell_candidates(24, 25, 'aligned')})
        return app, saved

    def test_all_saved_statuses_can_be_reviewed_again_without_replacing_peak(self):
        for initial in ('accepted', 'pending', 'rejected'):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                app, saved = self.prepare(Path(tmp), initial)
                before = sqlite_state(app.store.db_path)
                for status in ('rejected', 'pending', 'accepted'):
                    reviewed = app.review_annotation(saved['annotation_id'], status, 24, 25, 'aligned')
                    for key in ('annotation_id', 'candidate_id', 'source', 'label', 'lif_peak_id',
                                'lif_channel', 'ms_event_id', 'scan_id', 'created_at', 'confidence_mode'):
                        self.assertEqual(reviewed[key], saved[key], key)
                    self.assertEqual(reviewed['review_status'], status)
                    self.assertAlmostEqual(reviewed['residual_sec'], -2.0)
                    self.assertEqual(reviewed['time_model_version'], app.frozen_time_model()['time_model_version'])
                    reopened = AnnotationStore(app.store.db_path).get(saved['annotation_id'])
                    self.assertEqual(reopened, reviewed)
                    csv = pd.read_csv(io.StringIO(app.export_accepted_annotations_csv()['csv_text']))
                    event = csv[csv.MS_event_id.eq('ms-cell-1')].iloc[0]
                    self.assertEqual(event['Type'], 'LSK' if status == 'accepted' else 'unknown')
                after = sqlite_state(app.store.db_path)
                for table in ('time_models', 'time_model_audit_events', 'project_config'):
                    self.assertEqual(after[table], before[table])
                with sqlite3.connect(app.store.db_path) as conn:
                    audit = conn.execute('SELECT action, payload_json FROM audit_events ORDER BY rowid DESC LIMIT 3').fetchall()
                self.assertEqual([r[0] for r in audit], ['auto_candidate_accepted', 'auto_candidate_pending', 'auto_candidate_rejected'])
                self.assertEqual(after['audit_events'][:len(before['audit_events'])], before['audit_events'])

    def test_unsaved_stale_candidate_is_still_rejected_without_writing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app, _ = self.prepare(Path(tmp))
            before = sqlite_state(app.store.db_path)
            with self.assertRaisesRegex(BadRequest, 'Unknown or inactive'):
                app.review_annotation('cell:G2:g2-core:ms-cell-1', 'accepted', 24, 25, 'aligned')
            self.assertEqual(sqlite_state(app.store.db_path), before)

    def test_saved_relation_acceptance_still_checks_conflicting_label(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app, saved = self.prepare(Path(tmp), 'pending')
            app.create_manual_cell_pair('G2', 'g2-core', 'ms-cell-1')
            before = sqlite_state(app.store.db_path)
            with self.assertRaisesRegex(BadRequest, '只能有一个|冲突'):
                app.review_annotation(saved['annotation_id'], 'accepted', 24, 25, 'aligned')
            self.assertEqual(sqlite_state(app.store.db_path), before)

    def test_missing_physical_peak_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app, saved = self.prepare(Path(tmp), 'pending')
            object.__setattr__(app, 'lif_peaks', app.lif_peaks[app.lif_peaks.peak_id.ne('g1-core')])
            before = sqlite_state(app.store.db_path)
            with self.assertRaisesRegex(BadRequest, '原始.*无法'):
                app.review_annotation(saved['annotation_id'], 'accepted', 24, 25, 'aligned')
            self.assertEqual(sqlite_state(app.store.db_path), before)

    def test_saved_auto_outside_visible_window_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app, saved = self.prepare(Path(tmp))
            before = sqlite_state(app.store.db_path)
            with self.assertRaisesRegex(BadRequest, 'active window'):
                app.review_annotation(saved['annotation_id'], 'rejected', 1, 2, 'aligned')
            self.assertEqual(sqlite_state(app.store.db_path), before)

    def test_front_qc_auto_still_uses_calibration_candidate_validation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app, saved = self.prepare(Path(tmp))
            row = {**saved, 'review_stage': 'qc_calibration', 'annotation_id': 'auto_qc:missing'}
            with patch.object(type(app.store), 'get', return_value=row), \
                 patch.object(type(app), 'payload_from_auto_candidate_id', side_effect=BadRequest('front guard')):
                with self.assertRaisesRegex(BadRequest, 'front guard'):
                    app.review_annotation(row['annotation_id'], 'rejected', 0, 6, 'aligned')

    def test_saved_post_qc_can_be_reviewed_but_disabled_strategy_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project, db = create_legacy_project(Path(tmp))
            app = AppData.load(ProjectPaths.from_args(project_dir=str(project), annotation_db=str(db)))
            app.update_project_config({'annotation_start_min': 15.0})
            app.store.upsert_time_model({**app.active_time_model(), 'status': 'frozen'}, action='test_freeze')
            strategy = {**app.project_config()['post_qc_strategy'], 'reference_channels': ['G2']}
            app.update_project_config({'post_qc_strategy': strategy})
            candidate = app.build_post_qc_candidates(19, 21, 'aligned')[0]
            saved = app.review_annotation(candidate['candidate_id'], 'accepted', 19, 21, 'aligned')
            old_model = app.frozen_time_model()
            app.store.upsert_time_model({**old_model, 'time_model_version': 'test-shifted-qc',
                                         'ms_local_delta_sec': 5.0}, action='test_shift')
            self.assertNotIn(saved['candidate_id'], {r['candidate_id'] for r in app.build_post_qc_candidates(19, 21, 'aligned')})
            for status in ('rejected', 'pending', 'accepted'):
                reviewed = app.review_annotation(saved['annotation_id'], status, 19, 21, 'aligned')
                for key in ('source', 'ms_event_id', 'lif_anchor_peak_ids', 'label'):
                    self.assertEqual(reviewed[key], saved[key])
            app.update_project_config({'post_qc_strategy': {'mode': 'disabled'}})
            before = sqlite_state(db)
            with self.assertRaisesRegex(BadRequest, 'post_qc_strategy'):
                app.review_annotation(saved['annotation_id'], 'accepted', 19, 21, 'aligned')
            self.assertEqual(sqlite_state(db), before)
            self.assertEqual(app.review_annotation(saved['annotation_id'], 'rejected', 19, 21, 'aligned')['review_status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
