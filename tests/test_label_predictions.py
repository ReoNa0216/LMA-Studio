import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from annotation_app import label_predictions as lp


def evidence():
    return {"schema": lp.SCHEMA, "binding": "source-v1", "evidence_id": "window1",
            "start_min": 25., "window_min": .5, "context_sec": 2.,
            "channels": {"G1": "A", "R1": "B"},
            "lif_candidates": [{"lif_candidate_id": "g1", "label": "A"},
                               {"lif_candidate_id": "g2", "label": "A"},
                               {"lif_candidate_id": "r1", "label": "B"}],
            "ms_events": [{"ms_event_id": i, "event_version": "v1", "target": True}
                          for i in ("ms1", "ms2")], "traces": {}}


def rows():
    return [{"ms_event_id": i, "event_version": "v1", "label": "A",
             "lif_candidate_ids": ["g1", "g2"], "status": "certain", "reason": "same class evidence"}
            for i in ("ms1", "ms2")]


def batch():
    return {"run_id": "visual-1", "evidence_id": "window1", "predictions": rows(),
            "method": {"route": "codex_visual", "model": "test", "prompt_version": lp.PROMPT_VERSION,
                       "started_at": "2026-09-11T00:00:00+00:00", "elapsed_seconds": 30.}}


class LabelPredictionTest(unittest.TestCase):
    def test_window_allowlist_excludes_labels_barcode_and_other_routes(self):
        event = dict(event_id='ms1', revision=2, plot_time_min=25.1, raw_time_min=25.1,
                     pc34_760_apex=100., in_cell_event_map=True, barcode='H580', accepted_label='SECRET')
        peak = dict(peak_id='g1', channel='G1', plot_time_min=25.1, raw_time_min=25.,
                    display_y=2., label='UNTRUSTED ROW LABEL', annotation='SECRET')
        window = dict(start_min=24.9, end_min=25.6, lif_peaks=[peak], ms_events=[event],
                      lif_traces={'G1':[{'x':25.,'y':1.}]},
                      ms_traces={'pc34_760_linear':[{'x':25.,'y':100.}], 'barcode':[123]},
                      annotations=['SECRET'], cell_candidates=['SECRET'], umap=['SECRET'])
        app = SimpleNamespace(active_time_model=lambda:dict(annotation_start_min=24.,time_model_version='frozen1'),
                              window=lambda *a,**kw: window,
                              acquisition_layout={'lif_channels':[{'channel':'G1','identity_prior':'A'}]},
                              manifest={'intermediate_tables':{'ms_events':{'sha256':'xyz'}}})
        with patch.object(lp, 'project_binding', return_value='bound'):
            value = lp.build_evidence(app,25.,.5)
        encoded = json.dumps(value)
        for forbidden in ('SECRET', 'barcode', 'accepted_label', 'cell_candidates', 'umap', 'UNTRUSTED'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(value['lif_candidates'][0]['label'], 'A')
        self.assertTrue(value['ms_events'][0]['event_version'].startswith('revision:2:'))

    def test_same_class_multiple_candidates_and_shared_support_allowed(self):
        self.assertEqual(lp.validate_rows(evidence(), rows()), rows())

    def test_unknown_duplicate_outside_and_missing_event_rejected(self):
        cases = [rows()[:1], rows()+rows()[:1]]
        for key, value in (("ms_event_id", "outside"), ("event_version", "v2"),
                           ("lif_candidate_ids", ["unknown"]), ("lif_candidate_ids", ["g1", "g1"]),
                           ("lif_candidate_ids", ["g1", "r1"]), ("label", "other"),
                           ("status", "accepted"), ("reason", "")):
            changed = rows()
            changed[0][key] = value
            cases.append(changed)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                lp.validate_rows(evidence(), value)

    def test_manual_status_field_cannot_be_smuggled(self):
        value = rows()
        value[0]["review_status"] = "accepted"
        with self.assertRaises(ValueError):
            lp.validate_rows(evidence(), value)

    def test_uncertain_accepts_cross_class_evidence_but_no_label(self):
        value = rows()
        value[0].update(status="uncertain", label=None, lif_candidate_ids=["g1", "r1"])
        lp.validate_rows(evidence(), value)
        value[0]["label"] = "A"
        with self.assertRaises(ValueError):
            lp.validate_rows(evidence(), value)

    def test_save_reopen_idempotence_conflict_and_db_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "annotation.sqlite"
            db.write_bytes(b"original human annotation database")
            app = SimpleNamespace(project=SimpleNamespace(annotation_db_path=db))
            with patch.object(lp, "build_evidence", return_value=evidence()):
                path = lp.save_batch(app, evidence(), batch())
                before = path.read_bytes()
                self.assertEqual(lp.save_batch(app, evidence(), batch()).read_bytes(), before)
                conflicting = batch()
                conflicting["predictions"][0].update(status="uncertain", label=None)
                with self.assertRaises(ValueError):
                    lp.save_batch(app, evidence(), conflicting)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(db.read_bytes(), b"original human annotation database")
                result = lp.read_json(path)
                self.assertEqual(result["review_status"], "unreviewed_prediction")
                self.assertEqual(len(result["windows"]), 1)
                self.assertFalse(list(path.parent.glob('*.lock')))

    def test_stale_evidence_and_tampered_input_rejected_before_write(self):
        changed = evidence()
        changed["binding"] = "changed"
        with patch.object(lp, "build_evidence", return_value=changed), self.assertRaises(ValueError):
            lp.save_batch(None, evidence(), batch())

    def test_different_method_cannot_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            app = SimpleNamespace(project=SimpleNamespace(annotation_db_path=Path(folder)/'human.sqlite'))
            with patch.object(lp, "build_evidence", return_value=evidence()):
                path = lp.save_batch(app, evidence(), batch())
                before = path.read_bytes()
                value = batch()
                value["method"]["model"] = "different"
                with self.assertRaises(ValueError):
                    lp.save_batch(app, evidence(), value)
                self.assertEqual(path.read_bytes(), before)

    def test_run_path_traversal_rejected(self):
        for value in ("../human", "C:/human", "x/y", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lp.safe_run_id(value)

    def test_failed_publish_preserves_previous_completed_result(self):
        with tempfile.TemporaryDirectory() as folder:
            app = SimpleNamespace(project=SimpleNamespace(annotation_db_path=Path(folder)/'human.sqlite'))
            with patch.object(lp, "build_evidence", return_value=evidence()):
                path = lp.save_batch(app, evidence(), batch())
            before = path.read_bytes()
            other = evidence()
            other["evidence_id"] = "window2"
            value = batch()
            value["evidence_id"] = "window2"
            with patch.object(lp, "build_evidence", return_value=other), patch.object(lp.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    lp.save_batch(app, other, value)
            self.assertEqual(path.read_bytes(), before)

    def test_html_escapes_prediction_reason(self):
        value = batch()
        value['predictions'][0]['reason'] = '<script>alert(1)</script>'
        page = lp.review_html({**value, 'windows': []})
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)


if __name__ == '__main__':
    unittest.main()
