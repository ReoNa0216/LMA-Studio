"""Publish an identity-joined analysis copy without modifying the project."""
from __future__ import annotations

from contextlib import closing
import csv
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

import pandas as pd

from annotation_app.feature_umap import sha256, validate_matrix


def zip_filename(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写 ZIP 文件名")
    name = value.strip()
    if not name.lower().endswith('.zip'):
        name += '.zip'
    stem = name[:-4]
    if (not stem or stem.endswith(('.', ' ')) or len(name) > 180 or len(name.encode('utf-8')) > 255
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', stem, re.I)):
        raise ValueError("文件名无效，请勿填写路径或特殊字符")
    return name


def export_results(app, parent, filename, *, include_matrix):
    if type(include_matrix) is not bool:
        raise ValueError("请选择是否包含矩阵")
    if not isinstance(parent, (str, Path)) or not str(parent).strip():
        raise ValueError("请选择保存文件夹")
    parent = Path(parent).expanduser().resolve(strict=True)
    if not parent.is_dir() or parent.is_relative_to(app.project.project_dir.resolve()):
        raise ValueError("请选择项目外的保存文件夹")
    destination = parent / zip_filename(filename)
    if destination.exists():
        raise ValueError("同名 ZIP 已存在，请修改文件名")
    analysis = app.feature_analysis
    # Reserve the SQLite writer while taking the snapshot. Other readers,
    # including the existing projection code, keep using their own connections.
    with analysis.lock, closing(sqlite3.connect(app.project.annotation_db_path, timeout=10)) as guard:
        guard.execute('BEGIN IMMEDIATE')
        sources = [app.project.project_dir / 'lifms_project.json']
        state_path = analysis.root / 'state.json'
        if state_path.exists():
            sources.append(state_path)
        source_hashes = {path: sha256(path) for path in sources}
        prepared = app.prepare_annotations_export()
        rows = [dict(row) for row in prepared['rows']]
        coordinates_current = not (analysis.state['view'] == 'native' and analysis.scope_warning())
        if not coordinates_current:
            for row in rows:
                row.update(UMAP1=None, UMAP2=None)
        by_id = {}
        for row in rows:
            identity = str(row.get('MS_event_id') or '')
            if not identity or identity in by_id:
                raise ValueError("导出记录的 MS 事件身份缺失或重复，请检查标注冲突")
            by_id[identity] = row
        record = dict(schema='lma-analysis-results-v1', export_id=prepared['export_id'],
                      exported_at=prepared['timestamp'], csv_rows=len(rows),
                      label_policy='Current accepted human annotations; unannotated remains unknown',
                      join_key='MS_event_id = H5AD obs_names',
                      csv_filters=prepared['filters'], matrix_included=include_matrix,
                      coordinate_source=analysis.state['view'], coordinates_current=coordinates_current,
                      project_manifest_sha256=source_hashes[sources[0]])
        matrix = None
        if include_matrix:
            if not analysis.record:
                raise ValueError("当前项目没有矩阵")
            app.require_confirmed_calibration("导出带标签矩阵")
            scope = analysis.selection_scope()
            source_record, original, _ = validate_matrix(analysis.matrix_path, app)
            matrix = original[original.obs.current_apex_time_ns >= scope['start_ns']].copy()
            if matrix.n_obs == 0:
                raise ValueError("事件起点之后没有可导出的矩阵行，请检查配置")
            missing = set(matrix.obs_names) - set(by_id)
            if missing:
                raise ValueError("矩阵事件缺少对应导出记录，请检查当前标注和事件版本")
            joined = [by_id[identity] for identity in matrix.obs_names]
            for name in ('Type', 'annotation_status', 'is_qc', 'LIF_channel', 'annotation_id', 'UMAP1', 'UMAP2'):
                if name in matrix.obs:
                    raise ValueError(f"原始矩阵已含 {name} 列，不能覆盖其来源信息")
            matrix.obs['Type'] = [str(row.get('Type') or 'unknown') for row in joined]
            matrix.obs['annotation_status'] = ['accepted' if row.get('annotation_id') else 'unknown' for row in joined]
            matrix.obs['is_qc'] = [row.get('annotation_kind') == 'qc_anchor' and row.get('review_stage') == 'qc_survey' for row in joined]
            for name in ('LIF_channel', 'annotation_id'):
                matrix.obs[name] = [str(row.get(name) or '') for row in joined]
            # Do not silently export an old native embedding for a new scope.
            for name in ('UMAP1', 'UMAP2'):
                matrix.obs[name] = pd.to_numeric(pd.Series([row.get(name) for row in joined]), errors='coerce').to_numpy(dtype=float)
            matrix.obsm['X_umap'] = matrix.obs[['UMAP1', 'UMAP2']].to_numpy()
            record.update(matrix_rows=matrix.n_obs, features=matrix.n_vars,
                          source_matrix_rows=original.n_obs,
                          excluded_front_rows=original.n_obs-matrix.n_obs,
                          csv_rows_without_exported_matrix=len(set(by_id)-set(matrix.obs_names)),
                          selection_scope=scope, source_matrix_record_sha256=analysis.state['matrix'],
                          source_matrix_sha256=source_record['artifacts']['native_matrix.h5ad']['sha256'],
                          coordinate_source=analysis.state['view'], coordinates_current=coordinates_current,
                          qc_policy='Exclude confirmed front range; retain later QC with human-derived is_qc',
                          intensity_policy='Original float64 values and NaN; no fill/normalize/scale')
            matrix.uns['lma_export'] = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with tempfile.TemporaryDirectory(prefix='.lma-export-', dir=parent) as temporary:
            temporary = Path(temporary)
            csv_path = temporary / 'cells_and_qc.csv'
            buffer = io.StringIO()
            columns = app.export_columns()
            writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator='\n')
            writer.writeheader()
            writer.writerows({key: app.csv_value(row.get(key)) for key in columns} for row in rows)
            csv_path.write_bytes(buffer.getvalue().encode('utf-8-sig'))
            if matrix is not None:
                matrix.write_h5ad(temporary / 'labeled_matrix.h5ad', compression='gzip')
            record['artifacts'] = {p.name: {'sha256': sha256(p), 'bytes': p.stat().st_size}
                                   for p in temporary.iterdir() if p.is_file()}
            (temporary / 'export_record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
            archive = temporary / 'results.zip'
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
                for name in ['cells_and_qc.csv', 'export_record.json'] + (['labeled_matrix.h5ad'] if matrix is not None else []):
                    bundle.write(temporary / name, name)
            if any(sha256(path) != expected for path, expected in source_hashes.items()):
                raise ValueError("导出期间项目或矩阵视图发生变化，请重新导出")
            if include_matrix and sha256(analysis.matrix_path / 'native_matrix.h5ad') != record['source_matrix_sha256']:
                raise ValueError("导出期间原始矩阵发生变化，请检查项目")
            digest = sha256(archive)
            try:
                os.link(archive, destination)
            except FileExistsError as exc:
                raise ValueError("同名 ZIP 已存在，请修改文件名") from exc
        guard.rollback()
    return dict(filename=destination.name, path=str(destination), sha256=digest,
                csv_rows=len(rows), matrix_rows=matrix.n_obs if matrix is not None else None,
                coordinates_current=coordinates_current)
