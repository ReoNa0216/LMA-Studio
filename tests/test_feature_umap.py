import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import anndata as ad
import numpy as np
import pandas as pd
from flame_ms_core.exchange import read_event_package
from flame_ms_core.export import export_machine_contract

from annotation_app.app import AppData, ProjectPaths, RequestActivity
from annotation_app.feature_umap import (AnalysisJob, FeatureAnalysis, compute_embedding,
    sha256, unpack_results, validate_matrix, validated_parameters)
from test_ms_machine_import import MachineImportTest
from test_canonical_project_storage import _tree_snapshot


def fixture(root):
    request = MachineImportTest().prepare(root)
    package = read_event_package(request['ms_event_package_path'])
    m = package.manifest
    records = package.events.to_dict('records')
    for row in records:
        row['status'] = 'accepted'
    output = root / 'features'
    package_dir = output / 'source_events'
    export_machine_contract(records, package.events, package_dir,
        source_fingerprint=m['source_fingerprint'], detector_version=m['detector']['version'],
        parameter_hash=m['detector']['parameter_hash'], generation_id=m['detector']['generation_id'],
        analysis_start_ns=m['analysis_range']['start_ns'], analysis_end_ns=m['analysis_range']['end_ns'],
        scientific_settings=m['scientific_settings'])
    package = read_event_package(package_dir)
    rows = pd.DataFrame([dict(event_id=e.event_id, event_version=package.event_versions[e.event_id],
        source_sha256=m['source_fingerprint']['sha256'], current_apex_time_ns=e.current_apex_time_ns,
        apex_scan_id=e.current_scan_id) for e in package.events.itertuples()]).set_index('event_id', drop=False)
    rows.index.name = 'matrix_row_id'
    axis = pd.DataFrame({'feature_id':['f1','f2','f3','f4']}).set_index('feature_id',drop=False)
    matrix = ad.AnnData(np.array([[1.,2.,np.nan,4.],[3.,4.,5.,6.],[4.,5.,6.,7.]]), obs=rows, var=axis)
    matrix.write_h5ad(output / 'native_matrix.h5ad')
    rows.to_parquet(output / 'event_rows.parquet')
    axis.to_parquet(output / 'feature_axis.parquet')
    record = dict(interface='ms-event-studio-feature-v1', status='passed', complete=True,
        source_event_manifest_sha256=package.manifest_sha256, events=3, features=4,
        artifacts={p.relative_to(output).as_posix():dict(bytes=p.stat().st_size,sha256=sha256(p)) for p in output.rglob('*') if p.is_file()})
    (output / 'execution_record.json').write_text(json.dumps(record),encoding='utf-8')
    archive = root / 'analysis.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for path in output.rglob('*'):
            if path.is_file(): z.write(path, 'features/' + path.relative_to(output).as_posix())
    request['ms_event_package_path'] = archive
    return request, output, archive


