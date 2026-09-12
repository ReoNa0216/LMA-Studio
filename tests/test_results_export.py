import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import anndata as ad
import numpy as np
import pandas as pd

from annotation_app.app import AppData
from annotation_app.results_export import export_results, zip_filename
from test_feature_umap import fixture
import test_ms_machine_import as machine_fixture
from test_canonical_project_storage import _tree_snapshot


class ResultsExportTest(unittest.TestCase):
    def make_app(self, root, confirmed=True):
        request, source, _ = fixture(root)
        rows = pd.read_parquet(source / 'event_rows.parquet')
        request['annotation_start_min'] = int(rows.current_apex_time_ns.iloc[1]) / 60_000_000_000
        for segment in request['calibration_protocol']['segments']:
            segment['boundaries_confirmed'] = confirmed
        with patch('annotation_app.app.run_preprocessing_script', side_effect=machine_fixture.MachineImportTest.lif_only):
            return AppData.create_project_from_raw_inputs(**request)

    def test_matrix_labels_join_by_id_and_preserve_original_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_app(root)
            original = ad.read_h5ad(app.feature_analysis.matrix_path / 'native_matrix.h5ad')
            for index, stage in [(1, 'cell_annotation'), (2, 'qc_survey')]:
                event = app.ms_events.iloc[index]
                lif = app.lif_peaks.iloc[0]
                app.store.upsert_review(annotation_id=f'human-{index}', source='manual_created', review_status='accepted',
                    payload=dict(review_stage=stage, candidate_type='manual_cell_pair' if index == 1 else 'manual_qc_anchor_set',
                                 label='cell' if index == 1 else 'QC', lif_channel=str(lif.channel), lif_peak_id=str(lif.peak_id),
                                 ms_event_id=str(event.event_id), ms_time_min=float(event.time_min), residual_sec=0.1),
                    action='engineering_fixture')
            prepared = app.prepare_annotations_export()
            prepared['rows'].reverse()
            selected = original.obs_names[1:].tolist()
            before = _tree_snapshot(app.project.project_dir)
            with patch.object(AppData, 'prepare_annotations_export', return_value=prepared):
                result = export_results(app, root, 'PC9-结果', include_matrix=True)
            with zipfile.ZipFile(result['path']) as archive:
                archive.extract('labeled_matrix.h5ad', root)
                record = json.loads(archive.read('export_record.json'))
                csv_frame = pd.read_csv(io.BytesIO(archive.read('cells_and_qc.csv'))).set_index('MS_event_id')
            exported = ad.read_h5ad(root / 'labeled_matrix.h5ad')
            self.assertEqual(exported.obs_names.tolist(), selected)
            self.assertEqual(exported.obs.Type.tolist(), ['LSK', 'QC'])
            self.assertEqual(exported.obs.Type.tolist(), csv_frame.loc[selected, 'Type'].tolist())
            self.assertEqual(exported.obs.is_qc.tolist(), [False, True])
            np.testing.assert_array_equal(exported.X, original.X[1:])
            pd.testing.assert_frame_equal(exported.var, original.var)
            self.assertEqual(record['excluded_front_rows'], 1)
            self.assertEqual(_tree_snapshot(app.project.project_dir), before)
            with self.assertRaisesRegex(ValueError, '同名'):
                export_results(app, root, 'PC9-结果', include_matrix=True)
            payload = app.store.get('human-1')
            for status in ['pending', 'rejected']:
                app.store.upsert_review(annotation_id='human-1', source='manual_created', review_status=status,
                                        payload=payload, action='engineering_fixture')
                result = export_results(app, root, status, include_matrix=True)
                with zipfile.ZipFile(result['path']) as archive:
                    archive.extract('labeled_matrix.h5ad', root)
                    frame = pd.read_csv(io.BytesIO(archive.read('cells_and_qc.csv'))).set_index('MS_event_id')
                matrix = ad.read_h5ad(root/'labeled_matrix.h5ad')
                self.assertEqual(matrix.obs.loc[selected[0], 'Type'], 'unknown')
                self.assertEqual(frame.loc[selected[0], 'Type'], 'unknown')


    def test_real_projection_unknown_csv_only_and_unconfirmed_matrix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_app(root, confirmed=False)
            before = _tree_snapshot(app.project.project_dir)
            with self.assertRaises(ValueError):
                export_results(app, root, 'blocked', include_matrix=True)
            self.assertFalse((root / 'blocked.zip').exists())
            result = export_results(app, root, 'table', include_matrix=False)
            with zipfile.ZipFile(result['path']) as archive:
                self.assertNotIn('labeled_matrix.h5ad', archive.namelist())
                frame = pd.read_csv(io.BytesIO(archive.read('cells_and_qc.csv')))
            self.assertTrue(frame.Type.eq('unknown').all())
            self.assertEqual(_tree_snapshot(app.project.project_dir), before)

    def test_missing_identity_or_failed_writer_never_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_app(root)
            prepared = app.prepare_annotations_export()
            prepared['rows'] = prepared['rows'][:1]
            with patch.object(AppData, 'prepare_annotations_export', return_value=prepared):
                with self.assertRaisesRegex(ValueError, '缺少对应'):
                    export_results(app, root, 'missing', include_matrix=True)
            with patch.object(ad.AnnData, 'write_h5ad', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    export_results(app, root, 'failed', include_matrix=True)
            self.assertFalse((root/'missing.zip').exists())
            self.assertFalse((root/'failed.zip').exists())
            self.assertFalse(list(root.glob('.lma-export-*')))
            for name in ('../bad', 'CON', 'NUL.zip', 'a/b', '', 'a?.zip'):
                with self.assertRaises(ValueError): zip_filename(name)

    def test_stale_native_coordinates_omitted_from_both_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_app(root)
            prepared = app.prepare_annotations_export()
            for row in prepared['rows']:
                row.update(UMAP1=10., UMAP2=20.)
            app.feature_analysis.state['view'] = 'native'
            with patch.object(AppData, 'prepare_annotations_export', return_value=prepared), patch.object(app.feature_analysis, 'scope_warning', return_value='范围已变化'):
                result = export_results(app, root, 'stale', include_matrix=True)
            with zipfile.ZipFile(result['path']) as archive:
                frame = pd.read_csv(io.BytesIO(archive.read('cells_and_qc.csv')))
                archive.extract('labeled_matrix.h5ad', root)
            matrix = ad.read_h5ad(root/'labeled_matrix.h5ad')
            self.assertTrue(frame[['UMAP1','UMAP2']].isna().all().all())
            self.assertTrue(np.isnan(matrix.obsm['X_umap']).all())
            self.assertFalse(result['coordinates_current'])


if __name__ == '__main__':
    unittest.main()
