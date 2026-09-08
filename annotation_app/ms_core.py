"""LMA's storage projection of the shared caller and reviewed EventSet.

The imported upstream table remains a separate immutable artifact. The working
table uses the reviewed apex and the existing LMA column vocabulary.
"""
from __future__ import annotations

from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from flame_ms_core import __version__ as CORE_VERSION
from flame_ms_core.exchange import read_event_package, compare_event_packages
from flame_ms_core.parser import parse_ms_scan_summary
from flame_ms_core.scientific_settings import ProjectScientificSettings

PACKAGE_PATH = "provenance/ms_event_package"


def lma_scan_table(scan: pd.DataFrame) -> pd.DataFrame:
    result = scan.copy()
    for name in scan.columns:
        alias = name.replace("primary_marker", "pc34_760").replace("qc_marker", "qc_782")
        if alias != name:
            result[alias] = scan[name]
    result["ratio_760_782_max_pseudo1"] = scan["primary_qc_max_ratio_pseudo1"]
    result["ratio_760_782_sum_pseudo1"] = scan["primary_qc_sum_ratio_pseudo1"]
    result["log10_pc34_760_max"] = scan["log10_primary_marker_max"]
    result["log10_qc_782_max"] = scan["log10_qc_marker_max"]
    return result


def lma_event_table(events: pd.DataFrame, scan: pd.DataFrame, *, imported: bool) -> pd.DataFrame:
    result = events.copy()
    if imported:
        automatic = events[events["origin"].ne("manual_added")]
        # Validate immutable evidence too, before projecting current coordinates.
        lma_event_table(automatic, scan, imported=False)
        for target, source in {"scan_id": "current_scan_id", "scan_row_index": "current_scan_row_index",
                               "spectrum_index": "current_spectrum_index", "scan_time_ns": "current_apex_time_ns",
                               "apex_time_sec": "current_apex_time_sec", "apex_intensity": "current_apex_intensity"}.items():
            result[target] = events[source]
        for name in ("scan_row_index", "spectrum_index", "scan_time_ns"):
            result[name] = result[name].astype("int64")
        result["upstream_review_status"] = events["status"]
    else:
        result["event_id"] = events["auto_event_id"]
    result["event_strategy"] = "pc34_primary"
    result["primary_signal_col"] = "pc34_760_max_intensity"
    result["time_sec"] = result["scan_time_ns"].astype("int64") / 1e9
    result["time_min"] = result["scan_time_ns"].astype("int64") / 60e9
    result["ms_core_version"] = CORE_VERSION
    indices = result["scan_row_index"].astype(int).to_numpy()
    if len(indices) and (indices.min() < 0 or indices.max() >= len(scan)):
        raise ValueError("MS 事件 scan 超出原始数据范围")
    apex = scan.iloc[indices].reset_index(drop=True)
    for event_col, scan_col in (("scan_id", "scan_id"), ("spectrum_index", "spectrum_index"), ("scan_time_ns", "scan_time_ns")):
        if list(result[event_col].astype(str)) != list(apex[scan_col].astype(str)):
            raise ValueError("MS 事件身份与原始 scan/时间不一致")
    if not np.allclose(result["apex_intensity"].astype(float), apex["primary_marker_max_intensity"].astype(float), rtol=0, atol=1e-8):
        raise ValueError("MS 事件强度与原始 marker 测量不一致")
    for target, source in {"pc34_760_apex": "primary_marker_max_intensity",
                           "pc34_760_mz_at_apex": "primary_marker_mz_at_max_intensity",
                           "pc34_760_ppm_error_at_apex": "primary_marker_ppm_error_at_max_intensity",
                           "qc_782_apex": "qc_marker_max_intensity",
                           "qc_782_ppm_error_at_apex": "qc_marker_ppm_error_at_max_intensity",
                           "tic_apex": "tic", "ratio_760_782_max_pseudo1": "primary_qc_max_ratio_pseudo1"}.items():
        result[target] = apex[source].to_numpy()
    # Imported manual additions have no automatic support; a point window is
    # truthful. Never invent a new peak or replace immutable upstream support.
    for name in ("left_sec", "right_sec"):
        result[name] = pd.to_numeric(result[name], errors="coerce").fillna(result["time_sec"])
    return result


