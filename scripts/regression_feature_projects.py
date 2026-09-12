"""Exercise a real MS analysis ZIP through a new LMA project and native UMAP.

Reads an existing LMA manifest for raw inputs and channel configuration only.
Original labels, original coordinates and time models are never copied into
the new event population. Existing-project preservation is tested separately.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
import time

import anndata as ad
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from annotation_app.app import AppData, ProjectPaths
from annotation_app.feature_umap import compute_embedding, sha256, unpack_results


def tree(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob('*')
            if p.is_file() and not p.name.endswith(('-wal', '-shm', '-journal'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--existing', action='store_true', help='Validate a completed test project without rebuilding raw tables')
    args = parser.parse_args()
    root = args.output.resolve()
    if root == args.source.resolve() or args.source.resolve() in root.parents:
        raise ValueError('Test output must be outside the original project')
    source_before = tree(args.source)
    manifest = json.loads((args.source / 'lifms_project.json').read_text(encoding='utf-8'))
    ms = json.loads((root / 'ms-check.json').read_text(encoding='utf-8'))
    inputs = []
    for channel in manifest['acquisition_layout']['lif_channels']:
        key = channel['input_id'].removesuffix('_raw')
        path = Path(manifest['raw_inputs'][key]['path'])
        if not path.is_file():
            raise FileNotFoundError(path)
        inputs.append({**channel, 'key': key, 'path': path})
    calibration = copy.deepcopy(manifest['calibration_protocol'])
    calibration['boundaries_confirmed'] = False
    for segment in calibration['segments']:
        segment['boundaries_confirmed'] = False
    print(root.name, 'creating LMA from real MS ZIP', flush=True)
    started = time.monotonic()
    target = root / 'LMA'
    if args.existing:
        app = AppData.load(ProjectPaths.from_args(project_dir=str(target)))
        creation_seconds = None
    else:
        app = AppData.create_project_from_raw_inputs(project_dir=target,
            ms_path=Path(ms['raw']), lif_inputs=inputs,
            ms_event_package_path=Path(ms['zip']), calibration_protocol=calibration,
            post_qc_strategy=manifest['post_qc_strategy'],
            annotation_start_min=manifest['annotation_config']['annotation_start_min'],
            local_delta_seed_window_min=manifest['annotation_config']['local_delta_seed_window_min'])
        creation_seconds = time.monotonic() - started
    assert isinstance(app, AppData)
    analysis = app.feature_analysis
    assert sha256(analysis.matrix_path / 'native_matrix.h5ad') == ms['matrix_sha256']
    incoming = ad.read_h5ad(ms['matrix_path'])
    attached = ad.read_h5ad(analysis.matrix_path / 'native_matrix.h5ad')
    np.testing.assert_array_equal(incoming.X, attached.X)
    pd.testing.assert_frame_equal(incoming.obs, attached.obs)
    pd.testing.assert_frame_equal(incoming.var, attached.var)
    baseline = tree(target)
    original_event_ids = app.cell_event_map.ms_event_id.tolist()
    started = time.monotonic()
    analysis.calculate({}, lambda phase: print(root.name, phase, flush=True))
    umap_seconds = time.monotonic() - started
    first = analysis.coordinates.copy()
    assert len(first) == ms['events'] and np.isfinite(first[['UMAP1','UMAP2']]).all().all()
    assert first.ms_event_id.tolist() == incoming.obs_names.tolist()
    for name, digest in baseline.items():
        if name != 'analysis/feature_umap/state.json':
            assert sha256(target / name) == digest, name
    assert app.cell_event_map.ms_event_id.tolist() == original_event_ids
    expected = app.projected_cell_event_map_state()
    assert all(p['classification'] == 'unknown' for p in expected['points'])
    for point in expected['points']:
        row = app.export_unknown_event_row(point, export_id='read-only-check', exported_at='2026-09-12')
        assert row['Type'] == 'unknown'
        assert row['UMAP1'] == point['UMAP1'] and row['UMAP2'] == point['UMAP2']
    started = time.monotonic()
    reopened = AppData.load(ProjectPaths.from_args(project_dir=str(target)))
    assert reopened.projected_cell_event_map_state() == expected
    reopen_seconds = time.monotonic() - started
    reopened.feature_analysis.select_view('base')
    assert reopened.display_event_map().equals(reopened.cell_event_map)
    reopened.feature_analysis.select_view('native')
    with unpack_results(Path(ms['zip'])) as package:
        reopened.feature_analysis.import_matrix(package)
    assert reopened.projected_cell_event_map_state() == expected
    started = time.monotonic()
    repeated, _ = compute_embedding(attached, {})
    repeat_seconds = time.monotonic() - started
    pd.testing.assert_frame_equal(first, repeated)
    assert tree(args.source) == source_before
    assert sha256(analysis.matrix_path / 'native_matrix.h5ad') == ms['matrix_sha256']
    report = dict(project=str(target), source_template=str(args.source),
        source_unchanged=True, source_tree=source_before,
        feature=reopened.feature_analysis.overview(), record=reopened.feature_analysis.umap_record,
        matrix_values_nan_axis_ids_versions_exact=True,
        original_project_tables_db_unchanged=True, all_labels_unknown=True,
        coordinate_switch_reimport_reopen_repeat_equal=True, csv_coordinates_equal=True,
        timings=dict(create=creation_seconds, umap=umap_seconds,
                     reopen=reopen_seconds, repeat_umap=repeat_seconds))
    (root / 'lma-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'passed': root.name, 'feature': report['feature'], 'timings': report['timings']}), flush=True)


if __name__ == '__main__':
    main()