class FeatureUmapTest(unittest.TestCase):
    def test_zip_new_project_identity_reopen_and_no_annotation_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            request, source, archive = fixture(Path(temp))
            with patch('annotation_app.app.run_preprocessing_script', side_effect=MachineImportTest.lif_only):
                app = AppData.create_project_from_raw_inputs(**request)
            self.assertEqual(app.feature_analysis.overview()['events'],3)
            self.assertEqual(app.cell_event_map.ms_event_id.tolist(), ['EV_import_0','EV_import_1','EV_import_2'])
            before = _tree_snapshot(app.project.project_dir)
            reopened = AppData.load(app.project)
            self.assertTrue(reopened.feature_analysis.overview()['available'])
            self.assertFalse(reopened.cell_event_map_coordinates_available())
            self.assertEqual(_tree_snapshot(app.project.project_dir),before)
            self.assertEqual(sha256(source/'native_matrix.h5ad'),sha256(reopened.feature_analysis.matrix_path/'native_matrix.h5ad'))
            # Same input is reused, not duplicated.
            reopened.feature_analysis.import_matrix(source)
            self.assertEqual(_tree_snapshot(app.project.project_dir),before)
            bad = pd.read_parquet(source/'event_rows.parquet')
            bad.loc[bad.index[0],'event_version'] = 'wrong-version'
            bad.to_parquet(source/'event_rows.parquet')
            with self.assertRaises(ValueError): reopened.feature_analysis.import_matrix(source)
            self.assertEqual(_tree_snapshot(app.project.project_dir),before)

    def test_umap_reference_copy_parameters_and_finite_coordinates(self):
        import scanpy as sc
        rng = np.random.default_rng(22)
        original = rng.uniform(1,100,(12,8))
        original[0,0] = np.nan
        matrix = ad.AnnData(original.copy())
        actual, record = compute_embedding(matrix, {})
        self.assertEqual(record['actual'],dict(n_pcs=7,n_neighbors=11,random_state=1))
        np.testing.assert_array_equal(matrix.X,original)
        reference = ad.AnnData(np.nan_to_num(original,copy=True))
        sc.pp.pca(reference,n_comps=7,svd_solver='arpack',random_state=1)
        sc.pp.neighbors(reference,n_neighbors=11,n_pcs=7,use_rep='X_pca',metric='euclidean',random_state=1)
        sc.tl.umap(reference,random_state=1,n_components=2,min_dist=.5,spread=1.,init_pos='spectral')
        np.testing.assert_array_equal(actual[['UMAP1','UMAP2']].to_numpy(),reference.obsm['X_umap'])
        with self.assertRaises(ValueError): validated_parameters({},(3,10))
        with self.assertRaises(ValueError): validated_parameters({'n_neighbors':True},(12,8))

    def test_independent_native_coordinates_saved_and_base_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            request, source, _ = fixture(Path(temp))
            with patch('annotation_app.app.run_preprocessing_script', side_effect=MachineImportTest.lif_only):
                app = AppData.create_project_from_raw_inputs(**request)
            base = app.cell_event_map.copy()
            protected = {p:sha256(app.project.project_dir/p) for p in ['lifms_project.json','annotations/annotation.sqlite','data/cell_event_map.csv']}
            xy = pd.DataFrame(dict(ms_event_id=base.ms_event_id,UMAP1=[1.,2.,3.],UMAP2=[4.,5.,6.]))
            with patch('annotation_app.feature_umap.compute_embedding',return_value=(xy,{'requested':{},'actual':{}})):
                app.feature_analysis.calculate({})
            self.assertTrue(app.cell_event_map_coordinates_available())
            self.assertEqual(app.projected_cell_event_map_state()['points'][0]['UMAP1'],1.)
            pd.testing.assert_frame_equal(app.cell_event_map,base)
            reopened = AppData.load(app.project)
            self.assertEqual(reopened.projected_cell_event_map_state()['points'][0]['UMAP2'],4.)
            saved_state = dict(reopened.feature_analysis.state)
            changed_xy = xy.copy()
            changed_xy['UMAP1'] += 100
            with patch('annotation_app.feature_umap.compute_embedding',return_value=(changed_xy,{'requested':{'random_state':2},'actual':{}})), patch.object(reopened.feature_analysis,'_save',side_effect=OSError('disk full')):
                with self.assertRaises(OSError): reopened.feature_analysis.calculate({'random_state':2})
            self.assertEqual(reopened.feature_analysis.state,saved_state)
            self.assertEqual(reopened.projected_cell_event_map_state()['points'][0]['UMAP1'],1.)
            self.assertEqual(AppData.load(app.project).projected_cell_event_map_state()['points'][0]['UMAP1'],1.)
            reopened.feature_analysis.select_view('base')
            self.assertFalse(reopened.cell_event_map_coordinates_available())
            for path,digest in protected.items(): self.assertEqual(sha256(app.project.project_dir/path),digest)

    def test_zip_paths_and_legacy_binding_fail_before_project_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request, source, archive = fixture(root)
            with zipfile.ZipFile(archive,'a') as z: z.writestr('../escape','bad')
            with self.assertRaises(ValueError):
                with unpack_results(archive): pass
            self.assertFalse((root/'escape').exists())
            class Legacy:
                manifest = {}
            with self.assertRaisesRegex(ValueError,'独立项目'): validate_matrix(source,Legacy())

    def test_job_blocks_duplicate_and_tracks_desktop_close(self):
        with tempfile.TemporaryDirectory() as temp:
            request, _, _ = fixture(Path(temp))
            with patch('annotation_app.app.run_preprocessing_script', side_effect=MachineImportTest.lif_only):
                app = AppData.create_project_from_raw_inputs(**request)
            entered, release, finished = threading.Event(),threading.Event(),threading.Event()
            def calculate(*args):
                entered.set()
                release.wait(10)
                finished.set()
            activity = RequestActivity()
            job = AnalysisJob()
            with patch.object(app.feature_analysis,'calculate',side_effect=calculate):
                job.start(app,{},activity)
                self.assertTrue(entered.wait(5))
                self.assertTrue(activity.is_busy())
                with self.assertRaises(ValueError): job.start(app,{},activity)
                release.set()
                self.assertTrue(finished.wait(5))


if __name__ == '__main__': unittest.main()
