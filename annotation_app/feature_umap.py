"""Optional, identity-bound MS feature inputs and reproducible native UMAP.

No annotation, time model, base event map or raw matrix is rewritten here.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import time
import zipfile

import numpy as np
import pandas as pd
from flame_ms_core.exchange import read_event_package
from annotation_app.ms_core import PACKAGE_PATH

ROOT = "analysis/feature_umap"
DEFAULTS = {"n_pcs": 50, "n_neighbors": 15, "random_state": 1}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_path(root, name):
    parts = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or parts.is_absolute()
            or any(not p or p.rstrip(" .") != p or p in {"..", "."} for p in name.split("/"))):
        raise ValueError("结果包包含无效文件路径")
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("结果文件不能指向包外")
    return path


@contextmanager
def unpack_handoff(source):
    """Read the explicit Studio handoff, preserving formal event identity."""
    with tempfile.TemporaryDirectory(prefix="lma-ms-results-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(source) as archive:
            if 'handoff_record.json' not in archive.namelist():
                raise ValueError('请选择 MS Event Studio「传给 LMA Studio」导出的事件包 ZIP；分析结果 ZIP 不用于项目交接')
            record = json.loads(archive.read('handoff_record.json'))
            if not isinstance(record, dict) or record.get('schema') != 'ms-lma-handoff-v1':
                raise ValueError('不支持的 LMA 事件包格式')
            names = set()
            for item in archive.infolist():
                path = safe_path(root, item.filename.rstrip("/"))
                key = item.filename.casefold().rstrip("/")
                if key in names or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("结果包含重复路径或链接")
                names.add(key)
                if item.filename.startswith(("features/", "events/")) and not item.is_dir():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as src, path.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
        feature_root = root / "features"
        package = read_event_package(root / 'events')
        if package.manifest_sha256 != record.get('event_manifest_sha256'):
            raise ValueError('事件包校验失败')
        if record.get('feature_result_id') is not None:
            if sha256(feature_root / 'execution_record.json') != record.get('feature_record_sha256'):
                raise ValueError('矩阵记录校验失败')
            validate_matrix(feature_root)
            evidence = read_event_package(feature_root / 'source_events')
            if evidence.manifest_sha256 != package.manifest_sha256:
                raise ValueError('矩阵与交接事件不一致')
        elif feature_root.exists():
            raise ValueError('事件包包含未声明的矩阵')
        yield root


@contextmanager
def unpack_results(source):
    with unpack_handoff(source) as root:
        feature_root = root / 'features'
        if not feature_root.is_dir():
            raise ValueError('此事件包不含矩阵；请在 MS Event Studio「传给 LMA Studio」时勾选矩阵')
        yield feature_root


def validate_matrix(root, app=None):
    import anndata as ad
    root = Path(root)
    record = json.loads((root / "execution_record.json").read_text(encoding="utf-8"))
    if (record.get("interface") != "ms-event-studio-feature-v1" or
            record.get("status") != "passed" or record.get("complete") is not True):
        raise ValueError("只支持 MS Event Studio 已完成且通过校验的矩阵")
    required = {"native_matrix.h5ad", "event_rows.parquet", "feature_axis.parquet",
                "source_events/manifest.json", "source_events/events.parquet", "source_events/checksums.sha256"}
    artifacts = record.get("artifacts", {})
    if not required.issubset(artifacts):
        raise ValueError("矩阵缺少必需的事件与版本证据")
    for name, expected in artifacts.items():
        path = safe_path(root, name)
        if path.is_symlink() or path.stat().st_size != expected["bytes"] or sha256(path) != expected["sha256"]:
            raise ValueError(f"矩阵文件校验失败：{name}")
    package = read_event_package(root / "source_events")
    if (package.manifest["schema"] != "ms-event-machine-contract-v2" or
            record["source_event_manifest_sha256"] != package.manifest_sha256):
        raise ValueError("矩阵的上游事件证据不一致")
    if app is not None:
        entry = (app.manifest or {}).get("ms_event_import")
        if not entry:
            raise ValueError("当前项目没有可核对的 MS 事件身份；请用 LMA 事件包 ZIP 创建独立项目。原项目仍可继续使用")
        current = read_event_package(app.project.project_dir / PACKAGE_PATH)
        if current.manifest_sha256 != entry["manifest_sha256"] or current.event_versions != entry["event_versions"]:
            raise ValueError("当前项目上游事件已改变，请重新打开并检查项目")
        if current.event_versions != package.event_versions or current.manifest["source_fingerprint"]["sha256"] != package.manifest["source_fingerprint"]["sha256"]:
            raise ValueError("矩阵与当前项目的 MS 事件或版本不一致，不能按时间近似附加")
    rows = pd.read_parquet(root / "event_rows.parquet")
    axis = pd.read_parquet(root / "feature_axis.parquet")
    matrix = ad.read_h5ad(root / "native_matrix.h5ad")
    ids = rows.event_id.astype(str).tolist()
    if len(ids) != len(set(ids)) or ids != matrix.obs_names.tolist() or matrix.shape != (record["events"], record["features"]):
        raise ValueError("矩阵行身份或维度不一致")
    if matrix.var_names.tolist() != axis.feature_id.astype(str).tolist() or not matrix.var_names.is_unique:
        raise ValueError("矩阵 feature 轴不一致")
    if not isinstance(matrix.X, np.ndarray) or matrix.X.dtype != np.dtype("float64") or np.isinf(matrix.X).any():
        raise ValueError("原生矩阵必须是保留 NaN 的 float64 强度矩阵，不能包含无穷值")
    pd.testing.assert_frame_equal(matrix.obs, rows, check_dtype=False, check_categorical=False)
    pd.testing.assert_frame_equal(matrix.var.reset_index(drop=True), axis.reset_index(drop=True),
                                  check_dtype=False, check_categorical=False)
    events = package.events.set_index("event_id")
    source_sha = package.manifest["source_fingerprint"]["sha256"]
    for row in rows.itertuples():
        if row.event_id not in events.index or events.loc[row.event_id, "status"] != "accepted":
            raise ValueError("矩阵包含未保留的事件")
        if row.event_version != package.event_versions[row.event_id] or row.source_sha256 != source_sha:
            raise ValueError("矩阵行的事件版本不一致")
        event = events.loc[row.event_id]
        if int(row.current_apex_time_ns) != int(event.current_apex_time_ns) or str(row.apex_scan_id) != str(event.current_scan_id):
            raise ValueError("矩阵行的峰顶证据不一致")
    if app is not None and not set(ids).issubset(app.cell_event_map_event_ids() or set()):
        raise ValueError("矩阵含当前项目事件列表以外的行")
    return record, matrix, package


def validated_parameters(requested, shape):
    if not isinstance(requested, dict) or set(requested).difference(DEFAULTS):
        raise ValueError("UMAP 参数无效")
    values = {}
    for key, default in DEFAULTS.items():
        value = requested.get(key, default)
        if type(value) is not int or not (0 <= value <= 2147483647):
            raise ValueError("UMAP 参数必须为有效整数")
        values[key] = value
    if values["n_pcs"] < 1 or values["n_neighbors"] < 2:
        raise ValueError("主成分数至少为 1，邻居数至少为 2")
    if shape[0] < 4 or shape[1] < 2:
        raise ValueError("UMAP 至少需要 4 个事件和 2 个 feature")
    return {**values, "n_pcs": min(values["n_pcs"], min(shape) - 1),
            "n_neighbors": min(values["n_neighbors"], shape[0] - 1)}


def compute_embedding(matrix, requested, progress=lambda message: None):
    """User reference: NaN->zero on a copy, explicit PCA/neighbors/UMAP."""
    import anndata as ad
    import scanpy as sc
    parameters = validated_parameters(requested, matrix.shape)
    if not isinstance(matrix.X, np.ndarray) or np.isinf(matrix.X).any():
        raise ValueError("UMAP 需要不含无穷值的原生矩阵")
    values = np.nan_to_num(matrix.X, copy=True, nan=0.0)
    if not np.isfinite(values).all() or not np.any(np.var(values, axis=0) > 0):
        raise ValueError("矩阵没有可用于降维的有效变化")
    adata = ad.AnnData(values, obs=matrix.obs.copy(), var=matrix.var.copy())
    seed = parameters["random_state"]
    progress("正在计算 PCA")
    sc.pp.pca(adata, n_comps=parameters["n_pcs"], svd_solver="arpack", random_state=seed)
    progress("正在计算邻居图")
    sc.pp.neighbors(adata, n_neighbors=parameters["n_neighbors"], n_pcs=parameters["n_pcs"],
                    use_rep="X_pca", metric="euclidean", random_state=seed)
    progress("正在计算 UMAP")
    sc.tl.umap(adata, random_state=seed, n_components=2, min_dist=0.5, spread=1.0, init_pos="spectral")
    xy = adata.obsm["X_umap"]
    if xy.shape != (matrix.n_obs, 2) or not np.isfinite(xy).all():
        raise ValueError("UMAP 没有产生有效的二维坐标")
    coordinates = pd.DataFrame({"ms_event_id": matrix.obs_names, "UMAP1": xy[:, 0], "UMAP2": xy[:, 1]})
    provenance = {"requested": {**DEFAULTS, **requested}, "actual": parameters,
                  "preprocessing": "NaN -> 0 on independent copy; no normalization/log/scaling/correction",
                  "pca_solver": "arpack", "metric": "euclidean", "min_dist": 0.5,
                  "spread": 1.0, "init_pos": "spectral", "color_only": "scan_start_time",
                  "dependencies": {name: version(name) for name in ("scanpy", "anndata", "umap-learn", "numpy", "scipy", "scikit-learn", "numba")}}
    return coordinates, provenance


class FeatureAnalysis:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.root = app.project.project_dir / ROOT
        self.state = {"matrix": None, "embedding": None, "view": "base"}
        self.record = None
        self.coordinates = None
        self.umap_record = None
        if (self.root / "state.json").exists():
            self.state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
            if self.state.get("view") not in {"base", "native"} or not self.state.get("matrix"):
                raise ValueError("项目矩阵视图记录无效")
            for key in ("matrix", "embedding"):
                if self.state.get(key) is not None and not re.fullmatch("[0-9a-f]{64}", self.state[key]):
                    raise ValueError("项目矩阵记录无效")
            self.record, _, _ = validate_matrix(self.matrix_path, app)
            if sha256(self.matrix_path / "execution_record.json") != self.state["matrix"]:
                raise ValueError("项目矩阵记录已改变")
            if self.state.get("embedding"):
                self.coordinates, self.umap_record = self._read_coordinates()

    @property
    def matrix_path(self):
        return self.root / "matrices" / str(self.state["matrix"])

    def _save(self, state):
        write_json(self.root / "state.json", state)
        self.state = state

    def import_matrix(self, source):
        record, _, _ = validate_matrix(source, self.app)
        digest = sha256(Path(source) / "execution_record.json")
        parent = self.root / "matrices"
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / digest
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix=".import-", dir=parent) as temporary:
                copy = Path(temporary) / "matrix"
                copy.mkdir()
                for name in [*record["artifacts"], "execution_record.json"]:
                    path = safe_path(copy, name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(safe_path(Path(source), name), path)
                validate_matrix(copy, self.app)
                if sha256(copy / "execution_record.json") != digest:
                    raise ValueError("矩阵在导入期间发生变化")
                os.rename(copy, destination)
        else:
            validate_matrix(destination, self.app)
        if digest != self.state["matrix"]:
            self._save({"matrix": digest, "embedding": None, "view": "base"})
            self.coordinates = None
            self.umap_record = None
        self.record = record
        return self.overview()

    def selection_scope(self):
        config = self.app.project_config()
        return {"policy": "annotation-start-v1",
                "start_ns": round(float(config["annotation_start_min"]) * 60_000_000_000),
                "boundaries_confirmed": bool((config.get("calibration_protocol") or {}).get("boundaries_confirmed"))}

    def scope_warning(self):
        if self.coordinates is None:
            return ""
        if (self.umap_record or {}).get("selection_scope") != self.selection_scope():
            return "已保存的原生 UMAP 未按当前前段范围计算，请重新计算。"
        return ""

    def calculate(self, requested, progress=lambda message: None):
        self.app.require_confirmed_calibration("计算 UMAP")
        scope = self.selection_scope()
        record, matrix, _ = validate_matrix(self.matrix_path, self.app)
        matrix = matrix[matrix.obs.current_apex_time_ns >= scope["start_ns"]].copy()
        started = time.monotonic()
        coordinates, provenance = compute_embedding(matrix, requested, progress)
        provenance.update(matrix=self.state["matrix"], schema="lma-native-umap-v1", selection_scope=scope, selected_events=matrix.n_obs, excluded_front_events=record["events"] - matrix.n_obs)
        digest = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
        parent = self.root / "embeddings"
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / digest
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix=".umap-", dir=parent) as temporary:
                copy = Path(temporary) / "result"
                copy.mkdir()
                coordinates.to_parquet(copy / "coordinates.parquet", index=False)
                provenance.update(coordinates_sha256=sha256(copy / "coordinates.parquet"),
                                  elapsed_seconds=time.monotonic() - started)
                write_json(copy / "record.json", provenance)
                # Source identity must still hold when publishing a result.
                validate_matrix(self.matrix_path, self.app)
                os.rename(copy, destination)
        coordinates, umap_record = self._read_coordinates(digest)
        with self.lock:
            if self.selection_scope() != scope:
                raise ValueError("计算期间前段范围发生变化，未启用新 UMAP，请重新计算。")
            self._save({**self.state, "embedding": digest, "view": "native"})
            self.coordinates = coordinates
            self.umap_record = umap_record
        return self.overview()

    def _read_coordinates(self, digest=None):
        directory = self.root / "embeddings" / (digest or self.state["embedding"])
        record = json.loads((directory / "record.json").read_text(encoding="utf-8"))
        path = directory / "coordinates.parquet"
        if record["matrix"] != self.state["matrix"] or sha256(path) != record["coordinates_sha256"]:
            raise ValueError("已保存 UMAP 与矩阵不一致")
        coordinates = pd.read_parquet(path)
        rows = pd.read_parquet(self.matrix_path / "event_rows.parquet")
        scope = record.get("selection_scope")
        if scope is not None:
            if (scope.get("policy") != "annotation-start-v1" or scope.get("boundaries_confirmed") is not True
                    or type(scope.get("start_ns")) is not int or scope["start_ns"] < 0):
                raise ValueError("已保存 UMAP 的计算范围无效")
            rows = rows.loc[rows.current_apex_time_ns >= scope["start_ns"]]
        if coordinates.ms_event_id.tolist() != rows.event_id.tolist() or not np.isfinite(coordinates[["UMAP1", "UMAP2"]]).all().all():
            raise ValueError("已保存 UMAP 的事件身份或坐标无效")
        return coordinates, record

    def select_view(self, view):
        if view not in {"base", "native"} or (view == "native" and self.coordinates is None):
            raise ValueError("请先计算原生 UMAP")
        if not self.record:
            return self.overview()
        self._save({**self.state, "view": view})
        return self.overview()

    def event_map(self):
        base = self.app.cell_event_map
        if self.state["view"] != "native" or self.coordinates is None:
            return base
        # Preserve the roster: excluded QC events remain annotatable without points.
        result = base.copy()
        coordinates = self.coordinates.set_index("ms_event_id")
        for col in ("UMAP1", "UMAP2"):
            result[col] = result.ms_event_id.map(coordinates[col])
        return result

    def overview(self):
        return {"available": self.record is not None,
                "can_import": bool((self.app.manifest or {}).get("ms_event_import")),
                "events": self.record["events"] if self.record else 0,
                "features": self.record["features"] if self.record else 0,
                "has_umap": self.coordinates is not None, "view": self.state["view"],
                "base_coordinates_available": self.app.base_coordinates_available(),
                "selection_scope": self.selection_scope(),
                "scope_warning": self.scope_warning(),
                "umap_events": len(self.coordinates) if self.coordinates is not None else 0,
                "actual_parameters": (self.umap_record or {}).get("actual"),
                "defaults": DEFAULTS}


class AnalysisJob:
    """One background calculation per server; all writes are gated while running."""
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"status": "idle", "message": ""}

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def clear(self):
        with self.lock:
            if self.status["status"] != "running":
                self.status = {"status": "idle", "message": ""}

    def start(self, app, requested, activity):
        with self.lock:
            if self.status["status"] == "running":
                raise ValueError("UMAP 正在计算")
            self.status = {"status": "running", "message": "正在校验矩阵", "project_id": app.project_identity()}
        tracked = activity.track("/api/feature-umap/run")
        tracked.__enter__()
        def progress(message):
            with self.lock:
                self.status["message"] = message
        def run():
            try:
                app.feature_analysis.calculate(requested, progress)
                result = {"status": "succeeded", "message": "UMAP 已保存，可打开 UMAP 查看"}
            except Exception as exc:
                result = {"status": "failed", "message": str(exc)}
            finally:
                tracked.__exit__(None, None, None)
            with self.lock:
                self.status.update(result)
        try:
            threading.Thread(target=run, daemon=False, name="native-umap").start()
        except Exception:
            tracked.__exit__(None, None, None)
            with self.lock:
                self.status.update(status="failed", message="无法启动 UMAP")
            raise
        return self.snapshot()