def prepare_imported_ms(package_dir: Path, raw_path: Path, project_dir: Path):
    package = read_event_package(package_dir)
    if package.manifest["schema"] != "ms-event-machine-contract-v2":
        raise ValueError("旧版审阅包未记录 marker 设置；请用支持共用内核的 MS Event Studio 重新导出")
    settings = ProjectScientificSettings.from_manifest(package.manifest.get("scientific_settings"))
    if settings.primary_marker_mz != 760.5851:
        raise ValueError("LMA 当前 PC34 标注入口只接受 760.5851；其他 marker 需独立科学验证")
    parsed = parse_ms_scan_summary(raw_path)
    if parsed.fingerprint.sha256 != package.manifest["source_fingerprint"]["sha256"]:
        raise ValueError("MS 原始文件与审阅包的完整 SHA-256 不一致")
    events = lma_event_table(package.events, parsed.scans, imported=True)
    included = events[events.upstream_review_status.eq("accepted")]
    if included.empty:
        raise ValueError("审阅包没有已保留事件；请先在 MS Event Studio 完成审阅")
    canonical = pd.DataFrame({"ms_event_id": included.event_id, "scan_id": included.scan_id,
                              "scan_start_time": included.time_min, "UMAP1": np.nan, "UMAP2": np.nan})
    destination = project_dir / PACKAGE_PATH
    destination.mkdir(parents=True)
    for name in ("manifest.json", "events.parquet", "checksums.sha256"):
        shutil.copyfile(package_dir / name, destination / name)
    copied = read_event_package(destination)
    if copied.manifest_sha256 != package.manifest_sha256:
        raise ValueError("LMA 事件包在导入期间发生变化")
    entry = {"path": PACKAGE_PATH, "manifest_sha256": package.manifest_sha256,
             "core_version": CORE_VERSION, "inclusion_policy": "accepted_only",
             "event_versions": package.event_versions, "status_counts": package.manifest["status_counts"]}
    metadata = {"source_name": package_dir.name, "source_sha256": package.manifest_sha256,
                "row_count": len(canonical), "matched_event_count": len(canonical),
                "match_policy": "machine_event_identity_v1", "match_tolerance_sec": 0.0,
                "coordinates_available": False}
    (project_dir / "data").mkdir(exist_ok=True)
    events.to_parquet(project_dir / "data/ms_events.parquet", index=False)
    lma_scan_table(parsed.scans).to_parquet(project_dir / "data/ms_scan_summary.parquet", index=False)
    return canonical, metadata, entry


def validate_import_binding(project_dir: Path, manifest: dict, events: pd.DataFrame) -> None:
    entry = manifest.get("ms_event_import")
    if entry is None:
        return
    if entry.get("path") != PACKAGE_PATH or entry.get("inclusion_policy") != "accepted_only":
        raise ValueError("LMA 事件包绑定无效")
    package = read_event_package(project_dir / PACKAGE_PATH)
    if package.manifest_sha256 != entry["manifest_sha256"] or package.event_versions != entry["event_versions"]:
        raise ValueError("上游事件版本已改变；不能覆盖当前项目标注，请在新项目导入")
    if list(events.event_id) != list(package.events.event_id):
        raise ValueError("MS 审阅事件顺序或身份不一致")


def preview_package_update(project_dir: Path, manifest: dict, incoming_dir: Path) -> dict:
    entry = manifest.get("ms_event_import")
    if not entry:
        raise ValueError("当前项目没有导入 LMA 事件包")
    previous = read_event_package(project_dir / PACKAGE_PATH)
    if previous.manifest_sha256 != entry["manifest_sha256"]:
        raise ValueError("当前项目审阅包已被修改")
    incoming = read_event_package(incoming_dir)
    result = compare_event_packages(previous, incoming)
    result["requires_new_project"] = bool(result["added"] or result["removed"] or result["changed"] or result["generation_changed"])
    result["message"] = "上游更新需导入独立新项目；当前事件、标注和时间模型保留。" if result["requires_new_project"] else "上游事件未变化。"
    return result
