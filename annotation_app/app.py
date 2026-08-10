#!/usr/bin/env python3
"""Local browser application for human-assisted LIF-MS annotation.

The app loads first-principles preprocessing tables, slices synchronized LIF/MS
windows, records human review decisions in SQLite, and exports accepted
annotations. A coordinate source is restricted to three whitelisted columns;
author labels, h5ad, manual labels, and V2 outputs never enter candidate
generation or export.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import contextlib
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from annotation_app.cell_event_map import (
    CELL_EVENT_MAP_RELATIVE_PATH,
    DEFAULT_MATCH_TOLERANCE_SEC,
    CellEventMapError,
    cell_event_map_manifest_entry,
    import_cell_event_map,
    match_source_to_events,
    project_annotation_state,
    read_canonical_map,
    state_revision,
    write_canonical_map,
)
from annotation_app.umap_page import UMAP_HTML
from scripts.v3.lif_peak_detection import (
    lif_peak_detection_hash,
    normalize_lif_peak_detection,
    require_active_lif_peak_detection,
)
from scripts.v3.project_storage import (
    CANONICAL_ANNOTATION_DB_PATH,
    CANONICAL_CELL_EVENT_MAP_PATH,
    CANONICAL_EXPORTS_DIR,
    CANONICAL_INPUT_MANIFEST_PATH,
    CANONICAL_LIF_DIAGNOSTICS_DIR,
    CANONICAL_MS_DIAGNOSTICS_DIR,
    CANONICAL_PREPROCESSING_LOG_PATH,
    CANONICAL_PREPROCESSING_REPORT_PATH,
    CANONICAL_PROJECT_README_PATH,
    CANONICAL_PROJECT_PROTOCOL_PATH,
    CANONICAL_TABLE_PATHS,
    canonical_storage_layout_manifest_entry,
    manifest_uses_canonical_storage,
)


IS_FROZEN = bool(getattr(sys, "frozen", False))
LOGGER = logging.getLogger(__name__)


def bundle_root() -> Path:
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
    return Path(__file__).resolve().parents[1]


def default_user_root() -> Path:
    if IS_FROZEN:
        return Path.cwd().resolve()
    return Path(__file__).resolve().parents[1]


BUNDLE_ROOT = bundle_root()
ROOT = default_user_root()
SCRIPT_ROOT = BUNDLE_ROOT / "scripts/v3"

DEFAULT_PROJECT_DIR = ROOT
DEFAULT_RAW_DATA_DIR = ROOT / "CAR-T_data"
DEFAULT_ANNOTATION_DB_PATH = ROOT / "annotation_app/annotations/annotation.sqlite"
WRITE_TOKEN = uuid.uuid4().hex
APP_VERSION = "lma_studio_v0.4.1-rc1"
APP_DISPLAY_NAME = "LMA Studio"


_REQUEST_READ_SNAPSHOT = threading.local()


@contextlib.contextmanager
def request_read_snapshot():
    """Memoize immutable reads for one logical UI operation.

    Window rendering and manual-pair validation inspect the same project
    configuration, active time model, and annotation set many times.  Without
    a request boundary those helpers reopen SQLite once per annotation (an
    N+1 read pattern).  The cache is deliberately thread-local and short-lived:
    it never survives a request and therefore cannot hide later user edits.
    """

    existing = getattr(_REQUEST_READ_SNAPSHOT, "values", None)
    if existing is not None:
        yield
        return
    _REQUEST_READ_SNAPSHOT.values = {}
    try:
        yield
    finally:
        delattr(_REQUEST_READ_SNAPSHOT, "values")


def request_cached_read(owner: Any, key: str, loader: Callable[[], Any]) -> Any:
    values = getattr(_REQUEST_READ_SNAPSHOT, "values", None)
    if values is None:
        return loader()
    cache_key = (id(owner), str(key))
    if cache_key not in values:
        values[cache_key] = loader()
    return values[cache_key]


def invalidate_request_cached_reads(owner: Any, *keys: str) -> None:
    """Drop request-local snapshots after a write without leaking across requests."""

    values = getattr(_REQUEST_READ_SNAPSHOT, "values", None)
    if values is None:
        return
    for key in keys:
        values.pop((id(owner), str(key)), None)

DEFAULT_WINDOW_MIN = 2.5
MAX_TRACE_POINTS_PER_SERIES = 2200
DEFAULT_LIF_SIGNAL_MODE = "signal"
LIF_SIGNAL_MODES = {"raw", "signal"}
QC_SHIFT_WINDOW_MIN = 10.5
SHIFT_SEARCH_MIN_SEC = -60.0
SHIFT_SEARCH_MAX_SEC = 60.0
SHIFT_SEARCH_STEP_SEC = 0.25
SHIFT_MATCH_TOL_SEC = 1.50
LIF_PAIR_OFFSET_MAX_ABS_SEC = 30.0
LIF_PAIR_OFFSET_BIN_SEC = 0.25
LIF_PAIR_MATCH_TOL_SEC = 2.00
QC_AXIS_COHERENCE_TOL_SEC = LIF_PAIR_MATCH_TOL_SEC
QC_GROUP_MATCH_TOL_SEC = 4.00
WINDOW_CONTEXT_MARGIN_MIN = math.ceil(((max(QC_GROUP_MATCH_TOL_SEC, LIF_PAIR_MATCH_TOL_SEC) / 60.0) + 0.005) * 100.0) / 100.0
PRE_RUN_MAX_MIN = 40.0
QC_COMPONENT_SELECT_EPS = 1e-9
POST_QC_CANDIDATE_TOL_SEC = 2.50
CELL_CANDIDATE_TOL_SEC = 0.75
CELL_MIN_TIME_MIN = 40.0
CELL_MIN_LIF_SNR = 20.0
CELL_MIN_LIF_GAP_SEC = 3.0
CELL_MIN_MS_GAP_SEC = 1.2
CELL_MIN_PC34_APEX = 10000.0
DEFAULT_SAMPLE_VALVE_SWITCH_MIN = 36.0
DEFAULT_ANNOTATION_START_MIN = 40.0
DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN = 2.5
TIME_MODEL_CONFIG_KEYS = {
    "qc_calibration_end_min",
    "annotation_start_min",
    "local_delta_seed_window_min",
}
QC_ALIGNMENT_MODEL_KEY = "qc_alignment_model"
QC_ALIGNMENT_MODEL_VERSION = 1
QC_REFIT_MIN_EVIDENCE_PER_AXIS = 2
QC_REFIT_MIN_OUTLIER_TOL_SEC = 1.5
QC_REFIT_MAD_SCALE = 3.0
QC_REFIT_MAX_OUTLIER_TOL_SEC = QC_GROUP_MATCH_TOL_SEC
QC_REFIT_MAX_P90_RESIDUAL_SEC = 3.0
LOCAL_DELTA_SEARCH_MIN_SEC = -20.0
LOCAL_DELTA_SEARCH_MAX_SEC = 20.0
LOCAL_DELTA_SEARCH_STEP_SEC = 0.25
LOCAL_DELTA_MATCH_TOL_SEC = 1.50
LOCAL_DELTA_MAX_ABS_SEC = 20.0
LOCAL_DELTA_ABS_PRIOR_WEIGHT = 0.50
LOCAL_DELTA_CONFLICT_PENALTY_SEC = 0.50
FORBIDDEN_PATH_PARTS = [
    "hrgc-obs-check.csv",
    "clean+QC.h5ad",
    "data/anndata",
    "reports/archive",
    "scripts/archive",
    "data/interim/archive",
    "results/archive",
    "v2_",
    "manual",
    "override",
]
REVIEW_STATUSES = {"pending", "accepted", "rejected"}
ANNOTATION_SOURCES = {"auto_candidate", "manual_created"}
MISSING_PEAK_SYMBOL = "NA"
NATIVE_DIALOG_KINDS = {"directory", "file"}
PROJECT_MANIFEST_FILENAME = "lifms_project.json"
PROJECT_SCHEMA_VERSION = 3
ACQUISITION_LAYOUT_VERSION = 4
CALIBRATION_PROTOCOL_VERSION = 1
POST_QC_STRATEGY_VERSION = 1
CALIBRATION_REFERENCE_MODES = {"green_only", "red_only", "red_green"}
POST_QC_STRATEGY_MODES = {"signature", "scheduled_windows", "disabled"}
SEGMENTED_CALIBRATION_MATCHER_VERSION = "segmented_axis_reference_v2_monotone"
PROJECT_TABLE_BINDING_SCHEMA_VERSION = 1
MIN_LIF_INPUTS = 2
MAX_LIF_INPUTS = 4
MIN_QC_ANCHOR_CHANNELS = 2
MAX_QC_ANCHOR_CHANNELS = 4
QC_MATCHER_VERSION = "axis_aware_anchor_set_v3_monotone"
RAW_INPUT_MODE_COPY = "copy_into_project"
RAW_INPUT_MODE_EXTERNAL = "external_reference"
RAW_INPUT_MODES = {RAW_INPUT_MODE_COPY, RAW_INPUT_MODE_EXTERNAL}
REQUIRED_INTERMEDIATE_TABLES = {
    "lif_traces": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet",
    "lif_peaks": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet",
    "ms_events": "data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet",
    "ms_scan_summary": "data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet",
}
REQUIRED_INTERMEDIATE_TABLE_KEYS = tuple(REQUIRED_INTERMEDIATE_TABLES)
PROJECT_TABLE_BINDING_KEY = "project_table_binding"


def native_path_dialog_response(kind: str, chooser: Any) -> dict[str, Any]:
    if kind not in NATIVE_DIALOG_KINDS:
        raise ValueError(f"Unsupported native path dialog kind: {kind}")
    selected = chooser()
    path = "" if selected in {None, ""} else str(Path(selected).expanduser().resolve())
    return {"ok": True, "kind": kind, "path": path, "cancelled": not bool(path)}


def choose_native_path(kind: str, title: str = "", initial_dir: str = "", file_role: str = "") -> dict[str, Any]:
    if kind not in NATIVE_DIALOG_KINDS:
        raise BadRequest(f"kind must be one of {sorted(NATIVE_DIALOG_KINDS)}")

    def chooser() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        try:
            dialog_initial_dir = str(Path(initial_dir).expanduser()) if str(initial_dir).strip() else str(ROOT)
            if kind == "directory":
                return filedialog.askdirectory(
                    parent=root,
                    title=title or "选择项目保存路径",
                    initialdir=dialog_initial_dir,
                    mustexist=False,
                )
            if file_role == "ms":
                filetypes = [("MS raw files", "*.txt *.csv"), ("Text files", "*.txt"), ("CSV files", "*.csv"), ("All files", "*.*")]
            elif file_role == "cell_event_map":
                filetypes = [("Cell event coordinate CSV", "*.csv"), ("CSV files", "*.csv"), ("All files", "*.*")]
            else:
                filetypes = [("LIF raw files", "*.csv *.txt"), ("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")]
            return filedialog.askopenfilename(
                parent=root,
                title=title or "选择原始文件",
                initialdir=dialog_initial_dir,
                filetypes=filetypes,
            )
        finally:
            root.destroy()

    try:
        return native_path_dialog_response(kind, chooser)
    except BadRequest:
        raise
    except Exception as exc:
        raise BadRequest(f"无法打开本机路径选择窗口: {exc}") from exc


@dataclass(frozen=True)
class ProjectPaths:
    project_dir: Path
    raw_data_dir: Path
    annotation_db_path: Path
    lif_traces_path: Path
    lif_peaks_path: Path
    ms_events_path: Path
    ms_scan_path: Path

    @classmethod
    def from_args(
        cls,
        project_dir: str | None = None,
        raw_data_dir: str | None = None,
        annotation_db: str | None = None,
    ) -> "ProjectPaths":
        pdir = Path(project_dir).expanduser().resolve() if project_dir else DEFAULT_PROJECT_DIR
        if raw_data_dir:
            rdir = Path(raw_data_dir).expanduser().resolve()
        elif project_dir:
            rdir = pdir / "raw_inputs"
        else:
            rdir = DEFAULT_RAW_DATA_DIR
        if annotation_db:
            db = Path(annotation_db).expanduser().resolve()
        elif project_dir:
            db = pdir / "annotation_app/annotations/annotation.sqlite"
        else:
            db = DEFAULT_ANNOTATION_DB_PATH
        return cls(
            project_dir=pdir,
            raw_data_dir=rdir,
            annotation_db_path=db,
            lif_traces_path=pdir / REQUIRED_INTERMEDIATE_TABLES["lif_traces"],
            lif_peaks_path=pdir / REQUIRED_INTERMEDIATE_TABLES["lif_peaks"],
            ms_events_path=pdir / REQUIRED_INTERMEDIATE_TABLES["ms_events"],
            ms_scan_path=pdir / REQUIRED_INTERMEDIATE_TABLES["ms_scan_summary"],
        )

    @classmethod
    def for_new_project(cls, project_dir: str | Path) -> "ProjectPaths":
        """Construct paths for the current portable project layout.

        Existing projects must continue through :meth:`from_args` and their
        manifest.  Keeping this constructor explicit prevents opening an old
        project from ever becoming an implicit migration.
        """

        pdir = Path(project_dir).expanduser().resolve()
        return cls(
            project_dir=pdir,
            raw_data_dir=pdir / "raw_inputs",
            annotation_db_path=pdir / CANONICAL_ANNOTATION_DB_PATH,
            lif_traces_path=pdir / CANONICAL_TABLE_PATHS["lif_traces"],
            lif_peaks_path=pdir / CANONICAL_TABLE_PATHS["lif_peaks"],
            ms_events_path=pdir / CANONICAL_TABLE_PATHS["ms_events"],
            ms_scan_path=pdir / CANONICAL_TABLE_PATHS["ms_scan_summary"],
        )


def display_path(path: Path, base: Path = ROOT) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def project_manifest_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_MANIFEST_FILENAME


def normalize_raw_input_mode(raw_input_mode: str | None) -> str:
    mode = str(raw_input_mode or RAW_INPUT_MODE_EXTERNAL).strip() or RAW_INPUT_MODE_EXTERNAL
    if mode not in RAW_INPUT_MODES:
        raise BadRequest(f"raw_input_mode must be one of {sorted(RAW_INPUT_MODES)}")
    return mode


def normalize_lif_signal_mode(lif_signal_mode: str | None) -> str:
    mode = str(lif_signal_mode or DEFAULT_LIF_SIGNAL_MODE).strip().lower() or DEFAULT_LIF_SIGNAL_MODE
    if mode not in LIF_SIGNAL_MODES:
        raise BadRequest(f"lif_signal_mode must be one of {sorted(LIF_SIGNAL_MODES)}")
    return mode


def manifest_path_value(path: Path, project_dir: Path, raw_input_mode: str) -> str:
    if raw_input_mode == RAW_INPUT_MODE_COPY:
        return project_relative_or_absolute(path, project_dir).replace("\\", "/")
    return str(path.expanduser().resolve())


def channel_identity_prior_from_values(identities: dict[str, str], source: str = "project_manifest") -> dict[str, dict[str, str]]:
    fallback = {"G2": "Day0", "R1": "Day9", "R2": "Day3"}
    channels = list(dict.fromkeys([*identities.keys(), *fallback.keys()]))
    return {
        channel: {
            "identity_prior": str(identities.get(channel) or fallback.get(channel) or ""),
            "identity_prior_source": source,
            "identity_prior_file": "",
        }
        for channel in channels
    }


def default_time_axis_for_channel(channel: str) -> str:
    text = str(channel).strip().upper()
    if text.startswith("G"):
        return "green_axis"
    if text.startswith("R"):
        return "red_axis"
    return f"{filesystem_safe_name(text.lower(), fallback='lif')}_axis"


def detector_from_time_axis(time_axis: str) -> str:
    text = str(time_axis).strip().lower()
    if text.startswith("green"):
        return "green"
    if text.startswith("red"):
        return "red"
    return text.replace("_axis", "") or "lif"


def normalize_acquisition_layout(
    layout: dict[str, Any] | None = None,
    *,
    identities: dict[str, str] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    identities = identities or {}
    try:
        source_layout_version = int((layout or {}).get("layout_version", 0)) if isinstance(layout, dict) else 0
    except (TypeError, ValueError):
        source_layout_version = 0
    protocol_layout = source_layout_version >= ACQUISITION_LAYOUT_VERSION
    raw_channels = (layout or {}).get("lif_channels") if isinstance(layout, dict) else None
    lif_channels: list[dict[str, Any]] = []
    if isinstance(raw_channels, list) and raw_channels:
        for index, item in enumerate(raw_channels, start=1):
            if not isinstance(item, dict):
                raise BadRequest("acquisition_layout.lif_channels entries must be objects")
            channel = str(item.get("channel") or "").strip().upper()
            if not channel:
                raise BadRequest("每个 LIF 通道配置必须填写 channel")
            identity = str(item.get("identity_prior") or identities.get(channel) or "").strip()
            time_axis = str(item.get("time_axis") or default_time_axis_for_channel(channel)).strip()
            input_id = str(item.get("input_id") or f"lif_{index}_raw").strip()
            lif_channels.append(
                {
                    "input_id": input_id,
                    "channel": channel,
                    "identity_prior": identity,
                    "time_axis": time_axis,
                    "detector": str(item.get("detector") or detector_from_time_axis(time_axis)).strip().lower(),
                    "use_for_cell_annotation": bool(item.get("use_for_cell_annotation", True)),
                }
            )
    else:
        defaults = [("lif_g2_raw", "G2", "Day0"), ("lif_r1_raw", "R1", "Day9"), ("lif_r2_raw", "R2", "Day3")]
        for input_id, channel, fallback_identity in defaults:
            identity = str(identities.get(channel) or fallback_identity)
            time_axis = default_time_axis_for_channel(channel)
            lif_channels.append(
                {
                    "input_id": input_id,
                    "channel": channel,
                    "identity_prior": identity,
                    "time_axis": time_axis,
                    "detector": detector_from_time_axis(time_axis),
                    "use_for_cell_annotation": True,
                }
            )
    if not MIN_LIF_INPUTS <= len(lif_channels) <= MAX_LIF_INPUTS:
        raise BadRequest(f"项目必须配置 {MIN_LIF_INPUTS}-{MAX_LIF_INPUTS} 个 LIF 通道")
    channels = [row["channel"] for row in lif_channels]
    if len(set(channels)) != len(channels):
        raise BadRequest("LIF 通道名不能重复")
    if not any(bool(row.get("use_for_cell_annotation")) for row in lif_channels):
        raise BadRequest("至少一个 LIF 通道必须用于细胞标注")
    if qc_anchor_channels is not None:
        raw_anchors = qc_anchor_channels
    elif isinstance(layout, dict) and "qc_anchor_channels" in layout:
        raw_anchors = layout.get("qc_anchor_channels")
    elif protocol_layout:
        raw_anchors = []
    else:
        raw_anchors = ["G2", "R1"]
    if not isinstance(raw_anchors, (list, tuple)):
        raise BadRequest("acquisition_layout.qc_anchor_channels must be a list")
    anchors = list(raw_anchors)
    anchors = [str(ch).strip().upper() for ch in anchors if str(ch).strip()]
    if len(anchors) != len(set(anchors)):
        raise BadRequest("QC anchor 通道不能重复")
    max_anchor_count = min(MAX_QC_ANCHOR_CHANNELS, len(channels))
    if anchors and not MIN_QC_ANCHOR_CHANNELS <= len(anchors) <= max_anchor_count:
        raise BadRequest(f"QC anchor 必须选择 {MIN_QC_ANCHOR_CHANNELS}-{max_anchor_count} 个 LIF 通道")
    if not anchors and not protocol_layout:
        raise BadRequest(f"QC anchor 必须选择 {MIN_QC_ANCHOR_CHANNELS}-{max_anchor_count} 个 LIF 通道")
    missing = [ch for ch in anchors if ch not in channels]
    if missing:
        raise BadRequest(f"QC anchor 通道不在 LIF 通道配置中: {', '.join(missing)}")
    axis_by_channel = {row["channel"]: row["time_axis"] for row in lif_channels}
    if len(anchors) == 2 and set(anchors) == {"G2", "R1"}:
        anchors = ["G2", "R1"]
    else:
        anchors = [channel for channel in channels if channel in set(anchors)]
    covered_axes = {str(axis_by_channel[channel]) for channel in anchors}
    if anchors:
        required_axes = {
            str(row["time_axis"])
            for row in lif_channels
            if bool(row.get("use_for_cell_annotation", True))
        }
        missing_axes = sorted(required_axes - covered_axes)
        if missing_axes:
            raise BadRequest(f"QC anchor 必须至少覆盖每条标注时间轴，当前缺少: {', '.join(missing_axes)}")
        detector_by_channel = {
            row["channel"]: str(row.get("detector") or detector_from_time_axis(row["time_axis"])).strip().lower()
            for row in lif_channels
        }
        covered_detectors = {detector_by_channel[channel] for channel in anchors}
        missing_detectors = sorted({"green", "red"} - covered_detectors)
        if missing_detectors:
            raise BadRequest("QC anchor 必须至少包含一个绿色通道和一个红色通道")
    return {
        "layout_version": ACQUISITION_LAYOUT_VERSION,
        "source_layout_version": source_layout_version or ACQUISITION_LAYOUT_VERSION,
        "lif_channels": lif_channels,
        "qc_anchor_channels": anchors,
        "channel_time_axes": axis_by_channel,
        "qc_anchor_time_axes": sorted(covered_axes),
    }


def _reference_mode_for_channels(
    channels: list[str],
    detector_by_channel: dict[str, str],
) -> str:
    detectors = {str(detector_by_channel[channel]).strip().lower() for channel in channels}
    if detectors == {"green"}:
        return "green_only"
    if detectors == {"red"}:
        return "red_only"
    if detectors == {"green", "red"}:
        return "red_green"
    raise BadRequest(
        "校准参考段只支持 Green-only、Red-only 或 Red+Green 检测器组合"
    )


def normalize_calibration_protocol(
    protocol: dict[str, Any] | None,
    acquisition_layout: dict[str, Any] | None,
    *,
    require_confirmed: bool = True,
) -> dict[str, Any]:
    if not isinstance(protocol, dict):
        raise BadRequest("calibration_protocol 必须是对象")
    layout = normalize_acquisition_layout(acquisition_layout)
    channel_order = [str(row["channel"]) for row in layout["lif_channels"]]
    channel_set = set(channel_order)
    axis_by_channel = {
        str(channel): str(axis) for channel, axis in layout["channel_time_axes"].items()
    }
    detector_by_channel = {
        str(row["channel"]): str(row.get("detector") or detector_from_time_axis(row["time_axis"])).lower()
        for row in layout["lif_channels"]
    }
    raw_segments = protocol.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise BadRequest("calibration_protocol 至少需要一个分段参考窗口")

    segments: list[dict[str, Any]] = []
    segment_ids: set[str] = set()
    previous_order: int | None = None
    previous_end: float | None = None
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise BadRequest("calibration_protocol.segments entries must be objects")
        segment_id = str(raw.get("segment_id") or f"reference_{index}").strip()
        if not segment_id:
            raise BadRequest("每个校准参考段必须有 segment_id")
        if segment_id in segment_ids:
            raise BadRequest(f"校准参考段 segment_id 不能重复: {segment_id}")
        segment_ids.add(segment_id)
        try:
            order = int(raw.get("order", index))
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"校准参考段 {segment_id} 的顺序必须是整数") from exc
        if order != index or (previous_order is not None and order <= previous_order):
            raise BadRequest("校准参考段顺序必须与列表及时间先后严格一致")
        previous_order = order
        try:
            start_min = float(raw.get("start_min"))
            end_min = float(raw.get("end_min"))
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"校准参考段 {segment_id} 缺失有效起止边界") from exc
        if not math.isfinite(start_min) or not math.isfinite(end_min) or start_min < 0 or end_min <= start_min:
            raise BadRequest(f"校准参考段 {segment_id} 的边界必须有限且 end_min > start_min >= 0")
        if previous_end is not None and start_min < previous_end - 1e-9:
            raise BadRequest(f"校准参考段不能重叠: {segment_id}")
        previous_end = end_min
        confirmed = bool(raw.get("boundaries_confirmed", False))
        if require_confirmed and not confirmed:
            raise BadRequest(f"校准参考段 {segment_id} 的边界必须由用户确认")
        raw_channels = raw.get("reference_channels")
        if not isinstance(raw_channels, (list, tuple)) or not raw_channels:
            raise BadRequest(f"校准参考段 {segment_id} 缺失 reference_channels")
        requested = [str(channel).strip().upper() for channel in raw_channels if str(channel).strip()]
        if not requested or len(requested) != len(set(requested)):
            raise BadRequest(f"校准参考段 {segment_id} 的参考通道不能为空或重复")
        missing = sorted(set(requested) - channel_set)
        if missing:
            raise BadRequest(f"校准参考段 {segment_id} 包含未知通道: {', '.join(missing)}")
        requested_set = set(requested)
        channels = [channel for channel in channel_order if channel in requested_set]
        derived_mode = _reference_mode_for_channels(channels, detector_by_channel)
        declared_mode = str(raw.get("reference_mode") or derived_mode).strip().lower()
        if declared_mode not in CALIBRATION_REFERENCE_MODES:
            raise BadRequest(f"校准参考段 {segment_id} 的 reference_mode 不受支持")
        if declared_mode != derived_mode:
            raise BadRequest(
                f"校准参考段 {segment_id} 的 reference_mode={declared_mode} 与通道检测器组合 {derived_mode} 不一致"
            )
        axes = sorted({axis_by_channel[channel] for channel in channels})
        segments.append(
            {
                "segment_id": segment_id,
                "order": order,
                "start_min": start_min,
                "end_min": end_min,
                "reference_channels": channels,
                "reference_mode": derived_mode,
                "population_label": str(raw.get("population_label") or "").strip(),
                "boundaries_confirmed": confirmed,
                "time_axes": axes,
            }
        )

    reference_set = {
        channel for segment in segments for channel in segment["reference_channels"]
    }
    reference_channels = [channel for channel in channel_order if channel in reference_set]
    calibration_axes = sorted(
        {axis_by_channel[channel] for channel in reference_channels}
    )
    required_cell_axes = {
        str(row["time_axis"])
        for row in layout["lif_channels"]
        if bool(row.get("use_for_cell_annotation", True))
    }
    missing_axes = sorted(required_cell_axes - set(calibration_axes))
    if missing_axes:
        raise BadRequest(
            "calibration_protocol 未覆盖启用细胞标注的物理轴: " + ", ".join(missing_axes)
        )
    normalized = {
        "protocol_version": CALIBRATION_PROTOCOL_VERSION,
        "segments": segments,
        "reference_channels": reference_channels,
        "calibration_time_axes": calibration_axes,
        "boundaries_confirmed": all(bool(row["boundaries_confirmed"]) for row in segments),
    }
    compatibility_mode = str(protocol.get("compatibility_mode") or "").strip()
    if compatibility_mode:
        normalized["compatibility_mode"] = compatibility_mode
    return normalized


def suggest_calibration_segment_windows(
    lif_peaks: pd.DataFrame,
    segments: list[dict[str, Any]],
    *,
    annotation_start_min: float,
    cluster_gap_min: float = 1.50,
    edge_margin_min: float = 0.25,
) -> dict[str, Any]:
    """Suggest ordered project-local reference windows without confirming them.

    The result is deliberately advisory: every returned segment has
    ``boundaries_confirmed=False``.  Evidence is clustered independently for
    each declared segment and then jointly arbitrated so sequential segments
    cannot overlap or appear out of order.
    """

    try:
        annotation_start = float(annotation_start_min)
        gap_limit = float(cluster_gap_min)
        edge_margin = float(edge_margin_min)
    except (TypeError, ValueError) as exc:
        raise BadRequest("参考窗口建议参数必须是有效数值") from exc
    if not math.isfinite(annotation_start) or annotation_start <= 0:
        raise BadRequest("事件标注起点必须大于 0 min 才能建议前段参考窗口")
    if not math.isfinite(gap_limit) or gap_limit <= 0:
        raise BadRequest("参考峰簇间隔阈值必须大于 0 min")
    if not math.isfinite(edge_margin) or edge_margin <= 0:
        raise BadRequest("参考窗口边缘留白必须大于 0 min")
    if not isinstance(segments, list) or not segments:
        raise BadRequest("至少需要一个校准参考段才能建议窗口")

    declared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(segments, start=1):
        if not isinstance(raw, dict):
            raise BadRequest("校准参考段必须是对象")
        segment_id = str(raw.get("segment_id") or f"reference_{index}").strip()
        if not segment_id or segment_id in seen_ids:
            raise BadRequest("校准参考段 segment_id 不能为空或重复")
        seen_ids.add(segment_id)
        try:
            order = int(raw.get("order", index))
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"校准参考段 {segment_id} 的顺序必须是整数") from exc
        if order != index:
            raise BadRequest("窗口建议前，参考段列表必须按 order 连续排列")
        raw_channels = raw.get("reference_channels")
        if not isinstance(raw_channels, (list, tuple)):
            raise BadRequest(f"校准参考段 {segment_id} 缺失 reference_channels")
        channels = [str(value).strip().upper() for value in raw_channels if str(value).strip()]
        if not channels or len(channels) != len(set(channels)):
            raise BadRequest(f"校准参考段 {segment_id} 的参考通道不能为空或重复")
        declared.append(
            {
                "segment_id": segment_id,
                "order": order,
                "reference_channels": channels,
                "population_label": str(raw.get("population_label") or "").strip(),
            }
        )

    peaks = (
        automatic_lif_peak_evidence(lif_peaks).copy()
        if isinstance(lif_peaks, pd.DataFrame)
        else pd.DataFrame()
    )
    if not peaks.empty and "peak_stage" in peaks.columns and peaks["peak_stage"].eq("merged").any():
        peaks = peaks[peaks["peak_stage"].eq("merged")].copy()
    if "channel" not in peaks.columns or "time_min" not in peaks.columns:
        peaks = pd.DataFrame(columns=["channel", "time_min", "snr"])
    else:
        peaks["channel"] = peaks["channel"].astype(str).str.strip().str.upper()
        peaks["time_min"] = pd.to_numeric(peaks["time_min"], errors="coerce")
        if "snr" not in peaks.columns:
            peaks["snr"] = 1.0
        peaks["snr"] = pd.to_numeric(peaks["snr"], errors="coerce").fillna(1.0).clip(lower=0.0)
        peaks = peaks[
            peaks["time_min"].notna()
            & peaks["time_min"].ge(0.0)
            & peaks["time_min"].lt(annotation_start)
        ].copy()

    candidates_by_segment: list[list[dict[str, Any]]] = []
    for segment in declared:
        channels = segment["reference_channels"]
        selected = peaks[peaks["channel"].isin(channels)].sort_values(
            ["time_min", "channel"], kind="mergesort"
        )
        groups: list[pd.DataFrame] = []
        if not selected.empty:
            group_key = selected["time_min"].diff().fillna(0.0).gt(gap_limit).cumsum()
            groups = [group.copy() for _, group in selected.groupby(group_key, sort=False)]
        segment_candidates: list[dict[str, Any]] = []
        for group in groups:
            observed_channels = sorted(set(group["channel"].astype(str)))
            if not set(channels).issubset(observed_channels):
                continue
            first_peak = float(group["time_min"].min())
            last_peak = float(group["time_min"].max())
            span = max(0.0, last_peak - first_peak)
            margin = min(0.75, max(edge_margin, span * 0.08))
            start_min = max(0.0, first_peak - margin)
            end_min = min(annotation_start, last_peak + margin)
            if end_min <= start_min:
                continue
            score = float(np.log1p(group["snr"].to_numpy(float)).sum())
            score += 0.25 * float(len(group))
            segment_candidates.append(
                {
                    "suggested_start_min": round(start_min, 3),
                    "suggested_end_min": round(end_min, 3),
                    "first_peak_min": round(first_peak, 6),
                    "last_peak_min": round(last_peak, 6),
                    "peak_count": int(len(group)),
                    "observed_channels": observed_channels,
                    "score": score,
                }
            )
        segment_candidates.sort(
            key=lambda row: (-float(row["score"]), float(row["suggested_start_min"]), float(row["suggested_end_min"]))
        )
        candidates_by_segment.append(segment_candidates[:12])

    base_rows = [
        {
            **segment,
            "status": "missing_evidence" if not candidates else "evidence_available",
            "suggested_start_min": None,
            "suggested_end_min": None,
            "peak_count": 0,
            "score": None,
            "alternative_count": len(candidates),
            "boundaries_confirmed": False,
        }
        for segment, candidates in zip(declared, candidates_by_segment)
    ]
    missing_ids = [
        declared[index]["segment_id"]
        for index, candidates in enumerate(candidates_by_segment)
        if not candidates
    ]
    if missing_ids:
        warnings = ["以下参考段缺少完整通道峰形证据: " + ", ".join(missing_ids)]
        return {
            "segments": base_rows,
            "can_apply_suggestions": False,
            "requires_user_confirmation": True,
            "warnings": warnings,
        }

    valid_sequences: list[tuple[float, tuple[dict[str, Any], ...]]] = []

    def collect_sequences(
        segment_index: int,
        chosen: tuple[dict[str, Any], ...],
        score: float,
    ) -> None:
        if len(valid_sequences) >= 4096:
            return
        if segment_index >= len(candidates_by_segment):
            valid_sequences.append((score, chosen))
            return
        previous_end = float(chosen[-1]["suggested_end_min"]) if chosen else None
        for candidate in candidates_by_segment[segment_index]:
            if previous_end is not None and float(candidate["suggested_start_min"]) <= previous_end + 1e-9:
                continue
            collect_sequences(
                segment_index + 1,
                chosen + (candidate,),
                score + float(candidate["score"]),
            )

    collect_sequences(0, (), 0.0)
    if not valid_sequences:
        for row in base_rows:
            row["status"] = "order_conflict"
        return {
            "segments": base_rows,
            "can_apply_suggestions": False,
            "requires_user_confirmation": True,
            "warnings": ["峰形证据无法组成严格按序且不重叠的参考窗口，请人工检查错序或重叠。"],
        }

    valid_sequences.sort(
        key=lambda item: (
            -float(item[0]),
            tuple(float(row["suggested_start_min"]) for row in item[1]),
        )
    )
    best_score, best_sequence = valid_sequences[0]
    ambiguity_tolerance = max(1e-9, abs(float(best_score)) * 0.05)
    near_best = [
        sequence
        for score, sequence in valid_sequences
        if float(best_score) - float(score) <= ambiguity_tolerance
    ]
    result_rows: list[dict[str, Any]] = []
    for index, (segment, chosen) in enumerate(zip(declared, best_sequence)):
        alternative_windows = {
            (float(sequence[index]["suggested_start_min"]), float(sequence[index]["suggested_end_min"]))
            for sequence in near_best
        }
        ambiguous = len(alternative_windows) > 1
        result_rows.append(
            {
                **segment,
                **chosen,
                "status": "ambiguous" if ambiguous else "suggested",
                "alternative_count": len(alternative_windows),
                "boundaries_confirmed": False,
            }
        )
    warnings = []
    if any(row["status"] == "ambiguous" for row in result_rows):
        warnings.append("存在分数接近的多个峰簇窗口；已回填最高分方案，必须人工核对后确认。")
    return {
        "segments": result_rows,
        "can_apply_suggestions": True,
        "requires_user_confirmation": True,
        "warnings": warnings,
    }


def calibration_protocol_hash(
    protocol: dict[str, Any],
    acquisition_layout: dict[str, Any] | None,
) -> str:
    layout = normalize_acquisition_layout(acquisition_layout)
    # Confirmation is a workflow gate, not part of the physical boundary
    # identity. Draft projects therefore need the same stable protocol hash
    # before and after a user confirms unchanged boundaries.
    normalized = normalize_calibration_protocol(
        protocol,
        layout,
        require_confirmed=False,
    )
    channel_physics = {
        str(row["channel"]): {
            "detector": str(row["detector"]),
            "time_axis": str(row["time_axis"]),
        }
        for row in layout["lif_channels"]
    }
    payload = {
        "protocol_version": normalized["protocol_version"],
        "matcher_semantics": (
            "legacy_qc_anchor_channels"
            if normalized.get("compatibility_mode")
            else "segmented_calibration_v2_monotone_sequence"
        ),
        "channel_physics": channel_physics,
        "segments": [
            {
                "segment_id": row["segment_id"],
                "order": row["order"],
                "start_min": row["start_min"],
                "end_min": row["end_min"],
                "reference_channels": row["reference_channels"],
                "reference_mode": row["reference_mode"],
            }
            for row in normalized["segments"]
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def calibration_protocol_from_manifest(
    manifest: dict[str, Any] | None,
    project_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = project_config or {}
    layout = acquisition_layout_from_manifest(manifest)
    configured = config.get("calibration_protocol")
    if not isinstance(configured, dict) and manifest:
        configured = manifest.get("calibration_protocol")
    if isinstance(configured, dict):
        # Project storage may contain a deliberately unconfirmed draft. Code
        # that performs calibration still calls normalize_calibration_protocol
        # with its default require_confirmed=True gate.
        return normalize_calibration_protocol(
            copy.deepcopy(configured),
            layout,
            require_confirmed=False,
        )
    legacy_channels = list(layout.get("qc_anchor_channels") or [])
    if not legacy_channels:
        raise BadRequest("项目缺少 calibration_protocol，且没有可只读适配的旧 qc_anchor_channels")
    try:
        qc_end = float(config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN))
    except (TypeError, ValueError) as exc:
        raise BadRequest("旧项目 qc_calibration_end_min 无效") from exc
    detector_by_channel = {
        str(row["channel"]): str(row["detector"]) for row in layout["lif_channels"]
    }
    legacy = {
        "protocol_version": CALIBRATION_PROTOCOL_VERSION,
        "compatibility_mode": "v0.3_qc_anchor_channels",
        "segments": [
            {
                "segment_id": "legacy_qc_calibration",
                "order": 1,
                "start_min": 0.0,
                "end_min": qc_end,
                "reference_channels": legacy_channels,
                "reference_mode": _reference_mode_for_channels(legacy_channels, detector_by_channel),
                "population_label": "legacy QC reference",
                "boundaries_confirmed": True,
            }
        ],
    }
    return normalize_calibration_protocol(legacy, layout)


def normalize_post_qc_strategy(
    strategy: dict[str, Any] | None,
    acquisition_layout: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(strategy, dict):
        raise BadRequest("post_qc_strategy 必须是对象")
    layout = normalize_acquisition_layout(acquisition_layout)
    channel_order = [str(row["channel"]) for row in layout["lif_channels"]]
    channel_set = set(channel_order)
    detector_by_channel = {
        str(row["channel"]): str(row["detector"]) for row in layout["lif_channels"]
    }
    mode = str(strategy.get("mode") or "").strip().lower()
    if mode not in POST_QC_STRATEGY_MODES:
        raise BadRequest(
            "post_qc_strategy.mode 必须是 signature、scheduled_windows 或 disabled"
        )

    def normalized_channels(raw: Any, label: str) -> list[str]:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise BadRequest(f"{label} 必须配置至少一个参考通道")
        values = [str(channel).strip().upper() for channel in raw if str(channel).strip()]
        if not values or len(values) != len(set(values)):
            raise BadRequest(f"{label} 的参考通道不能为空或重复")
        missing = sorted(set(values) - channel_set)
        if missing:
            raise BadRequest(f"{label} 包含未知通道: {', '.join(missing)}")
        selected = set(values)
        return [channel for channel in channel_order if channel in selected]

    compatibility_mode = str(strategy.get("compatibility_mode") or "").strip()
    if mode == "disabled":
        normalized: dict[str, Any] = {
            "strategy_version": POST_QC_STRATEGY_VERSION,
            "mode": "disabled",
            "reference_channels": [],
            "windows": [],
        }
    elif mode == "signature":
        channels = normalized_channels(strategy.get("reference_channels"), "QC signature")
        normalized = {
            "strategy_version": POST_QC_STRATEGY_VERSION,
            "mode": "signature",
            "reference_channels": channels,
            "reference_mode": _reference_mode_for_channels(channels, detector_by_channel),
            "windows": [],
        }
    else:
        raw_windows = strategy.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise BadRequest("scheduled_windows 策略至少需要一个窗口")
        windows: list[dict[str, Any]] = []
        window_ids: set[str] = set()
        previous_end: float | None = None
        for index, raw in enumerate(raw_windows, start=1):
            if not isinstance(raw, dict):
                raise BadRequest("scheduled_windows entries must be objects")
            window_id = str(raw.get("window_id") or f"post_qc_{index}").strip()
            if window_id in window_ids:
                raise BadRequest(f"后段 QC window_id 不能重复: {window_id}")
            window_ids.add(window_id)
            try:
                start_min = float(raw.get("start_min"))
                end_min = float(raw.get("end_min"))
            except (TypeError, ValueError) as exc:
                raise BadRequest(f"后段 QC 窗口 {window_id} 缺失有效边界") from exc
            if not math.isfinite(start_min) or not math.isfinite(end_min) or start_min < 0 or end_min <= start_min:
                raise BadRequest(f"后段 QC 窗口 {window_id} 边界无效")
            if previous_end is not None and start_min < previous_end - 1e-9:
                raise BadRequest(f"后段 QC scheduled windows 不能重叠: {window_id}")
            previous_end = end_min
            channels = normalized_channels(
                raw.get("reference_channels") or strategy.get("reference_channels"),
                f"后段 QC 窗口 {window_id}",
            )
            windows.append(
                {
                    "window_id": window_id,
                    "order": index,
                    "start_min": start_min,
                    "end_min": end_min,
                    "reference_channels": channels,
                    "reference_mode": _reference_mode_for_channels(channels, detector_by_channel),
                }
            )
        union = {channel for row in windows for channel in row["reference_channels"]}
        normalized = {
            "strategy_version": POST_QC_STRATEGY_VERSION,
            "mode": "scheduled_windows",
            "reference_channels": [channel for channel in channel_order if channel in union],
            "windows": windows,
        }
    if compatibility_mode:
        normalized["compatibility_mode"] = compatibility_mode
    return normalized


def validate_post_qc_strategy_timing(
    strategy: dict[str, Any],
    calibration_end_min: float,
) -> None:
    """Reject scheduled post-QC windows that overlap front calibration.

    Candidate generation must not silently clip a user-configured window.  The
    calibration boundary is project-owned, so this validation deliberately
    lives outside the layout-only strategy normalizer.
    """
    try:
        boundary = float(calibration_end_min)
    except (TypeError, ValueError) as exc:
        raise BadRequest("calibration end must be numeric") from exc
    if not math.isfinite(boundary) or boundary < 0:
        raise BadRequest("calibration end must be a finite non-negative number")
    if str(strategy.get("mode") or "") != "scheduled_windows":
        return
    for window in strategy.get("windows") or []:
        start_min = float(window["start_min"])
        if start_min < boundary - 1e-9:
            raise BadRequest(
                f"后段 QC 窗口 {window['window_id']} 从 {start_min:g} min 开始，"
                f"与前段校准（结束于 {boundary:g} min）重叠；请完整移到前段之后"
            )


def post_qc_strategy_from_manifest(
    manifest: dict[str, Any] | None,
    project_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = project_config or {}
    layout = acquisition_layout_from_manifest(manifest)
    configured = config.get("post_qc_strategy")
    if not isinstance(configured, dict) and manifest:
        configured = manifest.get("post_qc_strategy")
    if isinstance(configured, dict):
        return normalize_post_qc_strategy(copy.deepcopy(configured), layout)
    legacy_channels = list(layout.get("qc_anchor_channels") or [])
    if not legacy_channels:
        return normalize_post_qc_strategy({"mode": "disabled"}, layout)
    return normalize_post_qc_strategy(
        {
            "mode": "signature",
            "reference_channels": legacy_channels,
            "compatibility_mode": "v0.3_qc_anchor_channels",
        },
        layout,
    )


def post_qc_strategy_hash(
    strategy: dict[str, Any],
    acquisition_layout: dict[str, Any] | None,
) -> str:
    normalized = normalize_post_qc_strategy(strategy, acquisition_layout)
    payload = {
        key: value
        for key, value in normalized.items()
        if key != "compatibility_mode"
    }
    payload["matcher_semantics"] = (
        "legacy_qc_anchor_channels"
        if normalized.get("compatibility_mode")
        else "post_qc_strategy_v2_monotone_sequence"
    )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def project_config_defaults_from_manifest(
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    protocol = calibration_protocol_from_manifest(manifest, {})
    strategy = post_qc_strategy_from_manifest(manifest, {})
    annotation_config = (
        manifest.get("annotation_config", {})
        if isinstance(manifest, dict) and isinstance(manifest.get("annotation_config"), dict)
        else {}
    )
    protocol_end = max(float(row["end_min"]) for row in protocol["segments"])
    validate_post_qc_strategy_timing(strategy, protocol_end)
    try:
        annotation_start = float(
            annotation_config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
        )
        seed_window = float(
            annotation_config.get(
                "local_delta_seed_window_min", DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN
            )
        )
    except (TypeError, ValueError) as exc:
        raise BadRequest("项目 annotation_config 时间参数无效") from exc
    if annotation_start < protocol_end:
        raise BadRequest("项目 annotation_start_min 早于 calibration_protocol 结束时间")
    if seed_window <= 0:
        raise BadRequest("项目 local_delta_seed_window_min 必须大于 0")
    return {
        "qc_calibration_end_min": protocol_end,
        "sample_valve_switch_min": float(
            annotation_config.get("sample_valve_switch_min", DEFAULT_SAMPLE_VALVE_SWITCH_MIN)
        ),
        "annotation_start_min": annotation_start,
        "local_delta_seed_window_min": seed_window,
        "calibration_protocol": protocol,
        "post_qc_strategy": strategy,
    }


def acquisition_layout_hash(layout: dict[str, Any] | None) -> str:
    normalized = normalize_acquisition_layout(layout)
    payload = {
        "lif_channels": [
            {
                "channel": row["channel"],
                "time_axis": row["time_axis"],
                "use_for_cell_annotation": bool(row.get("use_for_cell_annotation", True)),
            }
            for row in normalized["lif_channels"]
        ],
        "qc_anchor_channels": normalized["qc_anchor_channels"],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def is_legacy_qc_anchor_pair(anchor_channels: list[str] | tuple[str, ...]) -> bool:
    return [str(channel).strip().upper() for channel in anchor_channels] == ["G2", "R1"]


def is_legacy_acquisition_layout(layout: dict[str, Any] | None) -> bool:
    normalized = normalize_acquisition_layout(layout)
    channels = [row["channel"] for row in normalized["lif_channels"]]
    axes = normalized["channel_time_axes"]
    return (
        channels == ["G2", "R1", "R2"]
        and is_legacy_qc_anchor_pair(normalized["qc_anchor_channels"])
        and axes == {"G2": "green_axis", "R1": "red_axis", "R2": "red_axis"}
    )


def acquisition_layout_from_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if manifest and isinstance(manifest.get("acquisition_layout"), dict):
        raw = manifest["acquisition_layout"]
        identities = {
            str(row.get("channel", "")).strip().upper(): str(row.get("identity_prior", ""))
            for row in raw.get("lif_channels", [])
            if isinstance(row, dict)
        }
        return normalize_acquisition_layout(raw, identities=identities)
    identities: dict[str, str] = {}
    if manifest and isinstance(manifest.get("channel_identity_prior"), dict):
        for channel, value in manifest["channel_identity_prior"].items():
            identities[str(channel).strip().upper()] = str(value.get("identity_prior") if isinstance(value, dict) else value)
    return normalize_acquisition_layout(identities=identities)


def channel_identity_prior_from_manifest(manifest: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    if not manifest:
        return {}
    layout = acquisition_layout_from_manifest(manifest)
    if layout:
        return channel_identity_prior_from_values(
            {row["channel"]: row.get("identity_prior", "") for row in layout["lif_channels"]},
            source="project_manifest",
        )
    raw = manifest.get("channel_identity_prior", {})
    if not isinstance(raw, dict):
        return {}
    values: dict[str, str] = {}
    for channel in ["G2", "R1", "R2"]:
        value = raw.get(channel)
        if isinstance(value, dict):
            value = value.get("identity_prior")
        if value:
            values[channel] = str(value)
    return channel_identity_prior_from_values(values, source="project_manifest")


def read_project_manifest(project_dir: Path) -> dict[str, Any] | None:
    path = project_manifest_path(project_dir)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} must contain a JSON object")
    return data


def lif_peak_detection_from_manifest(
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the active detector without migrating retired project artifacts."""

    configured = (
        manifest.get("lif_peak_detection")
        if isinstance(manifest, dict)
        else None
    )
    if isinstance(configured, dict):
        try:
            normalized = require_active_lif_peak_detection(
                copy.deepcopy(configured)
            )
        except ValueError as exc:
            raise BadRequest(f"项目峰识别配置无效：{exc}") from exc
        if configured != normalized:
            raise BadRequest(
                "项目峰识别配置不完整；请在新的空目录中从原始输入重跑预处理，"
                "原项目不会被修改。"
            )
        expected_hash = lif_peak_detection_hash(normalized)
        declared_hash = str((manifest or {}).get("lif_peak_detection_hash") or "").strip().lower()
        if declared_hash != expected_hash:
            raise BadRequest(
                "项目峰识别配置与中间表绑定不一致；"
                "请使用原项目副本重新生成中间表，不要继续加载可能混用的峰表。"
            )
        return normalized
    raise BadRequest(
        "该项目使用已停用的旧峰识别标准。"
        "请不要修改原项目；请在新的空目录中重新选择原始 LIF、MS 和事件坐标 "
        "CSV，并用当前版本重跑预处理。旧人工标注不会自动迁移。"
    )


def validate_and_adapt_lif_peak_detector_binding(
    lif_peaks: pd.DataFrame,
    peak_detection: dict[str, Any],
    *,
    explicit_peak_detection: bool,
) -> pd.DataFrame:
    """Validate every raw/merged peak row against its immutable detector."""

    peaks = lif_peaks.copy()
    required = {"peak_tier", "detector_version", "detector_config_hash"}
    present = required.intersection(peaks.columns)
    expected_version = int(peak_detection["detector_version"])
    expected_hash = lif_peak_detection_hash(peak_detection)

    if explicit_peak_detection:
        missing = sorted(required - set(peaks.columns))
        if missing:
            raise BadRequest(
                "LIF peak 表缺少项目 detector 绑定字段: " + ", ".join(missing)
            )
    elif not present:
        raise BadRequest(
            "LIF 峰表没有当前峰识别规则所需的绑定信息。"
            "请在新的空目录中从原始输入重跑预处理；原项目不会被修改，"
            "旧人工标注不会自动迁移。"
        )
    elif present != required:
        raise BadRequest(
            "旧项目 LIF 峰表的识别规则绑定信息不完整，无法安全加载；"
            "请使用未改动的原项目副本。"
        )

    tier_series = (
        peaks["peak_tier"].fillna("").astype(str).str.strip().str.lower()
    )
    version_series = pd.to_numeric(peaks["detector_version"], errors="coerce")
    hash_series = (
        peaks["detector_config_hash"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )
    profile_series = (
        peaks["detector_profile"].fillna("").astype(str).str.strip().str.lower()
        if "detector_profile" in peaks.columns
        else None
    )
    weak_usage_series = (
        peaks["weak_usage"].fillna("").astype(str).str.strip().str.lower()
        if "weak_usage" in peaks.columns
        else None
    )
    invalid = bool(
        len(peaks)
        and (
            not tier_series.isin({"core", "weak"}).all()
            or version_series.isna().any()
            or not version_series.eq(expected_version).all()
            or not hash_series.eq(expected_hash).all()
            or (expected_version == 1 and tier_series.eq("weak").any())
            or (
                profile_series is not None
                and not profile_series.eq(
                    str(peak_detection["profile"]).strip().lower()
                ).all()
            )
            or (
                weak_usage_series is not None
                and not weak_usage_series.eq(
                    str(peak_detection["weak_usage"]).strip().lower()
                ).all()
            )
        )
    )
    if invalid:
        raise BadRequest(
            "LIF 峰表的识别规则或证据级别与项目配置不一致；"
            "请从原始输入在新项目或项目副本中重跑预处理。"
        )
    peaks["peak_tier"] = tier_series
    peaks["detector_version"] = version_series.astype("Int64")
    peaks["detector_config_hash"] = hash_series
    if profile_series is not None:
        peaks["detector_profile"] = profile_series
    if weak_usage_series is not None:
        peaks["weak_usage"] = weak_usage_series
    return peaks


def write_project_manifest(
    *,
    project_dir: Path,
    raw_input_mode: str,
    raw_inputs: dict[str, dict[str, Any]],
    channel_identity_prior: dict[str, str],
    intermediate_tables: dict[str, dict[str, Any]] | None = None,
    acquisition_layout: dict[str, Any] | None = None,
    cell_event_map: dict[str, Any] | None = None,
    calibration_protocol: dict[str, Any] | None = None,
    post_qc_strategy: dict[str, Any] | None = None,
    annotation_config: dict[str, Any] | None = None,
    lif_peak_detection: dict[str, Any] | None = None,
    annotation_db_path: str = "annotation_app/annotations/annotation.sqlite",
    storage_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binding = project_table_binding(intermediate_tables) if intermediate_tables else {}
    resolved_annotation_db = manifest_entry_path(
        project_dir,
        {"path": annotation_db_path},
    )
    annotation_db_relative = resolved_annotation_db.relative_to(
        project_dir.resolve()
    ).as_posix()
    layout = normalize_acquisition_layout(acquisition_layout, identities=channel_identity_prior)
    if calibration_protocol is None:
        legacy_channels = list(layout.get("qc_anchor_channels") or [])
        if not legacy_channels:
            raise BadRequest("新项目必须显式提供 calibration_protocol")
        detector_by_channel = {
            str(row["channel"]): str(row["detector"]) for row in layout["lif_channels"]
        }
        calibration_protocol = {
            "compatibility_mode": "legacy_creation_api",
            "segments": [
                {
                    "segment_id": "legacy_qc_calibration",
                    "order": 1,
                    "start_min": 0.0,
                    "end_min": QC_SHIFT_WINDOW_MIN,
                    "reference_channels": legacy_channels,
                    "reference_mode": _reference_mode_for_channels(legacy_channels, detector_by_channel),
                    "population_label": "legacy QC reference",
                    "boundaries_confirmed": True,
                }
            ],
        }
    normalized_protocol = normalize_calibration_protocol(
        calibration_protocol,
        layout,
        require_confirmed=False,
    )
    protocol_end_min = max(
        float(row["end_min"]) for row in normalized_protocol["segments"]
    )
    if post_qc_strategy is None:
        legacy_channels = list(layout.get("qc_anchor_channels") or normalized_protocol["reference_channels"])
        post_qc_strategy = {
            "mode": "signature",
            "reference_channels": legacy_channels,
            "compatibility_mode": "legacy_creation_api",
        }
    normalized_post_qc = normalize_post_qc_strategy(post_qc_strategy, layout)
    try:
        normalized_peak_detection = require_active_lif_peak_detection(
            lif_peak_detection
        )
    except ValueError as exc:
        raise BadRequest(f"LIF 峰识别配置无效：{exc}") from exc
    validate_post_qc_strategy_timing(normalized_post_qc, protocol_end_min)
    raw_annotation_config = annotation_config or {}
    try:
        annotation_start_min = float(
            raw_annotation_config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
        )
        seed_window_min = float(
            raw_annotation_config.get(
                "local_delta_seed_window_min", DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN
            )
        )
    except (TypeError, ValueError) as exc:
        raise BadRequest("annotation_config 时间参数必须是数字") from exc
    if not math.isfinite(annotation_start_min) or annotation_start_min < protocol_end_min:
        raise BadRequest("annotation_start_min 必须晚于或等于最后一个校准参考段")
    if not math.isfinite(seed_window_min) or seed_window_min <= 0:
        raise BadRequest("local_delta_seed_window_min 必须大于 0")
    normalized_annotation_config = {
        "annotation_start_min": annotation_start_min,
        "local_delta_seed_window_min": seed_window_min,
        "qc_calibration_end_min": protocol_end_min,
    }
    prior_values = {row["channel"]: row.get("identity_prior", "") for row in layout["lif_channels"]}
    manifest = {
        "project_id": uuid.uuid4().hex,
        "dataset_id": str(binding.get("binding_sha256") or hashlib.sha1(
            json.dumps(raw_inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()),
        "project_schema_version": PROJECT_SCHEMA_VERSION,
        "created_by_app_version": APP_VERSION,
        "raw_input_mode": normalize_raw_input_mode(raw_input_mode),
        "raw_inputs": raw_inputs,
        "acquisition_layout": layout,
        "calibration_protocol": normalized_protocol,
        "calibration_protocol_hash": calibration_protocol_hash(normalized_protocol, layout),
        "post_qc_strategy": normalized_post_qc,
        "post_qc_strategy_hash": post_qc_strategy_hash(normalized_post_qc, layout),
        "lif_peak_detection": normalized_peak_detection,
        "lif_peak_detection_hash": lif_peak_detection_hash(
            normalized_peak_detection
        ),
        "annotation_config": normalized_annotation_config,
        "intermediate_tables": intermediate_tables or {},
        "project_table_binding": binding,
        "annotation_db": {
            "path": annotation_db_relative,
            "schema_version": PROJECT_SCHEMA_VERSION,
        },
        "channel_identity_prior": prior_values,
        "updated_at": now_iso(),
    }
    if storage_layout is not None:
        manifest["storage_layout"] = copy.deepcopy(storage_layout)
    if cell_event_map is not None:
        manifest["cell_event_map"] = copy.deepcopy(cell_event_map)
    project_dir.mkdir(parents=True, exist_ok=True)
    project_manifest_path(project_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_raw_input_project_records(
    *,
    project_dir: Path,
    raw_paths: dict[str, Path],
    raw_input_mode: str,
    identities: dict[str, str],
    lif_inputs: list[dict[str, Any]] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
    calibration_protocol: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    mode = normalize_raw_input_mode(raw_input_mode)
    if lif_inputs is None:
        lif_inputs = [
            {"key": "lif_g2", "path": raw_paths["lif_g2"], "channel": "G2", "identity_prior": identities.get("G2", "Day0")},
            {"key": "lif_r1", "path": raw_paths["lif_r1"], "channel": "R1", "identity_prior": identities.get("R1", "Day9")},
            {"key": "lif_r2", "path": raw_paths["lif_r2"], "channel": "R2", "identity_prior": identities.get("R2", "Day3")},
        ]
    layout_input = {
        "layout_version": ACQUISITION_LAYOUT_VERSION if calibration_protocol is not None else 0,
        "lif_channels": [
            {
                "input_id": f"{str(item.get('key') or f'lif_{idx}').strip()}_raw",
                "channel": str(item.get("channel") or "").strip().upper(),
                "identity_prior": str(item.get("identity_prior") or identities.get(str(item.get("channel") or "").strip().upper(), "")),
                "time_axis": str(item.get("time_axis") or default_time_axis_for_channel(str(item.get("channel") or ""))),
                "detector": str(item.get("detector") or ""),
                "use_for_cell_annotation": bool(item.get("use_for_cell_annotation", True)),
            }
            for idx, item in enumerate(lif_inputs, start=1)
        ],
        "qc_anchor_channels": (
            []
            if calibration_protocol is not None and qc_anchor_channels is None
            else list(["G2", "R1"] if qc_anchor_channels is None else qc_anchor_channels)
        ),
    }
    layout = normalize_acquisition_layout(layout_input, identities=identities)
    if calibration_protocol is not None:
        normalize_calibration_protocol(
            calibration_protocol,
            layout,
            require_confirmed=False,
        )
    specs = []
    for item, channel_config in zip(lif_inputs, layout["lif_channels"]):
        key = str(item.get("key") or channel_config["input_id"].removesuffix("_raw"))
        specs.append(
            (
                key,
                channel_config["input_id"],
                "raw_lif_trace",
                channel_config["channel"],
                channel_config.get("identity_prior", ""),
                channel_config["detector"],
                bool(channel_config.get("use_for_cell_annotation")),
                "LIF trace physical QC and peak calling",
                ".csv",
                Path(item["path"]),
            )
        )
    specs.append(("ms", "ms_raw_txt", "raw_ms_spectra", "", "", "MS", False, "MS trace physical QC and event calling", ".txt", raw_paths["ms"]))
    rows: list[dict[str, Any]] = []
    manifest_inputs: dict[str, dict[str, Any]] = {}
    for key, input_id, input_class, channel, label, detector, use_for_cell_annotation, role, default_suffix, raw_path in specs:
        source_path = raw_path.expanduser()
        if mode == RAW_INPUT_MODE_COPY:
            suffix = source_path.suffix or default_suffix
            effective_path = project_dir / "raw_inputs" / f"{key}{suffix}"
        else:
            effective_path = source_path.resolve()
        path_value = manifest_path_value(effective_path, project_dir, mode)
        rows.append(
            {
                "input_id": input_id,
                "path": path_value,
                "input_class": input_class,
                "channel": channel,
                "label": str(label),
                "detector": detector,
                "role": role,
                "use_for_cell_annotation": bool(use_for_cell_annotation),
                "encoding_or_format": "ASCII mzML-like text export" if key == "ms" else "UTF-16 LE tab-delimited text",
            }
        )
        manifest_entry = {
            "path": path_value,
            "path_mode": mode,
        }
        if mode == RAW_INPUT_MODE_EXTERNAL:
            manifest_entry["original_source_path"] = str(source_path.resolve())
        else:
            manifest_entry["original_source_name"] = source_path.name
        manifest_inputs[key] = manifest_entry
    return rows, manifest_inputs, layout


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def filesystem_safe_name(value: str, *, fallback: str = "project") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value)).strip(" ._")
    cleaned = re.sub(r"_+", "_", cleaned)
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if not cleaned or cleaned.upper() in reserved:
        cleaned = fallback
    return cleaned[:80]


def export_filename_for_project(project_dir: Path, timestamp: datetime | None = None) -> str:
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{filesystem_safe_name(project_dir.name, fallback='lifms_project')}_{stamp}_accepted_annotations.csv"


def unique_file_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BadRequest(f"无法生成不覆盖现有文件的导出路径: {path}")


def qc_anchor_peak_id_map(group: dict[str, Any]) -> dict[str, str | None]:
    raw = group.get("lif_anchor_peak_ids")
    if isinstance(raw, dict):
        return {
            str(channel).strip().upper(): optional_peak_id(peak_id)
            for channel, peak_id in raw.items()
            if str(channel).strip()
        }
    anchors = group.get("lif_anchors")
    if isinstance(anchors, list):
        return {
            str(item.get("channel", "")).strip().upper(): optional_peak_id(item.get("peak_id"))
            for item in anchors
            if isinstance(item, dict) and str(item.get("channel", "")).strip()
        }
    anchor_a_channel = str(group.get("anchor_a_channel") or "").strip().upper()
    anchor_b_channel = str(group.get("anchor_b_channel") or "").strip().upper()
    if anchor_a_channel or anchor_b_channel:
        return {
            channel: optional_peak_id(peak_id)
            for channel, peak_id in [
                (anchor_a_channel, group.get("anchor_a_peak_id")),
                (anchor_b_channel, group.get("anchor_b_peak_id")),
            ]
            if channel
        }
    if "g2_peak_id" in group or "r1_peak_id" in group:
        return {
            "G2": optional_peak_id(group.get("g2_peak_id")),
            "R1": optional_peak_id(group.get("r1_peak_id")),
        }
    return {}


def has_dynamic_qc_anchor_payload(group: dict[str, Any]) -> bool:
    return isinstance(group.get("lif_anchor_peak_ids"), dict) or isinstance(group.get("lif_anchors"), list)


def dynamic_qc_candidate_digest(group: dict[str, Any]) -> str:
    anchor_map = qc_anchor_peak_id_map(group)
    payload = {
        "anchors": {channel: anchor_map[channel] or MISSING_PEAK_SYMBOL for channel in sorted(anchor_map)},
        "ms_event_id": str(group["ms_event_id"]),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def qc_group_plot_times(group: dict[str, Any]) -> list[float]:
    dynamic = group.get("lif_anchor_plot_times_min")
    if isinstance(dynamic, dict):
        values = [value for value in dynamic.values() if isinstance(value, (int, float))]
    else:
        values = [
            group.get("g2_plot_time_min"),
            group.get("r1_plot_time_min"),
        ]
    if isinstance(group.get("ms_plot_time_min"), (int, float)):
        values.append(group["ms_plot_time_min"])
    return [float(value) for value in values if isinstance(value, (int, float))]


def front_qc_group_belongs_to_window(
    group: dict[str, Any],
    window_start_min: float,
    window_end_min: float,
    *,
    context_margin_min: float = WINDOW_CONTEXT_MARGIN_MIN,
) -> bool:
    """Assign a front-QC relation to the window containing its MS event.

    A calibrated LIF peak and its MS event can legitimately straddle a display
    boundary by a fraction of a second.  The MS event provides a stable,
    one-dimensional owner for the review window; all related points must still
    be inside the context that the UI actually loaded and displayed.
    """

    start = float(window_start_min)
    end = float(window_end_min)
    ms_plot_time = group.get("ms_plot_time_min")
    if not isinstance(ms_plot_time, (int, float)):
        ms_plot_time = group.get("ms_time_min")
    if not isinstance(ms_plot_time, (int, float)):
        return False
    if not start <= float(ms_plot_time) <= end:
        return False
    plot_times = qc_group_plot_times(group)
    if not plot_times:
        return False
    context_start = start - max(0.0, float(context_margin_min))
    context_end = end + max(0.0, float(context_margin_min))
    return all(context_start <= value <= context_end for value in plot_times)


def saved_relation_belongs_to_window(
    *,
    ms_plot_time_min: float | None,
    lif_plot_times_min: list[float],
    window_start_min: float,
    window_end_min: float,
    context_start_min: float,
    context_end_min: float,
    context_margin_min: float = WINDOW_CONTEXT_MARGIN_MIN,
) -> bool:
    """Give a saved relation one window that can draw every endpoint.

    Normally the MS event owns the window.  A manually selected relation can,
    however, cross farther over a boundary than the fixed display context.  In
    that case the MS-owner window cannot draw the LIF endpoint at all.  The
    adjacent LIF window becomes the owner only when it can draw the complete
    relation and the normal MS-owner window cannot.  This prevents both the
    49.001-min "belongs nowhere" gap and duplicate lines in adjacent windows.
    """

    start = float(window_start_min)
    end = float(window_end_min)
    context_start = float(context_start_min)
    context_end = float(context_end_min)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return False
    lif_times = [
        float(value)
        for value in lif_plot_times_min
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    ms_time = (
        float(ms_plot_time_min)
        if isinstance(ms_plot_time_min, (int, float))
        and math.isfinite(float(ms_plot_time_min))
        else None
    )
    plot_times = ([ms_time] if ms_time is not None else []) + lif_times
    if not plot_times or not all(context_start <= value <= context_end for value in plot_times):
        return False

    def in_main(value: float, main_start: float, main_end: float) -> bool:
        return main_start <= value <= main_end

    if ms_time is None:
        return any(in_main(value, start, end) for value in lif_times)
    if in_main(ms_time, start, end):
        return True
    if not any(in_main(value, start, end) for value in lif_times):
        return False

    width = end - start
    if ms_time < start:
        ms_owner_start, ms_owner_end = start - width, start
    elif ms_time > end:
        ms_owner_start, ms_owner_end = end, end + width
    else:
        return False
    margin = max(0.0, float(context_margin_min))
    ms_owner_can_draw_complete_relation = all(
        ms_owner_start - margin <= value <= ms_owner_end + margin
        for value in plot_times
    )
    return not ms_owner_can_draw_complete_relation


def qc_group_auto_accept_block_reason(group: dict[str, Any]) -> str | None:
    if group.get("axis_coherent") is False:
        return "axis_incoherent"
    if group.get("complete_anchor_set") is False:
        return "partial_anchor_set"
    if int(group.get("conflict_count", 0) or 0) > 0:
        return "conflicting_anchor_set"
    tolerance = float(group.get("match_tolerance_sec", QC_GROUP_MATCH_TOL_SEC) or QC_GROUP_MATCH_TOL_SEC)
    if abs(float(group.get("composite_to_ms_residual_sec", 0.0) or 0.0)) > tolerance + QC_COMPONENT_SELECT_EPS:
        return "composite_residual_out_of_tolerance"
    max_axis_residual = group.get("max_abs_axis_to_ms_residual_sec")
    if isinstance(max_axis_residual, (int, float)) and float(max_axis_residual) > tolerance + QC_COMPONENT_SELECT_EPS:
        return "axis_residual_out_of_tolerance"
    return None


def qc_group_batch_accept_block_reason(
    group: dict[str, Any],
    *,
    window_start_min: float | None = None,
    window_end_min: float | None = None,
) -> str | None:
    if group.get("review_enabled") is False:
        return "review_disabled"
    if group.get("source") != "auto_candidate":
        return "not_auto_candidate"
    if group.get("review_status") != "pending":
        return f"status_{group.get('review_status')}"
    reason = qc_group_auto_accept_block_reason(group)
    if reason:
        return reason
    if window_start_min is not None and window_end_min is not None:
        plot_times = qc_group_plot_times(group)
        if not plot_times or not all(
            float(window_start_min) <= value <= float(window_end_min)
            for value in plot_times
        ):
            return "outside_main_window"
    return None


def candidate_id_for_group(group: dict[str, Any]) -> str:
    if has_dynamic_qc_anchor_payload(group):
        return f"auto_qc:v2:{dynamic_qc_candidate_digest(group)}"
    return f"auto_qc:{group['g2_peak_id']}:{group['r1_peak_id']}:{group['ms_event_id']}"


def post_qc_candidate_id(group: dict[str, Any]) -> str:
    strategy_hash = str(group.get("post_qc_strategy_hash") or "")
    strategy_mode = str(group.get("post_qc_strategy_mode") or "")
    if strategy_hash and strategy_mode:
        relation = {
            "strategy_hash": strategy_hash,
            "strategy_mode": strategy_mode,
            "window_id": str(group.get("post_qc_window_id") or ""),
            "ms_event_id": str(group.get("ms_event_id") or ""),
            "anchors": qc_anchor_peak_id_map(group),
        }
        digest = hashlib.sha1(
            json.dumps(
                relation,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        return f"post_qc:v3:{digest}"
    if has_dynamic_qc_anchor_payload(group):
        return f"post_qc:v2:{dynamic_qc_candidate_digest(group)}"
    return f"post_qc:{group['g2_peak_id']}:{group['r1_peak_id']}:{group['ms_event_id']}"


def cell_candidate_id(row: dict[str, Any]) -> str:
    return f"cell:{row['lif_channel']}:{row['lif_peak_id']}:{row['ms_event_id']}"


def manual_annotation_id(
    g2_peak_id: str | None,
    r1_peak_id: str | None,
    ms_event_id: str,
    *,
    lif_anchor_peak_ids: dict[str, str | None] | None = None,
) -> str:
    if lif_anchor_peak_ids is not None:
        payload = {
            "anchors": {
                str(channel).strip().upper(): optional_peak_id(peak_id) or MISSING_PEAK_SYMBOL
                for channel, peak_id in sorted(lif_anchor_peak_ids.items())
            },
            "ms_event_id": ms_event_id,
        }
        digest = hashlib.sha1(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return f"manual_qc:v2:{digest}"
    g2_key = g2_peak_id or MISSING_PEAK_SYMBOL
    r1_key = r1_peak_id or MISSING_PEAK_SYMBOL
    digest = hashlib.sha1(f"{g2_key}|{r1_key}|{ms_event_id}".encode("utf-8")).hexdigest()[:10]
    return f"manual_qc:{digest}"


def qc_relation_key(row: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    ms_event_id = optional_peak_id(row.get("ms_event_id"))
    anchor_map = qc_anchor_peak_id_map(row)
    if not ms_event_id or not anchor_map:
        return None
    anchors = tuple(
        (channel, peak_id or MISSING_PEAK_SYMBOL)
        for channel, peak_id in sorted(anchor_map.items())
    )
    return ms_event_id, anchors


def qc_relation_components(row: dict[str, Any]) -> frozenset[tuple[str, str, str]]:
    relation = qc_relation_key(row)
    if relation is None:
        return frozenset()
    ms_event_id, anchors = relation
    components = {("ms", "", ms_event_id)}
    components.update(
        ("lif", channel, peak_id)
        for channel, peak_id in anchors
        if peak_id != MISSING_PEAK_SYMBOL
    )
    return frozenset(components)


def latest_qc_reviews_by_relation(rows: list[dict[str, Any]]) -> dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]]:
    reviews: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("review_status")) not in {"accepted", "rejected"}:
            continue
        relation = qc_relation_key(row)
        if relation is None:
            continue
        previous = reviews.get(relation)
        row_order = (
            str(row.get("updated_at") or row.get("created_at") or ""),
            str(row.get("annotation_id") or ""),
        )
        previous_order = (
            str((previous or {}).get("updated_at") or (previous or {}).get("created_at") or ""),
            str((previous or {}).get("annotation_id") or ""),
        )
        if previous is None or row_order >= previous_order:
            reviews[relation] = row
    return reviews


def reconcile_qc_calibration_groups(
    groups: list[dict[str, Any]],
    reviewed_rows: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    reviews = latest_qc_reviews_by_relation(reviewed_rows)
    occupied_components: set[tuple[str, str, str]] = set()
    for row in reviews.values():
        if str(row.get("review_status")) == "accepted":
            occupied_components.update(qc_relation_components(row))

    reconciled: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for group in groups:
        exact_review = reviews.get(qc_relation_key(group))
        if exact_review is None and qc_relation_components(group) & occupied_components:
            continue
        reconciled.append((group, exact_review))
    return reconciled


def manual_cell_annotation_id(lif_channel: str, lif_peak_id: str, ms_event_id: str) -> str:
    digest = hashlib.sha1(f"{lif_channel}|{lif_peak_id}|{ms_event_id}".encode("utf-8")).hexdigest()[:10]
    return f"manual_cell:{digest}"


def optional_peak_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"} or text == MISSING_PEAK_SYMBOL:
        return None
    return text


def infer_channel_identity_prior(raw_data_dir: Path) -> dict[str, dict[str, str]]:
    """Infer channel-to-day priors from allowed raw-data file names only."""
    mapping: dict[str, dict[str, str]] = {}
    try:
        names = [path.name for path in raw_data_dir.iterdir() if path.is_file()]
    except OSError:
        names = []
    for name in names:
        lower = name.lower()
        if lower.endswith(".h5ad") or "hrgc-obs-check" in lower or "clean+qc" in lower:
            continue
        for day, channel in re.findall(r"(?i)day\s*([0-9]+)\s*-\s*([gr][0-9])", name):
            ch = channel.upper()
            if ch in {"G2", "R1", "R2"}:
                mapping[ch] = {
                    "identity_prior": f"Day{int(day)}",
                    "identity_prior_source": "raw_filename_experiment_config",
                    "identity_prior_file": name,
                }
    fallback = {"G2": "Day0", "R2": "Day3", "R1": "Day9"}
    for channel, day in fallback.items():
        mapping.setdefault(
            channel,
            {
                "identity_prior": day,
                "identity_prior_source": "default_experiment_config",
                "identity_prior_file": "",
            },
        )
    return mapping


def project_relative_or_absolute(path: Path, project_dir: Path) -> str:
    try:
        return str(path.absolute().relative_to(project_dir.absolute()))
    except ValueError:
        return str(path.absolute())


def link_or_copy_raw_input(src: Path, dst: Path) -> Path:
    src = src.expanduser().resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.symlink(src, dst)
    except OSError:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return dst


def load_preprocessing_module(script_name: str):
    script_path = SCRIPT_ROOT / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Missing bundled preprocessing script: {script_path}")
    module_name = f"_lifms_preprocess_{Path(script_name).stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load preprocessing script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_preprocessing_runner(script_name: str):
    script_path = SCRIPT_ROOT / script_name
    module = load_preprocessing_module(script_name)
    runner = getattr(module, "run", None)
    if not callable(runner):
        raise RuntimeError(f"Preprocessing script has no run(project_dir=...) entry: {script_path}")
    return runner


def run_preprocessing_script(script_name: str, project_dir: Path) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        runner = load_preprocessing_runner(script_name)
        runner(project_dir=project_dir)
    return output.getvalue()


def suggest_calibration_windows_from_raw_inputs(
    lif_inputs: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    annotation_start_min: float,
) -> dict[str, Any]:
    """Read selected LIF files without writing and suggest project windows."""

    if not isinstance(lif_inputs, list) or not 2 <= len(lif_inputs) <= 4:
        raise BadRequest("窗口建议需要 2–4 个 LIF 输入")
    prepared: list[dict[str, Any]] = []
    seen_channels: set[str] = set()
    for index, raw in enumerate(lif_inputs, start=1):
        if not isinstance(raw, dict):
            raise BadRequest("lif_inputs entries must be objects")
        channel = str(raw.get("channel") or "").strip().upper()
        path_text = str(raw.get("path") or "").strip()
        if not channel or not path_text:
            raise BadRequest(f"LIF {index} 必须选择文件并填写通道")
        if channel in seen_channels:
            raise BadRequest(f"LIF 通道不能重复: {channel}")
        seen_channels.add(channel)
        path = Path(path_text).expanduser().resolve()
        require_file(path)
        prepared.append(
            {
                "key": str(raw.get("key") or f"lif_{index}"),
                "channel": channel,
                "path": path,
                "label": str(raw.get("identity_prior") or "").strip(),
                "detector": str(raw.get("detector") or "").strip().lower(),
            }
        )
    validate_distinct_lif_input_files(prepared)

    module = load_preprocessing_module("run_v3_01_lif_trace_physical_qc.py")
    detection_config = require_active_lif_peak_detection()
    merged_tables: list[pd.DataFrame] = []
    summaries: list[dict[str, Any]] = []
    for item in prepared:
        try:
            trace = pd.read_csv(
                item["path"],
                sep="\t",
                header=None,
                names=["time_min", "raw"],
                encoding="utf-16",
            )
            trace["time_min"] = pd.to_numeric(trace["time_min"], errors="coerce")
            trace["raw"] = pd.to_numeric(trace["raw"], errors="coerce")
            trace = trace.dropna(subset=["time_min", "raw"]).sort_values("time_min").reset_index(drop=True)
            if len(trace) < 3:
                raise BadRequest(f"{item['channel']} LIF 文件没有足够的有效时间/强度行")
            trace["time_sec"] = trace["time_min"] * 60.0
            trace["channel"] = item["channel"]
            trace["label"] = item["label"]
            trace["detector"] = item["detector"]
            trace["phase"] = "calibration_window_preview"
            trace, meta = module.add_baseline_and_noise(
                trace,
                detection_config=detection_config,
            )
            raw_peaks = module.call_raw_peaks(
                trace,
                meta,
                detection_config=detection_config,
            )
            merged = module.merge_close_raw_peaks(raw_peaks)
        except BadRequest:
            raise
        except Exception as exc:
            raise BadRequest(f"无法分析 {item['channel']} LIF 峰形: {exc}") from exc
        if not merged.empty:
            merged_tables.append(merged)
        summaries.append(
            {
                "channel": item["channel"],
                "row_count": int(len(trace)),
                "merged_peak_count": int(len(merged)),
                "time_min_min": float(trace["time_min"].min()),
                "time_min_max": float(trace["time_min"].max()),
            }
        )
    all_peaks = pd.concat(merged_tables, ignore_index=True) if merged_tables else pd.DataFrame()
    result = suggest_calibration_segment_windows(
        all_peaks,
        segments,
        annotation_start_min=annotation_start_min,
    )
    result["channel_summaries"] = summaries
    result["source"] = "selected_raw_lif_peak_shape"
    return result


def assert_first_principles_path(path: Path) -> None:
    text = str(path)
    for part in FORBIDDEN_PATH_PARTS:
        if part in text:
            raise ValueError(f"Forbidden annotation input path detected: {path}")


def require_file(path: Path) -> None:
    assert_first_principles_path(path)
    if not path.exists():
        raise FileNotFoundError(f"Missing required annotation input: {display_path(path)}")


def raw_file_fingerprint(path: Path, full_hash_limit_bytes: int | None = 100 * 1024 * 1024) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing raw input file: {path}")
    assert_first_principles_path(path)
    stat = path.stat()
    size = int(stat.st_size)
    block = 1024 * 1024
    with path.open("rb") as fh:
        head = fh.read(min(block, size))
        if size > block:
            fh.seek(max(0, size - block))
            tail = fh.read(block)
        else:
            tail = head
    full_hash = ""
    if full_hash_limit_bytes is None or size <= full_hash_limit_bytes:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        full_hash = digest.hexdigest()
    return {
        "exists": True,
        "size_bytes": size,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "head_sha256_1mb": hashlib.sha256(head).hexdigest(),
        "tail_sha256_1mb": hashlib.sha256(tail).hexdigest(),
        "sha256": full_hash,
        "full_sha256_if_le_100mb": full_hash,
    }


def validate_distinct_lif_input_files(lif_inputs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not MIN_LIF_INPUTS <= len(lif_inputs) <= MAX_LIF_INPUTS:
        raise BadRequest(f"项目必须提供 {MIN_LIF_INPUTS}-{MAX_LIF_INPUTS} 个 LIF 原始文件")

    resolved_inputs: list[tuple[str, Path]] = []
    seen_keys: dict[str, str] = {}
    seen_paths: dict[str, tuple[str, Path]] = {}
    for index, item in enumerate(lif_inputs, start=1):
        key = str(item.get("key") or f"lif_{index}").strip()
        channel = str(item.get("channel") or f"LIF {index}").strip().upper()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key):
            raise BadRequest(
                f"{channel} 的输入标识无效；只能使用字母、数字、点、下划线或连字符"
            )
        reserved_stem = key.split(".", 1)[0].upper()
        windows_reserved = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
        if key.casefold() == "ms" or key.endswith(".") or reserved_stem in windows_reserved:
            raise BadRequest(f"{channel} 的输入标识与系统保留名称冲突，请重新添加该 LIF 输入")
        key_identity = key.casefold()
        previous_key_channel = seen_keys.get(key_identity)
        if previous_key_channel is not None:
            raise BadRequest(f"LIF 输入 key 不能重复: {previous_key_channel} 和 {channel} 都使用 {key}")
        seen_keys[key_identity] = channel
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            raise BadRequest(f"{channel} 缺少 LIF 原始文件路径")
        path = Path(raw_path).expanduser().resolve()
        path_key = os.path.normcase(str(path))
        previous = seen_paths.get(path_key)
        if previous is not None:
            previous_channel, previous_path = previous
            raise BadRequest(
                f"LIF 原始文件路径不能重复: {previous_channel} 和 {channel} 都指向 {previous_path}"
            )
        seen_paths[path_key] = (channel, path)
        resolved_inputs.append((channel, path))

    seen_hashes: dict[str, tuple[str, Path]] = {}
    fingerprints: dict[str, dict[str, Any]] = {}
    for channel, path in resolved_inputs:
        fingerprint = raw_file_fingerprint(path, full_hash_limit_bytes=None)
        digest = str(fingerprint.get("sha256") or "")
        previous = seen_hashes.get(digest)
        if previous is not None:
            previous_channel, previous_path = previous
            raise BadRequest(
                "LIF 原始文件内容不能重复: "
                f"{previous_channel} ({previous_path}) 和 {channel} ({path}) 的 SHA256 相同"
            )
        seen_hashes[digest] = (channel, path)
        fingerprints[os.path.normcase(str(path))] = fingerprint
    return fingerprints


def manifest_entry_path(project_dir: Path, entry: dict[str, Any]) -> Path:
    raw_path = str(entry.get("path", "")).strip()
    if not raw_path:
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} contains an entry without path")
    path = Path(raw_path).expanduser()
    if path.is_absolute() or path.drive or any(":" in part for part in path.parts):
        raise BadRequest("项目运行文件必须使用项目内相对路径，不能引用项目外文件")
    root = project_dir.resolve()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BadRequest("项目运行文件路径越出项目目录") from exc
    return resolved


def assert_matching_fingerprint(path: Path, expected: dict[str, Any], label: str, *, require_sha256: bool = False) -> None:
    actual = raw_file_fingerprint(path, full_hash_limit_bytes=None if require_sha256 else 100 * 1024 * 1024)
    if require_sha256 and not expected.get("sha256"):
        raise BadRequest(f"{label} 缺少全量 sha256，无法证明项目绑定")
    for key in ["size_bytes", "head_sha256_1mb", "tail_sha256_1mb", "sha256", "full_sha256_if_le_100mb"]:
        expected_value = expected.get(key)
        if expected_value in (None, ""):
            continue
        if actual.get(key) != expected_value:
            raise BadRequest(f"{label} 与 {PROJECT_MANIFEST_FILENAME} 记录不一致: {key}")


def validate_project_runtime_paths(
    project_dir: Path,
    manifest: dict[str, Any] | None,
) -> None:
    """Preflight every mutable/runtime artifact before any table I/O."""

    if not manifest:
        return
    raw_storage_layout = manifest.get("storage_layout")
    canonical_layout = False
    if raw_storage_layout is not None:
        try:
            canonical_layout = manifest_uses_canonical_storage(manifest)
        except (TypeError, ValueError):
            canonical_layout = False
        if not canonical_layout:
            raise BadRequest("项目目录布局声明无效或不受当前软件支持")
    tables = manifest.get("intermediate_tables")
    if not isinstance(tables, dict):
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} intermediate_tables 必须是对象")
    for key in REQUIRED_INTERMEDIATE_TABLE_KEYS:
        entry = tables.get(key)
        if not isinstance(entry, dict):
            raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} intermediate_tables.{key} 必须是对象")
        if canonical_layout:
            declared = str(entry.get("path") or "").replace("\\", "/")
            if declared != str(CANONICAL_TABLE_PATHS[key]).replace("\\", "/"):
                raise BadRequest("项目目录布局声明与数据文件路径不一致")
        manifest_entry_path(project_dir, entry)
    annotation_db = manifest.get("annotation_db")
    if not isinstance(annotation_db, dict) or not annotation_db.get("path"):
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} annotation_db 路径缺失")
    if canonical_layout and str(annotation_db["path"]).replace("\\", "/") != str(
        CANONICAL_ANNOTATION_DB_PATH
    ).replace("\\", "/"):
        raise BadRequest("项目目录布局声明与标注数据库路径不一致")
    manifest_entry_path(project_dir, {"path": annotation_db["path"]})
    event_map = manifest.get("cell_event_map")
    if canonical_layout and not isinstance(event_map, dict):
        raise BadRequest("项目目录布局声明缺少事件坐标表")
    if event_map is not None:
        if not isinstance(event_map, dict):
            raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} cell_event_map 必须是对象")
        if canonical_layout and str(event_map.get("path") or "").replace(
            "\\", "/"
        ) != str(CANONICAL_CELL_EVENT_MAP_PATH).replace("\\", "/"):
            raise BadRequest("项目目录布局声明与事件坐标表路径不一致")
        manifest_entry_path(project_dir, event_map)


def validate_project_manifest_against_files(project_dir: Path, manifest: dict[str, Any] | None) -> None:
    if not manifest:
        return
    validate_project_runtime_paths(project_dir, manifest)
    if int(manifest.get("project_schema_version", PROJECT_SCHEMA_VERSION)) > PROJECT_SCHEMA_VERSION:
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} schema 版本高于当前软件支持版本")
    intermediate_tables = manifest.get("intermediate_tables", {})
    if not isinstance(intermediate_tables, dict):
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} intermediate_tables 必须是对象")
    missing = set(REQUIRED_INTERMEDIATE_TABLES) - set(intermediate_tables)
    if missing:
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} 缺少中间表记录: {', '.join(sorted(missing))}")
    for key in REQUIRED_INTERMEDIATE_TABLES:
        entry = intermediate_tables[key]
        if not isinstance(entry, dict):
            raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} intermediate_tables.{key} 必须是对象")
        path = manifest_entry_path(project_dir, entry)
        require_file(path)
        assert_matching_fingerprint(path, entry, f"中间表 {key}", require_sha256=True)


def project_with_manifest_paths(project: ProjectPaths, manifest: dict[str, Any] | None) -> ProjectPaths:
    if not manifest:
        return project
    tables = manifest.get("intermediate_tables", {})
    if not isinstance(tables, dict):
        return project
    annotation_db_path = project.annotation_db_path
    annotation_db = manifest.get("annotation_db", {})
    if isinstance(annotation_db, dict) and annotation_db.get("path"):
        annotation_db_path = manifest_entry_path(
            project.project_dir,
            {"path": annotation_db["path"]},
        )
    return replace(
        project,
        annotation_db_path=annotation_db_path,
        lif_traces_path=manifest_entry_path(project.project_dir, tables["lif_traces"]),
        lif_peaks_path=manifest_entry_path(project.project_dir, tables["lif_peaks"]),
        ms_events_path=manifest_entry_path(project.project_dir, tables["ms_events"]),
        ms_scan_path=manifest_entry_path(project.project_dir, tables["ms_scan_summary"]),
    )


def load_project_cell_event_map(
    project_dir: Path,
    manifest: dict[str, Any] | None,
    ms_events: pd.DataFrame,
) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
    if not manifest or manifest.get("cell_event_map") is None:
        return None, None
    entry = manifest.get("cell_event_map")
    if not isinstance(entry, dict):
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} cell_event_map 必须是对象")
    if int(entry.get("schema_version", 0)) != 1:
        raise BadRequest("cell_event_map schema_version 不受支持")
    raw_path = Path(str(entry.get("path") or "")).expanduser()
    if raw_path.is_absolute():
        raise BadRequest("cell_event_map 必须是项目内相对路径，不能外部引用")
    resolved = (project_dir / raw_path).resolve()
    try:
        resolved.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise BadRequest("cell_event_map 路径越出项目目录") from exc
    expected_required = ["scan_start_time", "UMAP1", "UMAP2"]
    if entry.get("required_source_columns") != expected_required:
        raise BadRequest("cell_event_map required_source_columns 与当前契约不一致")
    try:
        frame = read_canonical_map(
            resolved,
            expected_sha256=str(entry.get("sha256") or ""),
        )
        rebound = match_source_to_events(
            frame[expected_required],
            ms_events,
            tolerance_sec=float(
                entry.get("match_tolerance_sec", DEFAULT_MATCH_TOLERANCE_SEC)
            ),
        )
    except CellEventMapError as exc:
        raise BadRequest(str(exc)) from exc
    if int(entry.get("row_count", -1)) != len(frame):
        raise BadRequest("cell_event_map row_count 与 canonical 文件不一致")
    if int(entry.get("matched_event_count", -1)) != frame["ms_event_id"].nunique():
        raise BadRequest("cell_event_map matched_event_count 与 canonical 文件不一致")
    expected_ids = frame["ms_event_id"].astype(str).tolist()
    rebound_ids = rebound["ms_event_id"].astype(str).tolist()
    if expected_ids != rebound_ids:
        raise BadRequest("cell_event_map 的 ms_event_id 绑定与当前 MS event 表不一致")
    def normalized_scan_id(value: Any) -> str:
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            value = int(value)
        return str(value)

    expected_scan_ids = [normalized_scan_id(value) for value in frame["scan_id"]]
    rebound_scan_ids = [normalized_scan_id(value) for value in rebound["scan_id"]]
    if expected_scan_ids != rebound_scan_ids:
        raise BadRequest("cell_event_map 的 scan_id 绑定与当前 MS event 表不一致")
    return frame, copy.deepcopy(entry)


def write_existing_project_manifest(project_dir: Path, manifest: dict[str, Any]) -> None:
    path = project_manifest_path(project_dir)
    payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def remove_staging_project(staging_dir: Path, intended_parent: Path) -> None:
    staging_dir = staging_dir.resolve()
    intended_parent = intended_parent.resolve()
    try:
        staging_dir.relative_to(intended_parent)
    except ValueError as exc:
        raise RuntimeError("Refusing to remove staging directory outside intended parent") from exc
    if ".lma-building-" not in staging_dir.name or staging_dir == intended_parent:
        raise RuntimeError("Refusing to remove a directory that is not an LMA staging project")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)


def commit_staging_project(
    staging_dir: Path,
    project_dir: Path,
    *,
    target_preexisted: bool,
) -> None:
    """Publish a complete staged project with one directory rename.

    An existing target is allowed only when the caller has already verified
    that it is empty.  It is removed immediately before the atomic rename and
    restored as an empty directory if publication fails.
    """

    staging_dir = staging_dir.resolve()
    project_dir = project_dir.resolve()
    if staging_dir.parent != project_dir.parent:
        raise RuntimeError("Staging project must be a sibling of the target directory")
    if ".lma-building-" not in staging_dir.name or staging_dir == project_dir:
        raise RuntimeError("Refusing to publish a directory that is not an LMA staging project")
    removed_empty_target = False
    if target_preexisted:
        try:
            project_dir.rmdir()
        except OSError as exc:
            raise BadRequest("新项目保存路径在构建期间不再为空，已取消发布") from exc
        removed_empty_target = True
    try:
        os.replace(staging_dir, project_dir)
    except Exception:
        if removed_empty_target and not project_dir.exists():
            project_dir.mkdir(parents=False, exist_ok=False)
        raise


def intermediate_table_fingerprints(project: ProjectPaths) -> dict[str, dict[str, Any]]:
    paths = {
        "lif_traces": project.lif_traces_path,
        "lif_peaks": project.lif_peaks_path,
        "ms_events": project.ms_events_path,
        "ms_scan_summary": project.ms_scan_path,
    }
    return {
        key: {
            "path": project_relative_or_absolute(path, project.project_dir).replace("\\", "/"),
            **raw_file_fingerprint(path, full_hash_limit_bytes=None),
        }
        for key, path in paths.items()
    }


def validate_staged_project_artifacts(project: ProjectPaths) -> ProjectPaths:
    """Validate immutable staged artifacts without creating annotation.sqlite."""

    manifest = read_project_manifest(project.project_dir)
    if manifest is None:
        raise BadRequest("staging 项目缺少 lifms_project.json")
    peak_detection = lif_peak_detection_from_manifest(manifest)
    validate_project_manifest_against_files(project.project_dir, manifest)
    resolved = project_with_manifest_paths(project, manifest)
    for path in [resolved.lif_traces_path, resolved.ms_scan_path]:
        require_file(path)
        try:
            # The manifest SHA already authenticates the whole file.  Reading
            # only parquet metadata avoids materializing very large raw traces
            # or scan summaries during the atomic publication check.
            pd.read_parquet(path, columns=[])
        except Exception as exc:
            raise BadRequest(f"staging 中间表无法读取: {display_path(path, resolved.project_dir)}") from exc
    try:
        lif_peaks = pd.read_parquet(resolved.lif_peaks_path)
        ms_events = pd.read_parquet(
            resolved.ms_events_path,
            columns=["event_id", "event_strategy", "primary_signal_col", "scan_id", "time_min"],
        )
    except Exception as exc:
        raise BadRequest("staging 项目中间表缺少 event-map 绑定所需字段") from exc
    validate_and_adapt_lif_peak_detector_binding(
        lif_peaks,
        peak_detection,
        explicit_peak_detection=isinstance(
            manifest.get("lif_peak_detection"), dict
        ),
    )
    acquisition_layout_from_manifest(manifest)
    load_project_cell_event_map(resolved.project_dir, manifest, ms_events)
    return resolved


def project_table_binding(intermediate_tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    for key in REQUIRED_INTERMEDIATE_TABLES:
        entry = intermediate_tables.get(key, {})
        sha256 = str(entry.get("sha256") or "")
        if not sha256:
            raise BadRequest(f"中间表 {key} 缺少全量 sha256，无法生成项目绑定")
        normalized[key] = {
            "path": str(entry.get("path", "")).replace("\\", "/"),
            "size_bytes": int(entry.get("size_bytes", 0)),
            "sha256": sha256,
        }
    payload = {
        # This binding predates project schema v2.  Keep the scientific table
        # identity stable so opening a v0.2.1 project remains read-compatible.
        "schema_version": PROJECT_TABLE_BINDING_SCHEMA_VERSION,
        "intermediate_tables": normalized,
    }
    payload["binding_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def read_sqlite_project_binding(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='project_config'"
            ).fetchone()
            if table is None:
                return None
            row = conn.execute(
                "SELECT value_json FROM project_config WHERE key = ?",
                (PROJECT_TABLE_BINDING_KEY,),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(str(row[0]))
            return value if isinstance(value, dict) else None
        finally:
            conn.close()
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise BadRequest(f"无法读取 SQLite 项目绑定: {exc}") from exc


def sqlite_annotation_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='annotations'"
            ).fetchone()
            if table is None:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BadRequest(f"无法读取 annotation.sqlite: {exc}") from exc


def validate_sqlite_project_binding(db_path: Path, binding: dict[str, Any], *, allow_adopt: bool) -> str:
    existing = read_sqlite_project_binding(db_path)
    if existing:
        if existing.get("binding_sha256") != binding.get("binding_sha256"):
            raise BadRequest("annotation.sqlite 的项目绑定与当前中间表不一致")
        return "matched"
    if not db_path.exists():
        return "new"
    if allow_adopt:
        return "adopt"
    raise BadRequest("annotation.sqlite 缺少项目绑定，不能自动接入已有 manifest 项目")


def validate_sqlite_input_manifest_against_files(
    db_path: Path,
    project_dir: Path,
    intermediate_tables: dict[str, dict[str, Any]],
) -> None:
    if not db_path.exists() or sqlite_annotation_count(db_path) == 0:
        return
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='input_manifest'"
            ).fetchone()
            if table is None:
                raise BadRequest("legacy annotation.sqlite 缺少 input_manifest，不能自动补登记项目绑定")
            rows = conn.execute("SELECT input_key, relative_path, size_bytes FROM input_manifest").fetchall()
        finally:
            conn.close()
    except BadRequest:
        raise
    except sqlite3.Error as exc:
        raise BadRequest(f"无法读取 SQLite input_manifest: {exc}") from exc
    by_key = {str(row[0]): row for row in rows}
    missing = set(REQUIRED_INTERMEDIATE_TABLES) - set(by_key)
    if missing:
        raise BadRequest(f"legacy annotation.sqlite input_manifest 缺少: {', '.join(sorted(missing))}")
    for key in REQUIRED_INTERMEDIATE_TABLES:
        row = by_key[key]
        entry = intermediate_tables[key]
        row_path = str(row[1]).replace("\\", "/")
        entry_path = str(entry["path"]).replace("\\", "/")
        if row_path != entry_path and Path(row_path).name != Path(entry_path).name:
            raise BadRequest(f"legacy annotation.sqlite input_manifest 路径不匹配: {key}")
        if int(row[2] or -1) != int(entry["size_bytes"]):
            raise BadRequest(f"legacy annotation.sqlite input_manifest 文件大小不匹配: {key}")


def assert_no_legacy_annotation_state(db_path: Path) -> None:
    legacy_state = db_path.parent / "annotation_state.json"
    if legacy_state.exists():
        raise BadRequest(f"检测到未绑定的旧 JSON 标注状态，请先人工迁移或移除: {display_path(legacy_state, db_path.parent)}")


def assert_new_project_target_is_clean(project_dir: Path, existing_outputs: list[Path]) -> None:
    blocked_paths = [
        project_manifest_path(project_dir),
        project_dir / CANONICAL_ANNOTATION_DB_PATH,
        project_dir / CANONICAL_INPUT_MANIFEST_PATH,
        project_dir / CANONICAL_PROJECT_PROTOCOL_PATH,
        project_dir / "annotation_app/annotations/annotation.sqlite",
        project_dir / "annotation_app/annotations/annotation_state.json",
        project_dir / "results/tables/v3/00_allowed_inputs.csv",
        project_dir / "results/tables/v3/00_imported_raw_inputs.csv",
        *existing_outputs,
    ]
    for path in blocked_paths:
        if path.exists():
            raise BadRequest(f"项目目录已有项目文件，为避免混用旧数据请换一个空目录: {display_path(path, project_dir)}")


def validate_annotation_db_against_tables(db_path: Path, lif_peak_ids: set[str], ms_event_ids: set[str]) -> None:
    if not db_path.exists():
        return
    db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
    except sqlite3.Error as exc:
        raise BadRequest(f"无法读取 annotation.sqlite: {exc}") from exc
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='annotations'"
        ).fetchone()
        if table is None:
            raise BadRequest("annotation.sqlite 中缺少 annotations 表")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(annotations)").fetchall()}
        select_columns = [
            column
            for column in ["annotation_id", "g2_peak_id", "r1_peak_id", "ms_event_id", "payload_json"]
            if column in columns
        ]
        if "annotation_id" not in select_columns:
            select_columns.insert(0, "rowid AS annotation_id")
        rows = conn.execute(f"SELECT {', '.join(select_columns)} FROM annotations").fetchall()
    except BadRequest:
        raise
    except sqlite3.Error as exc:
        raise BadRequest(f"annotation.sqlite 结构无法校验: {exc}") from exc
    finally:
        conn.close()

    missing_lif: set[str] = set()
    missing_ms: set[str] = set()
    for row in rows:
        row_map = dict(zip([desc[0] for desc in row.cursor_description], row)) if hasattr(row, "cursor_description") else {}
        if not row_map:
            row_map = {select_columns[index].split(" AS ")[-1]: value for index, value in enumerate(row)}
        for key in ["g2_peak_id", "r1_peak_id"]:
            value = optional_peak_id(row_map.get(key))
            if value and value not in lif_peak_ids:
                missing_lif.add(value)
        ms_value = optional_peak_id(row_map.get("ms_event_id"))
        if ms_value and ms_value not in ms_event_ids:
            missing_ms.add(ms_value)
        payload_raw = row_map.get("payload_json")
        if isinstance(payload_raw, str) and payload_raw.strip():
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key in ["lif_peak_id", "g1_peak_id", "g2_peak_id", "r1_peak_id", "r2_peak_id"]:
                    value = optional_peak_id(payload.get(key))
                    if value and value not in lif_peak_ids:
                        missing_lif.add(value)
                dynamic_ids = payload.get("lif_anchor_peak_ids")
                if isinstance(dynamic_ids, dict):
                    for peak_id in dynamic_ids.values():
                        value = optional_peak_id(peak_id)
                        if value and value not in lif_peak_ids:
                            missing_lif.add(value)
                dynamic_anchors = payload.get("lif_anchors")
                if isinstance(dynamic_anchors, list):
                    for anchor in dynamic_anchors:
                        if not isinstance(anchor, dict):
                            continue
                        value = optional_peak_id(anchor.get("peak_id"))
                        if value and value not in lif_peak_ids:
                            missing_lif.add(value)
                value = optional_peak_id(payload.get("ms_event_id"))
                if value and value not in ms_event_ids:
                    missing_ms.add(value)
    if missing_lif or missing_ms:
        parts = []
        if missing_lif:
            parts.append(f"LIF peak_id 不存在: {', '.join(sorted(missing_lif)[:5])}")
        if missing_ms:
            parts.append(f"MS event_id 不存在: {', '.join(sorted(missing_ms)[:5])}")
        raise BadRequest("annotation.sqlite 与当前项目中间表不匹配；" + "；".join(parts))


def clean_value(value: Any) -> Any:
    if value is pd.NA:
        return None
    if isinstance(value, (list, tuple)):
        return [clean_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean_value(item) for key, item in value.items()}
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if pd.isna(value):
        return None
    return value


def records(df: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    if df.empty:
        return []
    existing = [col for col in columns if col in df.columns]
    out = df[existing].copy()
    return [{key: clean_value(value) for key, value in row.items()} for row in out.to_dict("records")]


def false_if_missing(series: pd.Series) -> pd.Series:
    return series.astype("boolean").fillna(False).astype(bool)


class AnnotationStore:
    """SQLite store for human review decisions and audit events.

    First-principles preprocessing tables remain read-only. Human decisions are
    stored separately so they cannot leak back into peak/event extraction.
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_ANNOTATION_DB_PATH,
        *,
        default_project_config: dict[str, Any] | None = None,
    ) -> None:
        self.db_path = db_path
        self.legacy_state_path = self.db_path.parent / "annotation_state.json"
        self.default_project_config = copy.deepcopy(default_project_config or {})
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_legacy_json_if_needed()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS annotations (
                    annotation_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL CHECK(source IN ('auto_candidate', 'manual_created')),
                    review_status TEXT NOT NULL CHECK(review_status IN ('pending', 'accepted', 'rejected')),
                    exportable INTEGER NOT NULL,
                    label TEXT,
                    g2_peak_id TEXT,
                    r1_peak_id TEXT,
                    ms_event_id TEXT,
                    scan_id TEXT,
                    g2_raw_time_min REAL,
                    r1_raw_time_min REAL,
                    ms_time_min REAL,
                    time_model_name TEXT,
                    residual_sec REAL,
                    abs_residual_sec REAL,
                    candidate_rank REAL,
                    candidate_score REAL,
                    confidence_mode TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    audit_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    annotation_id TEXT NOT NULL,
                    candidate_id TEXT,
                    source TEXT NOT NULL CHECK(source IN ('auto_candidate', 'manual_created')),
                    prev_status TEXT,
                    new_status TEXT,
                    prev_label TEXT,
                    new_label TEXT,
                    g2_peak_id TEXT,
                    r1_peak_id TEXT,
                    ms_event_id TEXT,
                    scan_id TEXT,
                    g2_raw_time_min REAL,
                    r1_raw_time_min REAL,
                    ms_time_min REAL,
                    time_model_name TEXT,
                    expected_lif_time_sec REAL,
                    residual_sec REAL,
                    abs_residual_sec REAL,
                    candidate_rank REAL,
                    candidate_score REAL,
                    confidence_mode TEXT,
                    window_start_min REAL,
                    window_end_min REAL,
                    time_mode TEXT,
                    reason_code TEXT,
                    notes TEXT,
                    app_version TEXT NOT NULL,
                    input_policy TEXT,
                    payload_json TEXT NOT NULL,
                    event_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS input_manifest (
                    input_key TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL,
                    size_bytes INTEGER,
                    mtime_ns INTEGER,
                    recorded_at TEXT NOT NULL,
                    app_version TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS export_runs (
                    export_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    filter_json TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    csv_path TEXT,
                    csv_sha256 TEXT,
                    app_version TEXT NOT NULL,
                    input_manifest_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_config (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    app_version TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS time_models (
                    time_model_version TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen', 'exploratory')),
                    is_active INTEGER NOT NULL,
                    base_model_name TEXT NOT NULL,
                    qc_calibration_end_min REAL NOT NULL,
                    sample_valve_switch_min REAL NOT NULL,
                    annotation_start_min REAL NOT NULL,
                    local_delta_seed_window_min REAL NOT NULL,
                    ms_local_delta_sec REAL NOT NULL,
                    contains_cell_labels INTEGER NOT NULL,
                    max_training_time_min REAL NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    unique_match_count INTEGER NOT NULL,
                    conflict_count INTEGER NOT NULL,
                    median_abs_residual_sec REAL,
                    p90_abs_residual_sec REAL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS time_model_audit_events (
                    audit_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    time_model_version TEXT,
                    payload_json TEXT NOT NULL,
                    app_version TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "annotations", "time_model_version", "TEXT")
            self._ensure_column(conn, "audit_events", "time_model_version", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_status ON annotations(review_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_triplet ON annotations(g2_peak_id, r1_peak_id, ms_event_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_annotation ON audit_events(annotation_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_time_models_active ON time_models(is_active, status)")
            self._ensure_default_project_config(conn)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def _ensure_default_project_config(self, conn: sqlite3.Connection) -> None:
        timestamp = now_iso()
        defaults = {
            "qc_calibration_end_min": QC_SHIFT_WINDOW_MIN,
            "sample_valve_switch_min": DEFAULT_SAMPLE_VALVE_SWITCH_MIN,
            "annotation_start_min": DEFAULT_ANNOTATION_START_MIN,
            "local_delta_seed_window_min": DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN,
            **self.default_project_config,
        }
        for key, value in defaults.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO project_config (key, value_json, updated_at, app_version)
                VALUES (?, ?, ?, ?)
                """,
                (key, json.dumps(value), timestamp, APP_VERSION),
            )

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self.legacy_state_path.exists():
            return
        with self._connect() as conn:
            existing = int(conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0])
            if existing:
                return
        with self.legacy_state_path.open("r", encoding="utf-8") as fh:
            legacy = json.load(fh)
        if not isinstance(legacy, dict):
            return
        rows = legacy.get("annotations", {})
        if not isinstance(rows, dict) or not rows:
            return
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for annotation_id, row in rows.items():
                if not isinstance(row, dict):
                    continue
                payload = {
                    key: value
                    for key, value in row.items()
                    if key
                    not in {
                        "annotation_id",
                        "source",
                        "review_status",
                        "exportable",
                        "created_at",
                        "updated_at",
                    }
                }
                current = {
                    **payload,
                    "annotation_id": str(annotation_id),
                    "source": str(row.get("source", "auto_candidate")),
                    "review_status": str(row.get("review_status", "pending")),
                    "exportable": bool(row.get("exportable", False)),
                    "created_at": str(row.get("created_at") or timestamp),
                    "updated_at": str(row.get("updated_at") or timestamp),
                }
                self._upsert_annotation_row(conn, current)
                audit = self._audit_row(
                    annotation_id=str(annotation_id),
                    source=current["source"],
                    review_status=current["review_status"],
                    payload=payload,
                    action="legacy_json_migration",
                    previous={},
                    timestamp=timestamp,
                )
                self._insert_audit_row(conn, audit)

    def _decode_annotation_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        return {
            **payload,
            "annotation_id": row["annotation_id"],
            "source": row["source"],
            "review_status": row["review_status"],
            "exportable": bool(row["exportable"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _annotation_columns(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "annotation_id": row["annotation_id"],
            "source": row["source"],
            "review_status": row["review_status"],
            "exportable": 1 if row.get("exportable") else 0,
            "label": row.get("label"),
            "g2_peak_id": row.get("g2_peak_id"),
            "r1_peak_id": row.get("r1_peak_id"),
            "ms_event_id": row.get("ms_event_id"),
            "scan_id": row.get("scan_id"),
            "g2_raw_time_min": row.get("g2_raw_time_min"),
            "r1_raw_time_min": row.get("r1_raw_time_min"),
            "ms_time_min": row.get("ms_time_min"),
            "time_model_name": row.get("time_model_name"),
            "time_model_version": row.get("time_model_version"),
            "residual_sec": row.get("residual_sec"),
            "abs_residual_sec": row.get("abs_residual_sec"),
            "candidate_rank": row.get("candidate_rank"),
            "candidate_score": row.get("candidate_score"),
            "confidence_mode": row.get("confidence_mode"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "payload_json": json.dumps(
                {
                    key: value
                    for key, value in row.items()
                    if key
                    not in {
                        "annotation_id",
                        "source",
                        "review_status",
                        "exportable",
                        "created_at",
                        "updated_at",
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }

    def _upsert_annotation_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        cols = self._annotation_columns(row)
        conn.execute(
            """
            INSERT INTO annotations (
                annotation_id, source, review_status, exportable, label,
                g2_peak_id, r1_peak_id, ms_event_id, scan_id,
                g2_raw_time_min, r1_raw_time_min, ms_time_min,
                time_model_name, time_model_version, residual_sec, abs_residual_sec,
                candidate_rank, candidate_score, confidence_mode,
                created_at, updated_at, payload_json
            )
            VALUES (
                :annotation_id, :source, :review_status, :exportable, :label,
                :g2_peak_id, :r1_peak_id, :ms_event_id, :scan_id,
                :g2_raw_time_min, :r1_raw_time_min, :ms_time_min,
                :time_model_name, :time_model_version, :residual_sec, :abs_residual_sec,
                :candidate_rank, :candidate_score, :confidence_mode,
                :created_at, :updated_at, :payload_json
            )
            ON CONFLICT(annotation_id) DO UPDATE SET
                source=excluded.source,
                review_status=excluded.review_status,
                exportable=excluded.exportable,
                label=excluded.label,
                g2_peak_id=excluded.g2_peak_id,
                r1_peak_id=excluded.r1_peak_id,
                ms_event_id=excluded.ms_event_id,
                scan_id=excluded.scan_id,
                g2_raw_time_min=excluded.g2_raw_time_min,
                r1_raw_time_min=excluded.r1_raw_time_min,
                ms_time_min=excluded.ms_time_min,
                time_model_name=excluded.time_model_name,
                time_model_version=excluded.time_model_version,
                residual_sec=excluded.residual_sec,
                abs_residual_sec=excluded.abs_residual_sec,
                candidate_rank=excluded.candidate_rank,
                candidate_score=excluded.candidate_score,
                confidence_mode=excluded.confidence_mode,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            cols,
        )

    def _audit_row(
        self,
        *,
        annotation_id: str,
        source: str,
        review_status: str,
        payload: dict[str, Any],
        action: str,
        previous: dict[str, Any],
        timestamp: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        reason_code: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "audit_id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "action": action,
            "entity_type": "annotation",
            "annotation_id": annotation_id,
            "candidate_id": payload.get("candidate_id") or annotation_id,
            "source": source,
            "prev_status": previous.get("review_status", "pending"),
            "new_status": review_status,
            "prev_label": previous.get("label"),
            "new_label": payload.get("label"),
            "g2_peak_id": payload.get("g2_peak_id"),
            "r1_peak_id": payload.get("r1_peak_id"),
            "ms_event_id": payload.get("ms_event_id"),
            "scan_id": payload.get("scan_id"),
            "g2_raw_time_min": payload.get("g2_raw_time_min"),
            "r1_raw_time_min": payload.get("r1_raw_time_min"),
            "ms_time_min": payload.get("ms_time_min"),
            "time_model_name": payload.get("time_model_name"),
            "time_model_version": payload.get("time_model_version"),
            "expected_lif_time_sec": payload.get("expected_lif_time_sec"),
            "residual_sec": payload.get("residual_sec"),
            "abs_residual_sec": payload.get("abs_residual_sec"),
            "candidate_rank": payload.get("candidate_rank"),
            "candidate_score": payload.get("candidate_score"),
            "confidence_mode": payload.get("confidence_mode"),
            "window_start_min": window_start_min,
            "window_end_min": window_end_min,
            "time_mode": time_mode,
            "reason_code": reason_code,
            "notes": notes,
            "app_version": APP_VERSION,
            "input_policy": payload.get("input_policy"),
            "payload": payload,
        }
        return row

    def _insert_audit_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        event_json = json.dumps(row, ensure_ascii=False, sort_keys=True)
        payload_json = json.dumps(row.get("payload", {}), ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO audit_events (
                audit_id, timestamp, action, entity_type, annotation_id,
                candidate_id, source, prev_status, new_status, prev_label,
                new_label, g2_peak_id, r1_peak_id, ms_event_id, scan_id,
                g2_raw_time_min, r1_raw_time_min, ms_time_min,
                time_model_name, time_model_version, expected_lif_time_sec, residual_sec,
                abs_residual_sec, candidate_rank, candidate_score,
                confidence_mode, window_start_min, window_end_min, time_mode,
                reason_code, notes, app_version, input_policy,
                payload_json, event_json
            )
            VALUES (
                :audit_id, :timestamp, :action, :entity_type, :annotation_id,
                :candidate_id, :source, :prev_status, :new_status, :prev_label,
                :new_label, :g2_peak_id, :r1_peak_id, :ms_event_id, :scan_id,
                :g2_raw_time_min, :r1_raw_time_min, :ms_time_min,
                :time_model_name, :time_model_version, :expected_lif_time_sec, :residual_sec,
                :abs_residual_sec, :candidate_rank, :candidate_score,
                :confidence_mode, :window_start_min, :window_end_min, :time_mode,
                :reason_code, :notes, :app_version, :input_policy,
                :payload_json, :event_json
            )
            """,
            {**row, "payload_json": payload_json, "event_json": event_json},
        )

    def record_input_manifest(
        self,
        paths: dict[str, Path],
        *,
        project_dir: Path | None = None,
    ) -> None:
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, path in paths.items():
                stat = path.stat()
                conn.execute(
                    """
                    INSERT INTO input_manifest (
                        input_key, relative_path, size_bytes, mtime_ns,
                        recorded_at, app_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(input_key) DO UPDATE SET
                        relative_path=excluded.relative_path,
                        size_bytes=excluded.size_bytes,
                        mtime_ns=excluded.mtime_ns,
                        recorded_at=excluded.recorded_at,
                        app_version=excluded.app_version
                    """,
                    (
                        key,
                        (
                            project_relative_or_absolute(path, project_dir).replace("\\", "/")
                            if project_dir is not None
                            else display_path(path)
                        ),
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        timestamp,
                        APP_VERSION,
                    ),
                )

    def summary(self) -> dict[str, Any]:
        counts = {status: 0 for status in REVIEW_STATUSES}
        source_counts = {source: 0 for source in ANNOTATION_SOURCES}
        with self._lock, self._connect() as conn:
            for row in conn.execute("SELECT review_status, COUNT(*) AS n FROM annotations GROUP BY review_status"):
                if row["review_status"] in counts:
                    counts[row["review_status"]] = int(row["n"])
            for row in conn.execute("SELECT source, COUNT(*) AS n FROM annotations GROUP BY source"):
                if row["source"] in source_counts:
                    source_counts[row["source"]] = int(row["n"])
            total_records = int(conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0])
            audit_events = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        return {
            "backend": "sqlite",
            "db_path": display_path(self.db_path),
            "state_path": display_path(self.db_path),
            "audit_path": display_path(self.db_path),
            "counts": counts,
            "source_counts": source_counts,
            "total_records": total_records,
            "audit_events": audit_events,
        }

    def project_config(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM project_config").fetchall()
        return {str(row["key"]): json.loads(row["value_json"]) for row in rows}

    def qc_alignment_model(self) -> dict[str, Any] | None:
        model = self.project_config().get(QC_ALIGNMENT_MODEL_KEY)
        if model is None:
            return None
        if not isinstance(model, dict):
            raise BadRequest("项目中的 QC alignment model 格式无效")
        return model

    def save_qc_alignment_model(
        self,
        model: dict[str, Any],
        *,
        clear_frozen_time_model: bool = False,
        draft_time_model_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(model, dict) or not model.get("preview_hash"):
            raise BadRequest("QC alignment model 缺少可审计的 preview_hash")
        timestamp = now_iso()
        draft_row = (
            self._prepare_time_model_row(draft_time_model_payload, timestamp=timestamp)
            if draft_time_model_payload is not None
            else None
        )
        stored_model = {
            **model,
            "status": "active",
            "applied_at": timestamp,
        }
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_row = conn.execute(
                "SELECT * FROM time_models WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            active_model = self._decode_time_model_row(active_row) if active_row else None
            if active_model and str(active_model.get("status")) == "frozen" and not clear_frozen_time_model:
                raise BadRequest("应用新的 QC 对齐会清除当前已冻结 time model；请确认后重新进行后段局部校正")
            if active_model:
                conn.execute("UPDATE time_models SET is_active = 0, updated_at = ? WHERE is_active = 1", (timestamp,))
            conn.execute(
                """
                INSERT INTO project_config (key, value_json, updated_at, app_version)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at,
                    app_version=excluded.app_version
                """,
                (
                    QC_ALIGNMENT_MODEL_KEY,
                    json.dumps(stored_model, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    APP_VERSION,
                ),
            )
            self._insert_time_model_audit_row(
                conn,
                action="save_qc_alignment_refit_model",
                time_model_version=str((active_model or {}).get("time_model_version") or "") or None,
                payload={
                    "qc_alignment_model": stored_model,
                    "invalidated_time_model": active_model,
                },
            )
            if draft_row is not None:
                self._upsert_time_model_row(
                    conn,
                    draft_row,
                    action="create_draft_for_qc_alignment_refit",
                )
        return stored_model

    def record_project_table_binding(self, binding: dict[str, Any]) -> None:
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO project_config (key, value_json, updated_at, app_version)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at,
                    app_version=excluded.app_version
                """,
                (
                    PROJECT_TABLE_BINDING_KEY,
                    json.dumps(binding, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    APP_VERSION,
                ),
            )

    def update_project_config(
        self,
        updates: dict[str, Any],
        *,
        clear_frozen_time_model: bool = False,
        clear_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        if "lif_peak_detection" in updates:
            raise BadRequest(
                "LIF detector 是预处理峰表的只读绑定，不能只改项目配置。"
                "请新建项目或项目副本并重跑中间表；现有时间模型和人工标注均未改动。"
            )
        allowed = {
            "qc_calibration_end_min",
            "annotation_start_min",
            "local_delta_seed_window_min",
            "calibration_protocol",
            "post_qc_strategy",
        }
        current_config = self.project_config()
        cleaned: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key in {"calibration_protocol", "post_qc_strategy"}:
                if not isinstance(value, dict):
                    raise BadRequest(f"{key} must be an object")
                cleaned[key] = copy.deepcopy(value)
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise BadRequest(f"{key} must be numeric") from exc
            if not math.isfinite(number) or number < 0:
                raise BadRequest(f"{key} must be a finite non-negative number")
            if key == "local_delta_seed_window_min" and number <= 0:
                raise BadRequest("后段预校准取证范围必须大于 0 min")
            cleaned[key] = number
        if not cleaned:
            return current_config
        proposed_config = {**current_config, **cleaned}
        if float(proposed_config["annotation_start_min"]) < float(proposed_config["qc_calibration_end_min"]):
            raise BadRequest("annotation_start_min must be >= qc_calibration_end_min")
        active_model = self.active_time_model()
        sensitive_changes = {
            key: {"old": float(current_config.get(key, 0.0)), "new": float(proposed_config[key])}
            for key in TIME_MODEL_CONFIG_KEYS
            if key in cleaned and abs(float(current_config.get(key, 0.0)) - float(proposed_config[key])) > 1e-9
        }
        if "calibration_protocol" in cleaned:
            old_protocol_json = json.dumps(
                current_config.get("calibration_protocol"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            new_protocol_json = json.dumps(
                proposed_config.get("calibration_protocol"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            if old_protocol_json != new_protocol_json:
                sensitive_changes["calibration_protocol"] = {
                    "old_hash": hashlib.sha256(old_protocol_json.encode("utf-8")).hexdigest(),
                    "new_hash": hashlib.sha256(new_protocol_json.encode("utf-8")).hexdigest(),
                }
        clear_active_time_model = bool(active_model and sensitive_changes)
        clear_active_frozen = bool(
            active_model and active_model.get("status") == "frozen" and sensitive_changes
        )
        if clear_active_frozen and not clear_frozen_time_model:
            raise BadRequest("修改校准协议或后段时间节点会使当前已冻结 time model 失效；请确认后重新进行后段局部校正")
        qc_end_changed = "qc_calibration_end_min" in sensitive_changes
        calibration_protocol_changed = "calibration_protocol" in sensitive_changes
        existing_qc_alignment_model = current_config.get(QC_ALIGNMENT_MODEL_KEY)
        clear_active_qc_alignment = bool(
            (qc_end_changed or calibration_protocol_changed) and existing_qc_alignment_model
        )
        if clear_active_qc_alignment and not clear_qc_alignment_model:
            raise BadRequest("修改校准协议会使已应用的 anchor QC 对齐失效；请确认后重新审核并重算 QC 对齐")
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if clear_active_time_model:
                conn.execute(
                    "UPDATE time_models SET is_active = 0, updated_at = ? WHERE is_active = 1",
                    (timestamp,),
                )
                self._insert_time_model_audit_row(
                    conn,
                    action="invalidate_time_model_for_project_config_update",
                    time_model_version=str(active_model.get("time_model_version")),
                    payload={"updates": cleaned, "sensitive_changes": sensitive_changes, "previous_time_model": active_model},
                )
            if clear_active_qc_alignment:
                conn.execute("DELETE FROM project_config WHERE key = ?", (QC_ALIGNMENT_MODEL_KEY,))
                self._insert_time_model_audit_row(
                    conn,
                    action="clear_qc_alignment_model_for_qc_end_update",
                    time_model_version=str((active_model or {}).get("time_model_version") or "") or None,
                    payload={
                        "updates": cleaned,
                        "sensitive_changes": sensitive_changes,
                        "previous_qc_alignment_model": existing_qc_alignment_model,
                    },
                )
            for key, value in cleaned.items():
                conn.execute(
                    """
                    INSERT INTO project_config (key, value_json, updated_at, app_version)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at,
                        app_version=excluded.app_version
                    """,
                    (key, json.dumps(value), timestamp, APP_VERSION),
                )
            self._insert_time_model_audit_row(
                conn,
                action="project_config_update",
                time_model_version=None,
                payload={
                    "updates": cleaned,
                    "project_config": self._project_config_from_conn(conn),
                    "invalidated_time_model": clear_active_time_model,
                    "cleared_frozen_time_model": clear_active_frozen,
                    "cleared_qc_alignment_model": clear_active_qc_alignment,
                },
            )
        return self.project_config()

    def _project_config_from_conn(self, conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute("SELECT key, value_json FROM project_config").fetchall()
        return {str(row["key"]): json.loads(row["value_json"]) for row in rows}

    def active_time_model(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM time_models WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return self._decode_time_model_row(row) if row else None

    def _decode_time_model_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        return {
            **payload,
            "time_model_version": row["time_model_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "is_active": bool(row["is_active"]),
            "base_model_name": row["base_model_name"],
            "qc_calibration_end_min": row["qc_calibration_end_min"],
            "sample_valve_switch_min": row["sample_valve_switch_min"],
            "annotation_start_min": row["annotation_start_min"],
            "local_delta_seed_window_min": row["local_delta_seed_window_min"],
            "ms_local_delta_sec": row["ms_local_delta_sec"],
            "contains_cell_labels": bool(row["contains_cell_labels"]),
            "max_training_time_min": row["max_training_time_min"],
            "evidence_count": row["evidence_count"],
            "unique_match_count": row["unique_match_count"],
            "conflict_count": row["conflict_count"],
            "median_abs_residual_sec": row["median_abs_residual_sec"],
            "p90_abs_residual_sec": row["p90_abs_residual_sec"],
        }

    def ensure_draft_time_model(
        self,
        base_model_name: str,
        acquisition_layout_hash_value: str | None = None,
        *,
        calibration_protocol_hash_value: str | None = None,
        allow_unhashed_legacy_binding: bool = True,
    ) -> dict[str, Any]:
        existing = self.active_time_model()
        if existing:
            existing_layout_hash = str(existing.get("acquisition_layout_hash") or "")
            existing_protocol_hash = str(existing.get("calibration_protocol_hash") or "")
            if (
                acquisition_layout_hash_value
                and existing_layout_hash
                and existing_layout_hash != str(acquisition_layout_hash_value)
            ):
                raise BadRequest(
                    "现有 time model 的 acquisition layout 绑定与当前项目不一致；"
                    "请明确失效旧模型后重新校正"
                )
            if (
                calibration_protocol_hash_value
                and existing_protocol_hash
                and existing_protocol_hash != str(calibration_protocol_hash_value)
            ):
                raise BadRequest(
                    "现有 time model 的 calibration protocol 绑定与当前项目不一致；"
                    "请明确失效旧模型后重新校正"
                )
            missing_layout_hash = bool(
                acquisition_layout_hash_value and not existing_layout_hash
            )
            missing_protocol_hash = bool(
                calibration_protocol_hash_value and not existing_protocol_hash
            )
            if missing_layout_hash or missing_protocol_hash:
                if not allow_unhashed_legacy_binding:
                    raise BadRequest(
                        "现有 time model 没有 acquisition layout / calibration protocol 绑定，"
                        "不能静默迁移到新的校准配置；"
                        "请先清除旧 time model，再重新进行 QC 校正和后段局部校正"
                    )
                existing = self.upsert_time_model(
                    {
                        **existing,
                        "acquisition_layout_hash": (
                            acquisition_layout_hash_value
                            if missing_layout_hash
                            else existing.get("acquisition_layout_hash")
                        ),
                        "calibration_protocol_hash": (
                            calibration_protocol_hash_value
                            if missing_protocol_hash
                            else existing.get("calibration_protocol_hash")
                        ),
                    },
                    action="bind_legacy_time_model_to_project_protocol",
                )
            return existing
        config = self.project_config()
        payload = {
            "time_model_version": f"tm_{uuid.uuid4().hex[:12]}",
            "status": "draft",
            "base_model_name": base_model_name,
            "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
            "sample_valve_switch_min": float(config["sample_valve_switch_min"]),
            "annotation_start_min": float(config["annotation_start_min"]),
            "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
            "ms_local_delta_sec": 0.0,
            "contains_cell_labels": False,
            "max_training_time_min": float(config["annotation_start_min"]) + float(config["local_delta_seed_window_min"]),
            "evidence_count": 0,
            "unique_match_count": 0,
            "conflict_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
            "method": "default_zero_delta_pending_freeze",
            "residual_summary": {},
            "acquisition_layout_hash": acquisition_layout_hash_value,
            "calibration_protocol_hash": calibration_protocol_hash_value,
        }
        return self.upsert_time_model(payload, action="default_draft_time_model")

    def upsert_time_model(self, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        row = self._prepare_time_model_row(payload)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_time_model_row(conn, row, action=action)
        return self.active_time_model() or row

    def _prepare_time_model_row(self, payload: dict[str, Any], *, timestamp: str | None = None) -> dict[str, Any]:
        timestamp = timestamp or now_iso()
        version = str(payload.get("time_model_version") or f"tm_{uuid.uuid4().hex[:12]}")
        status = str(payload.get("status", "draft"))
        if status not in {"draft", "frozen", "exploratory"}:
            raise BadRequest("time model status must be draft, frozen, or exploratory")
        if bool(payload.get("contains_cell_labels", False)):
            raise BadRequest("local delta time model must not contain cell labels")
        return {
            **payload,
            "time_model_version": version,
            "created_at": str(payload.get("created_at") or timestamp),
            "updated_at": timestamp,
            "status": status,
            "is_active": 1,
            "contains_cell_labels": 0,
            "payload_json": json.dumps(
                {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "time_model_version",
                        "created_at",
                        "updated_at",
                        "status",
                        "is_active",
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }

    def _upsert_time_model_row(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any],
        *,
        action: str,
    ) -> None:
        version = str(row["time_model_version"])
        conn.execute("UPDATE time_models SET is_active = 0 WHERE time_model_version != ?", (version,))
        conn.execute(
            """
                INSERT INTO time_models (
                    time_model_version, created_at, updated_at, status, is_active,
                    base_model_name, qc_calibration_end_min, sample_valve_switch_min,
                    annotation_start_min, local_delta_seed_window_min, ms_local_delta_sec,
                    contains_cell_labels, max_training_time_min, evidence_count,
                    unique_match_count, conflict_count, median_abs_residual_sec,
                    p90_abs_residual_sec, payload_json
                )
                VALUES (
                    :time_model_version, :created_at, :updated_at, :status, :is_active,
                    :base_model_name, :qc_calibration_end_min, :sample_valve_switch_min,
                    :annotation_start_min, :local_delta_seed_window_min, :ms_local_delta_sec,
                    :contains_cell_labels, :max_training_time_min, :evidence_count,
                    :unique_match_count, :conflict_count, :median_abs_residual_sec,
                    :p90_abs_residual_sec, :payload_json
                )
                ON CONFLICT(time_model_version) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    is_active=excluded.is_active,
                    base_model_name=excluded.base_model_name,
                    qc_calibration_end_min=excluded.qc_calibration_end_min,
                    sample_valve_switch_min=excluded.sample_valve_switch_min,
                    annotation_start_min=excluded.annotation_start_min,
                    local_delta_seed_window_min=excluded.local_delta_seed_window_min,
                    ms_local_delta_sec=excluded.ms_local_delta_sec,
                    contains_cell_labels=excluded.contains_cell_labels,
                    max_training_time_min=excluded.max_training_time_min,
                    evidence_count=excluded.evidence_count,
                    unique_match_count=excluded.unique_match_count,
                    conflict_count=excluded.conflict_count,
                    median_abs_residual_sec=excluded.median_abs_residual_sec,
                    p90_abs_residual_sec=excluded.p90_abs_residual_sec,
                    payload_json=excluded.payload_json
            """,
            row,
        )
        self._insert_time_model_audit_row(conn, action=action, time_model_version=version, payload=row)

    def _insert_time_model_audit_row(
        self,
        conn: sqlite3.Connection,
        *,
        action: str,
        time_model_version: str | None,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO time_model_audit_events (
                audit_id, timestamp, action, time_model_version, payload_json, app_version
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                now_iso(),
                action,
                time_model_version,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                APP_VERSION,
            ),
        )

    def get(self, annotation_id: str) -> dict[str, Any] | None:
        values = getattr(_REQUEST_READ_SNAPSHOT, "values", None)
        if values is not None:
            index = request_cached_read(
                self,
                "annotation_record_index",
                lambda: {
                    str(row.get("annotation_id") or ""): row
                    for row in self.records()
                },
            )
            row = index.get(str(annotation_id))
            return copy.deepcopy(row) if row is not None else None
        return self._get_uncached(annotation_id)

    def _get_uncached(self, annotation_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)).fetchone()
            return self._decode_annotation_row(row) if row else None

    def records(self) -> list[dict[str, Any]]:
        return request_cached_read(
            self,
            "annotation_records",
            self._records_uncached,
        )

    def _records_uncached(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM annotations ORDER BY ms_time_min, annotation_id").fetchall()
            return [self._decode_annotation_row(row) for row in rows]

    def input_manifest(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM input_manifest ORDER BY input_key").fetchall()
            return [{key: clean_value(row[key]) for key in row.keys()} for row in rows]

    def record_export_run(
        self,
        *,
        export_id: str,
        timestamp: str,
        filter_payload: dict[str, Any],
        row_count: int,
        csv_path: Path,
        csv_sha256: str,
        input_manifest: list[dict[str, Any]],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO export_runs (
                    export_id, timestamp, filter_json, row_count, csv_path,
                    csv_sha256, app_version, input_manifest_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    timestamp,
                    json.dumps(filter_payload, ensure_ascii=False, sort_keys=True),
                    int(row_count),
                    display_path(csv_path),
                    csv_sha256,
                    APP_VERSION,
                    json.dumps(input_manifest, ensure_ascii=False, sort_keys=True),
                ),
            )

    def _invalidate_qc_alignment_model_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        reason: str,
        annotation_id: str | None = None,
    ) -> bool:
        model_row = conn.execute(
            "SELECT value_json FROM project_config WHERE key = ?",
            (QC_ALIGNMENT_MODEL_KEY,),
        ).fetchone()
        if not model_row:
            return False
        previous_qc_model = json.loads(model_row["value_json"])
        active_row = conn.execute(
            "SELECT * FROM time_models WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        active_model = self._decode_time_model_row(active_row) if active_row else None
        timestamp = now_iso()
        conn.execute("DELETE FROM project_config WHERE key = ?", (QC_ALIGNMENT_MODEL_KEY,))
        conn.execute("UPDATE time_models SET is_active = 0, updated_at = ? WHERE is_active = 1", (timestamp,))
        self._insert_time_model_audit_row(
            conn,
            action="invalidate_qc_alignment_model_for_qc_evidence_change",
            time_model_version=str((active_model or {}).get("time_model_version") or "") or None,
            payload={
                "reason": reason,
                "annotation_id": annotation_id,
                "previous_qc_alignment_model": previous_qc_model,
                "invalidated_time_model": active_model,
            },
        )
        return True

    def hard_delete_manual(
        self,
        annotation_id: str,
        *,
        invalidate_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)).fetchone()
            if not row:
                return {"annotation_id": annotation_id, "deleted": False, "reason": "not_found"}
            current = self._decode_annotation_row(row)
            if current.get("source") != "manual_created":
                raise BadRequest("Only manual_created annotations can be cleared")
            if invalidate_qc_alignment_model:
                self._invalidate_qc_alignment_model_in_conn(
                    conn,
                    reason="clear_manual_qc_calibration_anchor",
                    annotation_id=annotation_id,
                )
            conn.execute("DELETE FROM annotations WHERE annotation_id = ?", (annotation_id,))
            conn.execute("DELETE FROM audit_events WHERE annotation_id = ?", (annotation_id,))
        invalidate_request_cached_reads(
            self,
            "annotation_records",
            "annotation_record_index",
        )
        return {"annotation_id": annotation_id, "deleted": True, "source": "manual_created"}

    def upsert_review(
        self,
        *,
        annotation_id: str,
        source: str,
        review_status: str,
        payload: dict[str, Any],
        action: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        reason_code: str | None = None,
        notes: str | None = None,
        invalidate_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        if review_status not in REVIEW_STATUSES:
            raise BadRequest(f"review_status must be one of {sorted(REVIEW_STATUSES)}")
        if source not in ANNOTATION_SOURCES:
            raise BadRequest(f"source must be one of {sorted(ANNOTATION_SOURCES)}")
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if invalidate_qc_alignment_model:
                self._invalidate_qc_alignment_model_in_conn(
                    conn,
                    reason="qc_calibration_review_change",
                    annotation_id=annotation_id,
                )
            previous_row = conn.execute("SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)).fetchone()
            previous = self._decode_annotation_row(previous_row) if previous_row else {}
            if review_status == "pending" and source == "auto_candidate":
                conn.execute("DELETE FROM annotations WHERE annotation_id = ?", (annotation_id,))
                current = {}
            else:
                current = {
                    **previous,
                    **payload,
                    "annotation_id": annotation_id,
                    "source": source,
                    "review_status": review_status,
                    "exportable": review_status == "accepted",
                    "updated_at": timestamp,
                }
                current.setdefault("created_at", timestamp)
                self._upsert_annotation_row(conn, current)
            audit = self._audit_row(
                annotation_id=annotation_id,
                source=source,
                review_status=review_status,
                payload=payload,
                action=action,
                previous=previous,
                timestamp=timestamp,
                window_start_min=window_start_min,
                window_end_min=window_end_min,
                time_mode=time_mode,
                reason_code=reason_code,
                notes=notes,
            )
            self._insert_audit_row(conn, audit)
        invalidate_request_cached_reads(
            self,
            "annotation_records",
            "annotation_record_index",
        )
        return current or {
            "annotation_id": annotation_id,
            "source": source,
            "review_status": "pending",
            "exportable": False,
        }


class BadRequest(ValueError):
    pass


_PUBLIC_ERROR_FALLBACK = (
    "操作未完成。请检查当前页面中的项目设置和所选记录；"
    "如果刚修改过设置，请重新生成预览后再试。"
)


def user_facing_error_message(error: Any) -> str:
    """Translate implementation-facing failures before they reach the UI.

    Internal exceptions remain unchanged for logs and tests.  The HTTP boundary
    uses this function so a desktop alert never asks a scientist to interpret
    schema keys, hashes, matcher modes, or implementation version numbers.
    """

    message = str(error or "").strip()
    if not message:
        return _PUBLIC_ERROR_FALLBACK
    lowered = message.lower()
    if any(
        token in lowered
        for token in (
            "lif_peak_detection",
            "detector_config_hash",
            "detector_version",
            "peak_tier",
            "detector",
        )
    ):
        return (
            "项目的峰识别设置无效或不完整。请保留原项目不变，"
            "并在新的空目录中重新选择原始 LIF、MS 和事件坐标 CSV。"
        )
    if "preview_hash" in lowered or "protocol_hash" in lowered or " hash" in lowered:
        return "当前预览或项目设置已经变化，请重新打开对应步骤并生成新的预览。"
    if "must be numeric" in lowered:
        if "annotation_start_min" in lowered:
            return "事件标注起点必须填写为数字。"
        if all(
            token in lowered
            for token in ("start_min", "window_min", "preview_ms_delta_sec")
        ):
            return "开始时间、窗口宽度和预览 MS 时间差必须填写为数字。"
        return "时间参数必须填写为数字。"
    if (
        ("candidate_id" in lowered or any(prefix in lowered for prefix in ("auto_qc:", "post_qc:", "cell:")))
        and any(token in lowered for token in ("unknown", "inactive", "active window"))
    ):
        return "该候选关系不属于当前图窗或已经过期，请刷新图窗后重新选择。"
    if re.search(r"(?i)\bv(?:0\.3|1|2)(?:\.\d+)*\b", message):
        return (
            "该项目的设置格式无法由当前软件安全读取。请保留原项目不变，"
            "并在新的空目录中从原始输入重新创建。"
        )

    replacements = (
        ("post_qc_strategy", "后段质控巡检设置"),
        ("calibration_protocol", "前段参考设置"),
        ("qc_anchor_channels", "前段参考通道"),
        ("scheduled_windows", "按指定时间窗口巡检"),
        ("signature", "按参考通道巡检"),
        ("disabled", "不进行后段巡检"),
        ("green_axis", "绿色信号时间轴"),
        ("red_axis", "红色信号时间轴"),
        ("lif_anchor_peak_ids", "LIF 参考峰"),
        ("anchor_peak_id", "参考峰"),
        ("frozen time-axis model", "已锁定的时间校正结果"),
        ("frozen time model", "已锁定的时间校正结果"),
        ("draft time model", "尚未锁定的时间校正结果"),
        ("time-axis model", "时间校正结果"),
        ("time_axis", "采集时间基准"),
        ("time-axis", "采集时间基准"),
        ("preview_ms_delta_sec", "预览 MS 时间差"),
        ("local_delta_seed_window_min", "自动估计时间差的范围"),
        ("annotation_start_min", "事件标注起点"),
        ("window_min", "窗口宽度"),
        ("start_min", "开始时间"),
        ("end_min", "结束时间"),
        ("reference_channels", "参考通道"),
        ("project_dir", "项目保存路径"),
        ("candidate_id", "候选关系"),
        ("ms_event_id", "MS 事件"),
        ("lif_peak_id", "LIF 峰"),
        ("segment_id", "参考段"),
        ("frozen", "已锁定"),
        ("draft", "草稿"),
        ("anchor", "参考峰"),
        ("delta", "MS 时间差"),
    )
    translated = message
    for internal, visible in replacements:
        translated = re.sub(re.escape(internal), visible, translated, flags=re.IGNORECASE)

    forbidden = re.compile(
        r"(?i)(?:detector|\bhash\b|calibration_protocol|post_qc_strategy|"
        r"qc_anchor_channels|signature|scheduled_windows|disabled|green_axis|"
        r"red_axis|preview_hash|\banchor\b|\bdelta\b|\bfrozen\b|\bdraft\b|"
        r"time[-_]axis|peak_tier|\bv(?:0\.3|1|2)\b)"
    )
    if forbidden.search(translated) or re.search(r"\b[A-Za-z]+_[A-Za-z0-9_]+\b", translated):
        return _PUBLIC_ERROR_FALLBACK
    return translated


def display_phase_from_time_min(
    time_min: pd.Series | np.ndarray,
    calibration_protocol: dict[str, Any] | None = None,
    annotation_start_min: float | None = None,
) -> np.ndarray:
    t = np.asarray(time_min, dtype=float)
    if calibration_protocol and not calibration_protocol.get("compatibility_mode"):
        conditions: list[np.ndarray] = []
        choices: list[str] = []
        for segment in calibration_protocol.get("segments") or []:
            conditions.append(
                (t >= float(segment["start_min"]))
                & (t <= float(segment["end_min"]))
            )
            prefix = (
                "calibration"
                if bool(segment.get("boundaries_confirmed"))
                else "calibration_draft"
            )
            choices.append(f"{prefix}:{segment['segment_id']}")
        start_min = float(
            DEFAULT_ANNOTATION_START_MIN
            if annotation_start_min is None
            else annotation_start_min
        )
        conditions.append(t >= start_min)
        choices.append("annotation_region")
        return np.select(
            conditions,
            choices,
            default="pre_annotation_unassigned",
        )
    return np.select(
        [t < QC_SHIFT_WINDOW_MIN, (t >= QC_SHIFT_WINDOW_MIN) & (t < PRE_RUN_MAX_MIN), t >= PRE_RUN_MAX_MIN],
        ["qc_start", "pre_run", "cell_run"],
        default="unknown",
    )


def downsample_frame(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    max_points: int = MAX_TRACE_POINTS_PER_SERIES,
) -> pd.DataFrame:
    if len(df) <= max_points:
        return df[[x_col, y_col]].copy()
    ordered = df[[x_col, y_col]].sort_values(x_col).reset_index(drop=True)
    bucket_count = max(1, max_points // 4)
    buckets = np.floor(np.linspace(0, bucket_count - 1, len(ordered))).astype(int)
    ordered = ordered.assign(_bucket=buckets, _pos=np.arange(len(ordered)))

    keep_positions: list[int] = []
    for _, group in ordered.groupby("_bucket", sort=True):
        keep_positions.append(int(group["_pos"].iloc[0]))
        keep_positions.append(int(group.loc[group[y_col].idxmin(), "_pos"]))
        keep_positions.append(int(group.loc[group[y_col].idxmax(), "_pos"]))
        keep_positions.append(int(group["_pos"].iloc[-1]))

    compact = ordered.iloc[sorted(set(keep_positions))]
    return compact[[x_col, y_col]]


def xy_records(df: pd.DataFrame, x_col: str, y_col: str) -> list[dict[str, float]]:
    if df.empty:
        return []
    compact = downsample_frame(df, x_col, y_col)
    return [
        {"x": float(x), "y": float(y) if math.isfinite(float(y)) else 0.0}
        for x, y in compact[[x_col, y_col]].to_numpy()
    ]


def greedy_time_matches(
    lif_times_sec: np.ndarray,
    ms_times_sec: np.ndarray,
    shift_sec: float,
    tolerance_sec: float,
) -> list[tuple[int, int, float]]:
    """Return a one-to-one, order-preserving match between two time series.

    The public name is retained for compatibility with older callers.  The old
    implementation greedily consumed the smallest individual residual.  In a
    dense peak cluster that can cross two neighbouring relations, or even lose
    a second feasible relation.  A physical acquisition has a shared forward
    time direction, so a later LIF pulse cannot represent an earlier MS event
    when an earlier LIF pulse is assigned to a later event.

    This implementation solves the sparse monotone sequence-matching problem.
    Its lexicographic objective is: maximise cardinality, minimise total
    absolute residual, minimise the worst residual, then minimise squared
    residual.  A Fenwick prefix optimum keeps the work O(E log M), where E is
    the number of pairs inside the tolerance, rather than O(N*M).  That matters
    because the same matcher is evaluated over many candidate axis shifts.
    """

    lif_values = np.asarray(lif_times_sec, dtype=float)
    ms_values = np.asarray(ms_times_sec, dtype=float)
    tolerance = float(tolerance_sec)
    shift = float(shift_sec)
    if tolerance < 0 or not math.isfinite(tolerance) or not math.isfinite(shift):
        raise ValueError("time-match shift and tolerance must be finite; tolerance must be >= 0")
    finite_lif = np.flatnonzero(np.isfinite(lif_values))
    finite_ms = np.flatnonzero(np.isfinite(ms_values))
    if not len(finite_lif) or not len(finite_ms):
        return []
    lif_order = finite_lif[np.argsort(lif_values[finite_lif], kind="stable")]
    ms_order = finite_ms[np.argsort(ms_values[finite_ms], kind="stable")]
    sorted_lif = lif_values[lif_order] + shift
    sorted_ms = ms_values[ms_order]

    # State: match count, total |residual|, max |residual|, sum residual^2,
    # predecessor-node index.  Nodes hold only back-pointers, so shift scans do
    # not repeatedly copy complete match paths.
    State = tuple[int, float, float, float, int]
    empty: State = (0, 0.0, 0.0, 0.0, -1)
    nodes: list[tuple[int, int, int, float]] = []
    path_cache: dict[int, tuple[tuple[int, int], ...]] = {-1: ()}

    def state_path(node_index: int) -> tuple[tuple[int, int], ...]:
        cached = path_cache.get(node_index)
        if cached is not None:
            return cached
        missing: list[int] = []
        cursor = node_index
        while cursor not in path_cache:
            missing.append(cursor)
            cursor = nodes[cursor][0]
        path = path_cache[cursor]
        for current in reversed(missing):
            _previous, lif_index, ms_index, _residual = nodes[current]
            path = path + ((lif_index, ms_index),)
            path_cache[current] = path
        return path_cache[node_index]

    def better(candidate: State, current: State | None) -> bool:
        if current is None:
            return True
        if candidate[0] != current[0]:
            return candidate[0] > current[0]
        for index in (1, 2, 3):
            delta = candidate[index] - current[index]
            scale = max(1.0, abs(candidate[index]), abs(current[index]))
            if abs(delta) > 1e-12 * scale:
                return delta < 0.0
        # Exact scientific ties are resolved by the earliest index sequence so
        # output is stable across platforms and Python versions.
        return state_path(candidate[4]) < state_path(current[4])

    tree: list[State | None] = [None] * (len(sorted_ms) + 1)

    def query(prefix_length: int) -> State:
        best: State | None = None
        cursor = int(prefix_length)
        while cursor > 0:
            value = tree[cursor]
            if value is not None and better(value, best):
                best = value
            cursor -= cursor & -cursor
        return best or empty

    def update(position: int, candidate: State) -> None:
        cursor = int(position)
        while cursor < len(tree):
            if better(candidate, tree[cursor]):
                tree[cursor] = candidate
            cursor += cursor & -cursor

    for sorted_lif_index, value in enumerate(sorted_lif):
        left = int(np.searchsorted(sorted_ms, value - tolerance, side="left"))
        right = int(np.searchsorted(sorted_ms, value + tolerance, side="right"))
        pending: list[tuple[int, State]] = []
        for sorted_ms_index in range(left, right):
            previous = query(sorted_ms_index)  # Strictly earlier MS indices.
            residual = float(sorted_ms[sorted_ms_index] - value)
            abs_residual = abs(residual)
            node_index = len(nodes)
            nodes.append(
                (
                    previous[4],
                    int(lif_order[sorted_lif_index]),
                    int(ms_order[sorted_ms_index]),
                    residual,
                )
            )
            candidate: State = (
                previous[0] + 1,
                previous[1] + abs_residual,
                max(previous[2], abs_residual),
                previous[3] + residual * residual,
                node_index,
            )
            pending.append((sorted_ms_index + 1, candidate))
        # Delay updates until the whole LIF row has been considered; otherwise
        # two edges from one LIF peak could be chained into the same solution.
        for position, candidate in pending:
            update(position, candidate)

    best = query(len(sorted_ms))
    out: list[tuple[int, int, float]] = []
    node_index = best[4]
    while node_index >= 0:
        previous, lif_index, ms_index, residual = nodes[node_index]
        out.append((lif_index, ms_index, residual))
        node_index = previous
    out.reverse()
    return out


def automatic_lif_peak_evidence(lif_peaks: pd.DataFrame) -> pd.DataFrame:
    """Exclude weak display evidence from every automatic scientific path.

    A missing tier is interpreted as legacy-v1 ``core`` in memory.  This
    function never adds a column or rewrites the source table.
    """

    if "peak_tier" not in lif_peaks.columns:
        return lif_peaks
    return lif_peaks[
        ~lif_peaks["peak_tier"]
        .fillna("core")
        .astype(str)
        .str.strip()
        .str.lower()
        .eq("weak")
    ]


def estimate_channel_shift(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    channel: str,
    qc_calibration_end_min: float = QC_SHIFT_WINDOW_MIN,
) -> dict[str, Any]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    qc_end = float(qc_calibration_end_min)
    lif = lif_peaks[
        lif_peaks["channel"].eq(channel)
        & lif_peaks["time_min"].between(0.0, qc_end, inclusive="both")
    ].sort_values("time_sec").reset_index(drop=True)
    ms = ms_events[
        ms_events["time_min"].between(0.0, qc_end, inclusive="both")
    ].sort_values("time_sec").reset_index(drop=True)

    if lif.empty or ms.empty:
        return {
            "channel": channel,
            "shift_sec": 0.0,
            "match_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
            "status": "insufficient_peaks",
            "shift_estimation_matches": [],
        }

    lif_times = lif["time_sec"].to_numpy(float)
    ms_times = ms["time_sec"].to_numpy(float)
    best: tuple[int, float, float, float, list[tuple[int, int, float]]] | None = None
    for shift in np.arange(SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC + SHIFT_SEARCH_STEP_SEC / 2.0, SHIFT_SEARCH_STEP_SEC):
        matches = greedy_time_matches(lif_times, ms_times, float(shift), SHIFT_MATCH_TOL_SEC)
        abs_resid = np.asarray([abs(item[2]) for item in matches], dtype=float)
        median_abs = float(np.median(abs_resid)) if len(abs_resid) else float("inf")
        p90_abs = float(np.quantile(abs_resid, 0.90)) if len(abs_resid) else float("inf")
        candidate = (len(matches), -median_abs, -p90_abs, -abs(float(shift)), matches)
        if best is None or candidate[:4] > best[:4]:
            best = candidate

    assert best is not None
    match_count, neg_median, neg_p90, _, best_matches = best
    shift_sec = float(np.arange(SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC + SHIFT_SEARCH_STEP_SEC / 2.0, SHIFT_SEARCH_STEP_SEC)[0])
    # Recover the selected shift by re-scanning the same ordering. This keeps the
    # tie-break logic in one place and avoids storing numpy scalars in best.
    selected_key = best[:4]
    for shift in np.arange(SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC + SHIFT_SEARCH_STEP_SEC / 2.0, SHIFT_SEARCH_STEP_SEC):
        matches = greedy_time_matches(lif_times, ms_times, float(shift), SHIFT_MATCH_TOL_SEC)
        abs_resid = np.asarray([abs(item[2]) for item in matches], dtype=float)
        median_abs = float(np.median(abs_resid)) if len(abs_resid) else float("inf")
        p90_abs = float(np.quantile(abs_resid, 0.90)) if len(abs_resid) else float("inf")
        key = (len(matches), -median_abs, -p90_abs, -abs(float(shift)))
        if key == selected_key:
            shift_sec = float(shift)
            best_matches = matches
            break

    rows = []
    for rank, (lif_idx, ms_idx, residual) in enumerate(best_matches, start=1):
        lif_row = lif.iloc[int(lif_idx)]
        ms_row = ms.iloc[int(ms_idx)]
        rows.append(
            {
                "rank": rank,
                "channel": channel,
                "lif_peak_id": lif_row["peak_id"],
                "ms_event_id": ms_row["event_id"],
                "lif_raw_time_min": float(lif_row["time_min"]),
                "lif_aligned_time_min": float((lif_row["time_sec"] + shift_sec) / 60.0),
                "ms_time_min": float(ms_row["time_min"]),
                "shift_sec": shift_sec,
                "residual_sec": float(residual),
                "abs_residual_sec": abs(float(residual)),
            }
        )

    abs_values = [row["abs_residual_sec"] for row in rows]
    return {
        "channel": channel,
        "shift_sec": shift_sec,
        "match_count": int(match_count),
        "median_abs_residual_sec": float(np.median(abs_values)) if abs_values else None,
        "p90_abs_residual_sec": float(np.quantile(abs_values, 0.90)) if abs_values else None,
        "status": "auto_shift_only_suggestion",
        "search_window_min": [0.0, qc_end],
        "search_range_sec": [SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC],
        "match_tolerance_sec": SHIFT_MATCH_TOL_SEC,
        "shift_estimation_matches": rows,
    }


def build_axis_peak_clusters(
    lif_peaks: pd.DataFrame,
    channels: list[str],
    *,
    start_min: float,
    end_min: float,
    tolerance_sec: float = LIF_PAIR_MATCH_TOL_SEC,
) -> list[dict[str, Any]]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    selected = lif_peaks[
        lif_peaks["channel"].isin(channels)
        & lif_peaks["time_min"].between(float(start_min), float(end_min), inclusive="both")
    ].sort_values("time_sec")
    if selected.empty:
        return []
    raw_clusters: list[list[pd.Series]] = []
    for _, row in selected.iterrows():
        if not raw_clusters:
            raw_clusters.append([row])
            continue
        cluster_start = float(raw_clusters[-1][0]["time_sec"])
        if float(row["time_sec"]) - cluster_start <= float(tolerance_sec):
            raw_clusters[-1].append(row)
        else:
            raw_clusters.append([row])

    clusters: list[dict[str, Any]] = []
    for raw_cluster in raw_clusters:
        members: dict[str, pd.Series] = {}
        for row in raw_cluster:
            channel = str(row["channel"])
            current = members.get(channel)
            row_quality = float(row.get("snr", 0.0) or 0.0)
            current_quality = float(current.get("snr", 0.0) or 0.0) if current is not None else -1.0
            if current is None or row_quality > current_quality:
                members[channel] = row
        times = [float(row["time_sec"]) for row in members.values()]
        if times and max(times) - min(times) > float(tolerance_sec) + QC_COMPONENT_SELECT_EPS:
            continue
        clusters.append(
            {
                "time_sec": float(np.median(times)),
                "members": members,
                "support_count": len(members),
                "full_support": len(members) == len(channels),
                "quality_score": float(
                    sum(math.log1p(max(0.0, float(row.get("snr", 0.0) or 0.0))) for row in members.values())
                ),
            }
        )
    return clusters


def estimate_axis_shift(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    time_axis: str,
    channels: list[str],
    qc_calibration_end_min: float,
) -> dict[str, Any]:
    if len(channels) == 1:
        single = estimate_channel_shift(lif_peaks, ms_events, channels[0], qc_calibration_end_min)
        return {
            **single,
            "time_axis": time_axis,
            "channels": channels,
            "status": "auto_axis_shift_single_anchor",
        }
    clusters = build_axis_peak_clusters(
        lif_peaks,
        channels,
        start_min=0.0,
        end_min=float(qc_calibration_end_min),
    )
    ms = ms_events[
        ms_events["time_min"].between(0.0, float(qc_calibration_end_min), inclusive="both")
    ].sort_values("time_sec").reset_index(drop=True)
    if not clusters or ms.empty:
        return {
            "time_axis": time_axis,
            "channels": channels,
            "shift_sec": 0.0,
            "match_count": 0,
            "multi_channel_match_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
            "status": "insufficient_axis_anchor_peaks",
            "shift_estimation_matches": [],
        }

    lif_times = np.asarray([float(cluster["time_sec"]) for cluster in clusters], dtype=float)
    ms_times = ms["time_sec"].to_numpy(float)
    best: tuple[tuple[Any, ...], float, list[tuple[int, int, float]]] | None = None
    for shift in np.arange(SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC + SHIFT_SEARCH_STEP_SEC / 2.0, SHIFT_SEARCH_STEP_SEC):
        matches = greedy_time_matches(lif_times, ms_times, float(shift), SHIFT_MATCH_TOL_SEC)
        abs_resid = np.asarray([abs(item[2]) for item in matches], dtype=float)
        multi_count = sum(1 for lif_idx, _, _ in matches if int(clusters[int(lif_idx)]["support_count"]) >= 2)
        support_count = sum(int(clusters[int(lif_idx)]["support_count"]) for lif_idx, _, _ in matches)
        median_abs = float(np.median(abs_resid)) if len(abs_resid) else float("inf")
        p90_abs = float(np.quantile(abs_resid, 0.90)) if len(abs_resid) else float("inf")
        score = (len(matches), multi_count, support_count, -median_abs, -p90_abs, -abs(float(shift)))
        if best is None or score > best[0]:
            best = (score, float(shift), matches)
    assert best is not None
    _, shift_sec, matches = best
    rows: list[dict[str, Any]] = []
    for rank, (lif_idx, ms_idx, residual) in enumerate(matches, start=1):
        cluster = clusters[int(lif_idx)]
        ms_row = ms.iloc[int(ms_idx)]
        rows.append(
            {
                "rank": rank,
                "time_axis": time_axis,
                "channels": sorted(cluster["members"]),
                "lif_peak_ids": {
                    channel: str(row["peak_id"])
                    for channel, row in sorted(cluster["members"].items())
                },
                "lif_raw_time_min": float(cluster["time_sec"] / 60.0),
                "lif_aligned_time_min": float((cluster["time_sec"] + shift_sec) / 60.0),
                "ms_event_id": str(ms_row["event_id"]),
                "ms_time_min": float(ms_row["time_min"]),
                "shift_sec": shift_sec,
                "support_count": int(cluster["support_count"]),
                "residual_sec": float(residual),
                "abs_residual_sec": abs(float(residual)),
            }
        )
    abs_values = [row["abs_residual_sec"] for row in rows]
    return {
        "time_axis": time_axis,
        "channels": channels,
        "shift_sec": shift_sec,
        "match_count": len(rows),
        "multi_channel_match_count": sum(1 for row in rows if int(row["support_count"]) >= 2),
        "median_abs_residual_sec": float(np.median(abs_values)) if abs_values else None,
        "p90_abs_residual_sec": float(np.quantile(abs_values, 0.90)) if abs_values else None,
        "status": "auto_axis_shift_joint_anchor",
        "search_window_min": [0.0, float(qc_calibration_end_min)],
        "search_range_sec": [SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC],
        "match_tolerance_sec": SHIFT_MATCH_TOL_SEC,
        "shift_estimation_matches": rows,
    }


def estimate_segmented_axis_shift(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    time_axis: str,
    calibration_protocol: dict[str, Any],
    channel_time_axes: dict[str, str],
) -> dict[str, Any]:
    segment_inputs: list[tuple[dict[str, Any], list[str], list[dict[str, Any]], pd.DataFrame]] = []
    for segment in calibration_protocol["segments"]:
        channels = [
            str(channel)
            for channel in segment["reference_channels"]
            if str(channel_time_axes[channel]) == str(time_axis)
        ]
        if not channels:
            continue
        clusters = build_axis_peak_clusters(
            lif_peaks,
            channels,
            start_min=float(segment["start_min"]),
            end_min=float(segment["end_min"]),
        )
        ms = primary_pc34_events(ms_events)
        ms = ms[
            ms["time_min"].between(
                float(segment["start_min"]),
                float(segment["end_min"]),
                inclusive="both",
            )
        ].sort_values("time_sec").reset_index(drop=True)
        segment_inputs.append((segment, channels, clusters, ms))

    best: tuple[tuple[Any, ...], float, list[dict[str, Any]]] | None = None
    scored: list[tuple[tuple[Any, ...], float, list[dict[str, Any]]]] = []
    for shift in np.arange(
        SHIFT_SEARCH_MIN_SEC,
        SHIFT_SEARCH_MAX_SEC + SHIFT_SEARCH_STEP_SEC / 2.0,
        SHIFT_SEARCH_STEP_SEC,
    ):
        match_rows: list[dict[str, Any]] = []
        support_count = 0
        multi_channel_count = 0
        for segment, channels, clusters, ms in segment_inputs:
            if not clusters or ms.empty:
                continue
            lif_times = np.asarray([float(cluster["time_sec"]) for cluster in clusters], dtype=float)
            ms_times = ms["time_sec"].to_numpy(float)
            matches = greedy_time_matches(lif_times, ms_times, float(shift), SHIFT_MATCH_TOL_SEC)
            for lif_idx, ms_idx, residual in matches:
                cluster = clusters[int(lif_idx)]
                ms_row = ms.iloc[int(ms_idx)]
                support = int(cluster["support_count"])
                support_count += support
                multi_channel_count += int(support >= 2)
                match_rows.append(
                    {
                        "calibration_segment_id": str(segment["segment_id"]),
                        "calibration_segment_order": int(segment["order"]),
                        "population_label": str(segment.get("population_label") or ""),
                        "time_axis": str(time_axis),
                        "channels": sorted(cluster["members"]),
                        "lif_peak_ids": {
                            str(channel): str(row["peak_id"])
                            for channel, row in sorted(cluster["members"].items())
                        },
                        "lif_raw_time_min": float(cluster["time_sec"] / 60.0),
                        "lif_aligned_time_min": float((cluster["time_sec"] + float(shift)) / 60.0),
                        "ms_event_id": str(ms_row["event_id"]),
                        "ms_time_min": float(ms_row["time_min"]),
                        "shift_sec": float(shift),
                        "support_count": support,
                        "residual_sec": float(residual),
                        "abs_residual_sec": abs(float(residual)),
                    }
                )
        abs_residuals = np.asarray(
            [float(row["abs_residual_sec"]) for row in match_rows], dtype=float
        )
        median_abs = float(np.median(abs_residuals)) if len(abs_residuals) else float("inf")
        p90_abs = float(np.quantile(abs_residuals, 0.90)) if len(abs_residuals) else float("inf")
        independent_events = len({str(row["ms_event_id"]) for row in match_rows})
        score = (
            independent_events,
            len(match_rows),
            support_count,
            multi_channel_count,
            -median_abs,
            -p90_abs,
            -abs(float(shift)),
        )
        scored.append((score, float(shift), match_rows))
        if best is None or score > best[0]:
            best = (score, float(shift), match_rows)
    assert best is not None
    best_score, shift_sec, rows = best
    rows = sorted(
        rows,
        key=lambda row: (
            int(row["calibration_segment_order"]),
            float(row["ms_time_min"]),
            str(row["ms_event_id"]),
        ),
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    separated = sorted(
        (
            candidate
            for candidate in scored
            if abs(float(candidate[1]) - float(shift_sec)) >= max(1.0, SHIFT_MATCH_TOL_SEC)
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    runner_up = separated[0] if separated else None
    ambiguous = bool(
        runner_up
        and runner_up[0][:4] == best_score[:4]
        and float(-runner_up[0][4]) <= float(-best_score[4]) + 0.25
    )
    abs_values = [float(row["abs_residual_sec"]) for row in rows]
    channels = sorted(
        {
            channel
            for _segment, segment_channels, _clusters, _ms in segment_inputs
            for channel in segment_channels
        }
    )
    return {
        "time_axis": str(time_axis),
        "channels": channels,
        "shift_sec": float(shift_sec),
        "match_count": len(rows),
        "independent_event_count": len({str(row["ms_event_id"]) for row in rows}),
        "multi_channel_match_count": sum(int(row["support_count"] >= 2) for row in rows),
        "median_abs_residual_sec": float(np.median(abs_values)) if abs_values else None,
        "p90_abs_residual_sec": float(np.quantile(abs_values, 0.90)) if abs_values else None,
        "status": "insufficient_segmented_reference_evidence" if len(rows) < 2 else "auto_segmented_axis_shift",
        "recommendation_status": "insufficient_evidence" if len(rows) < 2 else "ambiguous" if ambiguous else "recommended",
        "search_range_sec": [SHIFT_SEARCH_MIN_SEC, SHIFT_SEARCH_MAX_SEC],
        "search_step_sec": SHIFT_SEARCH_STEP_SEC,
        "match_tolerance_sec": SHIFT_MATCH_TOL_SEC,
        "shift_estimation_matches": rows,
        "runner_up_shift_sec": float(runner_up[1]) if runner_up else None,
    }


def multi_anchor_groups_for_range(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    anchor_channels: list[str],
    channel_time_axes: dict[str, str],
    axis_shifts_sec: dict[str, float],
    context_start_min: float,
    context_end_min: float,
    minimum_raw_time_min: float,
    ms_shift_sec: float,
    tolerance_sec: float,
) -> list[dict[str, Any]]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    anchors = [str(channel).strip().upper() for channel in anchor_channels]
    required_axes = {str(channel_time_axes[channel]) for channel in anchors}
    ms = primary_pc34_events(ms_events)
    ms = ms[
        (ms["time_min"] >= float(minimum_raw_time_min))
        & ((ms["time_sec"].astype(float) + float(ms_shift_sec)) / 60.0).between(
            float(context_start_min), float(context_end_min), inclusive="both"
        )
    ].sort_values("time_sec").reset_index(drop=True)
    if ms.empty:
        return []
    ms_plot_times = ms["time_sec"].to_numpy(float) + float(ms_shift_sec)
    matched_by_ms: dict[int, dict[str, tuple[pd.Series, float, int]]] = {}
    for channel in anchors:
        axis = str(channel_time_axes[channel])
        shift_sec = float(axis_shifts_sec.get(axis, 0.0))
        peaks = lif_peaks[
            lif_peaks["channel"].eq(channel)
            & (lif_peaks["time_min"] >= float(minimum_raw_time_min))
        ].copy()
        if peaks.empty:
            continue
        peaks["plot_time_sec"] = peaks["time_sec"].astype(float) + shift_sec
        peaks = peaks[
            (peaks["plot_time_sec"] / 60.0).between(
                float(context_start_min), float(context_end_min), inclusive="both"
            )
        ].sort_values("plot_time_sec").reset_index(drop=True)
        if peaks.empty:
            continue
        peak_times = peaks["plot_time_sec"].to_numpy(float)
        matches = greedy_time_matches(peak_times, ms_plot_times, 0.0, float(tolerance_sec))
        for peak_idx, ms_idx, residual in matches:
            peak_time = float(peak_times[int(peak_idx)])
            ms_time = float(ms_plot_times[int(ms_idx)])
            peak_competitors = int(np.sum(np.abs(peak_times - ms_time) <= float(tolerance_sec)))
            ms_competitors = int(np.sum(np.abs(ms_plot_times - peak_time) <= float(tolerance_sec)))
            ambiguous = max(0, peak_competitors - 1) + max(0, ms_competitors - 1)
            matched_by_ms.setdefault(int(ms_idx), {})[channel] = (
                peaks.iloc[int(peak_idx)],
                float(residual),
                int(ambiguous),
            )

    groups: list[dict[str, Any]] = []
    for ms_idx, members in sorted(matched_by_ms.items()):
        same_axis_dropped_channels: list[str] = []
        ms_plot_sec_for_group = float(ms_plot_times[int(ms_idx)])
        for axis in required_axes:
            axis_candidates = []
            for channel, match in members.items():
                if str(channel_time_axes[channel]) != axis:
                    continue
                row, residual, _ambiguous = match
                plot_time_sec = float(row["time_sec"]) + float(axis_shifts_sec.get(axis, 0.0))
                axis_candidates.append((channel, match, plot_time_sec, float(row.get("snr", 0.0) or 0.0), residual))
            if len(axis_candidates) <= 1:
                continue
            axis_candidates.sort(key=lambda item: item[2])
            best_subset: list[tuple[Any, ...]] = []
            best_score: tuple[Any, ...] | None = None
            for left in range(len(axis_candidates)):
                for right in range(left, len(axis_candidates)):
                    subset = axis_candidates[left : right + 1]
                    if subset[-1][2] - subset[0][2] > LIF_PAIR_MATCH_TOL_SEC:
                        break
                    score = (
                        len(subset),
                        sum(math.log1p(max(0.0, item[3])) for item in subset),
                        -sum(abs(ms_plot_sec_for_group - item[2]) for item in subset),
                    )
                    if best_score is None or score > best_score:
                        best_score = score
                        best_subset = subset
            keep_channels = {str(item[0]) for item in best_subset}
            for channel, *_rest in axis_candidates:
                if channel not in keep_channels:
                    same_axis_dropped_channels.append(str(channel))
                    members.pop(channel, None)
        covered_axes = {str(channel_time_axes[channel]) for channel in members}
        if not required_axes.issubset(covered_axes):
            continue
        ms_row = ms.iloc[int(ms_idx)]
        anchor_rows: list[dict[str, Any]] = []
        peak_ids: dict[str, str | None] = {channel: None for channel in anchors}
        raw_times: dict[str, float | None] = {channel: None for channel in anchors}
        plot_times: dict[str, float | None] = {channel: None for channel in anchors}
        axis_member_times: dict[str, list[float]] = {axis: [] for axis in required_axes}
        quality_score = 0.0
        conflict_count = len(same_axis_dropped_channels)
        for channel in anchors:
            match = members.get(channel)
            if match is None:
                continue
            row, residual, ambiguous = match
            axis = str(channel_time_axes[channel])
            plot_time_sec = float(row["time_sec"]) + float(axis_shifts_sec.get(axis, 0.0))
            peak_ids[channel] = str(row["peak_id"])
            raw_times[channel] = float(row["time_min"])
            plot_times[channel] = float(plot_time_sec / 60.0)
            axis_member_times[axis].append(plot_time_sec)
            quality_score += math.log1p(max(0.0, float(row.get("snr", 0.0) or 0.0)))
            conflict_count += int(ambiguous)
            anchor_rows.append(
                {
                    "channel": channel,
                    "time_axis": axis,
                    "peak_id": str(row["peak_id"]),
                    "raw_time_min": float(row["time_min"]),
                    "plot_time_min": float(plot_time_sec / 60.0),
                    "snr": clean_value(row.get("snr")),
                    "match_residual_sec": float(residual),
                }
            )
        axis_composite_sec = {
            axis: float(np.median(times))
            for axis, times in axis_member_times.items()
            if times
        }
        composite_sec = float(np.median(list(axis_composite_sec.values())))
        ms_plot_sec = float(ms_plot_times[int(ms_idx)])
        residual_sec = ms_plot_sec - composite_sec
        axis_span_sec = float(max(axis_composite_sec.values()) - min(axis_composite_sec.values())) if len(axis_composite_sec) > 1 else 0.0
        axis_coherent = axis_span_sec <= QC_AXIS_COHERENCE_TOL_SEC + QC_COMPONENT_SELECT_EPS
        axis_residuals_to_ms_sec = {
            axis: float(ms_plot_sec - axis_time_sec)
            for axis, axis_time_sec in axis_composite_sec.items()
        }
        max_abs_axis_to_ms_residual_sec = max(
            (abs(value) for value in axis_residuals_to_ms_sec.values()),
            default=0.0,
        )
        if not axis_coherent:
            conflict_count += 1
        missing_channels = [channel for channel in anchors if peak_ids[channel] is None]
        group: dict[str, Any] = {
            "rank": len(groups) + 1,
            "qc_group_version": 2,
            "matcher_version": QC_MATCHER_VERSION,
            "anchor_channels": anchors,
            "lif_anchors": anchor_rows,
            "lif_anchor_peak_ids": peak_ids,
            "lif_anchor_raw_times_min": raw_times,
            "lif_anchor_plot_times_min": plot_times,
            "axis_composite_times_min": {
                axis: float(value / 60.0) for axis, value in axis_composite_sec.items()
            },
            "axis_residuals_to_ms_sec": axis_residuals_to_ms_sec,
            "axis_span_sec": axis_span_sec,
            "axis_coherence_tolerance_sec": QC_AXIS_COHERENCE_TOL_SEC,
            "axis_coherent": axis_coherent,
            "max_abs_axis_to_ms_residual_sec": float(max_abs_axis_to_ms_residual_sec),
            "covered_time_axes": sorted(covered_axes),
            "required_time_axes": sorted(required_axes),
            "lif_anchor_count": len(anchor_rows),
            "complete_anchor_set": not missing_channels and axis_coherent,
            "missing_lif_channels": missing_channels,
            "ms_event_id": str(ms_row["event_id"]),
            "ms_time_min": float(ms_row["time_min"]),
            "ms_plot_time_min": float(ms_plot_sec / 60.0),
            "lif_composite_plot_time_min": float(composite_sec / 60.0),
            "composite_to_ms_residual_sec": float(residual_sec),
            "abs_composite_to_ms_residual_sec": abs(float(residual_sec)),
            "lif_anchor_quality_score": float(quality_score),
            "lif_pair_quality_score": float(quality_score),
            "lif_pair_residual_sec": axis_span_sec,
            "conflict_count": int(conflict_count),
            "same_axis_conflict_count": len(same_axis_dropped_channels),
            "same_axis_dropped_channels": same_axis_dropped_channels,
            "selection_reason": (
                "ms_centered_axis_complete_anchor_set"
                if axis_coherent
                else "ms_centered_axis_incoherent_anchor_set"
            ),
            "match_tolerance_sec": float(tolerance_sec),
            "component_pair_count": 1,
            "component_ms_count": 1,
            "alternative_ms_event_ids": [],
            "skipped_pair_ids": [],
            "skipped_ms_event_ids": [],
        }
        for channel in ["G1", "G2", "R1", "R2"]:
            key = channel.lower()
            group[f"{key}_peak_id"] = peak_ids.get(channel)
            group[f"{key}_raw_time_min"] = raw_times.get(channel)
            group[f"{key}_plot_time_min"] = plot_times.get(channel)
        present_channels = [channel for channel in anchors if peak_ids[channel] is not None]
        if present_channels:
            first = present_channels[0]
            group.update(
                {
                    "anchor_a_channel": first,
                    "anchor_a_peak_id": peak_ids[first],
                    "anchor_a_raw_time_min": raw_times[first],
                    "anchor_a_plot_time_min": plot_times[first],
                }
            )
        if len(present_channels) > 1:
            second = present_channels[1]
            group.update(
                {
                    "anchor_b_channel": second,
                    "anchor_b_peak_id": peak_ids[second],
                    "anchor_b_raw_time_min": raw_times[second],
                    "anchor_b_plot_time_min": plot_times[second],
                }
            )
        groups.append(group)
    return groups


def build_segmented_calibration_groups(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    calibration_protocol: dict[str, Any],
    channel_time_axes: dict[str, str],
    axis_shifts_sec: dict[str, float],
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    for segment in calibration_protocol["segments"]:
        segment_groups = multi_anchor_groups_for_range(
            lif_peaks,
            ms_events,
            anchor_channels=list(segment["reference_channels"]),
            channel_time_axes=channel_time_axes,
            axis_shifts_sec=axis_shifts_sec,
            context_start_min=float(segment["start_min"]),
            context_end_min=float(segment["end_min"]),
            minimum_raw_time_min=float(segment["start_min"]),
            ms_shift_sec=0.0,
            tolerance_sec=QC_GROUP_MATCH_TOL_SEC,
        )
        for group in segment_groups:
            group.update(
                {
                    "calibration_segment_id": str(segment["segment_id"]),
                    "calibration_segment_order": int(segment["order"]),
                    "calibration_segment_start_min": float(segment["start_min"]),
                    "calibration_segment_end_min": float(segment["end_min"]),
                    "calibration_reference_mode": str(segment["reference_mode"]),
                    "calibration_population_label": str(segment.get("population_label") or ""),
                }
            )
            groups.append(group)
    groups.sort(
        key=lambda row: (
            int(row["calibration_segment_order"]),
            float(row["ms_time_min"]),
            str(row["ms_event_id"]),
        )
    )
    for rank, group in enumerate(groups, start=1):
        group["rank"] = rank
    return {
        "anchor_channels": list(calibration_protocol["reference_channels"]),
        "matcher_version": SEGMENTED_CALIBRATION_MATCHER_VERSION,
        "calibration_protocol_hash": calibration_protocol.get("protocol_hash"),
        "lif_r1_minus_g2_offset_sec": None,
        "lif_anchor_b_minus_anchor_a_offset_sec": None,
        "lif_pair_match_tolerance_sec": LIF_PAIR_MATCH_TOL_SEC,
        "match_tolerance_sec": QC_GROUP_MATCH_TOL_SEC,
        "segments": copy.deepcopy(calibration_protocol["segments"]),
        "groups": groups,
    }


def estimate_lif_pair_offset(g2: pd.DataFrame, r1: pd.DataFrame) -> float:
    offsets = []
    r_times = r1["time_sec"].to_numpy(float)
    for g_time in g2["time_sec"].to_numpy(float):
        close = r_times[
            (r_times >= g_time - LIF_PAIR_OFFSET_MAX_ABS_SEC)
            & (r_times <= g_time + LIF_PAIR_OFFSET_MAX_ABS_SEC)
        ]
        offsets.extend((close - g_time).tolist())
    if not offsets:
        return 0.0
    bins = np.arange(
        -LIF_PAIR_OFFSET_MAX_ABS_SEC,
        LIF_PAIR_OFFSET_MAX_ABS_SEC + LIF_PAIR_OFFSET_BIN_SEC,
        LIF_PAIR_OFFSET_BIN_SEC,
    )
    hist, edges = np.histogram(np.asarray(offsets, dtype=float), bins=bins)
    best = int(np.argmax(hist))
    return float((edges[best] + edges[best + 1]) / 2.0)


def lif_pair_quality(g_row: pd.Series, r_row: pd.Series) -> float:
    g_snr = max(0.0, float(g_row.get("snr", 0.0) or 0.0))
    r_snr = max(0.0, float(r_row.get("snr", 0.0) or 0.0))
    return float(math.log1p(g_snr) + math.log1p(r_snr))


def ms_qc_support_score(ms_row: pd.Series) -> float:
    support = max(0.0, float(ms_row.get("qc_782_apex", 0.0) or 0.0))
    return float(math.log1p(support))


def component_group_matches(
    pair_rows: list[tuple[pd.Series, pd.Series, float, float, float, float, float]],
    ms: pd.DataFrame,
    *,
    tolerance_sec: float = QC_GROUP_MATCH_TOL_SEC,
) -> list[dict[str, Any]]:
    """Match dense QC components without letting weak near-neighbors steal anchors."""
    tolerance = float(tolerance_sec)
    ms_times = ms["time_sec"].to_numpy(float)
    pair_to_ms: dict[int, set[int]] = {}
    ms_to_pair: dict[int, set[int]] = {}
    for pair_idx, row in enumerate(pair_rows):
        composite = float(row[4])
        left = int(np.searchsorted(ms_times, composite - tolerance, side="left"))
        right = int(np.searchsorted(ms_times, composite + tolerance, side="right"))
        for ms_idx in range(left, right):
            residual = float(ms_times[ms_idx] - composite)
            if abs(residual) <= tolerance + QC_COMPONENT_SELECT_EPS:
                pair_to_ms.setdefault(pair_idx, set()).add(ms_idx)
                ms_to_pair.setdefault(ms_idx, set()).add(pair_idx)

    visited_pairs: set[int] = set()
    visited_ms: set[int] = set()
    out: list[dict[str, Any]] = []

    for start_pair in sorted(pair_to_ms):
        if start_pair in visited_pairs:
            continue
        component_pairs: set[int] = set()
        component_ms: set[int] = set()
        queue_pairs = [start_pair]
        queue_ms: list[int] = []
        while queue_pairs or queue_ms:
            while queue_pairs:
                pair_idx = queue_pairs.pop()
                if pair_idx in component_pairs:
                    continue
                component_pairs.add(pair_idx)
                visited_pairs.add(pair_idx)
                for ms_idx in pair_to_ms.get(pair_idx, set()):
                    if ms_idx not in component_ms:
                        queue_ms.append(ms_idx)
            while queue_ms:
                ms_idx = queue_ms.pop()
                if ms_idx in component_ms:
                    continue
                component_ms.add(ms_idx)
                visited_ms.add(ms_idx)
                for pair_idx in ms_to_pair.get(ms_idx, set()):
                    if pair_idx not in component_pairs:
                        queue_pairs.append(pair_idx)

        if not component_pairs or not component_ms:
            continue

        component_pair_list = sorted(component_pairs)
        component_ms_list = sorted(component_ms)
        selected_count = min(len(component_pair_list), len(component_ms_list))
        if len(component_pair_list) > len(component_ms_list):
            selected_pairs = sorted(
                sorted(component_pair_list, key=lambda idx: (-float(pair_rows[idx][6]), float(pair_rows[idx][4]), idx))[
                    : selected_count
                ]
            )
        else:
            selected_pairs = component_pair_list[:selected_count]
        selected_ms_indices = component_ms_list[:selected_count]
        selection_reason = "chronological_dense_component"
        component_ambiguous = (
            len(component_pair_list) > 1 or len(component_ms_list) > 1
        )

        local_matches = []
        for pair_local, ms_local in enumerate(range(selected_count)):
            pair_idx = selected_pairs[pair_local]
            ms_idx = selected_ms_indices[ms_local]
            residual = float(ms_times[ms_idx] - float(pair_rows[pair_idx][4]))
            if abs(residual) <= tolerance + QC_COMPONENT_SELECT_EPS:
                local_matches.append((pair_local, ms_local, residual))

        matched_pairs = {selected_pairs[pair_local] for pair_local, _, _ in local_matches}
        matched_ms = {selected_ms_indices[ms_local] for _, ms_local, _ in local_matches}
        skipped_pair_ids = [
            str(pair_rows[idx][0]["peak_id"]) + "+" + str(pair_rows[idx][1]["peak_id"])
            for idx in component_pair_list
            if idx not in matched_pairs
        ]
        skipped_ms_ids = [
            str(ms.iloc[int(idx)]["event_id"])
            for idx in component_ms_list
            if idx not in matched_ms
        ]
        for pair_local, ms_local, residual in local_matches:
            pair_idx = selected_pairs[int(pair_local)]
            ms_idx = selected_ms_indices[int(ms_local)]
            alternative_ms_event_ids = [
                str(ms.iloc[int(candidate_ms_idx)]["event_id"])
                for candidate_ms_idx in sorted(pair_to_ms.get(pair_idx, set()))
                if int(candidate_ms_idx) != ms_idx
            ]
            out.append(
                {
                    "pair_idx": pair_idx,
                    "ms_idx": ms_idx,
                    "residual_sec": float(residual),
                    "component_pair_count": len(component_pair_list),
                    "component_ms_count": len(component_ms_list),
                    "component_ambiguous": component_ambiguous,
                    "selection_reason": selection_reason,
                    "alternative_ms_event_ids": alternative_ms_event_ids,
                    "skipped_pair_ids": skipped_pair_ids,
                    "skipped_ms_event_ids": skipped_ms_ids,
                }
            )

    out.sort(key=lambda item: float(pair_rows[int(item["pair_idx"])][4]))
    return out


def build_qc_alignment_groups(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    green_shift_sec: float = 0.0,
    red_shift_sec: float = 0.0,
    qc_calibration_end_min: float = QC_SHIFT_WINDOW_MIN,
    *,
    axis_shifts_sec: dict[str, float] | None = None,
    channel_time_axes: dict[str, str] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    qc_end = float(qc_calibration_end_min)
    anchors = [str(ch).strip().upper() for ch in (qc_anchor_channels or ["G2", "R1"])]
    if len(anchors) != len(set(anchors)) or not MIN_QC_ANCHOR_CHANNELS <= len(anchors) <= MAX_QC_ANCHOR_CHANNELS:
        raise BadRequest(f"QC anchor 必须包含 {MIN_QC_ANCHOR_CHANNELS}-{MAX_QC_ANCHOR_CHANNELS} 个不同 LIF 通道")
    channel_time_axes = channel_time_axes or {
        "G2": "green_axis",
        "G1": "green_axis",
        "R1": "red_axis",
        "R2": "red_axis",
    }
    axis_shifts_sec = axis_shifts_sec or {
        "green_axis": float(green_shift_sec),
        "red_axis": float(red_shift_sec),
    }
    if not is_legacy_qc_anchor_pair(anchors):
        groups = multi_anchor_groups_for_range(
            lif_peaks,
            ms_events,
            anchor_channels=anchors,
            channel_time_axes=channel_time_axes,
            axis_shifts_sec=axis_shifts_sec,
            context_start_min=0.0,
            context_end_min=qc_end,
            minimum_raw_time_min=0.0,
            ms_shift_sec=0.0,
            tolerance_sec=QC_GROUP_MATCH_TOL_SEC,
        )
        return {
            "anchor_channels": anchors,
            "matcher_version": QC_MATCHER_VERSION,
            "lif_r1_minus_g2_offset_sec": None,
            "lif_anchor_b_minus_anchor_a_offset_sec": None,
            "lif_pair_match_tolerance_sec": LIF_PAIR_MATCH_TOL_SEC,
            "match_tolerance_sec": QC_GROUP_MATCH_TOL_SEC,
            "groups": groups,
        }

    def shift_for_channel(channel: str) -> float:
        axis = str(channel_time_axes.get(channel, default_time_axis_for_channel(channel)))
        return float(axis_shifts_sec.get(axis, 0.0))

    anchor_a_channel, anchor_b_channel = anchors
    anchor_a = lif_peaks[
        lif_peaks["channel"].eq(anchor_a_channel)
        & lif_peaks["time_min"].between(0.0, qc_end, inclusive="both")
    ].sort_values("time_sec").reset_index(drop=True)
    anchor_b = lif_peaks[
        lif_peaks["channel"].eq(anchor_b_channel)
        & lif_peaks["time_min"].between(0.0, qc_end, inclusive="both")
    ].sort_values("time_sec").reset_index(drop=True)
    ms_mask = ms_events["time_min"].between(0.0, qc_end, inclusive="both")
    if "event_strategy" in ms_events.columns:
        ms_mask &= ms_events["event_strategy"].eq("pc34_primary")
    if "primary_signal_col" in ms_events.columns:
        ms_mask &= ms_events["primary_signal_col"].eq("pc34_760_max_intensity")
    ms = ms_events[ms_mask].sort_values("time_sec").reset_index(drop=True)

    if anchor_a.empty or anchor_b.empty or ms.empty:
        return {
            "anchor_channels": anchors,
            "lif_r1_minus_g2_offset_sec": None,
            "lif_anchor_b_minus_anchor_a_offset_sec": None,
            "match_tolerance_sec": QC_GROUP_MATCH_TOL_SEC,
            "groups": [],
        }

    pair_offset_sec = estimate_lif_pair_offset(anchor_a, anchor_b)
    anchor_a_times = anchor_a["time_sec"].to_numpy(float)
    anchor_b_times = anchor_b["time_sec"].to_numpy(float)
    lif_pairs = greedy_time_matches(anchor_a_times + pair_offset_sec, anchor_b_times, 0.0, LIF_PAIR_MATCH_TOL_SEC)

    composite_times_sec = []
    pair_rows = []
    for anchor_a_idx, anchor_b_idx, lif_pair_residual in lif_pairs:
        anchor_a_row = anchor_a.iloc[int(anchor_a_idx)]
        anchor_b_row = anchor_b.iloc[int(anchor_b_idx)]
        anchor_a_plot = float(anchor_a_row["time_sec"] + shift_for_channel(anchor_a_channel))
        anchor_b_plot = float(anchor_b_row["time_sec"] + shift_for_channel(anchor_b_channel))
        composite = float((anchor_a_plot + anchor_b_plot) / 2.0)
        composite_times_sec.append(composite)
        pair_rows.append((anchor_a_row, anchor_b_row, anchor_a_plot, anchor_b_plot, composite, lif_pair_residual, lif_pair_quality(anchor_a_row, anchor_b_row)))

    if not composite_times_sec:
        return {
            "anchor_channels": anchors,
            "lif_r1_minus_g2_offset_sec": pair_offset_sec,
            "lif_anchor_b_minus_anchor_a_offset_sec": pair_offset_sec,
            "match_tolerance_sec": QC_GROUP_MATCH_TOL_SEC,
            "groups": [],
        }

    group_matches = component_group_matches(pair_rows, ms)
    groups = []
    for rank, match in enumerate(group_matches, start=1):
        pair_idx = int(match["pair_idx"])
        ms_idx = int(match["ms_idx"])
        residual = float(match["residual_sec"])
        anchor_a_row, anchor_b_row, anchor_a_plot, anchor_b_plot, composite, lif_pair_residual, quality = pair_rows[pair_idx]
        m_row = ms.iloc[ms_idx]
        axis_span_sec = abs(float(anchor_b_plot - anchor_a_plot))
        axis_coherent = axis_span_sec <= QC_AXIS_COHERENCE_TOL_SEC + QC_COMPONENT_SELECT_EPS
        max_abs_axis_to_ms_residual_sec = max(
            abs(float(m_row["time_sec"]) - float(anchor_a_plot)),
            abs(float(m_row["time_sec"]) - float(anchor_b_plot)),
        )
        groups.append(
            {
                "rank": rank,
                "anchor_a_channel": anchor_a_channel,
                "anchor_b_channel": anchor_b_channel,
                "anchor_a_peak_id": anchor_a_row["peak_id"],
                "anchor_b_peak_id": anchor_b_row["peak_id"],
                "g2_peak_id": anchor_a_row["peak_id"],
                "r1_peak_id": anchor_b_row["peak_id"],
                "ms_event_id": m_row["event_id"],
                "anchor_a_raw_time_min": float(anchor_a_row["time_min"]),
                "anchor_b_raw_time_min": float(anchor_b_row["time_min"]),
                "g2_raw_time_min": float(anchor_a_row["time_min"]),
                "r1_raw_time_min": float(anchor_b_row["time_min"]),
                "ms_time_min": float(m_row["time_min"]),
                "anchor_a_plot_time_min": float(anchor_a_plot / 60.0),
                "anchor_b_plot_time_min": float(anchor_b_plot / 60.0),
                "g2_plot_time_min": float(anchor_a_plot / 60.0),
                "r1_plot_time_min": float(anchor_b_plot / 60.0),
                "ms_plot_time_min": float(m_row["time_min"]),
                "lif_r1_minus_g2_offset_sec": float(pair_offset_sec),
                "lif_anchor_b_minus_anchor_a_offset_sec": float(pair_offset_sec),
                "lif_pair_residual_sec": float(lif_pair_residual),
                "axis_span_sec": axis_span_sec,
                "axis_coherence_tolerance_sec": QC_AXIS_COHERENCE_TOL_SEC,
                "axis_coherent": axis_coherent,
                "max_abs_axis_to_ms_residual_sec": max_abs_axis_to_ms_residual_sec,
                "complete_anchor_set": axis_coherent,
                "conflict_count": (
                    (0 if axis_coherent else 1)
                    + (1 if bool(match.get("component_ambiguous")) else 0)
                ),
                "lif_pair_quality_score": float(quality),
                "composite_to_ms_residual_sec": float(residual),
                "abs_composite_to_ms_residual_sec": abs(float(residual)),
                "component_pair_count": int(match["component_pair_count"]),
                "component_ms_count": int(match["component_ms_count"]),
                "component_ambiguous": bool(match.get("component_ambiguous")),
                "selection_reason": match["selection_reason"],
                "alternative_ms_event_ids": match["alternative_ms_event_ids"],
                "skipped_pair_ids": match["skipped_pair_ids"],
                "skipped_ms_event_ids": match["skipped_ms_event_ids"],
                "match_tolerance_sec": QC_GROUP_MATCH_TOL_SEC,
            }
        )
    return {
        "anchor_channels": anchors,
        "lif_r1_minus_g2_offset_sec": pair_offset_sec,
        "lif_anchor_b_minus_anchor_a_offset_sec": pair_offset_sec,
        "lif_pair_match_tolerance_sec": LIF_PAIR_MATCH_TOL_SEC,
        "match_tolerance_sec": QC_GROUP_MATCH_TOL_SEC,
        "groups": groups,
    }


def primary_pc34_events(ms_events: pd.DataFrame) -> pd.DataFrame:
    mask = pd.Series(True, index=ms_events.index)
    if "event_strategy" in ms_events.columns:
        mask &= ms_events["event_strategy"].eq("pc34_primary")
    if "primary_signal_col" in ms_events.columns:
        mask &= ms_events["primary_signal_col"].eq("pc34_760_max_intensity")
    return ms_events[mask].copy()


def is_primary_pc34_event(row: pd.Series) -> bool:
    if "event_strategy" in row.index and str(row.get("event_strategy")) != "pc34_primary":
        return False
    if "primary_signal_col" in row.index and str(row.get("primary_signal_col")) != "pc34_760_max_intensity":
        return False
    return True


def qc_triplets_for_range(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    context_start_min: float,
    context_end_min: float,
    qc_calibration_end_min: float,
    green_shift_sec: float,
    red_shift_sec: float,
    ms_shift_sec: float,
    pair_offset_sec: float,
    tolerance_sec: float,
    axis_shifts_sec: dict[str, float] | None = None,
    channel_time_axes: dict[str, str] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    qc_end = float(qc_calibration_end_min)
    anchors = [str(ch).strip().upper() for ch in (qc_anchor_channels or ["G2", "R1"])]
    channel_time_axes = channel_time_axes or {"G2": "green_axis", "G1": "green_axis", "R1": "red_axis", "R2": "red_axis"}
    axis_shifts_sec = axis_shifts_sec or {"green_axis": green_shift_sec, "red_axis": red_shift_sec}
    if not is_legacy_qc_anchor_pair(anchors):
        return multi_anchor_groups_for_range(
            lif_peaks,
            ms_events,
            anchor_channels=anchors,
            channel_time_axes=channel_time_axes,
            axis_shifts_sec=axis_shifts_sec,
            context_start_min=float(context_start_min),
            context_end_min=float(context_end_min),
            minimum_raw_time_min=qc_end,
            ms_shift_sec=float(ms_shift_sec),
            tolerance_sec=float(tolerance_sec),
        )

    def shift_for(channel: str) -> float:
        axis = channel_time_axes.get(channel, default_time_axis_for_channel(channel))
        return float(axis_shifts_sec.get(axis, 0.0))

    anchor_a_channel, anchor_b_channel = anchors
    anchor_a = lif_peaks[lif_peaks["channel"].eq(anchor_a_channel)].copy()
    anchor_b = lif_peaks[lif_peaks["channel"].eq(anchor_b_channel)].copy()
    anchor_a["plot_time_min"] = anchor_a["time_min"] + shift_for(anchor_a_channel) / 60.0
    anchor_b["plot_time_min"] = anchor_b["time_min"] + shift_for(anchor_b_channel) / 60.0
    anchor_a = anchor_a[
        anchor_a["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")
        & (anchor_a["time_min"] >= qc_end)
    ].sort_values("time_sec").reset_index(drop=True)
    anchor_b = anchor_b[
        anchor_b["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")
        & (anchor_b["time_min"] >= qc_end)
    ].sort_values("time_sec").reset_index(drop=True)
    ms = primary_pc34_events(ms_events)
    ms = ms[
        ms["time_min"].between(context_start_min, context_end_min, inclusive="both")
        & (ms["time_min"] >= qc_end)
    ].sort_values("time_sec").reset_index(drop=True)
    ms["plot_time_sec"] = ms["time_sec"].astype(float) + float(ms_shift_sec)
    ms["plot_time_min"] = ms["plot_time_sec"] / 60.0
    if anchor_a.empty or anchor_b.empty or ms.empty:
        return []

    lif_pairs = greedy_time_matches(
        anchor_a["time_sec"].to_numpy(float) + pair_offset_sec,
        anchor_b["time_sec"].to_numpy(float),
        0.0,
        LIF_PAIR_MATCH_TOL_SEC,
    )
    pair_rows: list[tuple[pd.Series, pd.Series, float, float, float, float, float]] = []
    for anchor_a_idx, anchor_b_idx, lif_pair_residual in lif_pairs:
        anchor_a_row = anchor_a.iloc[int(anchor_a_idx)]
        anchor_b_row = anchor_b.iloc[int(anchor_b_idx)]
        anchor_a_plot = float(anchor_a_row["time_sec"] + shift_for(anchor_a_channel))
        anchor_b_plot = float(anchor_b_row["time_sec"] + shift_for(anchor_b_channel))
        composite = float((anchor_a_plot + anchor_b_plot) / 2.0)
        pair_rows.append((anchor_a_row, anchor_b_row, anchor_a_plot, anchor_b_plot, composite, lif_pair_residual, lif_pair_quality(anchor_a_row, anchor_b_row)))
    if not pair_rows:
        return []

    match_ms = ms.copy()
    match_ms["time_sec"] = match_ms["plot_time_sec"].astype(float)
    matches = component_group_matches(
        pair_rows,
        match_ms,
        tolerance_sec=float(tolerance_sec),
    )
    groups = []
    for rank, match in enumerate(matches, start=1):
        pair_idx = int(match["pair_idx"])
        ms_idx = int(match["ms_idx"])
        residual = float(match["residual_sec"])
        anchor_a_row, anchor_b_row, anchor_a_plot, anchor_b_plot, composite, lif_pair_residual, quality = pair_rows[int(pair_idx)]
        m_row = ms.iloc[int(ms_idx)]
        axis_span_sec = abs(float(anchor_b_plot - anchor_a_plot))
        axis_coherent = axis_span_sec <= QC_AXIS_COHERENCE_TOL_SEC + QC_COMPONENT_SELECT_EPS
        ms_plot_sec = float(m_row["plot_time_sec"])
        groups.append(
            {
                "rank": rank,
                "anchor_a_channel": anchor_a_channel,
                "anchor_b_channel": anchor_b_channel,
                "anchor_a_peak_id": str(anchor_a_row["peak_id"]),
                "anchor_b_peak_id": str(anchor_b_row["peak_id"]),
                "g2_peak_id": str(anchor_a_row["peak_id"]),
                "r1_peak_id": str(anchor_b_row["peak_id"]),
                "ms_event_id": str(m_row["event_id"]),
                "anchor_a_raw_time_min": float(anchor_a_row["time_min"]),
                "anchor_b_raw_time_min": float(anchor_b_row["time_min"]),
                "g2_raw_time_min": float(anchor_a_row["time_min"]),
                "r1_raw_time_min": float(anchor_b_row["time_min"]),
                "ms_time_min": float(m_row["time_min"]),
                "anchor_a_plot_time_min": float(anchor_a_plot / 60.0),
                "anchor_b_plot_time_min": float(anchor_b_plot / 60.0),
                "g2_plot_time_min": float(anchor_a_plot / 60.0),
                "r1_plot_time_min": float(anchor_b_plot / 60.0),
                "ms_plot_time_min": float(m_row["plot_time_min"]),
                "lif_r1_minus_g2_offset_sec": float(pair_offset_sec),
                "lif_pair_residual_sec": float(lif_pair_residual),
                "axis_span_sec": axis_span_sec,
                "axis_coherence_tolerance_sec": QC_AXIS_COHERENCE_TOL_SEC,
                "axis_coherent": axis_coherent,
                "max_abs_axis_to_ms_residual_sec": max(
                    abs(ms_plot_sec - float(anchor_a_plot)),
                    abs(ms_plot_sec - float(anchor_b_plot)),
                ),
                "complete_anchor_set": axis_coherent,
                "conflict_count": (
                    (0 if axis_coherent else 1)
                    + (1 if bool(match.get("component_ambiguous")) else 0)
                ),
                "lif_pair_quality_score": float(quality),
                "composite_to_ms_residual_sec": float(residual),
                "abs_composite_to_ms_residual_sec": abs(float(residual)),
                "component_pair_count": int(match["component_pair_count"]),
                "component_ms_count": int(match["component_ms_count"]),
                "component_ambiguous": bool(match.get("component_ambiguous")),
                "alternative_ms_event_ids": list(match.get("alternative_ms_event_ids") or []),
                "skipped_pair_ids": list(match.get("skipped_pair_ids") or []),
                "skipped_ms_event_ids": list(match.get("skipped_ms_event_ids") or []),
                "match_tolerance_sec": float(tolerance_sec),
                "selection_reason": str(match.get("selection_reason") or "post_qc_monotone_component"),
            }
        )
    return groups


def high_confidence_cell_pairs(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    channel: str,
    context_start_min: float,
    context_end_min: float,
    shift_sec: float,
    ms_shift_sec: float,
    annotation_start_min: float,
    excluded_ms_event_ids: set[str] | None = None,
    label: str = "cell",
) -> list[dict[str, Any]]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    excluded_ms_event_ids = excluded_ms_event_ids or set()
    lif = lif_peaks[lif_peaks["channel"].eq(channel)].copy()
    lif["plot_time_min"] = lif["time_min"] + shift_sec / 60.0
    lif = lif[
        lif["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")
        & (lif["time_min"] >= annotation_start_min)
        & (lif["snr"].fillna(0.0) >= CELL_MIN_LIF_SNR)
        & (lif["nearest_gap_sec"].fillna(9999.0) >= CELL_MIN_LIF_GAP_SEC)
        & (~false_if_missing(lif["close_peak_risk"]))
        & (~false_if_missing(lif["merge_risk"]))
    ].sort_values("time_sec").reset_index(drop=True)
    ms = primary_pc34_events(ms_events)
    if excluded_ms_event_ids:
        ms = ms[~ms["event_id"].astype(str).isin(excluded_ms_event_ids)].copy()
    ms["plot_time_min"] = ms["time_min"] + float(ms_shift_sec) / 60.0
    ms = ms[
        ms["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")
        & (ms["time_min"] >= annotation_start_min)
        & (ms["pc34_760_apex"].fillna(0.0) >= CELL_MIN_PC34_APEX)
        & (ms["nearest_event_gap_sec"].fillna(9999.0) >= CELL_MIN_MS_GAP_SEC)
        & (~ms["collision_risk_high"].fillna(False).astype(bool))
        & (~ms["low_quality_scan_window"].fillna(False).astype(bool))
    ].sort_values("time_sec").reset_index(drop=True)
    if lif.empty or ms.empty:
        return []

    lif_shifted = lif["time_sec"].to_numpy(float) + shift_sec
    ms_times = ms["time_sec"].to_numpy(float) + float(ms_shift_sec)
    matches = greedy_time_matches(lif["time_sec"].to_numpy(float), ms_times, shift_sec, CELL_CANDIDATE_TOL_SEC)
    rows = []
    for rank, (lif_idx, ms_idx, residual) in enumerate(matches, start=1):
        lif_time = lif_shifted[int(lif_idx)]
        ms_time = ms_times[int(ms_idx)]
        lif_competitors = int(np.sum(np.abs(lif_shifted - ms_time) <= CELL_CANDIDATE_TOL_SEC))
        ms_competitors = int(np.sum(np.abs(ms_times - lif_time) <= CELL_CANDIDATE_TOL_SEC))
        if lif_competitors != 1 or ms_competitors != 1:
            continue
        lif_row = lif.iloc[int(lif_idx)]
        ms_row = ms.iloc[int(ms_idx)]
        rows.append(
            {
                "rank": rank,
                "lif_channel": channel,
                "lif_peak_id": str(lif_row["peak_id"]),
                "ms_event_id": str(ms_row["event_id"]),
                "scan_id": clean_value(ms_row.get("scan_id")),
                "label": str(label or "cell"),
                "lif_raw_time_min": float(lif_row["time_min"]),
                "lif_plot_time_min": float((float(lif_row["time_sec"]) + shift_sec) / 60.0),
                "ms_time_min": float(ms_row["time_min"]),
                "ms_plot_time_min": float((float(ms_row["time_sec"]) + float(ms_shift_sec)) / 60.0),
                "residual_sec": float(residual),
                "abs_residual_sec": abs(float(residual)),
                "lif_snr": clean_value(lif_row.get("snr")),
                "lif_nearest_gap_sec": clean_value(lif_row.get("nearest_gap_sec")),
                "ms_pc34_760_apex": clean_value(ms_row.get("pc34_760_apex")),
                "ms_nearest_event_gap_sec": clean_value(ms_row.get("nearest_event_gap_sec")),
                "confidence_mode": "high_confidence_cell_unique_shift_match",
                "selection_reason": "strict_snr_gap_intensity_unique_nearest",
            }
        )
    return rows


def local_delta_evidence_pairs(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    annotation_start_min: float,
    seed_window_min: float,
    green_shift_sec: float,
    red_shift_sec: float,
    ms_delta_sec: float,
    channel_shifts_sec: dict[str, float] | None = None,
) -> dict[str, Any]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    seed_end_min = annotation_start_min + seed_window_min
    lif_parts = []
    if channel_shifts_sec is None:
        channel_shifts_sec = {"G2": green_shift_sec, "R1": red_shift_sec, "R2": red_shift_sec}
    for channel, shift_sec in channel_shifts_sec.items():
        sub = lif_peaks[lif_peaks["channel"].eq(channel)].copy()
        sub["lif_plot_time_sec"] = sub["time_sec"].astype(float) + float(shift_sec)
        sub = sub[
            sub["time_min"].between(annotation_start_min, seed_end_min, inclusive="both")
            & (sub["snr"].fillna(0.0) >= CELL_MIN_LIF_SNR)
            & (sub["nearest_gap_sec"].fillna(9999.0) >= CELL_MIN_LIF_GAP_SEC)
            & (~false_if_missing(sub["close_peak_risk"]))
            & (~false_if_missing(sub["merge_risk"]))
        ].copy()
        lif_parts.append(sub)
    lif = pd.concat(lif_parts, ignore_index=True) if lif_parts else pd.DataFrame()
    ms = primary_pc34_events(ms_events)
    ms = ms[
        ms["time_min"].between(annotation_start_min, seed_end_min, inclusive="both")
        & (ms["pc34_760_apex"].fillna(0.0) >= CELL_MIN_PC34_APEX)
        & (ms["nearest_event_gap_sec"].fillna(9999.0) >= CELL_MIN_MS_GAP_SEC)
        & (~false_if_missing(ms["collision_risk_high"]))
        & (~false_if_missing(ms["low_quality_scan_window"]))
    ].sort_values("time_sec").reset_index(drop=True)
    if lif.empty or ms.empty:
        return {
            "delta_sec": float(ms_delta_sec),
            "evidence": [],
            "evidence_count": 0,
            "unique_match_count": 0,
            "conflict_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
        }
    lif = lif.sort_values("lif_plot_time_sec").reset_index(drop=True)
    lif_times = lif["lif_plot_time_sec"].to_numpy(float)
    ms_times = ms["time_sec"].to_numpy(float) + float(ms_delta_sec)
    matches = greedy_time_matches(lif_times, ms_times, 0.0, LOCAL_DELTA_MATCH_TOL_SEC)
    evidence = []
    conflict_count = 0
    for rank, (lif_idx, ms_idx, residual) in enumerate(matches, start=1):
        lif_time = lif_times[int(lif_idx)]
        ms_time = ms_times[int(ms_idx)]
        lif_competitors = int(np.sum(np.abs(lif_times - ms_time) <= LOCAL_DELTA_MATCH_TOL_SEC))
        ms_competitors = int(np.sum(np.abs(ms_times - lif_time) <= LOCAL_DELTA_MATCH_TOL_SEC))
        if lif_competitors != 1 or ms_competitors != 1:
            conflict_count += 1
            continue
        lif_row = lif.iloc[int(lif_idx)]
        ms_row = ms.iloc[int(ms_idx)]
        evidence.append(
            {
                "rank": rank,
                "candidate_type": "time_delta_preview_unlabeled",
                "source": "first_principles_unlabeled_peak_topology",
                "lif_channel": str(lif_row["channel"]),
                "lif_peak_id": str(lif_row["peak_id"]),
                "ms_event_id": str(ms_row["event_id"]),
                "lif_raw_time_min": float(lif_row["time_min"]),
                "lif_plot_time_min": float(lif_time / 60.0),
                "ms_time_min": float(ms_row["time_min"]),
                "ms_plot_time_min": float(ms_time / 60.0),
                "ms_local_delta_sec": float(ms_delta_sec),
                "residual_sec": float(residual),
                "abs_residual_sec": abs(float(residual)),
                "lif_snr": clean_value(lif_row.get("snr")),
                "lif_nearest_gap_sec": clean_value(lif_row.get("nearest_gap_sec")),
                "ms_pc34_760_apex": clean_value(ms_row.get("pc34_760_apex")),
                "ms_qc_782_apex": clean_value(ms_row.get("qc_782_apex")),
                "ms_nearest_event_gap_sec": clean_value(ms_row.get("nearest_event_gap_sec")),
            }
        )
    abs_values = [row["abs_residual_sec"] for row in evidence]
    return {
        "delta_sec": float(ms_delta_sec),
        "evidence": evidence,
        "evidence_count": len(evidence),
        "unique_match_count": len(evidence),
        "conflict_count": int(conflict_count),
        "median_abs_residual_sec": float(np.median(abs_values)) if abs_values else None,
        "p90_abs_residual_sec": float(np.quantile(abs_values, 0.90)) if abs_values else None,
    }


def local_delta_qc_anchor_set_evidence(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    annotation_start_min: float,
    seed_window_min: float,
    qc_calibration_end_min: float,
    ms_delta_sec: float,
    axis_shifts_sec: dict[str, float],
    channel_time_axes: dict[str, str],
    qc_anchor_channels: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    seed_start_min = float(annotation_start_min)
    seed_end_min = seed_start_min + float(seed_window_min)
    tolerance_sec = POST_QC_CANDIDATE_TOL_SEC
    guard_min = (abs(float(ms_delta_sec)) + tolerance_sec) / 60.0 + 0.01
    anchors = [str(channel).strip().upper() for channel in qc_anchor_channels]
    groups = multi_anchor_groups_for_range(
        lif_peaks,
        ms_events,
        anchor_channels=anchors,
        channel_time_axes=channel_time_axes,
        axis_shifts_sec=axis_shifts_sec,
        context_start_min=seed_start_min - guard_min,
        context_end_min=seed_end_min + guard_min,
        minimum_raw_time_min=float(qc_calibration_end_min),
        ms_shift_sec=float(ms_delta_sec),
        tolerance_sec=tolerance_sec,
    )
    evidence: list[dict[str, Any]] = []
    rejected_conflict_count = 0
    for group in groups:
        group_conflicts = int(group.get("conflict_count", 0) or 0)
        if group.get("axis_coherent") is False or group_conflicts > 0:
            rejected_conflict_count += max(1, group_conflicts)
            continue
        composite_plot_min = float(group["lif_composite_plot_time_min"])
        ms_plot_min = float(group["ms_plot_time_min"])
        if not (seed_start_min <= composite_plot_min <= seed_end_min):
            continue
        if not (seed_start_min <= ms_plot_min <= seed_end_min):
            continue
        evidence.append(
            {
                **group,
                "rank": len(evidence) + 1,
                "candidate_type": "time_delta_preview_qc_anchor_set",
                "source": "first_principles_qc_anchor_set_topology",
                "lif_channel": "+".join(anchors),
                "lif_plot_time_min": composite_plot_min,
                "ms_local_delta_sec": float(ms_delta_sec),
                "residual_sec": float(group["composite_to_ms_residual_sec"]),
                "abs_residual_sec": float(group["abs_composite_to_ms_residual_sec"]),
            }
        )
    abs_values = [float(row["abs_residual_sec"]) for row in evidence]
    return {
        "delta_sec": float(ms_delta_sec),
        "evidence": evidence,
        "evidence_count": len(evidence),
        "unique_match_count": len(evidence),
        "complete_anchor_set_count": sum(1 for row in evidence if bool(row.get("complete_anchor_set"))),
        "total_anchor_support": sum(int(row.get("lif_anchor_count", 0)) for row in evidence),
        "conflict_count": rejected_conflict_count + sum(int(row.get("conflict_count", 0)) for row in evidence),
        "median_abs_residual_sec": float(np.median(abs_values)) if abs_values else None,
        "p90_abs_residual_sec": float(np.quantile(abs_values, 0.90)) if abs_values else None,
        "method": "qc_anchor_set_seed_window_shift_preview",
        "match_tolerance_sec": tolerance_sec,
        "contains_cell_labels": False,
        "matcher_version": QC_MATCHER_VERSION,
    }


def local_delta_qc_pair_evidence(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    annotation_start_min: float,
    seed_window_min: float,
    qc_calibration_end_min: float,
    green_shift_sec: float,
    red_shift_sec: float,
    ms_delta_sec: float,
    pair_offset_sec: float,
    axis_shifts_sec: dict[str, float] | None = None,
    channel_time_axes: dict[str, str] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    seed_start_min = float(annotation_start_min)
    seed_end_min = seed_start_min + float(seed_window_min)
    tolerance_sec = POST_QC_CANDIDATE_TOL_SEC
    guard_min = (abs(float(ms_delta_sec)) + tolerance_sec + LIF_PAIR_MATCH_TOL_SEC) / 60.0 + 0.01
    groups = qc_triplets_for_range(
        lif_peaks,
        ms_events,
        context_start_min=seed_start_min - guard_min,
        context_end_min=seed_end_min + guard_min,
        qc_calibration_end_min=float(qc_calibration_end_min),
        green_shift_sec=float(green_shift_sec),
        red_shift_sec=float(red_shift_sec),
        ms_shift_sec=float(ms_delta_sec),
        pair_offset_sec=float(pair_offset_sec),
        tolerance_sec=tolerance_sec,
        axis_shifts_sec=axis_shifts_sec,
        channel_time_axes=channel_time_axes,
        qc_anchor_channels=qc_anchor_channels,
    )
    evidence: list[dict[str, Any]] = []
    rejected_conflict_count = 0
    for group in groups:
        group_conflicts = int(group.get("conflict_count", 0) or 0)
        if group.get("axis_coherent") is False or group_conflicts > 0:
            rejected_conflict_count += max(1, group_conflicts)
            continue
        anchor_a_plot_min = float(group["anchor_a_plot_time_min"])
        anchor_b_plot_min = float(group["anchor_b_plot_time_min"])
        composite_plot_min = (anchor_a_plot_min + anchor_b_plot_min) / 2.0
        ms_plot_min = float(group["ms_plot_time_min"])
        if not (seed_start_min <= composite_plot_min <= seed_end_min):
            continue
        if not (seed_start_min <= ms_plot_min <= seed_end_min):
            continue
        evidence.append(
            {
                **group,
                "rank": len(evidence) + 1,
                "candidate_type": "time_delta_preview_qc_pair",
                "source": "first_principles_qc_anchor_pair_topology",
                "lif_channel": f"{group['anchor_a_channel']}+{group['anchor_b_channel']}",
                "lif_plot_time_min": composite_plot_min,
                "ms_local_delta_sec": float(ms_delta_sec),
                "residual_sec": float(group["composite_to_ms_residual_sec"]),
                "abs_residual_sec": float(group["abs_composite_to_ms_residual_sec"]),
            }
        )
    abs_values = [row["abs_residual_sec"] for row in evidence]
    return {
        "delta_sec": float(ms_delta_sec),
        "evidence": evidence,
        "evidence_count": len(evidence),
        "unique_match_count": len(evidence),
        "conflict_count": rejected_conflict_count,
        "median_abs_residual_sec": float(np.median(abs_values)) if abs_values else None,
        "p90_abs_residual_sec": float(np.quantile(abs_values, 0.90)) if abs_values else None,
        "method": "qc_pair_seed_window_shift_preview",
        "match_tolerance_sec": tolerance_sec,
        "contains_cell_labels": False,
    }


def local_delta_preview_evidence(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    annotation_start_min: float,
    seed_window_min: float,
    green_shift_sec: float,
    red_shift_sec: float,
    ms_delta_sec: float,
    channel_shifts_sec: dict[str, float] | None = None,
    qc_calibration_end_min: float | None = None,
    pair_offset_sec: float | None = None,
    axis_shifts_sec: dict[str, float] | None = None,
    channel_time_axes: dict[str, str] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    anchors = [str(channel).strip().upper() for channel in (qc_anchor_channels or [])]
    if (
        not is_legacy_qc_anchor_pair(anchors)
        and qc_calibration_end_min is not None
        and axis_shifts_sec is not None
        and channel_time_axes is not None
    ):
        return local_delta_qc_anchor_set_evidence(
            lif_peaks,
            ms_events,
            annotation_start_min=annotation_start_min,
            seed_window_min=seed_window_min,
            qc_calibration_end_min=float(qc_calibration_end_min),
            ms_delta_sec=ms_delta_sec,
            axis_shifts_sec=axis_shifts_sec,
            channel_time_axes=channel_time_axes,
            qc_anchor_channels=anchors,
        )
    if qc_calibration_end_min is not None and pair_offset_sec is not None:
        pair_preview = local_delta_qc_pair_evidence(
            lif_peaks,
            ms_events,
            annotation_start_min=annotation_start_min,
            seed_window_min=seed_window_min,
            qc_calibration_end_min=float(qc_calibration_end_min),
            green_shift_sec=green_shift_sec,
            red_shift_sec=red_shift_sec,
            ms_delta_sec=ms_delta_sec,
            pair_offset_sec=float(pair_offset_sec),
            axis_shifts_sec=axis_shifts_sec,
            channel_time_axes=channel_time_axes,
            qc_anchor_channels=qc_anchor_channels,
        )
        if int(pair_preview["evidence_count"]) > 0:
            return pair_preview
    fallback = local_delta_evidence_pairs(
        lif_peaks,
        ms_events,
        annotation_start_min=annotation_start_min,
        seed_window_min=seed_window_min,
        green_shift_sec=green_shift_sec,
        red_shift_sec=red_shift_sec,
        ms_delta_sec=ms_delta_sec,
        channel_shifts_sec=channel_shifts_sec,
    )
    return {
        **fallback,
        "method": "unlabeled_seed_window_shift_preview",
        "match_tolerance_sec": LOCAL_DELTA_MATCH_TOL_SEC,
        "contains_cell_labels": False,
    }


def estimate_local_delta_shift(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    annotation_start_min: float,
    seed_window_min: float,
    green_shift_sec: float,
    red_shift_sec: float,
    channel_shifts_sec: dict[str, float] | None = None,
    qc_calibration_end_min: float | None = None,
    pair_offset_sec: float | None = None,
    axis_shifts_sec: dict[str, float] | None = None,
    channel_time_axes: dict[str, str] | None = None,
    qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    lif_peaks = automatic_lif_peak_evidence(lif_peaks)
    anchors = [str(channel).strip().upper() for channel in (qc_anchor_channels or [])]
    if (
        not is_legacy_qc_anchor_pair(anchors)
        and qc_calibration_end_min is not None
        and axis_shifts_sec is not None
        and channel_time_axes is not None
    ):
        candidates: list[dict[str, Any]] = []
        for delta in np.arange(
            LOCAL_DELTA_SEARCH_MIN_SEC,
            LOCAL_DELTA_SEARCH_MAX_SEC + LOCAL_DELTA_SEARCH_STEP_SEC / 2.0,
            LOCAL_DELTA_SEARCH_STEP_SEC,
        ):
            candidate = local_delta_qc_anchor_set_evidence(
                lif_peaks,
                ms_events,
                annotation_start_min=annotation_start_min,
                seed_window_min=seed_window_min,
                qc_calibration_end_min=float(qc_calibration_end_min),
                ms_delta_sec=float(delta),
                axis_shifts_sec=axis_shifts_sec,
                channel_time_axes=channel_time_axes,
                qc_anchor_channels=anchors,
            )
            median_abs = candidate["median_abs_residual_sec"]
            p90_abs = candidate["p90_abs_residual_sec"]
            regularized_error = float(median_abs if median_abs is not None else 9999.0) + LOCAL_DELTA_ABS_PRIOR_WEIGHT * abs(float(delta))
            conflict_penalized_error = regularized_error + LOCAL_DELTA_CONFLICT_PENALTY_SEC * int(candidate["conflict_count"])
            score_key = (
                int(candidate["unique_match_count"]),
                int(candidate["complete_anchor_set_count"]),
                int(candidate["total_anchor_support"]),
                -conflict_penalized_error,
                -float(p90_abs if p90_abs is not None else 9999.0),
                -int(candidate["conflict_count"]),
                -abs(float(delta)),
            )
            candidate["score_key"] = score_key
            candidate["regularized_abs_error_sec"] = regularized_error
            candidate["conflict_penalized_abs_error_sec"] = conflict_penalized_error
            candidates.append(candidate)
        candidates.sort(key=lambda item: item["score_key"], reverse=True)
        best = candidates[0]
        separated = [
            item
            for item in candidates[1:]
            if abs(float(item["delta_sec"]) - float(best["delta_sec"])) >= max(1.0, POST_QC_CANDIDATE_TOL_SEC)
        ]
        runner_up = separated[0] if separated else None
        ambiguous = bool(
            runner_up
            and int(runner_up["unique_match_count"]) == int(best["unique_match_count"])
            and int(runner_up["complete_anchor_set_count"]) == int(best["complete_anchor_set_count"])
            and int(runner_up["total_anchor_support"]) == int(best["total_anchor_support"])
            and float(runner_up["conflict_penalized_abs_error_sec"]) <= float(best["conflict_penalized_abs_error_sec"]) + 0.25
        )
        enough_evidence = int(best["unique_match_count"]) >= 2
        best["method"] = "qc_anchor_set_seed_window_shift_grid_search"
        best["search_range_sec"] = [LOCAL_DELTA_SEARCH_MIN_SEC, LOCAL_DELTA_SEARCH_MAX_SEC]
        best["search_step_sec"] = LOCAL_DELTA_SEARCH_STEP_SEC
        best["match_tolerance_sec"] = POST_QC_CANDIDATE_TOL_SEC
        best["contains_cell_labels"] = False
        best["delta_abs_prior_weight"] = LOCAL_DELTA_ABS_PRIOR_WEIGHT
        best["conflict_penalty_sec"] = LOCAL_DELTA_CONFLICT_PENALTY_SEC
        best["recommendation_status"] = (
            "insufficient_evidence" if not enough_evidence else "ambiguous" if ambiguous else "recommended"
        )
        best["runner_up"] = (
            {
                "delta_sec": float(runner_up["delta_sec"]),
                "unique_match_count": int(runner_up["unique_match_count"]),
                "complete_anchor_set_count": int(runner_up["complete_anchor_set_count"]),
                "total_anchor_support": int(runner_up["total_anchor_support"]),
                "conflict_count": int(runner_up["conflict_count"]),
                "regularized_abs_error_sec": float(runner_up["regularized_abs_error_sec"]),
                "conflict_penalized_abs_error_sec": float(runner_up["conflict_penalized_abs_error_sec"]),
            }
            if runner_up
            else None
        )
        return best
    pair_candidates: list[dict[str, Any]] = []
    for delta in np.arange(
        LOCAL_DELTA_SEARCH_MIN_SEC,
        LOCAL_DELTA_SEARCH_MAX_SEC + LOCAL_DELTA_SEARCH_STEP_SEC / 2.0,
        LOCAL_DELTA_SEARCH_STEP_SEC,
    ):
        if qc_calibration_end_min is not None and pair_offset_sec is not None:
            candidate = local_delta_qc_pair_evidence(
                lif_peaks,
                ms_events,
                annotation_start_min=annotation_start_min,
                seed_window_min=seed_window_min,
                qc_calibration_end_min=float(qc_calibration_end_min),
                green_shift_sec=green_shift_sec,
                red_shift_sec=red_shift_sec,
                ms_delta_sec=float(delta),
                pair_offset_sec=float(pair_offset_sec),
                axis_shifts_sec=axis_shifts_sec,
                channel_time_axes=channel_time_axes,
                qc_anchor_channels=qc_anchor_channels,
            )
            median_abs = candidate["median_abs_residual_sec"]
            p90_abs = candidate["p90_abs_residual_sec"]
            regularized_error = float(median_abs if median_abs is not None else 9999.0) + LOCAL_DELTA_ABS_PRIOR_WEIGHT * abs(float(delta))
            score_key = (
                int(candidate["unique_match_count"]),
                -regularized_error,
                -float(p90_abs if p90_abs is not None else 9999.0),
                -abs(float(delta)),
            )
            candidate["score_key"] = score_key
            candidate["regularized_abs_error_sec"] = regularized_error
            pair_candidates.append(candidate)
    pair_candidates.sort(key=lambda item: item["score_key"], reverse=True)
    best_pair = pair_candidates[0] if pair_candidates else None
    if best_pair is not None and int(best_pair["unique_match_count"]) > 0:
        separated = [
            item
            for item in pair_candidates[1:]
            if abs(float(item["delta_sec"]) - float(best_pair["delta_sec"]))
            >= max(1.0, POST_QC_CANDIDATE_TOL_SEC)
        ]
        runner_up = separated[0] if separated else None
        ambiguous = bool(
            runner_up
            and int(runner_up["unique_match_count"]) == int(best_pair["unique_match_count"])
            and int(runner_up["conflict_count"]) == int(best_pair["conflict_count"])
            and float(runner_up["regularized_abs_error_sec"])
            <= float(best_pair["regularized_abs_error_sec"]) + 0.25
        )
        best_pair["method"] = "qc_pair_seed_window_shift_grid_search"
        best_pair["search_range_sec"] = [LOCAL_DELTA_SEARCH_MIN_SEC, LOCAL_DELTA_SEARCH_MAX_SEC]
        best_pair["search_step_sec"] = LOCAL_DELTA_SEARCH_STEP_SEC
        best_pair["match_tolerance_sec"] = POST_QC_CANDIDATE_TOL_SEC
        best_pair["contains_cell_labels"] = False
        best_pair["delta_abs_prior_weight"] = LOCAL_DELTA_ABS_PRIOR_WEIGHT
        best_pair["recommendation_status"] = (
            "insufficient_evidence"
            if int(best_pair["unique_match_count"]) < 2
            else "ambiguous"
            if ambiguous
            else "recommended"
        )
        best_pair["runner_up"] = (
            {
                "delta_sec": float(runner_up["delta_sec"]),
                "unique_match_count": int(runner_up["unique_match_count"]),
                "conflict_count": int(runner_up["conflict_count"]),
                "regularized_abs_error_sec": float(runner_up["regularized_abs_error_sec"]),
            }
            if runner_up
            else None
        )
        return best_pair

    fallback_candidates: list[dict[str, Any]] = []
    zero_evidence: dict[str, Any] | None = None
    for delta in np.arange(
        LOCAL_DELTA_SEARCH_MIN_SEC,
        LOCAL_DELTA_SEARCH_MAX_SEC + LOCAL_DELTA_SEARCH_STEP_SEC / 2.0,
        LOCAL_DELTA_SEARCH_STEP_SEC,
    ):
        candidate = local_delta_evidence_pairs(
            lif_peaks,
            ms_events,
            annotation_start_min=annotation_start_min,
            seed_window_min=seed_window_min,
            green_shift_sec=green_shift_sec,
            red_shift_sec=red_shift_sec,
            ms_delta_sec=float(delta),
            channel_shifts_sec=channel_shifts_sec,
        )
        median_abs = candidate["median_abs_residual_sec"]
        p90_abs = candidate["p90_abs_residual_sec"]
        score_key = (
            int(candidate["unique_match_count"]),
            -float(median_abs if median_abs is not None else 9999.0),
            -float(p90_abs if p90_abs is not None else 9999.0),
            -int(candidate["conflict_count"]),
            -abs(float(delta)),
        )
        candidate["score_key"] = score_key
        candidate["regularized_abs_error_sec"] = float(
            median_abs if median_abs is not None else 9999.0
        ) + LOCAL_DELTA_ABS_PRIOR_WEIGHT * abs(float(delta))
        fallback_candidates.append(candidate)
        if abs(float(delta)) < LOCAL_DELTA_SEARCH_STEP_SEC / 2.0:
            zero_evidence = candidate
    fallback_candidates.sort(key=lambda item: item["score_key"], reverse=True)
    best = fallback_candidates[0]
    if int(best["unique_match_count"]) <= 0 and zero_evidence is not None:
        best = zero_evidence
    separated = [
        item
        for item in fallback_candidates
        if item is not best
        and abs(float(item["delta_sec"]) - float(best["delta_sec"])) >= max(1.0, LOCAL_DELTA_MATCH_TOL_SEC)
    ]
    runner_up = separated[0] if separated else None
    ambiguous = bool(
        runner_up
        and int(runner_up["unique_match_count"]) == int(best["unique_match_count"])
        and int(runner_up["conflict_count"]) == int(best["conflict_count"])
        and float(runner_up["regularized_abs_error_sec"])
        <= float(best["regularized_abs_error_sec"]) + 0.25
    )
    best["method"] = "unlabeled_seed_window_shift_only_grid_search"
    best["search_range_sec"] = [LOCAL_DELTA_SEARCH_MIN_SEC, LOCAL_DELTA_SEARCH_MAX_SEC]
    best["search_step_sec"] = LOCAL_DELTA_SEARCH_STEP_SEC
    best["match_tolerance_sec"] = LOCAL_DELTA_MATCH_TOL_SEC
    best["contains_cell_labels"] = False
    best["delta_abs_prior_weight"] = LOCAL_DELTA_ABS_PRIOR_WEIGHT
    best["recommendation_status"] = (
        "insufficient_evidence"
        if int(best["unique_match_count"]) < 2
        else "ambiguous"
        if ambiguous
        else "recommended"
    )
    best["runner_up"] = (
        {
            "delta_sec": float(runner_up["delta_sec"]),
            "unique_match_count": int(runner_up["unique_match_count"]),
            "conflict_count": int(runner_up["conflict_count"]),
            "regularized_abs_error_sec": float(runner_up["regularized_abs_error_sec"]),
        }
        if runner_up
        else None
    )
    return best


def draft_calibration_alignment(
    *,
    acquisition_layout: dict[str, Any],
    calibration_protocol: dict[str, Any],
) -> dict[str, Any]:
    """Return a non-scientific zero-shift view for an unconfirmed project.

    It lets users open the project and inspect raw peak shapes. No candidate,
    accepted anchor, QC refit, or downstream time model is derived from these
    provisional boundaries.
    """

    layout = normalize_acquisition_layout(acquisition_layout)
    protocol = normalize_calibration_protocol(
        calibration_protocol,
        layout,
        require_confirmed=False,
    )
    if bool(protocol.get("boundaries_confirmed")):
        raise BadRequest("draft_calibration_alignment 只用于边界待确认的项目")
    protocol_hash = calibration_protocol_hash(protocol, layout)
    channel_time_axes = {
        str(channel): str(axis)
        for channel, axis in layout["channel_time_axes"].items()
    }
    configured_axes = sorted(set(channel_time_axes.values()))
    axis_shifts_sec = {axis: 0.0 for axis in configured_axes}
    channels = {
        str(row["channel"]): {
            "channel": str(row["channel"]),
            "shift_sec": 0.0,
            "match_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
            "status": "calibration_boundaries_unconfirmed",
            "shift_estimation_matches": [],
        }
        for row in layout["lif_channels"]
    }
    axes = {
        axis: {
            "time_axis": axis,
            "shift_sec": 0.0,
            "match_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
            "status": "calibration_boundaries_unconfirmed",
            "shift_estimation_matches": [],
        }
        for axis in configured_axes
    }
    return {
        "model": f"calibration_draft_{protocol_hash[:12]}",
        "status": "calibration_boundaries_unconfirmed",
        "description": (
            "参考段边界尚未确认；当前只提供原始峰形浏览，物理轴平移未估计，"
            "前段校准、后段 delta 和事件标注均已门禁。"
        ),
        "green_to_ms_shift_sec": 0.0,
        "red_to_ms_shift_sec": 0.0,
        "axis_shifts_sec": axis_shifts_sec,
        "channel_time_axes": channel_time_axes,
        "qc_anchor_channels": list(protocol["reference_channels"]),
        "qc_anchor_time_axes": list(protocol["calibration_time_axes"]),
        "calibration_protocol": {**protocol, "protocol_hash": protocol_hash},
        "calibration_protocol_hash": protocol_hash,
        "matcher_version": SEGMENTED_CALIBRATION_MATCHER_VERSION,
        "acquisition_layout_hash": acquisition_layout_hash(layout),
        "r2_uses": "red_to_ms_shift_sec",
        "ms_shift_sec": 0.0,
        "channels": channels,
        "axes": axes,
        "qc_groups": {
            "status": "calibration_boundaries_unconfirmed",
            "groups": [],
        },
    }


def estimate_segmented_shift_alignment(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    acquisition_layout: dict[str, Any],
    calibration_protocol: dict[str, Any],
) -> dict[str, Any]:
    layout = normalize_acquisition_layout(acquisition_layout)
    protocol = normalize_calibration_protocol(calibration_protocol, layout)
    protocol_hash = calibration_protocol_hash(protocol, layout)
    protocol = {**protocol, "protocol_hash": protocol_hash}
    channel_time_axes = {
        str(channel): str(axis) for channel, axis in layout["channel_time_axes"].items()
    }
    axis_estimates = {
        axis: estimate_segmented_axis_shift(
            lif_peaks,
            ms_events,
            time_axis=axis,
            calibration_protocol=protocol,
            channel_time_axes=channel_time_axes,
        )
        for axis in protocol["calibration_time_axes"]
    }
    axis_shifts_sec = {
        str(axis): float(estimate["shift_sec"])
        for axis, estimate in axis_estimates.items()
    }
    for row in layout["lif_channels"]:
        axis_shifts_sec.setdefault(str(row["time_axis"]), 0.0)
    channel_estimates: dict[str, dict[str, Any]] = {}
    for row in layout["lif_channels"]:
        channel = str(row["channel"])
        axis = str(row["time_axis"])
        estimate = axis_estimates.get(axis)
        if estimate:
            channel_estimates[channel] = {
                **estimate,
                "channel": channel,
                "shift_sec": float(axis_shifts_sec[axis]),
                "shift_source_channels": list(estimate["channels"]),
                "shift_estimation_matches": [
                    copy.deepcopy(match)
                    for match in estimate["shift_estimation_matches"]
                    if channel in set(match.get("channels") or [])
                ],
            }
        else:
            channel_estimates[channel] = {
                "channel": channel,
                "shift_sec": 0.0,
                "match_count": 0,
                "median_abs_residual_sec": None,
                "p90_abs_residual_sec": None,
                "status": "axis_not_covered_by_calibration_protocol",
                "shift_estimation_matches": [],
            }
    qc_groups = build_segmented_calibration_groups(
        lif_peaks,
        ms_events,
        calibration_protocol=protocol,
        channel_time_axes=channel_time_axes,
        axis_shifts_sec=axis_shifts_sec,
    )
    return {
        "model": f"segmented_calibration_{protocol_hash[:12]}",
        "status": "suggestion_not_annotation",
        "description": (
            f"按 {len(protocol['segments'])} 个用户确认参考段估计物理轴平移；"
            "同一 time_axis 的通道共享一个平移，不要求同轴通道同时出峰，MS760/MS782 不移动。"
        ),
        "green_to_ms_shift_sec": float(axis_shifts_sec.get("green_axis", 0.0)),
        "red_to_ms_shift_sec": float(axis_shifts_sec.get("red_axis", 0.0)),
        "axis_shifts_sec": axis_shifts_sec,
        "channel_time_axes": channel_time_axes,
        # Deprecated compatibility projection. New code consumes calibration_protocol.
        "qc_anchor_channels": list(protocol["reference_channels"]),
        "qc_anchor_time_axes": list(protocol["calibration_time_axes"]),
        "calibration_protocol": protocol,
        "calibration_protocol_hash": protocol_hash,
        "matcher_version": SEGMENTED_CALIBRATION_MATCHER_VERSION,
        "acquisition_layout_hash": acquisition_layout_hash(layout),
        "r2_uses": "red_to_ms_shift_sec",
        "ms_shift_sec": 0.0,
        "channels": channel_estimates,
        "axes": axis_estimates,
        "qc_groups": qc_groups,
    }


def estimate_shift_alignment(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    qc_calibration_end_min: float = QC_SHIFT_WINDOW_MIN,
    acquisition_layout: dict[str, Any] | None = None,
    calibration_protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if calibration_protocol is not None:
        return estimate_segmented_shift_alignment(
            lif_peaks,
            ms_events,
            acquisition_layout=normalize_acquisition_layout(acquisition_layout),
            calibration_protocol=calibration_protocol,
        )
    qc_end = float(qc_calibration_end_min)
    layout = normalize_acquisition_layout(acquisition_layout)
    channel_time_axes = layout["channel_time_axes"]
    qc_anchor_channels = layout["qc_anchor_channels"]
    channel_configs = layout["lif_channels"]
    axis_shifts_sec: dict[str, float] = {}
    axis_sources: dict[str, str] = {}
    axis_estimates: dict[str, dict[str, Any]] = {}
    anchor_estimates: dict[str, dict[str, Any]] = {}
    if is_legacy_qc_anchor_pair(qc_anchor_channels):
        anchor_estimates = {
            channel: estimate_channel_shift(lif_peaks, ms_events, channel, qc_end)
            for channel in qc_anchor_channels
        }
        for channel in qc_anchor_channels:
            axis = str(channel_time_axes[channel])
            axis_shifts_sec[axis] = float(anchor_estimates[channel]["shift_sec"])
            axis_sources[axis] = channel
            axis_estimates[axis] = {
                **anchor_estimates[channel],
                "time_axis": axis,
                "channels": [channel],
            }
    else:
        channels_by_axis: dict[str, list[str]] = {}
        for channel in qc_anchor_channels:
            channels_by_axis.setdefault(str(channel_time_axes[channel]), []).append(channel)
        axis_estimates = {
            axis: estimate_axis_shift(
                lif_peaks,
                ms_events,
                time_axis=axis,
                channels=channels,
                qc_calibration_end_min=qc_end,
            )
            for axis, channels in channels_by_axis.items()
        }
        for axis, estimate in axis_estimates.items():
            axis_shifts_sec[axis] = float(estimate["shift_sec"])
            axis_sources[axis] = "+".join(estimate["channels"])
            for channel in estimate["channels"]:
                anchor_estimates[channel] = {
                    **estimate,
                    "channel": channel,
                    "shift_source_channels": list(estimate["channels"]),
                }
    for row in channel_configs:
        axis = str(row["time_axis"])
        axis_shifts_sec.setdefault(axis, 0.0)
        axis_sources.setdefault(axis, "")
    green_shift = float(axis_shifts_sec.get("green_axis", 0.0))
    red_shift = float(axis_shifts_sec.get("red_axis", 0.0))
    qc_groups = build_qc_alignment_groups(
        lif_peaks,
        ms_events,
        green_shift,
        red_shift,
        qc_end,
        axis_shifts_sec=axis_shifts_sec,
        channel_time_axes=channel_time_axes,
        qc_anchor_channels=qc_anchor_channels,
    )
    channel_estimates: dict[str, dict[str, Any]] = {}
    for row in channel_configs:
        channel = row["channel"]
        axis = row["time_axis"]
        if channel in anchor_estimates:
            channel_estimates[channel] = anchor_estimates[channel]
            continue
        source = axis_sources.get(axis, "")
        source_estimate = axis_estimates.get(axis)
        if source_estimate:
            channel_estimates[channel] = {
                **source_estimate,
                "channel": channel,
                "shift_sec": axis_shifts_sec[axis],
                "shift_source_channel": source,
                "shift_estimation_matches": [],
            }
        else:
            channel_estimates[channel] = {
                "channel": channel,
                "shift_sec": axis_shifts_sec.get(axis, 0.0),
                "match_count": 0,
                "median_abs_residual_sec": None,
                "p90_abs_residual_sec": None,
                "status": "axis_not_covered_by_qc_anchor",
                "shift_estimation_matches": [],
            }
    return {
        "model": (
            f"shift_only_auto_0_{qc_end:g}min_qc"
            if is_legacy_qc_anchor_pair(qc_anchor_channels)
            else f"shift_only_axis_aware_auto_0_{qc_end:g}min_qc"
        ),
        "status": "suggestion_not_annotation",
        "description": f"0-{qc_end:g} min 全 QC 区段自动估计整体平移；QC anchor={'+'.join(qc_anchor_channels)}；同一 time_axis 的 LIF 通道共用平移，MS760/MS782 不移动。",
        "green_to_ms_shift_sec": green_shift,
        "red_to_ms_shift_sec": red_shift,
        "axis_shifts_sec": axis_shifts_sec,
        "channel_time_axes": channel_time_axes,
        "qc_anchor_channels": qc_anchor_channels,
        "qc_anchor_time_axes": layout.get("qc_anchor_time_axes", []),
        "matcher_version": "legacy_pair_v1" if is_legacy_qc_anchor_pair(qc_anchor_channels) else QC_MATCHER_VERSION,
        "acquisition_layout_hash": acquisition_layout_hash(layout),
        "r2_uses": "red_to_ms_shift_sec",
        "ms_shift_sec": 0.0,
        "channels": channel_estimates,
        "axes": axis_estimates,
        "qc_groups": qc_groups,
    }


def accepted_qc_alignment_refit(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    annotations: list[dict[str, Any]],
    *,
    acquisition_layout: dict[str, Any] | None,
    calibration_protocol: dict[str, Any] | None = None,
    qc_calibration_end_min: float,
    current_axis_shifts_sec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layout = normalize_acquisition_layout(acquisition_layout)
    layout_hash = acquisition_layout_hash(layout)
    qc_end = float(qc_calibration_end_min)
    normalized_protocol = (
        normalize_calibration_protocol(calibration_protocol, layout)
        if calibration_protocol is not None
        else None
    )
    protocol_hash = (
        calibration_protocol_hash(normalized_protocol, layout)
        if normalized_protocol is not None
        else ""
    )
    anchor_channels = [
        str(channel)
        for channel in (
            normalized_protocol["reference_channels"]
            if normalized_protocol is not None
            else layout["qc_anchor_channels"]
        )
    ]
    channel_axes = {str(channel): str(axis) for channel, axis in layout["channel_time_axes"].items()}
    required_axes = sorted(
        normalized_protocol["calibration_time_axes"]
        if normalized_protocol is not None
        else {channel_axes[channel] for channel in anchor_channels}
    )
    protocol_segments = {
        str(row["segment_id"]): row for row in (normalized_protocol or {}).get("segments", [])
    }
    previous_shifts = {
        axis: float((current_axis_shifts_sec or {}).get(axis, 0.0))
        for axis in required_axes
    }
    unique_peaks = lif_peaks.drop_duplicates("peak_id", keep=False)
    weak_peak_ids = {
        str(row["peak_id"])
        for _, row in unique_peaks.iterrows()
        if str(row.get("peak_tier") or "core").strip().lower() == "weak"
    }
    peak_by_id = {
        str(row["peak_id"]): row
        for _, row in automatic_lif_peak_evidence(unique_peaks).iterrows()
    }
    ms_by_id = {
        str(row["event_id"]): row
        for _, row in ms_events.drop_duplicates("event_id", keep=False).iterrows()
    }
    axis_observations: dict[str, list[dict[str, Any]]] = {axis: [] for axis in required_axes}
    conflicts: list[dict[str, Any]] = []
    accepted_qc_records: list[dict[str, Any]] = []

    for record in annotations:
        if str(record.get("review_status")) != "accepted" or str(record.get("label")) != "QC":
            continue
        candidate_type = str(record.get("candidate_type") or "")
        review_stage = str(record.get("review_stage") or "")
        annotation_id = str(record.get("annotation_id") or "")
        if review_stage and review_stage != "qc_calibration":
            continue
        if not review_stage and candidate_type not in {
            "qc_calibration_anchor_0_10p5",
            "qc_calibration_segment_anchor",
            "manual_qc_triplet",
            "manual_qc_anchor_set",
        } and not annotation_id.startswith("auto_qc:"):
            continue
        record_layout_hash = str(record.get("acquisition_layout_hash") or "")
        if record_layout_hash and record_layout_hash != layout_hash:
            conflicts.append(
                {
                    "annotation_id": annotation_id,
                    "reason": "acquisition_layout_hash_mismatch",
                }
            )
            continue
        ms_event_id = str(record.get("ms_event_id") or "")
        ms_row = ms_by_id.get(ms_event_id)
        if ms_row is None or not is_primary_pc34_event(ms_row):
            conflicts.append(
                {
                    "annotation_id": annotation_id,
                    "ms_event_id": ms_event_id,
                    "reason": "missing_or_non_primary_ms760_event",
                }
            )
            continue
        ms_time_sec = float(ms_row["time_sec"])
        segment = None
        record_anchor_channels = anchor_channels
        record_axes = required_axes
        min_time_sec = 0.0
        max_time_sec = qc_end * 60.0
        if normalized_protocol is not None:
            segment_id = str(record.get("calibration_segment_id") or "")
            segment = protocol_segments.get(segment_id)
            if segment is None:
                matching_segments = [
                    row
                    for row in normalized_protocol["segments"]
                    if float(row["start_min"]) * 60.0 - 1e-9
                    <= ms_time_sec
                    <= float(row["end_min"]) * 60.0 + 1e-9
                ]
                if len(matching_segments) == 1:
                    segment = matching_segments[0]
                else:
                    conflicts.append(
                        {
                            "annotation_id": annotation_id,
                            "ms_event_id": ms_event_id,
                            "reason": "missing_or_ambiguous_calibration_segment_id",
                        }
                    )
                    continue
            record_anchor_channels = [str(channel) for channel in segment["reference_channels"]]
            record_axes = [str(axis) for axis in segment["time_axes"]]
            min_time_sec = float(segment["start_min"]) * 60.0
            max_time_sec = float(segment["end_min"]) * 60.0
        if ms_time_sec < min_time_sec - 1e-9 or ms_time_sec > max_time_sec + 1e-9:
            continue
        accepted_qc_records.append(record)
        anchor_ids = qc_anchor_peak_id_map(record)
        for axis in record_axes:
            selected_rows: list[pd.Series] = []
            selected_ids: dict[str, str] = {}
            invalid_reason = ""
            for channel in record_anchor_channels:
                if channel_axes[channel] != axis:
                    continue
                peak_id = optional_peak_id(anchor_ids.get(channel))
                if not peak_id:
                    continue
                peak_row = peak_by_id.get(peak_id)
                if peak_row is None:
                    invalid_reason = (
                        "weak_lif_peak_not_training_evidence"
                        if peak_id in weak_peak_ids
                        else "missing_or_duplicate_lif_peak"
                    )
                    break
                if str(peak_row["channel"]).strip().upper() != channel:
                    invalid_reason = "lif_peak_channel_mismatch"
                    break
                peak_time_sec = float(peak_row["time_sec"])
                if peak_time_sec < min_time_sec - 1e-9 or peak_time_sec > max_time_sec + 1e-9:
                    invalid_reason = "lif_peak_outside_qc_range"
                    break
                selected_rows.append(peak_row)
                selected_ids[channel] = peak_id
            if invalid_reason or not selected_rows:
                conflicts.append(
                    {
                        "annotation_id": annotation_id,
                        "ms_event_id": ms_event_id,
                        "time_axis": axis,
                        "reason": invalid_reason or "axis_has_no_selected_anchor",
                    }
                )
                continue
            lif_times_sec = [float(row["time_sec"]) for row in selected_rows]
            span_sec = float(max(lif_times_sec) - min(lif_times_sec))
            if span_sec > QC_AXIS_COHERENCE_TOL_SEC + 1e-9:
                conflicts.append(
                    {
                        "annotation_id": annotation_id,
                        "ms_event_id": ms_event_id,
                        "time_axis": axis,
                        "reason": "same_axis_anchor_conflict",
                        "axis_span_sec": span_sec,
                    }
                )
                continue
            lif_axis_time_sec = float(np.median(lif_times_sec))
            observed_shift_sec = float(ms_time_sec - lif_axis_time_sec)
            if not math.isfinite(observed_shift_sec) or not (
                SHIFT_SEARCH_MIN_SEC <= observed_shift_sec <= SHIFT_SEARCH_MAX_SEC
            ):
                conflicts.append(
                    {
                        "annotation_id": annotation_id,
                        "ms_event_id": ms_event_id,
                        "time_axis": axis,
                        "reason": "observed_shift_outside_supported_range",
                        "observed_shift_sec": clean_value(observed_shift_sec),
                    }
                )
                continue
            axis_observations[axis].append(
                {
                    "annotation_id": annotation_id,
                    "ms_event_id": ms_event_id,
                    "time_axis": axis,
                    "lif_peak_ids": selected_ids,
                    "lif_axis_time_sec": lif_axis_time_sec,
                    "ms_time_sec": ms_time_sec,
                    "axis_span_sec": span_sec,
                    "observed_shift_sec": observed_shift_sec,
                    "calibration_segment_id": str((segment or {}).get("segment_id") or "legacy_qc_calibration"),
                }
            )

    axis_models: dict[str, dict[str, Any]] = {}
    inlier_evidence: list[dict[str, Any]] = []
    insufficient_axes: list[str] = []
    for axis in required_axes:
        grouped_by_ms: dict[str, list[dict[str, Any]]] = {}
        for observation in axis_observations[axis]:
            grouped_by_ms.setdefault(str(observation["ms_event_id"]), []).append(observation)
        independent: list[dict[str, Any]] = []
        for ms_event_id, rows in grouped_by_ms.items():
            values = [float(row["observed_shift_sec"]) for row in rows]
            if max(values) - min(values) > QC_REFIT_MIN_OUTLIER_TOL_SEC:
                conflicts.append(
                    {
                        "ms_event_id": ms_event_id,
                        "time_axis": axis,
                        "reason": "duplicate_ms_event_has_conflicting_anchors",
                    }
                )
                continue
            selected = copy.deepcopy(rows[0])
            selected["observed_shift_sec"] = float(np.median(values))
            independent.append(selected)
        values = np.asarray([float(row["observed_shift_sec"]) for row in independent], dtype=float)
        if len(values) < QC_REFIT_MIN_EVIDENCE_PER_AXIS:
            insufficient_axes.append(f"{axis}({len(values)} 条)")
            continue
        median_shift = float(np.median(values))
        mad = float(np.median(np.abs(values - median_shift)))
        threshold = min(
            QC_REFIT_MAX_OUTLIER_TOL_SEC,
            max(QC_REFIT_MIN_OUTLIER_TOL_SEC, QC_REFIT_MAD_SCALE * 1.4826 * mad),
        )
        if len(values) == 2 and float(np.ptp(values)) > 2.0 * QC_REFIT_MIN_OUTLIER_TOL_SEC:
            insufficient_axes.append(f"{axis}(2 条证据互相矛盾)")
            continue
        inlier_mask = np.abs(values - median_shift) <= threshold + 1e-9
        inliers = [row for row, keep in zip(independent, inlier_mask.tolist()) if keep]
        if len(inliers) < QC_REFIT_MIN_EVIDENCE_PER_AXIS:
            insufficient_axes.append(f"{axis}({len(inliers)} 条稳健内点)")
            continue
        shift_sec = float(np.median([float(row["observed_shift_sec"]) for row in inliers]))
        residuals = np.asarray(
            [float(row["observed_shift_sec"]) - shift_sec for row in inliers],
            dtype=float,
        )
        median_abs_residual = float(np.median(np.abs(residuals)))
        p90_abs_residual = float(np.quantile(np.abs(residuals), 0.9))
        if p90_abs_residual > QC_REFIT_MAX_P90_RESIDUAL_SEC + 1e-9:
            insufficient_axes.append(
                f"{axis}(P90 残差 {p90_abs_residual:.2f} sec 超过 {QC_REFIT_MAX_P90_RESIDUAL_SEC:g} sec)"
            )
            continue
        for row in inliers:
            row["residual_sec"] = float(row["observed_shift_sec"] - shift_sec)
            row["inlier"] = True
            inlier_evidence.append(row)
        outlier_count = len(independent) - len(inliers)
        axis_models[axis] = {
            "shift_sec": shift_sec,
            "previous_shift_sec": previous_shifts[axis],
            "evidence_count": len(independent),
            "inlier_count": len(inliers),
            "outlier_count": outlier_count,
            "median_abs_residual_sec": median_abs_residual,
            "p90_abs_residual_sec": p90_abs_residual,
            "mad_shift_sec": mad,
            "outlier_threshold_sec": threshold,
        }
    if insufficient_axes:
        raise BadRequest(
            "已接受的 QC anchor 不足以稳定重算全部物理轴："
            + "；".join(insufficient_axes)
            + "。每条轴至少需要 2 个独立且一致的 accepted anchor。"
        )

    axis_shifts = {axis: float(axis_models[axis]["shift_sec"]) for axis in required_axes}
    evidence_payload = sorted(
        inlier_evidence,
        key=lambda row: (str(row["time_axis"]), float(row["ms_time_sec"]), str(row["annotation_id"])),
    )
    all_observations = sorted(
        [copy.deepcopy(row) for axis in required_axes for row in axis_observations[axis]],
        key=lambda row: json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )
    conflict_payload = sorted(
        [copy.deepcopy(row) for row in conflicts],
        key=lambda row: json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )
    accepted_annotation_ids = sorted(
        {str(row.get("annotation_id") or "") for row in accepted_qc_records}
    )
    signature_payload = {
        "model_version": QC_ALIGNMENT_MODEL_VERSION,
        "method": "accepted_qc_anchor_axis_median_mad",
        "qc_calibration_end_min": qc_end,
        "acquisition_layout_hash": layout_hash,
        "calibration_protocol_hash": protocol_hash,
        "axis_shifts_sec": axis_shifts,
        "axis_models": {
            axis: {
                key: value
                for key, value in axis_models[axis].items()
                if key != "previous_shift_sec"
            }
            for axis in required_axes
        },
        "accepted_annotation_ids": accepted_annotation_ids,
        "all_axis_observations": all_observations,
        "conflicts": conflict_payload,
    }
    preview_hash = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    used_annotation_ids = sorted({str(row["annotation_id"]) for row in evidence_payload})
    return {
        "model_version": QC_ALIGNMENT_MODEL_VERSION,
        "model_id": f"qca_{preview_hash[:12]}",
        "status": "preview",
        "method": "accepted_qc_anchor_axis_median_mad",
        "qc_calibration_end_min": qc_end,
        "acquisition_layout_hash": layout_hash,
        "calibration_protocol_hash": protocol_hash,
        "axis_shifts_sec": axis_shifts,
        "previous_axis_shifts_sec": previous_shifts,
        "axes": axis_models,
        "evidence": evidence_payload,
        "all_axis_observations": all_observations,
        "accepted_annotation_count": len(accepted_qc_records),
        "used_annotation_count": len(used_annotation_ids),
        "used_annotation_ids": used_annotation_ids,
        "conflict_count": len(conflicts),
        "conflicts": conflict_payload,
        "preview_hash": preview_hash,
    }


def apply_qc_alignment_model(
    automatic_alignment: dict[str, Any],
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    qc_calibration_end_min: float,
    acquisition_layout: dict[str, Any] | None,
    model: dict[str, Any],
    calibration_protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layout = normalize_acquisition_layout(acquisition_layout)
    layout_hash = acquisition_layout_hash(layout)
    qc_end = float(qc_calibration_end_min)
    if int(model.get("model_version", 0)) != QC_ALIGNMENT_MODEL_VERSION:
        raise BadRequest("QC alignment model 版本不受支持")
    if str(model.get("acquisition_layout_hash") or "") != layout_hash:
        raise BadRequest("已保存的 QC alignment model 与当前 acquisition layout 不一致")
    if abs(float(model.get("qc_calibration_end_min", -1.0)) - qc_end) > 1e-9:
        raise BadRequest("已保存的 QC alignment model 与当前 QC 结束时间不一致")
    normalized_protocol = (
        normalize_calibration_protocol(calibration_protocol, layout)
        if calibration_protocol is not None
        else None
    )
    protocol_hash = (
        calibration_protocol_hash(normalized_protocol, layout)
        if normalized_protocol is not None
        else ""
    )
    if normalized_protocol is not None and str(model.get("calibration_protocol_hash") or "") != protocol_hash:
        raise BadRequest("已保存的 QC alignment model 与当前 calibration protocol 不一致")
    required_axes = sorted(
        normalized_protocol["calibration_time_axes"]
        if normalized_protocol is not None
        else {str(axis) for axis in layout["qc_anchor_time_axes"]}
    )
    raw_shifts = model.get("axis_shifts_sec")
    if not isinstance(raw_shifts, dict) or any(axis not in raw_shifts for axis in required_axes):
        raise BadRequest("QC alignment model 没有覆盖全部物理轴")
    axis_shifts = {axis: float(raw_shifts[axis]) for axis in required_axes}
    if any(not math.isfinite(value) or abs(value) > max(abs(SHIFT_SEARCH_MIN_SEC), SHIFT_SEARCH_MAX_SEC) for value in axis_shifts.values()):
        raise BadRequest("QC alignment model 包含无效物理轴平移")

    alignment = copy.deepcopy(automatic_alignment)
    alignment["model"] = (
        f"accepted_anchor_refit_segmented_{protocol_hash[:12]}"
        if normalized_protocol is not None
        else f"accepted_anchor_refit_0_{qc_end:g}min_qc"
    )
    alignment["status"] = "accepted_anchor_refit_active"
    alignment["description"] = (
        (
            f"{len(normalized_protocol['segments'])} 个项目参考段的 accepted anchors 按物理轴稳健重拟合；"
            if normalized_protocol is not None
            else f"0-{qc_end:g} min accepted QC anchors 按物理轴稳健重拟合；"
        )
        + "同一 time_axis 的 LIF 通道共用平移，MS760/MS782 不移动。"
    )
    alignment["axis_shifts_sec"] = axis_shifts
    alignment["green_to_ms_shift_sec"] = float(axis_shifts.get("green_axis", 0.0))
    alignment["red_to_ms_shift_sec"] = float(axis_shifts.get("red_axis", 0.0))
    alignment["qc_alignment_model"] = copy.deepcopy(model)
    for channel, details in alignment.get("channels", {}).items():
        axis = str(layout["channel_time_axes"].get(channel, default_time_axis_for_channel(channel)))
        details["shift_sec"] = float(axis_shifts.get(axis, 0.0))
        details["status"] = "accepted_anchor_refit"
        details["shift_estimation_matches"] = []
    for axis in required_axes:
        details = alignment.setdefault("axes", {}).setdefault(axis, {})
        details.update(
            {
                "time_axis": axis,
                "shift_sec": float(axis_shifts[axis]),
                "status": "accepted_anchor_refit",
                "refit_summary": copy.deepcopy(model.get("axes", {}).get(axis, {})),
            }
        )
    if normalized_protocol is not None:
        alignment["calibration_protocol"] = copy.deepcopy(normalized_protocol)
        alignment["calibration_protocol_hash"] = protocol_hash
        alignment["qc_groups"] = build_segmented_calibration_groups(
            lif_peaks,
            ms_events,
            calibration_protocol={**normalized_protocol, "protocol_hash": protocol_hash},
            channel_time_axes=layout["channel_time_axes"],
            axis_shifts_sec=axis_shifts,
        )
    else:
        alignment["qc_groups"] = build_qc_alignment_groups(
            lif_peaks,
            ms_events,
            alignment["green_to_ms_shift_sec"],
            alignment["red_to_ms_shift_sec"],
            qc_end,
            axis_shifts_sec=axis_shifts,
            channel_time_axes=layout["channel_time_axes"],
            qc_anchor_channels=layout["qc_anchor_channels"],
        )
    return alignment


@dataclass(frozen=True)
class AppData:
    project: ProjectPaths
    lif_traces: pd.DataFrame
    lif_peaks: pd.DataFrame
    ms_events: pd.DataFrame
    ms_scan: pd.DataFrame
    alignment: dict[str, Any]
    store: AnnotationStore
    channel_identity_prior: dict[str, dict[str, str]]
    acquisition_layout: dict[str, Any] | None = None
    calibration_protocol: dict[str, Any] | None = None
    post_qc_strategy: dict[str, Any] | None = None
    lif_peak_detection: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    cell_event_map: pd.DataFrame | None = None
    cell_event_map_info: dict[str, Any] | None = None

    def active_lif_peak_detection(self) -> dict[str, Any]:
        """Resolve the active v2 detector for loaded or in-memory app data."""

        if self.lif_peak_detection is not None:
            try:
                return require_active_lif_peak_detection(
                    self.lif_peak_detection
                )
            except ValueError as exc:
                raise BadRequest(f"项目峰识别配置无效：{exc}") from exc
        # Directly constructed AppData objects are used by deterministic unit
        # and scientific tests. AppData.load() validates a real manifest before
        # constructing this object; a missing field here therefore means there
        # is no artifact to adapt, so use the one active standard.
        return require_active_lif_peak_detection()

    @classmethod
    def load(cls, project: ProjectPaths | None = None) -> "AppData":
        project = project or ProjectPaths.from_args()
        manifest = read_project_manifest(project.project_dir)
        peak_detection = lif_peak_detection_from_manifest(manifest)
        validate_project_manifest_against_files(project.project_dir, manifest)
        project = project_with_manifest_paths(project, manifest)
        for path in [project.lif_traces_path, project.lif_peaks_path, project.ms_events_path, project.ms_scan_path]:
            require_file(path)
        intermediate_tables = intermediate_table_fingerprints(project)
        binding = project_table_binding(intermediate_tables)

        lif_traces = pd.read_parquet(project.lif_traces_path).sort_values(["channel", "time_min"]).reset_index(drop=True)
        all_lif_peaks = pd.read_parquet(project.lif_peaks_path)
        explicit_peak_detection = isinstance(
            (manifest or {}).get("lif_peak_detection"), dict
        )
        all_lif_peaks = validate_and_adapt_lif_peak_detector_binding(
            all_lif_peaks,
            peak_detection,
            explicit_peak_detection=explicit_peak_detection,
        )
        lif_peaks = (
            all_lif_peaks[all_lif_peaks["peak_stage"].eq("merged")]
            .sort_values(["time_min", "channel"])
            .reset_index(drop=True)
        )
        ms_events = pd.read_parquet(project.ms_events_path).sort_values("time_min").reset_index(drop=True)
        ms_scan = pd.read_parquet(project.ms_scan_path).sort_values("scan_start_time_min").reset_index(drop=True)
        cell_event_map, cell_event_map_info = load_project_cell_event_map(
            project.project_dir,
            manifest,
            ms_events,
        )
        acquisition_layout = acquisition_layout_from_manifest(manifest)
        channel_identity_prior = channel_identity_prior_from_manifest(manifest) or infer_channel_identity_prior(project.raw_data_dir)
        if "peak_id" not in lif_peaks.columns or "event_id" not in ms_events.columns:
            raise BadRequest("项目中间表缺少 peak_id 或 event_id，无法校验 annotation.sqlite")
        validate_annotation_db_against_tables(
            project.annotation_db_path,
            set(lif_peaks["peak_id"].astype(str)),
            set(ms_events["event_id"].astype(str)),
        )
        assert_no_legacy_annotation_state(project.annotation_db_path)
        annotation_count_before_load = sqlite_annotation_count(project.annotation_db_path)
        db_existed_before_load = project.annotation_db_path.exists()
        allow_adopt = annotation_count_before_load == 0
        if allow_adopt:
            validate_sqlite_input_manifest_against_files(project.annotation_db_path, project.project_dir, intermediate_tables)
        binding_status = validate_sqlite_project_binding(
            project.annotation_db_path,
            binding,
            allow_adopt=allow_adopt,
        )
        store_defaults = project_config_defaults_from_manifest(manifest)
        # A v0.3 project has neither of the split semantic objects.  They are
        # compatibility projections, not migrations: do not persist them on
        # open.  The in-memory adapter below must first consume the project's
        # already stored qc_calibration_end_min (which may differ from 10.5).
        if not isinstance((manifest or {}).get("calibration_protocol"), dict):
            store_defaults.pop("calibration_protocol", None)
        if not isinstance((manifest or {}).get("post_qc_strategy"), dict):
            store_defaults.pop("post_qc_strategy", None)
        store = AnnotationStore(
            project.annotation_db_path,
            default_project_config=store_defaults,
        )
        project_config = store.project_config()
        calibration_protocol = calibration_protocol_from_manifest(manifest, project_config)
        post_qc_strategy = post_qc_strategy_from_manifest(manifest, project_config)
        legacy_protocol = bool(calibration_protocol.get("compatibility_mode"))
        protocol_confirmed = bool(calibration_protocol.get("boundaries_confirmed"))
        lif_peaks["phase"] = display_phase_from_time_min(
            lif_peaks["time_min"],
            calibration_protocol,
            float(project_config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)),
        )
        automatic_peaks = automatic_lif_peak_evidence(lif_peaks)
        if protocol_confirmed:
            alignment = estimate_shift_alignment(
                automatic_peaks,
                ms_events,
                float(project_config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN)),
                acquisition_layout=acquisition_layout,
                calibration_protocol=None if legacy_protocol else calibration_protocol,
            )
        else:
            alignment = draft_calibration_alignment(
                acquisition_layout=acquisition_layout,
                calibration_protocol=calibration_protocol,
            )
        alignment.setdefault("calibration_protocol", copy.deepcopy(calibration_protocol))
        alignment.setdefault(
            "calibration_protocol_hash",
            calibration_protocol_hash(calibration_protocol, acquisition_layout),
        )
        alignment["post_qc_strategy"] = copy.deepcopy(post_qc_strategy)
        alignment["post_qc_strategy_hash"] = post_qc_strategy_hash(
            post_qc_strategy, acquisition_layout
        )
        persisted_qc_alignment = store.qc_alignment_model()
        if persisted_qc_alignment:
            if not protocol_confirmed:
                raise BadRequest(
                    "项目参考段边界尚未确认，但仍存在已应用的 QC 对齐模型；"
                    "请恢复已确认协议，或在配置中明确清除旧模型后重新确认边界"
                )
            alignment = apply_qc_alignment_model(
                alignment,
                automatic_peaks,
                ms_events,
                qc_calibration_end_min=float(project_config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN)),
                acquisition_layout=acquisition_layout,
                model=persisted_qc_alignment,
                calibration_protocol=None if legacy_protocol else calibration_protocol,
            )
        current_layout_hash = str(alignment.get("acquisition_layout_hash") or "")
        current_protocol_hash = str(
            alignment.get("calibration_protocol_hash")
            or calibration_protocol_hash(calibration_protocol, acquisition_layout)
        )
        existing_time_model = store.active_time_model()
        if existing_time_model and not protocol_confirmed:
            raise BadRequest(
                "项目参考段边界尚未确认，但仍存在 active time model；"
                "请恢复已确认协议，或明确失效旧模型后重新确认边界"
            )
        existing_layout_hash = str((existing_time_model or {}).get("acquisition_layout_hash") or "")
        existing_protocol_hash = str(
            (existing_time_model or {}).get("calibration_protocol_hash") or ""
        )
        if existing_layout_hash and current_layout_hash and existing_layout_hash != current_layout_hash:
            raise BadRequest("项目 QC anchor/time-axis 配置与当前 frozen/draft time model 不一致；请恢复原配置或清除旧 time model 后重新校正")
        if (
            existing_protocol_hash
            and current_protocol_hash
            and existing_protocol_hash != current_protocol_hash
        ):
            raise BadRequest(
                "项目 calibration protocol 与当前 frozen/draft time model 不一致；"
                "请恢复原配置或明确失效旧 time model 后重新校正"
            )
        if existing_time_model and current_layout_hash and not existing_layout_hash:
            if not is_legacy_acquisition_layout(acquisition_layout):
                raise BadRequest(
                    "现有 time model 没有 acquisition layout 绑定，不能静默迁移到新的 QC anchor 配置；"
                    "请先清除旧 time model，再重新进行 QC 校正和后段局部校正"
                )
            # Schema-v1 projects used the fixed G2/R1/R2 layout and did not
            # persist this hash.  Loading them must remain side-effect free:
            # the legacy interpretation is applied in memory until the user
            # performs an explicit mutating operation.
        if existing_time_model and current_protocol_hash and not existing_protocol_hash:
            if not legacy_protocol:
                raise BadRequest(
                    "现有 time model 没有 calibration protocol 绑定，不能静默迁移到新协议；"
                    "请明确失效旧 time model 后重新校正"
                )
        elif existing_time_model is None and protocol_confirmed:
            store.ensure_draft_time_model(
                str(alignment["model"]),
                current_layout_hash,
                calibration_protocol_hash_value=current_protocol_hash,
                allow_unhashed_legacy_binding=False,
            )

        # A matched, established project is read-only during load.  Only a
        # brand-new/empty database may be adopted and populated implicitly.
        may_initialize_binding = (
            binding_status in {"new", "adopt"}
            and (not db_existed_before_load or annotation_count_before_load == 0)
        )
        if may_initialize_binding:
            store.record_project_table_binding(binding)
            store.record_input_manifest(
                {
                    "lif_traces": project.lif_traces_path,
                    "lif_peaks": project.lif_peaks_path,
                    "ms_events": project.ms_events_path,
                    "ms_scan_summary": project.ms_scan_path,
                },
                project_dir=project.project_dir,
            )
        return cls(
            project=project,
            lif_traces=lif_traces,
            lif_peaks=lif_peaks,
            ms_events=ms_events,
            ms_scan=ms_scan,
            alignment=alignment,
            store=store,
            channel_identity_prior=channel_identity_prior,
            acquisition_layout=acquisition_layout,
            calibration_protocol=calibration_protocol,
            post_qc_strategy=post_qc_strategy,
            lif_peak_detection=peak_detection,
            manifest=manifest,
            cell_event_map=cell_event_map,
            cell_event_map_info=cell_event_map_info,
        )

    def meta(self) -> dict[str, Any]:
        min_time = float(
            min(
                self.lif_traces["time_min"].min(),
                self.ms_scan["scan_start_time_min"].min(),
                self.ms_events["time_min"].min(),
            )
        )
        max_time = float(
            max(
                self.lif_traces["time_min"].max(),
                self.ms_scan["scan_start_time_min"].max(),
                self.ms_events["time_min"].max(),
            )
        )
        labels = (
            self.lif_traces[["channel", "label", "detector"]]
            .drop_duplicates()
            .sort_values("channel")
            .to_dict("records")
        )
        return {
            "root": str(self.project.project_dir),
            "project_id": self.project_identity(),
            "project": {
                "project_dir": str(self.project.project_dir),
                "raw_data_dir": str(self.project.raw_data_dir),
                "annotation_db_path": str(self.project.annotation_db_path),
            },
            "default_window_min": DEFAULT_WINDOW_MIN,
            "time_min_min": min_time,
            "time_min_max": max_time,
            "lif_trace_rows": int(len(self.lif_traces)),
            "lif_peak_rows": int(len(self.lif_peaks)),
            "ms_event_rows": int(len(self.ms_events)),
            "ms_scan_rows": int(len(self.ms_scan)),
            "lif_channels": labels,
            "acquisition_layout": normalize_acquisition_layout(self.acquisition_layout),
            "calibration_protocol": copy.deepcopy(self.calibration_protocol),
            "post_qc_strategy": copy.deepcopy(self.post_qc_strategy),
            "lif_peak_detection": copy.deepcopy(
                self.active_lif_peak_detection()
            ),
            "cell_event_map": {
                "available": self.cell_event_map is not None,
                "row_count": int(len(self.cell_event_map)) if self.cell_event_map is not None else 0,
                "sha256": str((self.cell_event_map_info or {}).get("sha256") or ""),
                "attach_allowed": self.cell_event_map is None,
            },
            "channel_identity_prior": self.channel_identity_prior,
            "inputs": {
                "lif_traces": display_path(self.project.lif_traces_path, self.project.project_dir),
                "lif_peaks": display_path(self.project.lif_peaks_path, self.project.project_dir),
                "ms_events": display_path(self.project.ms_events_path, self.project.project_dir),
                "ms_scan_summary": display_path(self.project.ms_scan_path, self.project.project_dir),
            },
            "input_policy": "仅使用第一性原理前处理中间表；不读取作者 CSV、h5ad、manual/V2/archive 输入。",
            "alignment": self.alignment,
            "project_config": self.project_config(),
            "time_model": self.active_time_model(),
            "annotation_store": self.store.summary(),
            "write_token": WRITE_TOKEN,
        }

    def annotation_review_stage(self, row: dict[str, Any]) -> str:
        explicit = str(row.get("review_stage") or "")
        if explicit in {"qc_calibration", "qc_survey", "cell_annotation"}:
            return explicit
        candidate_type = self.infer_candidate_type(row)
        if candidate_type.startswith("cell") or candidate_type == "manual_cell_pair":
            return "cell_annotation"
        if candidate_type.startswith("qc_survey_") or candidate_type == "manual_qc_anchor_partial":
            return "qc_survey"
        if candidate_type == "qc_calibration_anchor_0_10p5":
            return "qc_calibration"
        ms_time = clean_value(row.get("ms_time_min"))
        annotation_start = float(self.project_config().get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN))
        if isinstance(ms_time, (int, float)):
            return "qc_survey" if float(ms_time) >= annotation_start else "qc_calibration"
        return "qc_calibration"

    def infer_candidate_type(self, row: dict[str, Any]) -> str:
        candidate_type = str(row.get("candidate_type") or "")
        if candidate_type:
            return candidate_type
        annotation_id = str(row.get("annotation_id") or "")
        source = str(row.get("source") or "")
        if annotation_id.startswith("auto_qc:"):
            return "qc_calibration_anchor_0_10p5"
        if annotation_id.startswith("post_qc:"):
            return "qc_survey_post_10p5"
        if annotation_id.startswith("cell:"):
            return "cell_high_confidence"
        if annotation_id.startswith("manual_cell:"):
            return "manual_cell_pair"
        if annotation_id.startswith("manual_qc:"):
            if row.get("g2_peak_id") and row.get("r1_peak_id"):
                return "manual_qc_triplet"
            return "manual_qc_anchor_partial"
        if source == "manual_created":
            return "manual_qc_triplet"
        return ""

    def project_identity(self) -> str:
        manifest = self.manifest or {}
        explicit = str(manifest.get("project_id") or manifest.get("dataset_id") or "")
        if explicit:
            return explicit
        return hashlib.sha256(str(self.project.project_dir.resolve()).encode("utf-8")).hexdigest()

    def cell_event_map_sha256(self) -> str:
        return str((self.cell_event_map_info or {}).get("sha256") or "")

    def cell_event_map_event_ids(self) -> set[str] | None:
        if self.cell_event_map is None:
            return None
        return set(self.cell_event_map["ms_event_id"].astype(str))

    def require_third_stage_event_in_map(self, ms_event_id: str) -> None:
        allowed = self.cell_event_map_event_ids()
        if allowed is not None and str(ms_event_id) not in allowed:
            raise BadRequest(
                f"MS event {ms_event_id} 不在当前单细胞 event map 白名单中，不能在第三阶段审核"
            )

    def projected_cell_event_map_state(self) -> dict[str, Any]:
        if self.cell_event_map is None:
            raise BadRequest("当前项目没有单细胞 event map")
        frozen = self.frozen_time_model()
        config = self.project_config()
        annotations = [
            row
            for row in self.store.records()
            if self.annotation_review_stage(row) != "qc_survey"
            or self.qc_survey_matches_current_strategy(row, config=config)
        ]
        state = project_annotation_state(
            self.cell_event_map,
            annotations,
            active_time_model_version=(
                str(frozen.get("time_model_version") or "") if frozen else None
            ),
            annotation_start_min=float(
                config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
            ),
        )
        state.update(
            {
                "project_id": self.project_identity(),
                "map_sha256": self.cell_event_map_sha256(),
                "channel_identity_prior": self.channel_identity_prior,
            }
        )
        state["revision"] = state_revision(
            project_id=state["project_id"],
            map_sha256=state["map_sha256"],
            projected_state=state,
        )
        return state

    def cell_event_map_revision(self) -> dict[str, Any]:
        state = self.projected_cell_event_map_state()
        return {
            "project_id": state["project_id"],
            "map_sha256": state["map_sha256"],
            "active_time_model_version": state["active_time_model_version"],
            "revision": state["revision"],
            "counts": state["counts"],
        }

    def ensure_third_stage_acceptance_allowed(
        self,
        payload: dict[str, Any],
        *,
        annotation_id: str,
    ) -> None:
        stage = self.annotation_review_stage(payload)
        if stage not in {"qc_survey", "cell_annotation"}:
            return
        if stage == "qc_survey" and not self.qc_survey_matches_current_strategy(payload):
            raise BadRequest(
                "该 QC 巡检候选不属于当前 post_qc_strategy（策略可能已修改或禁用）；请在当前窗口重新生成并审核"
            )
        frozen = self.frozen_time_model()
        if not frozen:
            raise BadRequest("请先完成后段局部校正并冻结 delta，再接受第三阶段标注")
        active_version = str(frozen.get("time_model_version") or "")
        payload_version = str(payload.get("time_model_version") or "")
        if not payload_version or payload_version != active_version:
            raise BadRequest("第三阶段标注不属于当前 frozen time model，请在当前图窗重新审核")
        ms_event_id = str(payload.get("ms_event_id") or "")
        if not ms_event_id:
            raise BadRequest("第三阶段 annotation 缺少 ms_event_id")
        self.require_third_stage_event_in_map(ms_event_id)
        conflicting = [
            row
            for row in self.store.records()
            if str(row.get("annotation_id") or "") != str(annotation_id)
            and str(row.get("review_status") or "") == "accepted"
            and str(row.get("ms_event_id") or "") == ms_event_id
            and str(row.get("time_model_version") or "") == active_version
            and self.annotation_review_stage(row) in {"qc_survey", "cell_annotation"}
        ]
        if conflicting:
            relation = conflicting[0]
            raise BadRequest(
                "同一 MS event 在当前 time model 下只能有一个第三阶段标注，已有冲突："
                f"{relation.get('annotation_id')}；请先撤销旧关系再仲裁"
            )
        if self.cell_event_map is None:
            return
        state = self.projected_cell_event_map_state()
        point = next(
            (
                item
                for item in state["points"]
                if str(item.get("ms_event_id")) == ms_event_id
            ),
            None,
        )
        existing_relations = [
            relation
            for relation in (point or {}).get("accepted_relations", [])
            if str(relation.get("annotation_id") or "") != str(annotation_id)
        ]
        if existing_relations:
            details = ", ".join(
                f"{relation.get('kind')}:{relation.get('annotation_id')}"
                for relation in existing_relations[:5]
            )
            raise BadRequest(
                "同一 MS event 只能有一个 active accepted 第三阶段分类；"
                f"请先撤销现有记录 {details}"
            )

    def attach_cell_event_map(self, source_path: Path) -> "AppData":
        if self.cell_event_map is not None or (self.manifest or {}).get("cell_event_map"):
            raise BadRequest("当前项目已绑定 cell event map；当前版本不支持替换")
        manifest = copy.deepcopy(self.manifest or read_project_manifest(self.project.project_dir))
        if not manifest:
            raise BadRequest("旧项目必须先建立 lifms_project.json 才能附加 event map")
        destination = self.project.project_dir / (
            CANONICAL_CELL_EVENT_MAP_PATH
            if manifest_uses_canonical_storage(manifest)
            else CELL_EVENT_MAP_RELATIVE_PATH
        )
        if destination.exists():
            raise BadRequest("项目中已存在未登记的 canonical event map，拒绝覆盖")
        try:
            canonical, import_metadata = import_cell_event_map(
                source_path,
                self.ms_events,
                tolerance_sec=DEFAULT_MATCH_TOLERANCE_SEC,
            )
        except CellEventMapError as exc:
            raise BadRequest(str(exc)) from exc
        allowed_ids = set(canonical["ms_event_id"].astype(str))
        annotation_start = float(
            self.project_config().get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
        )
        missing_history: set[str] = set()
        ms_time_by_event = {
            str(row["event_id"]): float(row["time_min"])
            for _, row in self.ms_events[["event_id", "time_min"]].iterrows()
        }
        for row in self.store.records():
            if str(row.get("review_status") or "") != "accepted":
                continue
            if self.annotation_review_stage(row) not in {"qc_survey", "cell_annotation"}:
                continue
            event_id = str(row.get("ms_event_id") or "")
            try:
                ms_time = float(row.get("ms_time_min"))
            except (TypeError, ValueError):
                ms_time = ms_time_by_event.get(event_id, math.nan)
            if not math.isfinite(ms_time):
                raise BadRequest(
                    f"已有 accepted 第三阶段 annotation {row.get('annotation_id')} 无法绑定 MS 时间，拒绝附加 event map"
                )
            if ms_time < annotation_start:
                continue
            if event_id and event_id not in allowed_ids:
                missing_history.add(event_id)
        if missing_history:
            preview = ", ".join(sorted(missing_history)[:10])
            raise BadRequest(
                "event map 缺少已有 accepted 第三阶段 MS event，不能附加: " + preview
            )

        try:
            write_canonical_map(canonical, destination)
            entry = cell_event_map_manifest_entry(
                canonical_path=destination,
                project_dir=self.project.project_dir,
                import_metadata=import_metadata,
            )
            manifest["project_schema_version"] = PROJECT_SCHEMA_VERSION
            manifest["acquisition_layout"] = normalize_acquisition_layout(
                self.acquisition_layout
            )
            manifest["channel_identity_prior"] = {
                row["channel"]: row.get("identity_prior", "")
                for row in manifest["acquisition_layout"]["lif_channels"]
            }
            if isinstance(manifest.get("annotation_db"), dict):
                manifest["annotation_db"]["schema_version"] = PROJECT_SCHEMA_VERSION
            manifest["cell_event_map"] = entry
            manifest["updated_by_app_version"] = APP_VERSION
            manifest["updated_at"] = now_iso()
            write_existing_project_manifest(self.project.project_dir, manifest)
        except (CellEventMapError, OSError) as exc:
            if destination.exists():
                destination.unlink()
            raise BadRequest(f"附加 event map 失败: {exc}") from exc
        return replace(
            self,
            manifest=manifest,
            cell_event_map=canonical,
            cell_event_map_info=entry,
        )

    def export_accepted_annotations_csv(self) -> dict[str, Any]:
        timestamp = now_iso()
        export_id = f"export_{timestamp.replace(':', '').replace('-', '').replace('Z', '')}_{uuid.uuid4().hex[:8]}"
        csv_filename = export_filename_for_project(self.project.project_dir)
        frozen = self.frozen_time_model()
        active_version = str(frozen.get("time_model_version", "")) if frozen else ""
        filters = {
            "review_status": "accepted",
            "exportable": True,
            "include_stages": ["qc_survey", "cell_annotation"],
            "include_unannotated_event_map_rows_as_unknown": self.cell_event_map is not None,
            "calibration_evidence_policy": "sqlite_audit_only",
            "current_time_model_only_for_post_qc_and_cell": True,
            "active_time_model_version": active_version,
            "input_policy": "first_principles_preprocessing_tables_plus_human_review",
            "label_policy": "Day labels are channel identity priors from raw filename/project config, not author CSV/h5ad labels",
            "cell_event_map_sha256": self.cell_event_map_sha256(),
        }
        rows: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in self.store.records():
            if row.get("review_status") != "accepted" or not bool(row.get("exportable")):
                continue
            stage = self.annotation_review_stage(row)
            if stage == "qc_calibration":
                skipped.append(
                    {
                        "annotation_id": row.get("annotation_id"),
                        "reason": "calibration_evidence_audit_only",
                    }
                )
                continue
            if stage not in {"qc_survey", "cell_annotation"}:
                skipped.append(
                    {
                        "annotation_id": row.get("annotation_id"),
                        "reason": "not_a_main_cell_export_stage",
                    }
                )
                continue
            if stage in {"qc_survey", "cell_annotation"}:
                row_version = str(row.get("time_model_version") or "")
                if not active_version or not row_version or row_version != active_version:
                    skipped.append(
                        {
                            "annotation_id": row.get("annotation_id"),
                            "reason": "stale_time_model_version",
                            "row_time_model_version": row_version,
                            "active_time_model_version": active_version,
                        }
                    )
                    continue
            if stage == "qc_survey" and not self.qc_survey_matches_current_strategy(row):
                skipped.append(
                    {
                        "annotation_id": row.get("annotation_id"),
                        "reason": "stale_post_qc_strategy_hash",
                        "row_post_qc_strategy_hash": str(
                            row.get("post_qc_strategy_hash") or ""
                        ),
                        "active_post_qc_strategy_hash": str(
                            self.project_config().get("post_qc_strategy_hash") or ""
                        ),
                    }
                )
                continue
            rows.append(self.export_row(row, stage=stage, export_id=export_id, exported_at=timestamp))
        if self.cell_event_map is not None:
            projected = self.projected_cell_event_map_state()
            exported_event_ids = {
                str(row.get("MS_event_id") or "")
                for row in rows
                if row.get("MS_event_id")
            }
            for point in projected.get("points", []):
                event_id = str(point.get("ms_event_id") or "")
                if not event_id or event_id in exported_event_ids:
                    continue
                classification = str(point.get("classification") or "unknown")
                if classification == "conflict":
                    raise BadRequest(
                        "主 CSV 不能为同一 MS event 输出多个分类；请先解决 UMAP 中的标注冲突: "
                        + event_id
                    )
                if classification != "unknown":
                    raise BadRequest(
                        "UMAP 中存在当前有效标注，但主 CSV 无法生成对应行，请重新打开项目后再导出: "
                        + event_id
                    )
                rows.append(
                    self.export_unknown_event_row(
                        point,
                        export_id=export_id,
                        exported_at=timestamp,
                    )
                )
                exported_event_ids.add(event_id)
        missing_cell_numbers = [
            str(row.get("annotation_id") or "")
            for row in rows
            if not str(row.get("CellNumber") or "").strip()
        ]
        if missing_cell_numbers:
            raise BadRequest(
                "主 CSV 中每条后段 QC/细胞记录都必须对应一个 MS event 和稳定 CellNumber；"
                "请检查这些标注: " + ", ".join(missing_cell_numbers[:10])
            )
        cell_numbers = [str(row["CellNumber"]) for row in rows]
        duplicate_cell_numbers = sorted(
            value for value, count in Counter(cell_numbers).items() if count > 1
        )
        if duplicate_cell_numbers:
            raise BadRequest(
                "同一 MS event 不能在主 CSV 中重复分类；请先仲裁这些 CellNumber: "
                + ", ".join(duplicate_cell_numbers[:10])
            )
        rows.sort(key=lambda row: str(row.get("CellNumber") or ""))
        columns = self.export_columns()
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: self.csv_value(row.get(key)) for key in columns})
        csv_text = buffer.getvalue()
        csv_bytes = csv_text.encode("utf-8-sig")
        csv_sha256 = hashlib.sha256(csv_bytes).hexdigest()
        export_dir = self.project.annotation_db_path.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        csv_path = unique_file_path(export_dir / csv_filename)
        csv_path.write_bytes(csv_bytes)
        self.store.record_export_run(
            export_id=export_id,
            timestamp=timestamp,
            filter_payload={**filters, "skipped": skipped},
            row_count=len(rows),
            csv_path=csv_path,
            csv_sha256=csv_sha256,
            input_manifest=self.store.input_manifest(),
        )
        return {
            "export_id": export_id,
            "timestamp": timestamp,
            "filename": csv_path.name,
            "csv_path": display_path(csv_path),
            "csv_sha256": csv_sha256,
            "row_count": len(rows),
            "skipped": skipped,
            "csv_text": csv_text,
        }

    def export_unknown_event_row(
        self,
        point: dict[str, Any],
        *,
        export_id: str,
        exported_at: str,
    ) -> dict[str, Any]:
        """Emit one compact roster row for an event without an accepted relation."""

        row = self.export_row(
            {
                "annotation_id": None,
                "source": None,
                "review_status": None,
                "candidate_type": "unannotated_event",
                "ms_event_id": point.get("ms_event_id"),
                "scan_id": point.get("scan_id"),
                "ms_time_min": point.get("scan_start_time"),
            },
            stage="cell_annotation",
            export_id=export_id,
            exported_at=exported_at,
        )
        row.update(
            {
                "Type": "unknown",
                "annotation_kind": None,
                "review_stage": None,
                "LIF_channel": None,
                "LIF_peak_id": None,
                "residual_sec": None,
                "annotation_id": None,
            }
        )
        return row

    def export_row(self, row: dict[str, Any], *, stage: str, export_id: str, exported_at: str) -> dict[str, Any]:
        candidate_type = self.infer_candidate_type(row)
        is_cell = stage == "cell_annotation" or candidate_type.startswith("cell") or candidate_type == "manual_cell_pair"
        lif_channel = str(row.get("lif_channel") or "")
        if not lif_channel:
            if row.get("g2_peak_id") and is_cell:
                lif_channel = "G2"
            elif row.get("r1_peak_id") and is_cell:
                lif_channel = "R1"
            elif row.get("r2_peak_id") and is_cell:
                lif_channel = "R2"
        prior = self.channel_identity_prior.get(lif_channel, {}) if lif_channel else {}
        identity_prior = str(prior.get("identity_prior") or "")
        layout_row = next(
            (
                item
                for item in self.layout_lif_channels()
                if str(item.get("channel") or "") == lif_channel
            ),
            {},
        )
        if is_cell:
            annotation_kind = "cell_pair"
            evidence_role = "cell_annotation"
            annotation_label = f"{identity_prior} cell" if identity_prior else "cell"
            label_source = "human_accepted_lif_ms_pair+experiment_config_channel_map"
        else:
            annotation_kind = "qc_anchor"
            evidence_role = "qc_anchor"
            annotation_label = "QC"
            label_source = "human_accepted_qc_anchor+experiment_protocol"
        lif_peak_id = row.get("lif_peak_id")
        if not lif_peak_id and is_cell:
            lif_peak_id = row.get("g2_peak_id") or row.get("r1_peak_id") or row.get("r2_peak_id")
        g1_raw = row.get("g1_raw_time_min")
        g2_raw = row.get("g2_raw_time_min")
        r1_raw = row.get("r1_raw_time_min")
        r2_raw = row.get("r2_raw_time_min")
        lif_raw = row.get("lif_raw_time_min")
        if is_cell and lif_raw is not None:
            if lif_channel == "G1":
                g1_raw = lif_raw
            elif lif_channel == "G2":
                g2_raw = lif_raw
            elif lif_channel == "R1":
                r1_raw = lif_raw
            elif lif_channel == "R2":
                r2_raw = lif_raw
        g1_plot = row.get("g1_plot_time_min")
        g2_plot = row.get("g2_plot_time_min")
        r1_plot = row.get("r1_plot_time_min")
        r2_plot = row.get("r2_plot_time_min")
        lif_plot = row.get("lif_plot_time_min")
        if is_cell and lif_plot is not None:
            if lif_channel == "G1":
                g1_plot = lif_plot
            elif lif_channel == "G2":
                g2_plot = lif_plot
            elif lif_channel == "R1":
                r1_plot = lif_plot
            elif lif_channel == "R2":
                r2_plot = lif_plot
        dynamic_peak_ids = qc_anchor_peak_id_map(row) if not is_cell else {}
        anchor_channels = list(row.get("anchor_channels") or dynamic_peak_ids.keys()) if not is_cell else []
        dynamic_raw_times = row.get("lif_anchor_raw_times_min") if isinstance(row.get("lif_anchor_raw_times_min"), dict) else {}
        dynamic_plot_times = row.get("lif_anchor_plot_times_min") if isinstance(row.get("lif_anchor_plot_times_min"), dict) else {}
        g1_peak_id = row.get("g1_peak_id") or (lif_peak_id if is_cell and lif_channel == "G1" else None)
        if dynamic_peak_ids:
            g1_peak_id = dynamic_peak_ids.get("G1")
            g2_value = dynamic_peak_ids.get("G2")
            r1_value = dynamic_peak_ids.get("R1")
            r2_value = dynamic_peak_ids.get("R2")
            g2_raw = dynamic_raw_times.get("G2", g2_raw)
            r1_raw = dynamic_raw_times.get("R1", r1_raw)
            r2_raw = dynamic_raw_times.get("R2", r2_raw)
            g1_raw = dynamic_raw_times.get("G1", g1_raw)
            g2_plot = dynamic_plot_times.get("G2", g2_plot)
            r1_plot = dynamic_plot_times.get("R1", r1_plot)
            r2_plot = dynamic_plot_times.get("R2", r2_plot)
            g1_plot = dynamic_plot_times.get("G1", g1_plot)
        else:
            g2_value = row.get("g2_peak_id")
            r1_value = row.get("r1_peak_id")
            r2_value = row.get("r2_peak_id")

        def qc_channel_value(channel: str, value: Any) -> Any:
            if annotation_kind != "qc_anchor":
                return value
            return value or (MISSING_PEAK_SYMBOL if channel in anchor_channels else None)

        umap1 = None
        umap2 = None
        cell_number = None
        event_id = str(row.get("ms_event_id") or "")
        if self.cell_event_map is not None and stage in {"qc_survey", "cell_annotation"}:
            coordinate_rows = self.cell_event_map[
                self.cell_event_map["ms_event_id"].astype(str).eq(event_id)
            ]
            if not coordinate_rows.empty:
                umap1 = float(coordinate_rows.iloc[0]["UMAP1"])
                umap2 = float(coordinate_rows.iloc[0]["UMAP2"])
                event_positions = np.flatnonzero(
                    self.cell_event_map["ms_event_id"].astype(str).eq(event_id).to_numpy()
                )
                if len(event_positions):
                    width = max(5, len(str(len(self.cell_event_map))))
                    cell_number = f"Cell{int(event_positions[0]) + 1:0{width}d}"
        if cell_number is None and event_id and "event_id" in self.ms_events.columns:
            event_positions = np.flatnonzero(
                self.ms_events["event_id"].astype(str).eq(event_id).to_numpy()
            )
            if len(event_positions):
                width = max(5, len(str(len(self.ms_events))))
                cell_number = f"Cell{int(event_positions[0]) + 1:0{width}d}"

        event_rows = (
            self.ms_events[self.ms_events["event_id"].astype(str).eq(event_id)]
            if event_id and "event_id" in self.ms_events.columns
            else self.ms_events.iloc[0:0]
        )
        event_row = event_rows.iloc[0] if not event_rows.empty else pd.Series(dtype=object)
        scan_id_value = row.get("scan_id")
        if scan_id_value is None:
            scan_id_value = clean_value(event_row.get("scan_id"))
        scan_rows = self.ms_scan.iloc[0:0]
        if scan_id_value is not None and "scan_id" in self.ms_scan.columns:
            scan_rows = self.ms_scan[
                self.ms_scan["scan_id"].astype(str).eq(str(scan_id_value))
            ]
        scan_row = scan_rows.iloc[0] if not scan_rows.empty else pd.Series(dtype=object)
        output_channels = lif_channel
        output_peak_ids = lif_peak_id
        if not is_cell:
            present_anchor_pairs = [
                (channel, peak_id)
                for channel, peak_id in dynamic_peak_ids.items()
                if peak_id and str(peak_id) != MISSING_PEAK_SYMBOL
            ]
            output_channels = ";".join(channel for channel, _ in present_anchor_pairs)
            output_peak_ids = ";".join(str(peak_id) for _, peak_id in present_anchor_pairs)
        # Main CSV Type is a project-owned scientific identity, never a
        # payload/source label.  Historical rows without a configured identity
        # remain useful as generic cells without leaking author annotations.
        type_value = (identity_prior or "cell") if is_cell else "QC"

        return {
            "CellNumber": cell_number,
            "scan_Id": scan_id_value,
            "scan_start_time": row.get("ms_time_min")
            if row.get("ms_time_min") is not None
            else clean_value(event_row.get("time_min")),
            "TIC": clean_value(
                event_row.get("tic_apex")
                if event_row.get("tic_apex") is not None
                else scan_row.get("tic")
            ),
            "PC(34:1)_mz": clean_value(
                scan_row.get("pc34_760_mz_at_max_intensity")
            ),
            "PC(34:1)_intensity": clean_value(event_row.get("pc34_760_apex")),
            "Type": type_value,
            "LIF_channel": output_channels,
            "LIF_peak_id": output_peak_ids,
            "MS_event_id": row.get("ms_event_id"),
            "export_id": export_id,
            "exported_at": exported_at,
            "annotation_id": row.get("annotation_id"),
            "annotation_kind": annotation_kind,
            "review_stage": stage,
            "source": row.get("source"),
            "review_status": row.get("review_status"),
            "candidate_type": candidate_type,
            "confidence_mode": row.get("confidence_mode"),
            "annotation_label": annotation_label,
            "cell_id": row.get("ms_event_id") if is_cell else None,
            "cell_label": annotation_label if is_cell else None,
            "cell_source_channel": lif_channel if is_cell else None,
            "cell_source_peak_id": lif_peak_id if is_cell else None,
            "label_source": label_source,
            "payload_label": row.get("label"),
            "evidence_role": evidence_role,
            "lif_channel": lif_channel,
            "lif_peak_id": lif_peak_id,
            "candidate_channel": lif_channel,
            "lif_detector": layout_row.get("detector"),
            "lif_time_axis": layout_row.get("time_axis"),
            "channel_identity_prior": identity_prior,
            "channel_identity_prior_source": prior.get("identity_prior_source"),
            "channel_identity_prior_file": prior.get("identity_prior_file"),
            "g1_peak_id": qc_channel_value("G1", g1_peak_id),
            "g2_peak_id": qc_channel_value("G2", g2_value),
            "r1_peak_id": qc_channel_value("R1", r1_value),
            "r2_peak_id": qc_channel_value("R2", r2_value),
            "ms_event_id": row.get("ms_event_id"),
            "scan_id": row.get("scan_id"),
            "UMAP1": umap1,
            "UMAP2": umap2,
            "cell_event_map_sha256": (
                self.cell_event_map_sha256()
                if stage in {"qc_survey", "cell_annotation"} and umap1 is not None
                else None
            ),
            "g1_raw_time_min": g1_raw,
            "g2_raw_time_min": g2_raw,
            "r1_raw_time_min": r1_raw,
            "r2_raw_time_min": r2_raw,
            "lif_raw_time_min": lif_raw,
            "ms_time_min": row.get("ms_time_min"),
            "g1_plot_time_min": g1_plot,
            "g2_plot_time_min": g2_plot,
            "r1_plot_time_min": r1_plot,
            "r2_plot_time_min": r2_plot,
            "lif_plot_time_min": lif_plot,
            "ms_plot_time_min": row.get("ms_plot_time_min"),
            "residual_sec": row.get("residual_sec"),
            "abs_residual_sec": row.get("abs_residual_sec"),
            "lif_anchor_count": row.get("lif_anchor_count"),
            "missing_lif_channels": row.get("missing_lif_channels"),
            "complete_anchor_set": row.get("complete_anchor_set"),
            "qc_anchor_channels_json": json.dumps(anchor_channels, ensure_ascii=False, separators=(",", ":")),
            "qc_anchor_peak_ids_json": json.dumps(dynamic_peak_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "qc_anchor_raw_times_json": json.dumps(dynamic_raw_times, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "qc_anchor_plot_times_json": json.dumps(dynamic_plot_times, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "qc_anchor_time_axes_json": json.dumps(row.get("required_time_axes") or [], ensure_ascii=False, separators=(",", ":")),
            "matcher_version": row.get("matcher_version"),
            "acquisition_layout_hash": row.get("acquisition_layout_hash"),
            "candidate_rank": row.get("candidate_rank"),
            "candidate_score": row.get("candidate_score"),
            "selection_reason": row.get("selection_reason"),
            "cross_channel_candidate_conflict": row.get(
                "cross_channel_candidate_conflict"
            ),
            "arbitration_status": row.get("arbitration_status"),
            "arbitration_reason": row.get("arbitration_reason"),
            "cross_channel_alternatives_json": json.dumps(
                row.get("cross_channel_alternatives") or [],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "calibration_segment_id": row.get("calibration_segment_id"),
            "calibration_segment_order": row.get("calibration_segment_order"),
            "calibration_population_label": row.get(
                "calibration_population_label"
            ),
            "calibration_reference_mode": row.get("calibration_reference_mode"),
            "calibration_protocol_hash": row.get("calibration_protocol_hash"),
            "post_qc_strategy_mode": row.get("post_qc_strategy_mode"),
            "post_qc_window_id": row.get("post_qc_window_id"),
            "post_qc_strategy_hash": row.get("post_qc_strategy_hash"),
            "time_model_name": row.get("time_model_name"),
            "time_model_version": row.get("time_model_version"),
            "time_model_status": row.get("time_model_status"),
            "ms_local_delta_sec": row.get("ms_local_delta_sec"),
            "contains_cell_labels": row.get("contains_cell_labels"),
            "input_policy": row.get("input_policy") or "first_principles_preprocessing_tables_only",
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def export_columns(self) -> list[str]:
        return [
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

    def csv_value(self, value: Any) -> Any:
        value = clean_value(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return value

    @classmethod
    def create_project_from_raw_inputs(
        cls,
        *,
        project_dir: Path,
        lif_g2_path: Path | None = None,
        lif_r1_path: Path | None = None,
        lif_r2_path: Path | None = None,
        ms_path: Path,
        g2_identity: str = "Day0",
        r1_identity: str = "Day9",
        r2_identity: str = "Day3",
        raw_input_mode: str = RAW_INPUT_MODE_EXTERNAL,
        lif_inputs: list[dict[str, Any]] | None = None,
        qc_anchor_channels: list[str] | tuple[str, ...] | None = None,
        calibration_protocol: dict[str, Any] | None = None,
        post_qc_strategy: dict[str, Any] | None = None,
        lif_peak_detection: dict[str, Any] | None = None,
        annotation_start_min: float | None = None,
        local_delta_seed_window_min: float = DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN,
        cell_event_map_path: Path | None = None,
        _staging_build: bool = False,
    ) -> "AppData | ProjectPaths":
        project_dir = project_dir.expanduser().resolve()
        mode = normalize_raw_input_mode(raw_input_mode)
        try:
            # The server owns the single active scientific standard.  Validate
            # before inspecting inputs or creating staging artifacts so a
            # forged detector-v1 request is a zero-write failure.
            lif_peak_detection = require_active_lif_peak_detection(
                lif_peak_detection
            )
        except ValueError as exc:
            raise BadRequest(f"LIF 峰识别配置无效：{exc}") from exc
        if lif_inputs is None:
            if not (lif_g2_path and lif_r1_path and lif_r2_path):
                raise BadRequest("必须提供 3 个 LIF 原始文件")
            lif_inputs = [
                {"key": "lif_g2", "path": lif_g2_path, "channel": "G2", "identity_prior": g2_identity},
                {"key": "lif_r1", "path": lif_r1_path, "channel": "R1", "identity_prior": r1_identity},
                {"key": "lif_r2", "path": lif_r2_path, "channel": "R2", "identity_prior": r2_identity},
            ]
            qc_anchor_channels = qc_anchor_channels or ["G2", "R1"]
        source_lif_fingerprints = validate_distinct_lif_input_files(lif_inputs)
        source_raw_paths = {"ms": ms_path.expanduser().resolve()}
        for item in lif_inputs:
            key = str(item.get("key") or "").strip()
            if not key:
                raise BadRequest("每个 LIF 输入必须包含 key")
            source_raw_paths[key] = Path(item["path"]).expanduser().resolve()
        if not IS_FROZEN and project_dir == ROOT:
            raise BadRequest("项目保存路径不能使用当前代码仓库根目录；请新建独立项目目录")
        source_ms_fingerprint = raw_file_fingerprint(source_raw_paths["ms"])
        if not _staging_build:
            if cell_event_map_path is None:
                raise BadRequest(
                    "新项目必须选择单细胞事件坐标 CSV（scan_start_time / UMAP1 / UMAP2）"
                )
            cell_event_map_path = cell_event_map_path.expanduser().resolve()
            raw_file_fingerprint(cell_event_map_path, full_hash_limit_bytes=None)
            existing_outputs = [
                project_dir / CANONICAL_TABLE_PATHS["lif_traces"],
                project_dir / CANONICAL_TABLE_PATHS["lif_peaks"],
                project_dir / CANONICAL_TABLE_PATHS["ms_events"],
                project_dir / CANONICAL_TABLE_PATHS["ms_scan_summary"],
                project_dir / CANONICAL_CELL_EVENT_MAP_PATH,
            ]
            assert_new_project_target_is_clean(project_dir, existing_outputs)
            target_preexisted = project_dir.exists()
            if target_preexisted and any(project_dir.iterdir()):
                raise BadRequest("新项目保存路径必须不存在或为空目录")
            intended_parent = project_dir.parent
            intended_parent.mkdir(parents=True, exist_ok=True)
            staging_dir = intended_parent / f".{project_dir.name}.lma-building-{uuid.uuid4().hex}"
            published = False
            try:
                cls.create_project_from_raw_inputs(
                    project_dir=staging_dir,
                    ms_path=ms_path,
                    raw_input_mode=mode,
                    lif_inputs=lif_inputs,
                    qc_anchor_channels=qc_anchor_channels,
                    calibration_protocol=calibration_protocol,
                    post_qc_strategy=post_qc_strategy,
                    lif_peak_detection=lif_peak_detection,
                    annotation_start_min=annotation_start_min,
                    local_delta_seed_window_min=local_delta_seed_window_min,
                    cell_event_map_path=cell_event_map_path,
                    _staging_build=True,
                )
                commit_staging_project(
                    staging_dir,
                    project_dir,
                    target_preexisted=target_preexisted,
                )
                published = True
                return cls.load(ProjectPaths.for_new_project(project_dir))
            except Exception:
                if staging_dir.exists():
                    remove_staging_project(staging_dir, intended_parent)
                if published and project_dir.exists():
                    rollback_dir = intended_parent / (
                        f".{project_dir.name}.lma-building-rollback-{uuid.uuid4().hex}"
                    )
                    os.replace(project_dir, rollback_dir)
                    remove_staging_project(rollback_dir, intended_parent)
                    if target_preexisted:
                        project_dir.mkdir(parents=False, exist_ok=False)
                raise
        existing_outputs = [
            project_dir / CANONICAL_TABLE_PATHS["lif_traces"],
            project_dir / CANONICAL_TABLE_PATHS["lif_peaks"],
            project_dir / CANONICAL_TABLE_PATHS["ms_events"],
            project_dir / CANONICAL_TABLE_PATHS["ms_scan_summary"],
        ]
        assert_new_project_target_is_clean(project_dir, existing_outputs)

        project_dir.mkdir(parents=True, exist_ok=True)
        lock_dir = (project_dir / CANONICAL_INPUT_MANIFEST_PATH).parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        identities = {
            str(item.get("channel") or "").strip().upper(): str(item.get("identity_prior") or "")
            for item in lif_inputs
        }
        rows, manifest_raw_inputs, acquisition_layout = build_raw_input_project_records(
            project_dir=project_dir,
            raw_paths={**source_raw_paths, "ms": ms_path.expanduser().resolve()},
            raw_input_mode=mode,
            identities=identities,
            lif_inputs=lif_inputs,
            qc_anchor_channels=qc_anchor_channels,
            calibration_protocol=calibration_protocol,
        )
        if calibration_protocol is None:
            effective_protocol = calibration_protocol_from_manifest(
                {"project_schema_version": 2, "acquisition_layout": acquisition_layout},
                {"qc_calibration_end_min": QC_SHIFT_WINDOW_MIN},
            )
        else:
            effective_protocol = normalize_calibration_protocol(
                calibration_protocol,
                acquisition_layout,
                require_confirmed=False,
            )
        if post_qc_strategy is None:
            legacy_channels = list(
                acquisition_layout.get("qc_anchor_channels")
                or effective_protocol["reference_channels"]
            )
            effective_post_qc_strategy = normalize_post_qc_strategy(
                {"mode": "signature", "reference_channels": legacy_channels},
                acquisition_layout,
            )
        else:
            effective_post_qc_strategy = normalize_post_qc_strategy(
                post_qc_strategy, acquisition_layout
            )
        try:
            effective_lif_peak_detection = require_active_lif_peak_detection(
                lif_peak_detection
            )
        except ValueError as exc:
            raise BadRequest(f"LIF 峰识别配置无效：{exc}") from exc
        effective_annotation_start_min = float(
            DEFAULT_ANNOTATION_START_MIN
            if annotation_start_min is None
            else annotation_start_min
        )
        protocol_end_min = max(
            float(row["end_min"]) for row in effective_protocol["segments"]
        )
        validate_post_qc_strategy_timing(
            effective_post_qc_strategy,
            protocol_end_min,
        )
        if not math.isfinite(effective_annotation_start_min) or effective_annotation_start_min < protocol_end_min:
            raise BadRequest("事件标注起点必须晚于或等于最后一个校准参考段")
        if not math.isfinite(float(local_delta_seed_window_min)) or float(local_delta_seed_window_min) <= 0:
            raise BadRequest("后段预校准取证范围必须大于 0 min")
        missing_cell_labels = [
            row["channel"]
            for row in acquisition_layout["lif_channels"]
            if bool(row.get("use_for_cell_annotation"))
            and not str(row.get("identity_prior") or "").strip()
        ]
        if missing_cell_labels:
            raise BadRequest(
                "用于细胞标注的 LIF 通道必须填写样本标签: "
                + ", ".join(missing_cell_labels)
            )
        if mode == RAW_INPUT_MODE_COPY:
            for key, source_path in source_raw_paths.items():
                destination = project_dir / str(manifest_raw_inputs[key]["path"])
                if destination.exists():
                    raise BadRequest(f"项目目录 raw_inputs 中已有同名文件: {display_path(destination, project_dir)}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
        raw_data_dir = project_dir / "raw_inputs"
        effective_lif_inputs = []
        for row in rows:
            if row["input_class"] != "raw_lif_trace":
                continue
            row_path = Path(row["path"])
            full_path = row_path if row_path.is_absolute() else project_dir / row_path
            effective_lif_inputs.append(
                {
                    "key": str(row["input_id"]),
                    "channel": str(row["channel"]),
                    "path": full_path,
                }
            )
        effective_lif_fingerprints = validate_distinct_lif_input_files(effective_lif_inputs)
        if mode == RAW_INPUT_MODE_COPY:
            changed_during_copy = []
            for source_item, effective_item in zip(lif_inputs, effective_lif_inputs):
                source_key = os.path.normcase(str(Path(source_item["path"]).expanduser().resolve()))
                effective_key = os.path.normcase(str(Path(effective_item["path"]).resolve()))
                if source_lif_fingerprints[source_key]["sha256"] != effective_lif_fingerprints[effective_key]["sha256"]:
                    changed_during_copy.append(str(source_item.get("channel") or effective_item["channel"]))
            if changed_during_copy:
                raise BadRequest(
                    "LIF 原始文件在复制期间发生变化，请确认文件不再被写入后重新创建项目: "
                    + ", ".join(changed_during_copy)
                )
        locked_rows = []
        effective_ms_path: Path | None = None
        effective_ms_fingerprint: dict[str, Any] | None = None
        input_id_to_key = {str(row["input_id"]): key for key, row in zip([str(item.get("key")) for item in lif_inputs], rows) if row["input_class"] == "raw_lif_trace"}
        input_id_to_key["ms_raw_txt"] = "ms"
        for row in rows:
            raw_path = Path(row["path"])
            full_path = raw_path if raw_path.is_absolute() else project_dir / raw_path
            path_key = os.path.normcase(str(full_path.resolve()))
            if row["input_class"] == "raw_lif_trace":
                fp = effective_lif_fingerprints[path_key]
            else:
                fp = raw_file_fingerprint(full_path)
                effective_ms_path = full_path
                effective_ms_fingerprint = fp
            manifest_raw_inputs[input_id_to_key[str(row["input_id"])]].update(fp)
            locked_rows.append(
                {
                    **row,
                    "allowed_stage": "main annotation preprocessing",
                    **fp,
                }
            )
        allowed = pd.DataFrame(locked_rows)
        if mode == RAW_INPUT_MODE_COPY:
            if effective_ms_fingerprint is None:
                raise BadRequest("项目输入清单缺少复制后的 MS 原始文件")
            copy_stability_keys = (
                "size_bytes",
                "mtime_iso",
                "head_sha256_1mb",
                "tail_sha256_1mb",
                "sha256",
            )
            if any(
                source_ms_fingerprint.get(key) not in (None, "")
                and effective_ms_fingerprint.get(key)
                != source_ms_fingerprint.get(key)
                for key in copy_stability_keys
            ):
                raise BadRequest(
                    "MS 原始文件在复制期间发生变化；请确认采集或导出已经结束，"
                    "再在新的空目录中创建项目"
                )
        allowed.to_csv(project_dir / CANONICAL_INPUT_MANIFEST_PATH, index=False)
        preprocessing_protocol = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "storage_layout": canonical_storage_layout_manifest_entry(),
            "calibration_protocol": effective_protocol,
            "post_qc_strategy": effective_post_qc_strategy,
            "lif_peak_detection": effective_lif_peak_detection,
            "annotation_config": {
                "qc_calibration_end_min": protocol_end_min,
                "annotation_start_min": effective_annotation_start_min,
                "local_delta_seed_window_min": float(local_delta_seed_window_min),
            },
        }
        (project_dir / CANONICAL_PROJECT_PROTOCOL_PATH).write_text(
            json.dumps(preprocessing_protocol, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (project_dir / CANONICAL_PREPROCESSING_REPORT_PATH).write_text(
            "\n".join(
                [
                    "# 标注项目导入记录",
                    "",
                    f"导入时间：`{now_iso()}`",
                    "",
                    f"- 本导入只锁定 {len(lif_inputs)} 个用户配置的 LIF 原始文件和 1 个 MS 原始文件。",
                    "- 不读取作者标签、人工结果或任何下游注释作为峰识别依据。",
                    "- 生成的中间表用于浏览器人工标注；后续时间校正和 annotation 由软件内人工审核完成。",
                    f"- 前段校准参考通道：`{' + '.join(effective_protocol['reference_channels'])}`。",
                    (
                        f"- 前段参考段数量：`{len(effective_protocol['segments'])}`；"
                        + (
                            "边界已由用户确认。"
                            if effective_protocol.get("boundaries_confirmed")
                            else "当前为项目级待确认草稿，只可用于峰形浏览，尚未用于时间对齐。"
                        )
                    ),
                    "- 后段 QC 策略：`"
                    + {
                        "disabled": "Off",
                        "signature": "QC signature",
                        "scheduled_windows": "Scheduled windows",
                    }.get(effective_post_qc_strategy["mode"], "按项目设置")
                    + "`。",
                    (
                        "- LIF 峰检测："
                        "使用当前自适应双层峰识别标准；"
                        "weak 峰仅供人工复核，不参与自动匹配或时间模型训练。"
                    ),
                    f"- 事件标注起点：`{effective_annotation_start_min:g} min`。",
                    "",
                    "## 原始输入",
                    "",
                    "```csv",
                    allowed[["input_id", "path", "input_class", "channel", "label", "detector", "size_bytes"]].to_csv(index=False).strip(),
                    "```",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (project_dir / CANONICAL_PROJECT_README_PATH).write_text(
            "\n".join(
                [
                    "# LMA Studio 项目",
                    "",
                    "这个文件夹包含可直接打开的标注项目。分享时请压缩并发送整个文件夹；",
                    "项目文件夹可以整体重命名或移动，打开时选择新的项目根目录即可。",
                    "复制、重命名或压缩项目前，请先关闭 LMA Studio，避免数据库仍在写入。",
                    (
                        "当前项目已复制原始输入，可在 `raw_inputs/` 中保留重跑所需文件。"
                        if mode == RAW_INPUT_MODE_COPY
                        else "当前项目不包含原始 LIF/MS 文件；接收方可以打开、查看、继续标注和导出，"
                        "若要从原始数据重跑前处理，需另行提供原始 LIF/MS 文件。"
                    ),
                    "",
                    "## 目录说明",
                    "",
                    "- `data/`：软件显示与匹配所需的 LIF、MS 和事件坐标数据。",
                    "- `annotations/`：人工标注、时间模型和导出结果。",
                    "- `provenance/`：原始输入清单、项目参数和前处理记录，用于复现与审计。",
                    "- `diagnostics/`：峰识别与 MS event 识别的质量检查图表；不代表细胞或 QC 身份。",
                    "- `raw_inputs/`：仅在创建项目时选择“复制原始文件”才会出现。",
                    "- `lifms_project.json`：项目索引与完整性绑定。",
                    "",
                    "## 请勿这样做",
                    "",
                    "不要单独移动、重命名、替换或编辑上述目录内的文件；这会破坏项目完整性绑定。",
                    "若只想更改项目名称，请重命名最外层的整个项目文件夹。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        for diagnostics_dir in (
            CANONICAL_LIF_DIAGNOSTICS_DIR,
            CANONICAL_MS_DIAGNOSTICS_DIR,
        ):
            (project_dir / diagnostics_dir).mkdir(parents=True, exist_ok=True)

        scripts = [
            ("run_v3_01_lif_trace_physical_qc.py", "LIF 峰识别与质量检查"),
            ("run_v3_02_ms_event_calling.py", "MS event 识别与质量检查"),
        ]
        log_lines = []
        for script, display_name in scripts:
            try:
                script_output = run_preprocessing_script(script, project_dir)
                log_lines.append(f"## {display_name}\n{script_output}")
            except Exception as exc:
                log_lines.append(f"## {display_name}\n{type(exc).__name__}: {exc}")
                (project_dir / CANONICAL_PREPROCESSING_LOG_PATH).write_text("\n\n".join(log_lines), encoding="utf-8")
                raise BadRequest(f"{display_name}失败：\n{str(exc)[-4000:]}") from exc
        (project_dir / CANONICAL_PREPROCESSING_LOG_PATH).write_text("\n\n".join(log_lines), encoding="utf-8")
        if effective_ms_path is None or effective_ms_fingerprint is None:
            raise BadRequest("项目输入清单缺少可验证的 MS 原始文件")
        final_ms_fingerprint = raw_file_fingerprint(effective_ms_path)
        ms_stability_keys = (
            "size_bytes",
            "mtime_iso",
            "head_sha256_1mb",
            "tail_sha256_1mb",
            "sha256",
        )
        if any(
            effective_ms_fingerprint.get(key) not in (None, "")
            and final_ms_fingerprint.get(key) != effective_ms_fingerprint.get(key)
            for key in ms_stability_keys
        ):
            raise BadRequest(
                "MS 原始文件在前处理期间发生变化；请确认采集或导出已经结束，"
                "再在新的空目录中创建项目"
            )
        final_fingerprints = validate_distinct_lif_input_files(effective_lif_inputs)
        changed_channels = [
            str(item["channel"])
            for item in effective_lif_inputs
            if final_fingerprints[os.path.normcase(str(Path(item["path"]).resolve()))]["sha256"]
            != effective_lif_fingerprints[os.path.normcase(str(Path(item["path"]).resolve()))]["sha256"]
        ]
        if changed_channels:
            raise BadRequest(
                "LIF 原始文件在前处理期间发生变化，请确认文件不再被写入后重新创建项目: "
                + ", ".join(changed_channels)
            )
        intermediate_tables = {
            "lif_traces": {"path": project_relative_or_absolute(existing_outputs[0], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[0], full_hash_limit_bytes=None)},
            "lif_peaks": {"path": project_relative_or_absolute(existing_outputs[1], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[1], full_hash_limit_bytes=None)},
            "ms_events": {"path": project_relative_or_absolute(existing_outputs[2], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[2], full_hash_limit_bytes=None)},
            "ms_scan_summary": {"path": project_relative_or_absolute(existing_outputs[3], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[3], full_hash_limit_bytes=None)},
        }
        if cell_event_map_path is None:
            raise BadRequest("内部错误：staging 项目缺少 cell event map source")
        try:
            canonical_map, map_import_metadata = import_cell_event_map(
                cell_event_map_path,
                pd.read_parquet(existing_outputs[2]),
                tolerance_sec=DEFAULT_MATCH_TOLERANCE_SEC,
            )
            canonical_path = project_dir / CANONICAL_CELL_EVENT_MAP_PATH
            write_canonical_map(canonical_map, canonical_path)
            map_manifest_entry = cell_event_map_manifest_entry(
                canonical_path=canonical_path,
                project_dir=project_dir,
                import_metadata=map_import_metadata,
            )
        except CellEventMapError as exc:
            raise BadRequest(f"单细胞 event map 导入失败: {exc}") from exc
        write_project_manifest(
            project_dir=project_dir,
            raw_input_mode=mode,
            raw_inputs=manifest_raw_inputs,
            channel_identity_prior=identities,
            intermediate_tables=intermediate_tables,
            acquisition_layout=acquisition_layout,
            cell_event_map=map_manifest_entry,
            calibration_protocol=effective_protocol,
            post_qc_strategy=effective_post_qc_strategy,
            lif_peak_detection=effective_lif_peak_detection,
            annotation_config={
                "annotation_start_min": effective_annotation_start_min,
                "local_delta_seed_window_min": float(local_delta_seed_window_min),
            },
            annotation_db_path=CANONICAL_ANNOTATION_DB_PATH,
            storage_layout=canonical_storage_layout_manifest_entry(),
        )

        project = ProjectPaths.for_new_project(project_dir)
        if _staging_build:
            resolved = validate_staged_project_artifacts(project)
            staged_app = cls.load(resolved)
            expected_binding = project_table_binding(
                intermediate_table_fingerprints(staged_app.project)
            )
            stored_binding = read_sqlite_project_binding(
                staged_app.project.annotation_db_path
            )
            if stored_binding != expected_binding:
                raise BadRequest("staging annotation.sqlite 未完成中间表绑定")
            (project_dir / CANONICAL_EXPORTS_DIR).mkdir(parents=True, exist_ok=True)
            return staged_app.project
        return cls.load(project)

    def channel_shift_sec(self, channel: str, time_mode: str) -> float:
        if time_mode != "aligned":
            return 0.0
        channel = str(channel).strip().upper()
        layout = normalize_acquisition_layout(self.acquisition_layout)
        axis = layout["channel_time_axes"].get(channel, default_time_axis_for_channel(channel))
        axis_shifts = self.alignment.get("axis_shifts_sec")
        if isinstance(axis_shifts, dict) and axis in axis_shifts:
            return float(axis_shifts[axis])
        if axis == "green_axis":
            return float(self.alignment.get("green_to_ms_shift_sec", 0.0))
        if axis == "red_axis":
            return float(self.alignment.get("red_to_ms_shift_sec", 0.0))
        return 0.0

    def layout_lif_channels(self) -> list[dict[str, Any]]:
        return normalize_acquisition_layout(self.acquisition_layout)["lif_channels"]

    def cell_annotation_channels(self) -> list[str]:
        return [
            str(row["channel"])
            for row in self.layout_lif_channels()
            if bool(row.get("use_for_cell_annotation", True))
        ]

    def cell_label_for_channel(self, channel: str) -> str:
        channel = str(channel)
        prior = self.channel_identity_prior.get(channel, {})
        identity = str(prior.get("identity_prior") or "").strip()
        if not identity:
            layout_row = next(
                (row for row in self.layout_lif_channels() if str(row.get("channel")) == channel),
                {},
            )
            identity = str(layout_row.get("identity_prior") or "").strip()
        return f"{identity} cell" if identity else "cell"

    def aligned_channel_shifts_sec(self) -> dict[str, float]:
        return {channel: self.channel_shift_sec(channel, "aligned") for channel in self.cell_annotation_channels()}

    def project_config(self) -> dict[str, Any]:
        return request_cached_read(
            self,
            "project_config",
            self._project_config_uncached,
        )

    def _project_config_uncached(self) -> dict[str, Any]:
        config = self.store.project_config()
        protocol_manifest = self.manifest or {
            "project_schema_version": 2,
            "acquisition_layout": self.acquisition_layout,
        }
        protocol = calibration_protocol_from_manifest(
            protocol_manifest,
            {
                **config,
                **(
                    {"calibration_protocol": self.calibration_protocol}
                    if self.calibration_protocol is not None and "calibration_protocol" not in config
                    else {}
                ),
            },
        )
        strategy = post_qc_strategy_from_manifest(
            protocol_manifest,
            {
                **config,
                **(
                    {"post_qc_strategy": self.post_qc_strategy}
                    if self.post_qc_strategy is not None and "post_qc_strategy" not in config
                    else {}
                ),
            },
        )
        return {
            "qc_calibration_end_min": float(config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN)),
            "sample_valve_switch_min": float(config.get("sample_valve_switch_min", DEFAULT_SAMPLE_VALVE_SWITCH_MIN)),
            "annotation_start_min": float(config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)),
            "local_delta_seed_window_min": float(
                config.get("local_delta_seed_window_min", DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN)
            ),
            "qc_alignment_model": config.get(QC_ALIGNMENT_MODEL_KEY),
            "calibration_protocol": protocol,
            "calibration_protocol_hash": calibration_protocol_hash(
                protocol, self.acquisition_layout
            ),
            "post_qc_strategy": strategy,
            "post_qc_strategy_hash": post_qc_strategy_hash(
                strategy, self.acquisition_layout
            ),
            "lif_peak_detection": copy.deepcopy(
                self.active_lif_peak_detection()
            ),
            "lif_peak_detection_hash": lif_peak_detection_hash(
                self.active_lif_peak_detection()
            ),
        }

    def active_time_model(self) -> dict[str, Any]:
        return request_cached_read(
            self,
            "active_time_model",
            self._active_time_model_uncached,
        )

    def _active_time_model_uncached(self) -> dict[str, Any]:
        model = self.store.active_time_model()
        if model:
            return model
        config = self.project_config()
        if not bool(config.get("calibration_protocol", {}).get("boundaries_confirmed")):
            return {
                "time_model_version": "",
                "status": "calibration_boundaries_unconfirmed",
                "base_model_name": str(self.alignment.get("model") or "calibration_draft"),
                "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
                "sample_valve_switch_min": float(config["sample_valve_switch_min"]),
                "annotation_start_min": float(config["annotation_start_min"]),
                "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
                "ms_local_delta_sec": 0.0,
                "contains_cell_labels": False,
                "max_training_time_min": float(config["annotation_start_min"]),
                "evidence_count": 0,
                "unique_match_count": 0,
                "conflict_count": 0,
                "median_abs_residual_sec": None,
                "p90_abs_residual_sec": None,
                "acquisition_layout_hash": str(
                    self.alignment.get("acquisition_layout_hash") or ""
                ),
                "calibration_protocol_hash": str(
                    config.get("calibration_protocol_hash") or ""
                ),
                "persisted": False,
            }
        return self.store.ensure_draft_time_model(
            str(self.alignment["model"]),
            str(self.alignment.get("acquisition_layout_hash") or ""),
            calibration_protocol_hash_value=str(
                self.alignment.get("calibration_protocol_hash")
                or config.get("calibration_protocol_hash")
                or ""
            ),
            allow_unhashed_legacy_binding=bool(
                config.get("calibration_protocol", {}).get("compatibility_mode")
            ),
        )

    def frozen_time_model(self) -> dict[str, Any] | None:
        model = self.active_time_model()
        return model if str(model.get("status")) == "frozen" else None

    def qc_evidence_change_requires_invalidation(
        self,
        row: dict[str, Any],
        *,
        previous_review_status: str | None,
        new_review_status: str | None,
    ) -> bool:
        return bool(
            self.store.qc_alignment_model()
            and self.annotation_review_stage(row) == "qc_calibration"
            and (str(previous_review_status) == "accepted")
            != (str(new_review_status) == "accepted")
        )

    def require_qc_evidence_invalidation_confirmation(
        self,
        row: dict[str, Any],
        *,
        clear_qc_alignment_model: bool,
        previous_review_status: str | None,
        new_review_status: str | None,
    ) -> bool:
        required = self.qc_evidence_change_requires_invalidation(
            row,
            previous_review_status=previous_review_status,
            new_review_status=new_review_status,
        )
        if required and not clear_qc_alignment_model:
            raise BadRequest(
                "修改 QC 校正证据会清除已应用的 QC 对齐和下游 time model；请确认后重新重算 QC 对齐"
            )
        return required

    def require_confirmed_calibration(self, action: str) -> None:
        protocol = self.project_config().get("calibration_protocol") or {}
        if bool(protocol.get("boundaries_confirmed")):
            return
        raise BadRequest(
            f"参考段边界尚未全部确认，不能{action}。"
            "请先查看原始峰形，再到“配置”确认每个参考段边界。"
        )

    def invalidate_qc_model_request_reads(self) -> None:
        """Expose a QC-model invalidation immediately inside the current request."""

        invalidate_request_cached_reads(
            self,
            "project_config",
            "active_time_model",
        )

    def reset_to_automatic_qc_alignment(self) -> None:
        self.require_confirmed_calibration("重算前段校准")
        config = self.project_config()
        alignment = estimate_shift_alignment(
            self.lif_peaks,
            self.ms_events,
            float(config["qc_calibration_end_min"]),
            acquisition_layout=self.acquisition_layout,
            calibration_protocol=(
                None
                if bool(config.get("calibration_protocol", {}).get("compatibility_mode"))
                else config.get("calibration_protocol")
            ),
        )
        object.__setattr__(self, "alignment", alignment)

    def qc_alignment_refit_preview(self) -> dict[str, Any]:
        self.require_confirmed_calibration("预览前段 QC 对齐")
        config = self.project_config()
        return accepted_qc_alignment_refit(
            self.lif_peaks,
            self.ms_events,
            self.store.records(),
            acquisition_layout=self.acquisition_layout,
            calibration_protocol=(
                None
                if bool((self.calibration_protocol or {}).get("compatibility_mode"))
                else self.project_config().get("calibration_protocol")
            ),
            qc_calibration_end_min=float(config["qc_calibration_end_min"]),
            current_axis_shifts_sec=self.alignment.get("axis_shifts_sec"),
        )

    def save_qc_alignment_refit(
        self,
        expected_preview_hash: str,
        *,
        clear_frozen_time_model: bool = False,
    ) -> dict[str, Any]:
        preview = self.qc_alignment_refit_preview()
        if not expected_preview_hash or expected_preview_hash != str(preview.get("preview_hash") or ""):
            raise BadRequest("QC anchor 证据已变化，当前预览已过期；请重新预览后再应用")
        config = self.project_config()
        automatic_alignment = estimate_shift_alignment(
            self.lif_peaks,
            self.ms_events,
            float(config["qc_calibration_end_min"]),
            acquisition_layout=self.acquisition_layout,
            calibration_protocol=(
                None
                if bool(config.get("calibration_protocol", {}).get("compatibility_mode"))
                else config.get("calibration_protocol")
            ),
        )
        candidate_alignment = apply_qc_alignment_model(
            automatic_alignment,
            self.lif_peaks,
            self.ms_events,
            qc_calibration_end_min=float(config["qc_calibration_end_min"]),
            acquisition_layout=self.acquisition_layout,
            model=preview,
            calibration_protocol=(
                None
                if bool((self.calibration_protocol or {}).get("compatibility_mode"))
                else config.get("calibration_protocol")
            ),
        )
        draft_time_model = {
            "time_model_version": f"tm_{uuid.uuid4().hex[:12]}",
            "status": "draft",
            "base_model_name": str(candidate_alignment["model"]),
            "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
            "sample_valve_switch_min": float(config["sample_valve_switch_min"]),
            "annotation_start_min": float(config["annotation_start_min"]),
            "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
            "ms_local_delta_sec": 0.0,
            "contains_cell_labels": False,
            "max_training_time_min": float(config["annotation_start_min"])
            + float(config["local_delta_seed_window_min"]),
            "evidence_count": 0,
            "unique_match_count": 0,
            "conflict_count": 0,
            "median_abs_residual_sec": None,
            "p90_abs_residual_sec": None,
            "method": "default_zero_delta_after_qc_alignment_refit",
            "residual_summary": {},
            "acquisition_layout_hash": candidate_alignment.get("acquisition_layout_hash"),
            "calibration_protocol_hash": config.get("calibration_protocol_hash"),
        }
        stored_model = self.store.save_qc_alignment_model(
            preview,
            clear_frozen_time_model=clear_frozen_time_model,
            draft_time_model_payload=draft_time_model,
        )
        candidate_alignment["qc_alignment_model"] = stored_model
        object.__setattr__(self, "alignment", candidate_alignment)
        time_model = self.store.active_time_model()
        if not time_model or str(time_model.get("status")) != "draft":
            raise RuntimeError("QC 对齐事务没有创建 active draft time model")
        return {
            "qc_alignment_model": stored_model,
            "alignment": candidate_alignment,
            "project_config": self.project_config(),
            "time_model": time_model,
            "warning": "",
        }

    def ms_shift_sec_at(self, time_min: float, time_mode: str, *, require_frozen: bool = False) -> float:
        if time_mode != "aligned":
            return 0.0
        model = self.active_time_model()
        if require_frozen and str(model.get("status")) != "frozen":
            return 0.0
        if float(time_min) < float(model.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)):
            return 0.0
        return float(model.get("ms_local_delta_sec", 0.0) or 0.0)

    def time_model_payload_fields(self) -> dict[str, Any]:
        model = self.active_time_model()
        payload = {
            "time_model_name": str(model.get("base_model_name", self.alignment["model"])),
            "time_model_version": str(model.get("time_model_version", "")),
            "time_model_status": str(model.get("status", "draft")),
            "contains_cell_labels": bool(model.get("contains_cell_labels", False)),
            "ms_local_delta_sec": float(model.get("ms_local_delta_sec", 0.0) or 0.0),
            "acquisition_layout_hash": model.get("acquisition_layout_hash") or self.alignment.get("acquisition_layout_hash"),
            "calibration_protocol_hash": model.get("calibration_protocol_hash")
            or self.alignment.get("calibration_protocol_hash")
            or self.project_config().get("calibration_protocol_hash"),
        }
        if self.cell_event_map is not None:
            payload["cell_event_map_sha256"] = self.cell_event_map_sha256()
        return payload

    def local_delta_anchor_pair_kwargs(self, config: dict[str, Any]) -> dict[str, Any]:
        protocol = config.get("calibration_protocol") or self.calibration_protocol or {}
        if not bool(protocol.get("compatibility_mode")):
            # New projects estimate the downstream MS delta from unlabeled peak
            # topology across every cell-enabled channel.  Front reference
            # populations are deliberately not reused as downstream QC identity
            # labels: sequential G1/G2 segments need not co-occur after the
            # annotation boundary.
            return {}
        pair_offset = self.alignment.get("qc_groups", {}).get("lif_anchor_b_minus_anchor_a_offset_sec")
        if pair_offset is None:
            pair_offset = self.alignment.get("qc_groups", {}).get("lif_r1_minus_g2_offset_sec")
        payload = {
            "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
            "axis_shifts_sec": self.alignment.get("axis_shifts_sec"),
            "channel_time_axes": self.alignment.get("channel_time_axes"),
            "qc_anchor_channels": self.alignment.get("qc_anchor_channels"),
        }
        if pair_offset is not None:
            payload["pair_offset_sec"] = float(pair_offset)
        return payload

    def local_delta_preview(self, delta_sec: float | None = None) -> dict[str, Any]:
        self.require_confirmed_calibration("预览后段 delta")
        config = self.project_config()
        model = self.active_time_model()
        delta = float(model.get("ms_local_delta_sec", 0.0) if delta_sec is None else delta_sec)
        if abs(delta) > LOCAL_DELTA_MAX_ABS_SEC:
            raise BadRequest(f"ms local delta absolute value must be <= {LOCAL_DELTA_MAX_ABS_SEC} sec")
        preview = local_delta_preview_evidence(
            self.lif_peaks,
            self.ms_events,
            annotation_start_min=float(config["annotation_start_min"]),
            seed_window_min=float(config["local_delta_seed_window_min"]),
            green_shift_sec=float(self.alignment["green_to_ms_shift_sec"]),
            red_shift_sec=float(self.alignment["red_to_ms_shift_sec"]),
            ms_delta_sec=delta,
            channel_shifts_sec=self.aligned_channel_shifts_sec(),
            **self.local_delta_anchor_pair_kwargs(config),
        )
        return {
            **preview,
            "project_config": config,
            "time_model": model,
            "seed_start_min": float(config["annotation_start_min"]),
            "seed_end_min": float(config["annotation_start_min"]) + float(config["local_delta_seed_window_min"]),
            "contains_cell_labels": False,
        }

    def estimate_local_delta_model(self) -> dict[str, Any]:
        self.require_confirmed_calibration("估计后段 delta")
        config = self.project_config()
        result = estimate_local_delta_shift(
            self.lif_peaks,
            self.ms_events,
            annotation_start_min=float(config["annotation_start_min"]),
            seed_window_min=float(config["local_delta_seed_window_min"]),
            green_shift_sec=float(self.alignment["green_to_ms_shift_sec"]),
            red_shift_sec=float(self.alignment["red_to_ms_shift_sec"]),
            channel_shifts_sec=self.aligned_channel_shifts_sec(),
            **self.local_delta_anchor_pair_kwargs(config),
        )
        recommendation_status = str(result.get("recommendation_status", "recommended"))
        if recommendation_status != "recommended":
            if recommendation_status == "ambiguous":
                raise BadRequest("自动估计存在多个近似最优平移，请结合预览使用滑块人工确认")
            raise BadRequest("自动估计的 QC anchor 证据不足，请扩大后段预校准取证范围或使用滑块人工确认")
        payload = {
            **self.active_time_model(),
            "time_model_version": str(self.active_time_model().get("time_model_version")),
            "status": "draft",
            "base_model_name": str(self.alignment["model"]),
            "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
            "sample_valve_switch_min": float(config["sample_valve_switch_min"]),
            "annotation_start_min": float(config["annotation_start_min"]),
            "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
            "ms_local_delta_sec": float(result["delta_sec"]),
            "contains_cell_labels": False,
            "max_training_time_min": float(config["annotation_start_min"]) + float(config["local_delta_seed_window_min"]),
            "evidence_count": int(result["evidence_count"]),
            "unique_match_count": int(result["unique_match_count"]),
            "conflict_count": int(result["conflict_count"]),
            "median_abs_residual_sec": result["median_abs_residual_sec"],
            "p90_abs_residual_sec": result["p90_abs_residual_sec"],
            "method": result["method"],
            "matcher_version": result.get("matcher_version", self.alignment.get("matcher_version")),
            "recommendation_status": recommendation_status,
            "complete_anchor_set_count": int(result.get("complete_anchor_set_count", result["unique_match_count"])),
            "acquisition_layout_hash": self.alignment.get("acquisition_layout_hash"),
            "calibration_protocol_hash": config.get("calibration_protocol_hash"),
            "residual_summary": {
                "median_abs_residual_sec": result["median_abs_residual_sec"],
                "p90_abs_residual_sec": result["p90_abs_residual_sec"],
            },
            "evidence_preview": result["evidence"][:50],
            "search_range_sec": result["search_range_sec"],
            "search_step_sec": result["search_step_sec"],
            "match_tolerance_sec": result["match_tolerance_sec"],
        }
        return self.store.upsert_time_model(payload, action="estimate_local_delta_from_unlabeled_topology")

    def estimate_local_delta_preview(self) -> dict[str, Any]:
        self.require_confirmed_calibration("估计后段 delta")
        config = self.project_config()
        result = estimate_local_delta_shift(
            self.lif_peaks,
            self.ms_events,
            annotation_start_min=float(config["annotation_start_min"]),
            seed_window_min=float(config["local_delta_seed_window_min"]),
            green_shift_sec=float(self.alignment["green_to_ms_shift_sec"]),
            red_shift_sec=float(self.alignment["red_to_ms_shift_sec"]),
            channel_shifts_sec=self.aligned_channel_shifts_sec(),
            **self.local_delta_anchor_pair_kwargs(config),
        )
        preview = self.local_delta_preview(float(result["delta_sec"]))
        return {
            **preview,
            "method": result["method"],
            "search_range_sec": result["search_range_sec"],
            "search_step_sec": result["search_step_sec"],
            "match_tolerance_sec": result["match_tolerance_sec"],
            "recommendation_status": result.get("recommendation_status", "recommended"),
            "runner_up": result.get("runner_up"),
            "complete_anchor_set_count": result.get("complete_anchor_set_count"),
        }

    def update_local_delta_draft(self, delta_sec: float) -> dict[str, Any]:
        self.require_confirmed_calibration("保存后段 delta")
        model = self.active_time_model()
        if str(model.get("status")) == "frozen":
            model = {**model, "time_model_version": f"tm_{uuid.uuid4().hex[:12]}", "status": "draft"}
        preview = self.local_delta_preview(delta_sec)
        payload = {
            **model,
            "status": "draft",
            "ms_local_delta_sec": float(delta_sec),
            "contains_cell_labels": False,
            "max_training_time_min": preview["seed_end_min"],
            "evidence_count": int(preview["evidence_count"]),
            "unique_match_count": int(preview["unique_match_count"]),
            "conflict_count": int(preview["conflict_count"]),
            "median_abs_residual_sec": preview["median_abs_residual_sec"],
            "p90_abs_residual_sec": preview["p90_abs_residual_sec"],
            "method": "manual_slider_unlabeled_topology_preview",
            "evidence_preview": preview["evidence"][:50],
            "acquisition_layout_hash": self.alignment.get("acquisition_layout_hash"),
            "calibration_protocol_hash": self.project_config().get(
                "calibration_protocol_hash"
            ),
        }
        return self.store.upsert_time_model(payload, action="manual_update_local_delta_draft")

    def freeze_local_delta_model(self) -> dict[str, Any]:
        self.require_confirmed_calibration("冻结后段 time model")
        model = self.active_time_model()
        if bool(model.get("contains_cell_labels", False)):
            raise BadRequest("Cannot freeze a time model that contains cell labels")
        status = "frozen"
        records = self.store.records()
        active_version = str(model.get("time_model_version") or "")
        first_cell = [
            row
            for row in records
            if str(row.get("candidate_type", "")).startswith("cell")
            and str(row.get("review_status")) == "accepted"
            and (
                not str(row.get("time_model_version") or "")
                or str(row.get("time_model_version") or "") == active_version
            )
        ]
        if first_cell:
            status = "exploratory"
        payload = {**model, "status": status, "contains_cell_labels": False}
        return self.store.upsert_time_model(payload, action=f"freeze_local_delta_{status}")

    def update_project_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        clear_frozen_time_model = bool(updates.get("clear_frozen_time_model"))
        clear_qc_alignment_model = bool(updates.get("clear_qc_alignment_model"))
        current_config = self.project_config()
        normalized_updates = copy.deepcopy(updates)
        current_protocol = normalize_calibration_protocol(
            current_config["calibration_protocol"],
            self.acquisition_layout,
            require_confirmed=False,
        )
        proposed_protocol = current_protocol
        if "calibration_protocol" in updates:
            proposed_protocol = normalize_calibration_protocol(
                updates.get("calibration_protocol"),
                self.acquisition_layout,
                require_confirmed=False,
            )
            persist_protocol = True
            if (
                current_protocol.get("compatibility_mode")
                and proposed_protocol.get("compatibility_mode")
            ):
                current_semantics = copy.deepcopy(current_protocol)
                proposed_semantics = copy.deepcopy(proposed_protocol)
                current_semantics.pop("compatibility_mode", None)
                proposed_semantics.pop("compatibility_mode", None)
                if proposed_semantics == current_semantics:
                    persist_protocol = False
                else:
                    proposed_protocol.pop("compatibility_mode", None)
                    proposed_protocol = normalize_calibration_protocol(
                        proposed_protocol,
                        self.acquisition_layout,
                        require_confirmed=False,
                    )
            if persist_protocol:
                normalized_updates["calibration_protocol"] = proposed_protocol
            else:
                normalized_updates.pop("calibration_protocol", None)
            normalized_updates["qc_calibration_end_min"] = max(
                float(row["end_min"]) for row in proposed_protocol["segments"]
            )
        current_strategy = normalize_post_qc_strategy(
            current_config["post_qc_strategy"], self.acquisition_layout
        )
        proposed_strategy = current_strategy
        if "post_qc_strategy" in updates:
            proposed_strategy = normalize_post_qc_strategy(
                updates.get("post_qc_strategy"), self.acquisition_layout
            )
            persist_strategy = True
            if (
                current_strategy.get("compatibility_mode")
                and proposed_strategy.get("compatibility_mode")
            ):
                current_semantics = {
                    key: value
                    for key, value in current_strategy.items()
                    if key != "compatibility_mode"
                }
                proposed_semantics = {
                    key: value
                    for key, value in proposed_strategy.items()
                    if key != "compatibility_mode"
                }
                if proposed_semantics != current_semantics:
                    # Compatibility is an adapter, not a user-selectable
                    # matcher.  Editing its scientific fields is an explicit
                    # switch to the v1 strategy semantics.
                    proposed_strategy.pop("compatibility_mode", None)
                else:
                    persist_strategy = False
            if persist_strategy:
                normalized_updates["post_qc_strategy"] = proposed_strategy
            else:
                normalized_updates.pop("post_qc_strategy", None)
        try:
            proposed_qc_end = float(
                normalized_updates.get("qc_calibration_end_min", current_config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN))
            )
        except (TypeError, ValueError) as exc:
            raise BadRequest("qc_calibration_end_min must be numeric") from exc
        if not math.isfinite(proposed_qc_end) or proposed_qc_end < 0:
            raise BadRequest("qc_calibration_end_min must be a finite non-negative number")
        qc_end_changed = abs(
            proposed_qc_end - float(current_config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN))
        ) > 1e-9
        if (
            qc_end_changed
            and proposed_protocol.get("compatibility_mode")
            and "calibration_protocol" not in updates
        ):
            # v0.3 exposes qc_calibration_end_min as its only persisted front
            # boundary.  Rebuild the in-memory compatibility projection now,
            # rather than leaving its hash/segment stale until the next open.
            compat_protocol = copy.deepcopy(proposed_protocol)
            compat_protocol["segments"][-1]["end_min"] = proposed_qc_end
            proposed_protocol = normalize_calibration_protocol(
                compat_protocol,
                self.acquisition_layout,
                require_confirmed=False,
            )
            if "calibration_protocol" in self.store.project_config():
                normalized_updates["calibration_protocol"] = proposed_protocol
        current_protocol_hash = calibration_protocol_hash(
            current_protocol, self.acquisition_layout
        )
        proposed_protocol_hash = calibration_protocol_hash(
            proposed_protocol, self.acquisition_layout
        )
        protocol_changed = current_protocol_hash != proposed_protocol_hash
        current_protocol_ready = bool(current_protocol.get("boundaries_confirmed"))
        proposed_protocol_ready = bool(proposed_protocol.get("boundaries_confirmed"))
        readiness_changed = current_protocol_ready != proposed_protocol_ready
        current_strategy_hash = post_qc_strategy_hash(
            current_strategy,
            self.acquisition_layout,
        )
        proposed_strategy_hash = post_qc_strategy_hash(
            proposed_strategy,
            self.acquisition_layout,
        )
        strategy_changed = current_strategy_hash != proposed_strategy_hash
        validate_post_qc_strategy_timing(proposed_strategy, proposed_qc_end)
        legacy_protocol = bool(proposed_protocol.get("compatibility_mode"))
        if proposed_protocol_ready:
            proposed_alignment = estimate_shift_alignment(
                self.lif_peaks,
                self.ms_events,
                proposed_qc_end,
                acquisition_layout=self.acquisition_layout,
                calibration_protocol=None if legacy_protocol else proposed_protocol,
            )
        else:
            proposed_alignment = draft_calibration_alignment(
                acquisition_layout=self.acquisition_layout,
                calibration_protocol=proposed_protocol,
            )
        persisted_qc_alignment = self.store.qc_alignment_model()
        if (
            proposed_protocol_ready
            and persisted_qc_alignment
            and not qc_end_changed
            and not protocol_changed
            and not readiness_changed
        ):
            proposed_alignment = apply_qc_alignment_model(
                proposed_alignment,
                self.lif_peaks,
                self.ms_events,
                qc_calibration_end_min=proposed_qc_end,
                acquisition_layout=self.acquisition_layout,
                model=persisted_qc_alignment,
                calibration_protocol=(
                    None
                    if legacy_protocol
                    else proposed_protocol
                ),
            )
        config = self.store.update_project_config(
            normalized_updates,
            clear_frozen_time_model=clear_frozen_time_model,
            clear_qc_alignment_model=clear_qc_alignment_model,
        )
        object.__setattr__(self, "calibration_protocol", proposed_protocol)
        object.__setattr__(self, "post_qc_strategy", proposed_strategy)
        proposed_alignment["post_qc_strategy"] = copy.deepcopy(proposed_strategy)
        proposed_alignment["post_qc_strategy_hash"] = proposed_strategy_hash
        object.__setattr__(self, "alignment", proposed_alignment)
        warning = ""
        response_time_model: dict[str, Any] = {
            "status": "unavailable",
            "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
            "annotation_start_min": float(config["annotation_start_min"]),
            "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
        }
        try:
            model = self.active_time_model()
            response_time_model = model
            if proposed_protocol_ready and str(model.get("status")) != "frozen":
                payload = {
                    **model,
                    "status": "draft",
                    "base_model_name": str(self.alignment["model"]),
                    "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
                    "sample_valve_switch_min": float(config["sample_valve_switch_min"]),
                    "annotation_start_min": float(config["annotation_start_min"]),
                    "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
                    "contains_cell_labels": False,
                    "ms_local_delta_sec": 0.0 if (clear_frozen_time_model or qc_end_changed or protocol_changed or readiness_changed) else float(model.get("ms_local_delta_sec", 0.0) or 0.0),
                    "max_training_time_min": float(config["annotation_start_min"]) + float(config["local_delta_seed_window_min"]),
                    "acquisition_layout_hash": self.alignment.get("acquisition_layout_hash"),
                    "calibration_protocol_hash": proposed_protocol_hash,
                }
                response_time_model = self.store.upsert_time_model(
                    payload,
                    action="sync_draft_time_model_to_project_config",
                )
        except Exception as exc:
            warning = (
                "项目时间节点已保存，但尚未锁定的时间校正结果同步失败："
                f"{user_facing_error_message(exc)}"
            )
        if strategy_changed:
            strategy_warning = (
                "后段质控巡检设置已修改；既有人工质控记录完整保留为历史记录，"
                "但不再作为当前设置的结果，后段质控候选与导出已按新设置重算。"
            )
            warning = f"{warning} {strategy_warning}".strip()
        if not proposed_protocol_ready:
            draft_warning = (
                "参考段边界仍待确认；项目只允许浏览原始峰形。"
                "确认全部边界后才会重新计算前段候选并解锁后段时间模型。"
            )
            warning = f"{warning} {draft_warning}".strip()
        object.__setattr__(self, "_project_config_update_warning", warning)
        object.__setattr__(self, "_project_config_update_time_model", response_time_model)
        return self.project_config()

    def auto_group_by_id(self, annotation_id: str) -> dict[str, Any]:
        for group in self.alignment.get("qc_groups", {}).get("groups", []):
            if candidate_id_for_group(group) == annotation_id:
                return group
        raise BadRequest(f"Unknown auto candidate_id: {annotation_id}")

    def payload_from_qc_group(
        self,
        group: dict[str, Any],
        *,
        candidate_id: str,
        candidate_type: str,
        confidence_mode: str,
    ) -> dict[str, Any]:
        ms = self.ms_events[self.ms_events["event_id"].eq(group["ms_event_id"])]
        scan_id = None if ms.empty else clean_value(ms.iloc[0].get("scan_id"))
        anchor_map = qc_anchor_peak_id_map(group)
        present_channels = [channel for channel, peak_id in anchor_map.items() if peak_id]
        anchor_a_channel = str(group.get("anchor_a_channel") or (present_channels[0] if present_channels else ""))
        anchor_b_channel = str(group.get("anchor_b_channel") or (present_channels[1] if len(present_channels) > 1 else ""))
        payload = {
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
            "review_stage": "qc_calibration" if not candidate_type.startswith("qc_survey") else "qc_survey",
            "label": "QC",
            "anchor_a_channel": anchor_a_channel or None,
            "anchor_b_channel": anchor_b_channel or None,
            "anchor_a_peak_id": group.get("anchor_a_peak_id") or anchor_map.get(anchor_a_channel),
            "anchor_b_peak_id": group.get("anchor_b_peak_id") or anchor_map.get(anchor_b_channel),
            "g2_peak_id": group.get("g2_peak_id"),
            "r1_peak_id": group.get("r1_peak_id"),
            "ms_event_id": group["ms_event_id"],
            "scan_id": scan_id,
            "g2_raw_time_min": group.get("g2_raw_time_min"),
            "r1_raw_time_min": group.get("r1_raw_time_min"),
            "anchor_a_raw_time_min": group.get("anchor_a_raw_time_min") or (group.get("lif_anchor_raw_times_min") or {}).get(anchor_a_channel),
            "anchor_b_raw_time_min": group.get("anchor_b_raw_time_min") or (group.get("lif_anchor_raw_times_min") or {}).get(anchor_b_channel),
            "ms_time_min": group["ms_time_min"],
            "g2_plot_time_min": group.get("g2_plot_time_min"),
            "r1_plot_time_min": group.get("r1_plot_time_min"),
            "anchor_a_plot_time_min": group.get("anchor_a_plot_time_min") or (group.get("lif_anchor_plot_times_min") or {}).get(anchor_a_channel),
            "anchor_b_plot_time_min": group.get("anchor_b_plot_time_min") or (group.get("lif_anchor_plot_times_min") or {}).get(anchor_b_channel),
            "ms_plot_time_min": group["ms_plot_time_min"],
            **self.time_model_payload_fields(),
            "expected_lif_time_sec": None,
            "residual_sec": group.get("composite_to_ms_residual_sec"),
            "abs_residual_sec": group.get("abs_composite_to_ms_residual_sec"),
            "candidate_rank": group.get("rank"),
            "candidate_score": None,
            "confidence_mode": confidence_mode,
            "lif_pair_residual_sec": group.get("lif_pair_residual_sec"),
            "selection_reason": group.get("selection_reason"),
            "input_policy": "first_principles_preprocessing_tables_only",
        }
        for key in [
            "qc_group_version",
            "matcher_version",
            "anchor_channels",
            "lif_anchors",
            "lif_anchor_peak_ids",
            "lif_anchor_raw_times_min",
            "lif_anchor_plot_times_min",
            "axis_composite_times_min",
            "axis_residuals_to_ms_sec",
            "axis_span_sec",
            "axis_coherence_tolerance_sec",
            "axis_coherent",
            "max_abs_axis_to_ms_residual_sec",
            "covered_time_axes",
            "required_time_axes",
            "lif_anchor_count",
            "complete_anchor_set",
            "missing_lif_channels",
            "lif_composite_plot_time_min",
            "conflict_count",
            "same_axis_conflict_count",
            "same_axis_dropped_channels",
            "calibration_segment_id",
            "calibration_segment_order",
            "calibration_segment_start_min",
            "calibration_segment_end_min",
            "calibration_reference_mode",
            "calibration_population_label",
            "g1_peak_id",
            "g1_raw_time_min",
            "g1_plot_time_min",
            "r2_peak_id",
            "r2_raw_time_min",
            "r2_plot_time_min",
        ]:
            if key in group:
                payload[key] = clean_value(group.get(key))
        payload["acquisition_layout_hash"] = self.alignment.get("acquisition_layout_hash")
        return payload

    def payload_from_auto_group(self, group: dict[str, Any]) -> dict[str, Any]:
        return self.payload_from_qc_group(
            group,
            candidate_id=candidate_id_for_group(group),
            candidate_type=(
                "qc_calibration_segment_anchor"
                if group.get("calibration_segment_id")
                else "qc_calibration_anchor_0_10p5"
            ),
            confidence_mode=(
                "auto_segmented_calibration_candidate"
                if group.get("calibration_segment_id")
                else "auto_qc_shift_candidate"
            ),
        )

    def payload_from_post_qc_group(self, group: dict[str, Any]) -> dict[str, Any]:
        config = self.project_config()
        strategy = normalize_post_qc_strategy(
            config.get("post_qc_strategy"), self.acquisition_layout
        )
        compatibility = bool(strategy.get("compatibility_mode"))
        payload = self.payload_from_qc_group(
            group,
            candidate_id=post_qc_candidate_id(group),
            candidate_type=(
                "qc_survey_post_10p5"
                if compatibility
                else f"qc_survey_{strategy['mode']}"
            ),
            confidence_mode=(
                "post_qc_shift_only_candidate"
                if compatibility
                else f"post_qc_{strategy['mode']}_candidate"
            ),
        )
        payload.update(
            {
                "post_qc_strategy_mode": str(strategy["mode"]),
                "post_qc_strategy_hash": config.get("post_qc_strategy_hash"),
                "post_qc_window_id": group.get("post_qc_window_id"),
            }
        )
        return payload

    def payload_from_qc_ids(self, g2_peak_id: str, r1_peak_id: str, ms_event_id: str, *, post_qc: bool) -> dict[str, Any]:
        g2 = self.lif_peaks[self.lif_peaks["peak_id"].eq(g2_peak_id)]
        r1 = self.lif_peaks[self.lif_peaks["peak_id"].eq(r1_peak_id)]
        ms = self.ms_events[self.ms_events["event_id"].eq(ms_event_id)]
        if g2.empty:
            raise BadRequest(f"Unknown anchor A peak_id: {g2_peak_id}")
        if r1.empty:
            raise BadRequest(f"Unknown anchor B peak_id: {r1_peak_id}")
        if ms.empty:
            raise BadRequest(f"Unknown MS event_id: {ms_event_id}")
        g2_row = g2.iloc[0]
        r1_row = r1.iloc[0]
        ms_row = ms.iloc[0]
        weak_anchor_ids = [
            str(row["peak_id"])
            for row in (g2_row, r1_row)
            if str(row.get("peak_tier") or "core").strip().lower() == "weak"
        ]
        if weak_anchor_ids:
            raise BadRequest(
                "Weak LIF peaks cannot be reconstructed as automatic QC candidates: "
                + ", ".join(weak_anchor_ids)
            )
        anchor_a_channel = str(g2_row["channel"])
        anchor_b_channel = str(r1_row["channel"])
        g2_plot_sec = float(g2_row["time_sec"]) + self.channel_shift_sec(anchor_a_channel, "aligned")
        r1_plot_sec = float(r1_row["time_sec"]) + self.channel_shift_sec(anchor_b_channel, "aligned")
        ms_shift_sec = self.ms_shift_sec_at(float(ms_row["time_min"]), "aligned")
        ms_time_sec = float(ms_row["time_sec"]) + ms_shift_sec
        residual = ms_time_sec - ((g2_plot_sec + r1_plot_sec) / 2.0)
        group = {
            "rank": None,
            "anchor_a_channel": anchor_a_channel,
            "anchor_b_channel": anchor_b_channel,
            "anchor_a_peak_id": g2_peak_id,
            "anchor_b_peak_id": r1_peak_id,
            "g2_peak_id": g2_peak_id,
            "r1_peak_id": r1_peak_id,
            "ms_event_id": ms_event_id,
            "g2_raw_time_min": float(g2_row["time_min"]),
            "r1_raw_time_min": float(r1_row["time_min"]),
            "anchor_a_raw_time_min": float(g2_row["time_min"]),
            "anchor_b_raw_time_min": float(r1_row["time_min"]),
            "ms_time_min": float(ms_row["time_min"]),
            "g2_plot_time_min": float(g2_plot_sec / 60.0),
            "r1_plot_time_min": float(r1_plot_sec / 60.0),
            "anchor_a_plot_time_min": float(g2_plot_sec / 60.0),
            "anchor_b_plot_time_min": float(r1_plot_sec / 60.0),
            "ms_plot_time_min": float(ms_time_sec / 60.0),
            "lif_pair_residual_sec": float(r1_row["time_sec"] - g2_row["time_sec"]),
            "composite_to_ms_residual_sec": float(residual),
            "abs_composite_to_ms_residual_sec": abs(float(residual)),
            "selection_reason": "reconstructed_from_candidate_id",
        }
        if post_qc:
            return self.payload_from_post_qc_group(group)
        return self.payload_from_auto_group(group)

    def payload_from_cell_ids(self, lif_channel: str, lif_peak_id: str, ms_event_id: str) -> dict[str, Any]:
        lif_channel = str(lif_channel).strip().upper()
        if lif_channel not in self.cell_annotation_channels():
            raise BadRequest(f"LIF 通道 {lif_channel} 未配置为细胞标注通道")
        lif = self.lif_peaks[self.lif_peaks["peak_id"].eq(lif_peak_id)]
        ms = self.ms_events[self.ms_events["event_id"].eq(ms_event_id)]
        if lif.empty:
            raise BadRequest(f"Unknown LIF peak_id: {lif_peak_id}")
        if ms.empty:
            raise BadRequest(f"Unknown MS event_id: {ms_event_id}")
        lif_row = lif.iloc[0]
        ms_row = ms.iloc[0]
        if not is_primary_pc34_event(ms_row):
            raise BadRequest("Cell annotation requires an MS760 PC34 primary event")
        self.require_third_stage_event_in_map(ms_event_id)
        if str(ms_event_id) in self.accepted_qc_survey_ms_event_ids():
            raise BadRequest("This MS760 event has already been accepted as QC in QC survey")
        if str(lif_row["channel"]) != lif_channel:
            raise BadRequest(f"Cell candidate channel mismatch: {lif_channel} vs {lif_row['channel']}")
        shift_sec = self.channel_shift_sec(lif_channel, "aligned")
        ms_shift_sec = self.ms_shift_sec_at(float(ms_row["time_min"]), "aligned", require_frozen=True)
        lif_plot_sec = float(lif_row["time_sec"]) + shift_sec
        ms_plot_sec = float(ms_row["time_sec"]) + ms_shift_sec
        residual = ms_plot_sec - lif_plot_sec
        return {
            "candidate_id": f"cell:{lif_channel}:{lif_peak_id}:{ms_event_id}",
            "candidate_type": "cell_high_confidence",
            "review_stage": "cell_annotation",
            "label": self.cell_label_for_channel(lif_channel),
            "lif_channel": lif_channel,
            "lif_peak_id": lif_peak_id,
            "lif_peak_tier": str(lif_row.get("peak_tier") or "core"),
            "lif_peak_detection_hash": str(
                lif_row.get("detector_config_hash")
                or self.project_config().get("lif_peak_detection_hash")
                or ""
            ),
            "g1_peak_id": lif_peak_id if lif_channel == "G1" else None,
            "r2_peak_id": lif_peak_id if lif_channel == "R2" else None,
            "g2_peak_id": lif_peak_id if lif_channel == "G2" else None,
            "r1_peak_id": lif_peak_id if lif_channel == "R1" else None,
            "ms_event_id": ms_event_id,
            "scan_id": clean_value(ms_row.get("scan_id")),
            "lif_raw_time_min": float(lif_row["time_min"]),
            "lif_plot_time_min": float(lif_plot_sec / 60.0),
            "ms_time_min": float(ms_row["time_min"]),
            "ms_plot_time_min": float(ms_plot_sec / 60.0),
            **self.time_model_payload_fields(),
            "expected_lif_time_sec": float(lif_plot_sec),
            "residual_sec": float(residual),
            "abs_residual_sec": abs(float(residual)),
            "candidate_rank": None,
            "candidate_score": None,
            "confidence_mode": "high_confidence_cell_unique_shift_match",
            "lif_snr": clean_value(lif_row.get("snr")),
            "lif_nearest_gap_sec": clean_value(lif_row.get("nearest_gap_sec")),
            "ms_pc34_760_apex": clean_value(ms_row.get("pc34_760_apex")),
            "ms_nearest_event_gap_sec": clean_value(ms_row.get("nearest_event_gap_sec")),
            "selection_reason": "strict_snr_gap_intensity_unique_nearest",
            "input_policy": "first_principles_preprocessing_tables_only",
        }

    def payload_from_auto_candidate_id(
        self,
        annotation_id: str,
        *,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
    ) -> dict[str, Any]:
        if annotation_id.startswith("auto_qc:"):
            if window_start_min is None or window_end_min is None:
                raise BadRequest("Reviewing an automatic front-QC candidate requires its active window")
            group = self.auto_group_by_id(annotation_id)
            active_start = float(window_start_min)
            active_end = float(window_end_min)
            if not front_qc_group_belongs_to_window(
                group,
                active_start,
                active_end,
            ):
                raise BadRequest(
                    f"Unknown or inactive front-QC candidate_id in active window: {annotation_id}"
                )
            return self.payload_from_auto_group(group)
        if annotation_id.startswith("post_qc:"):
            if not self.frozen_time_model():
                raise BadRequest("Freeze local time model before reviewing QC survey candidates")
            if window_start_min is None or window_end_min is None:
                raise BadRequest("Reviewing a post-QC candidate requires its active window")
            active_start = float(window_start_min)
            active_end = float(window_end_min)
            for group in self.build_post_qc_candidates(
                active_start - WINDOW_CONTEXT_MARGIN_MIN,
                active_end + WINDOW_CONTEXT_MARGIN_MIN,
                "aligned",
            ):
                if post_qc_candidate_id(group) != annotation_id:
                    continue
                plot_times = qc_group_plot_times(group)
                if plot_times and all(active_start <= value <= active_end for value in plot_times):
                    return self.payload_from_post_qc_group(group)
            raise BadRequest(f"Unknown or inactive post-QC candidate_id in active window: {annotation_id}")
        if annotation_id.startswith("cell:"):
            if not self.frozen_time_model():
                raise BadRequest("Freeze local time model before reviewing cell candidates")
            parts = annotation_id.split(":", 3)
            if len(parts) != 4:
                raise BadRequest(f"Malformed cell candidate_id: {annotation_id}")
            if window_start_min is None or window_end_min is None:
                raise BadRequest("Reviewing an automatic cell candidate requires its active window")
            active_start = float(window_start_min)
            active_end = float(window_end_min)
            matched_candidate = None
            for candidate in self.build_cell_candidates(
                active_start - WINDOW_CONTEXT_MARGIN_MIN,
                active_end + WINDOW_CONTEXT_MARGIN_MIN,
                "aligned",
            ):
                if cell_candidate_id(candidate) != annotation_id:
                    continue
                if all(
                    active_start <= float(candidate[key]) <= active_end
                    for key in ("lif_plot_time_min", "ms_plot_time_min")
                ):
                    matched_candidate = candidate
                    break
            if matched_candidate is None:
                raise BadRequest(
                    f"Unknown or inactive automatic cell candidate_id in active window: {annotation_id}"
                )
            payload = self.payload_from_cell_ids(parts[1], parts[2], parts[3])
            for key in [
                "candidate_type",
                "cross_channel_candidate_conflict",
                "arbitration_status",
                "arbitration_reason",
                "cross_channel_alternatives",
                "selection_reason",
            ]:
                if key in matched_candidate:
                    payload[key] = clean_value(matched_candidate.get(key))
            return payload
        raise BadRequest(f"Unknown auto candidate_id: {annotation_id}")

    def payload_from_manual_anchor_set(
        self,
        lif_anchor_peak_ids: dict[str, str | None],
        ms_event_id: str,
        *,
        allow_lif_missing: bool,
        anchor_channels: list[str] | tuple[str, ...] | None = None,
        calibration_segment: dict[str, Any] | None = None,
        post_qc_window: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not ms_event_id:
            raise BadRequest("Manual QC anchor requires an MS760 event")
        layout = normalize_acquisition_layout(self.acquisition_layout)
        anchors = [
            str(channel)
            for channel in (anchor_channels or layout["qc_anchor_channels"])
        ]
        if not anchors:
            raise BadRequest("Manual QC anchor 缺少当前参考段或后段策略通道")
        channel_time_axes = layout["channel_time_axes"]
        unexpected = sorted(set(lif_anchor_peak_ids) - set(anchors))
        if unexpected:
            raise BadRequest(f"Manual QC anchor contains unconfigured channels: {', '.join(unexpected)}")
        normalized_ids = {
            channel: optional_peak_id(lif_anchor_peak_ids.get(channel))
            for channel in anchors
        }
        if not any(normalized_ids.values()):
            raise BadRequest(f"Manual QC anchor requires at least one of {', '.join(anchors)} plus MS760")

        peak_rows: dict[str, pd.Series] = {}
        for channel, peak_id in normalized_ids.items():
            if not peak_id:
                continue
            selected = self.lif_peaks[self.lif_peaks["peak_id"].eq(peak_id)]
            if selected.empty:
                raise BadRequest(f"Unknown {channel} peak_id: {peak_id}")
            row = selected.iloc[0]
            if str(row["channel"]) != channel:
                raise BadRequest(f"Manual QC anchor expected {channel}, got {row['channel']}")
            if str(row.get("peak_tier") or "core").strip().lower() == "weak":
                raise BadRequest(
                    "weak LIF 峰仅供人工细胞配对复核，不能作为 QC 对齐或后段 delta 的训练 anchor"
                )
            peak_rows[channel] = row
        covered_axes = {str(channel_time_axes[channel]) for channel in peak_rows}
        required_axes = {str(channel_time_axes[channel]) for channel in anchors}
        if not allow_lif_missing and not required_axes.issubset(covered_axes):
            missing_axes = sorted(required_axes - covered_axes)
            raise BadRequest(f"Manual QC calibration anchor must cover every time axis; missing {', '.join(missing_axes)}")

        ms = self.ms_events[self.ms_events["event_id"].eq(ms_event_id)]
        if ms.empty:
            raise BadRequest(f"Unknown MS event_id: {ms_event_id}")
        ms_row = ms.iloc[0]
        if not is_primary_pc34_event(ms_row):
            raise BadRequest("Manual QC anchor requires an MS760 PC34 primary event")
        if allow_lif_missing:
            self.require_third_stage_event_in_map(ms_event_id)

        lif_anchors: list[dict[str, Any]] = []
        raw_times: dict[str, float | None] = {channel: None for channel in anchors}
        plot_times: dict[str, float | None] = {channel: None for channel in anchors}
        axis_times: dict[str, list[float]] = {axis: [] for axis in required_axes}
        for channel in anchors:
            row = peak_rows.get(channel)
            if row is None:
                continue
            axis = str(channel_time_axes[channel])
            plot_sec = float(row["time_sec"]) + self.channel_shift_sec(channel, "aligned")
            raw_times[channel] = float(row["time_min"])
            plot_times[channel] = float(plot_sec / 60.0)
            axis_times[axis].append(plot_sec)
            lif_anchors.append(
                {
                    "channel": channel,
                    "time_axis": axis,
                    "peak_id": str(row["peak_id"]),
                    "raw_time_min": float(row["time_min"]),
                    "plot_time_min": float(plot_sec / 60.0),
                    "snr": clean_value(row.get("snr")),
                }
            )
        axis_composites = {
            axis: float(np.median(values)) for axis, values in axis_times.items() if values
        }
        lif_composite_sec = float(np.median(list(axis_composites.values())))
        ms_shift_sec = self.ms_shift_sec_at(float(ms_row["time_min"]), "aligned")
        ms_plot_sec = float(ms_row["time_sec"]) + ms_shift_sec
        residual = ms_plot_sec - lif_composite_sec
        missing_channels = [channel for channel in anchors if normalized_ids[channel] is None]
        payload: dict[str, Any] = {
            "candidate_id": None,
            "candidate_type": "manual_qc_anchor_set_partial" if missing_channels else "manual_qc_anchor_set",
            "review_stage": "qc_survey" if allow_lif_missing else "qc_calibration",
            "label": "QC",
            "qc_group_version": 2,
            "matcher_version": QC_MATCHER_VERSION,
            "anchor_channels": anchors,
            "lif_anchors": lif_anchors,
            "lif_anchor_peak_ids": normalized_ids,
            "lif_anchor_raw_times_min": raw_times,
            "lif_anchor_plot_times_min": plot_times,
            "axis_composite_times_min": {
                axis: float(value / 60.0) for axis, value in axis_composites.items()
            },
            "covered_time_axes": sorted(covered_axes),
            "required_time_axes": sorted(required_axes),
            "lif_anchor_count": len(lif_anchors),
            "complete_anchor_set": not missing_channels,
            "missing_lif_channels": missing_channels,
            "ms_event_id": ms_event_id,
            "scan_id": clean_value(ms_row.get("scan_id")),
            "ms_time_min": float(ms_row["time_min"]),
            "ms_plot_time_min": float(ms_plot_sec / 60.0),
            "lif_composite_plot_time_min": float(lif_composite_sec / 60.0),
            **self.time_model_payload_fields(),
            "expected_lif_time_sec": None,
            "residual_sec": float(residual),
            "abs_residual_sec": abs(float(residual)),
            "candidate_rank": None,
            "candidate_score": None,
            "confidence_mode": "manual_qc_anchor_set_partial" if missing_channels else "manual_qc_anchor_set",
            "selection_reason": "manual_axis_aware_anchor_set",
            "acquisition_layout_hash": self.alignment.get("acquisition_layout_hash"),
            "input_policy": "first_principles_preprocessing_tables_only",
        }
        if calibration_segment is not None:
            payload.update(
                {
                    "candidate_type": "qc_calibration_segment_anchor",
                    "calibration_segment_id": str(calibration_segment["segment_id"]),
                    "calibration_segment_order": int(calibration_segment["order"]),
                    "calibration_segment_start_min": float(calibration_segment["start_min"]),
                    "calibration_segment_end_min": float(calibration_segment["end_min"]),
                    "calibration_population_label": str(
                        calibration_segment.get("population_label") or ""
                    ),
                    "calibration_reference_mode": str(
                        calibration_segment.get("reference_mode") or ""
                    ),
                    "calibration_protocol_hash": self.project_config().get(
                        "calibration_protocol_hash"
                    ),
                }
            )
        if allow_lif_missing:
            strategy = self.project_config().get("post_qc_strategy", {})
            if strategy.get("compatibility_mode"):
                # Preserve the v0.3 manual post-QC identity even though the
                # modern UI sends an axis-aware anchor map.  The compatibility
                # matcher recognizes these historical types and annotations.
                payload["candidate_type"] = (
                    "manual_qc_anchor_partial"
                    if missing_channels
                    else "manual_qc_triplet"
                )
                payload["confidence_mode"] = payload["candidate_type"]
            else:
                payload["candidate_type"] = (
                    f"qc_survey_{strategy.get('mode', 'signature')}"
                )
            payload.update(
                {
                    "post_qc_strategy_mode": str(strategy.get("mode") or ""),
                    "post_qc_strategy_hash": self.project_config().get(
                        "post_qc_strategy_hash"
                    ),
                    "post_qc_window_id": (
                        str(post_qc_window.get("window_id"))
                        if post_qc_window is not None
                        else None
                    ),
                }
            )
        for channel in ["G1", "G2", "R1", "R2"]:
            key = channel.lower()
            payload[f"{key}_peak_id"] = normalized_ids.get(channel)
            payload[f"{key}_raw_time_min"] = raw_times.get(channel)
            payload[f"{key}_plot_time_min"] = plot_times.get(channel)
        present = [channel for channel in anchors if normalized_ids[channel]]
        if present:
            first = present[0]
            payload.update(
                {
                    "anchor_a_channel": first,
                    "anchor_a_peak_id": normalized_ids[first],
                    "anchor_a_raw_time_min": raw_times[first],
                    "anchor_a_plot_time_min": plot_times[first],
                }
            )
        if len(present) > 1:
            second = present[1]
            payload.update(
                {
                    "anchor_b_channel": second,
                    "anchor_b_peak_id": normalized_ids[second],
                    "anchor_b_raw_time_min": raw_times[second],
                    "anchor_b_plot_time_min": plot_times[second],
                }
            )
        return payload

    def payload_from_manual_triplet(
        self,
        g2_peak_id: str | None,
        r1_peak_id: str | None,
        ms_event_id: str,
        *,
        allow_lif_missing: bool = False,
        lif_anchor_peak_ids: dict[str, str | None] | None = None,
        anchor_channels: list[str] | tuple[str, ...] | None = None,
        calibration_segment: dict[str, Any] | None = None,
        post_qc_window: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if lif_anchor_peak_ids is not None:
            return self.payload_from_manual_anchor_set(
                lif_anchor_peak_ids,
                ms_event_id,
                allow_lif_missing=allow_lif_missing,
                anchor_channels=anchor_channels,
                calibration_segment=calibration_segment,
                post_qc_window=post_qc_window,
            )
        if not ms_event_id:
            raise BadRequest("Manual QC anchor requires an MS760 event")
        anchors = self.alignment.get("qc_anchor_channels") or normalize_acquisition_layout(self.acquisition_layout)["qc_anchor_channels"]
        anchor_a_channel, anchor_b_channel = str(anchors[0]), str(anchors[1])
        if not g2_peak_id and not r1_peak_id:
            raise BadRequest(f"Manual QC anchor requires {anchor_a_channel} or {anchor_b_channel} plus MS760")
        if not allow_lif_missing and (not g2_peak_id or not r1_peak_id):
            raise BadRequest(f"Manual QC calibration anchor requires {anchor_a_channel}, {anchor_b_channel}, and MS760")
        g2 = self.lif_peaks[self.lif_peaks["peak_id"].eq(g2_peak_id)] if g2_peak_id else pd.DataFrame()
        r1 = self.lif_peaks[self.lif_peaks["peak_id"].eq(r1_peak_id)] if r1_peak_id else pd.DataFrame()
        ms = self.ms_events[self.ms_events["event_id"].eq(ms_event_id)]
        if g2_peak_id and g2.empty:
            raise BadRequest(f"Unknown G2 peak_id: {g2_peak_id}")
        if r1_peak_id and r1.empty:
            raise BadRequest(f"Unknown R1 peak_id: {r1_peak_id}")
        if ms.empty:
            raise BadRequest(f"Unknown MS event_id: {ms_event_id}")
        g2_row = g2.iloc[0] if not g2.empty else None
        r1_row = r1.iloc[0] if not r1.empty else None
        ms_row = ms.iloc[0]
        if not is_primary_pc34_event(ms_row):
            raise BadRequest("Manual QC anchor requires an MS760 PC34 primary event")
        if g2_row is not None and str(g2_row["channel"]) != anchor_a_channel:
            raise BadRequest(f"Manual QC triplet requires a {anchor_a_channel} peak as the first LIF peak")
        if r1_row is not None and str(r1_row["channel"]) != anchor_b_channel:
            raise BadRequest(f"Manual QC triplet requires a {anchor_b_channel} peak as the second LIF peak")
        g2_plot_sec = (
            float(g2_row["time_sec"]) + self.channel_shift_sec(anchor_a_channel, "aligned")
            if g2_row is not None
            else None
        )
        r1_plot_sec = (
            float(r1_row["time_sec"]) + self.channel_shift_sec(anchor_b_channel, "aligned")
            if r1_row is not None
            else None
        )
        ms_shift_sec = self.ms_shift_sec_at(float(ms_row["time_min"]), "aligned")
        ms_time_sec = float(ms_row["time_sec"]) + ms_shift_sec
        lif_plot_secs = [value for value in [g2_plot_sec, r1_plot_sec] if value is not None]
        composite = sum(lif_plot_secs) / len(lif_plot_secs)
        residual = ms_time_sec - composite
        missing_channels = [name for name, value in [(anchor_a_channel, g2_peak_id), (anchor_b_channel, r1_peak_id)] if not value]
        return {
            "candidate_id": None,
            "candidate_type": "manual_qc_anchor_partial" if missing_channels else "manual_qc_triplet",
            "review_stage": "qc_survey" if allow_lif_missing else "qc_calibration",
            "label": "QC",
            "anchor_a_channel": anchor_a_channel,
            "anchor_b_channel": anchor_b_channel,
            "anchor_a_peak_id": g2_peak_id,
            "anchor_b_peak_id": r1_peak_id,
            "g2_peak_id": g2_peak_id,
            "r1_peak_id": r1_peak_id,
            "ms_event_id": ms_event_id,
            "scan_id": clean_value(ms_row.get("scan_id")),
            "g2_raw_time_min": float(g2_row["time_min"]) if g2_row is not None else None,
            "r1_raw_time_min": float(r1_row["time_min"]) if r1_row is not None else None,
            "anchor_a_raw_time_min": float(g2_row["time_min"]) if g2_row is not None else None,
            "anchor_b_raw_time_min": float(r1_row["time_min"]) if r1_row is not None else None,
            "ms_time_min": float(ms_row["time_min"]),
            "g2_plot_time_min": float(g2_plot_sec / 60.0) if g2_plot_sec is not None else None,
            "r1_plot_time_min": float(r1_plot_sec / 60.0) if r1_plot_sec is not None else None,
            "anchor_a_plot_time_min": float(g2_plot_sec / 60.0) if g2_plot_sec is not None else None,
            "anchor_b_plot_time_min": float(r1_plot_sec / 60.0) if r1_plot_sec is not None else None,
            "ms_plot_time_min": float(ms_time_sec / 60.0),
            **self.time_model_payload_fields(),
            "expected_lif_time_sec": None,
            "residual_sec": float(residual),
            "abs_residual_sec": abs(float(residual)),
            "candidate_rank": None,
            "candidate_score": None,
            "confidence_mode": "manual_qc_anchor_partial" if missing_channels else "manual_qc_triplet",
            "lif_anchor_count": len(lif_plot_secs),
            "missing_lif_channels": missing_channels,
            "missing_peak_symbol": MISSING_PEAK_SYMBOL,
            "input_policy": "first_principles_preprocessing_tables_only",
        }

    def review_auto_candidate(
        self,
        annotation_id: str,
        review_status: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        clear_qc_alignment_model: bool = False,
        defer_alignment_reset: bool = False,
    ) -> dict[str, Any]:
        with request_read_snapshot():
            return self._review_auto_candidate_from_request_snapshot(
                annotation_id,
                review_status,
                window_start_min=window_start_min,
                window_end_min=window_end_min,
                time_mode=time_mode,
                clear_qc_alignment_model=clear_qc_alignment_model,
                defer_alignment_reset=defer_alignment_reset,
            )

    def _review_auto_candidate_from_request_snapshot(
        self,
        annotation_id: str,
        review_status: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        clear_qc_alignment_model: bool = False,
        defer_alignment_reset: bool = False,
    ) -> dict[str, Any]:
        payload = self.payload_from_auto_candidate_id(
            annotation_id,
            window_start_min=window_start_min,
            window_end_min=window_end_min,
        )
        if str(payload.get("review_stage") or "") == "qc_calibration":
            self.require_confirmed_calibration("审核前段校准证据")
        if review_status == "accepted":
            self.ensure_third_stage_acceptance_allowed(
                payload,
                annotation_id=annotation_id,
            )
        existing = self.store.get(annotation_id)
        invalidate_qc_alignment = self.require_qc_evidence_invalidation_confirmation(
            payload,
            clear_qc_alignment_model=clear_qc_alignment_model,
            previous_review_status=str((existing or {}).get("review_status") or "pending"),
            new_review_status=review_status,
        )
        row = self.store.upsert_review(
            annotation_id=annotation_id,
            source="auto_candidate",
            review_status=review_status,
            payload=payload,
            action=f"auto_candidate_{review_status}",
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
            invalidate_qc_alignment_model=invalidate_qc_alignment,
        )
        if invalidate_qc_alignment:
            self.invalidate_qc_model_request_reads()
        if invalidate_qc_alignment and not defer_alignment_reset:
            self.reset_to_automatic_qc_alignment()
        return row

    def review_annotation(
        self,
        annotation_id: str,
        review_status: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        clear_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        with request_read_snapshot():
            return self._review_annotation_from_request_snapshot(
                annotation_id,
                review_status,
                window_start_min=window_start_min,
                window_end_min=window_end_min,
                time_mode=time_mode,
                clear_qc_alignment_model=clear_qc_alignment_model,
            )

    def _review_annotation_from_request_snapshot(
        self,
        annotation_id: str,
        review_status: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        clear_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        existing = self.store.get(annotation_id)
        if existing and existing.get("source") == "manual_created":
            payload = {
                key: value
                for key, value in existing.items()
                if key
                not in {
                    "annotation_id",
                    "source",
                    "review_status",
                    "exportable",
                    "created_at",
                    "updated_at",
                }
            }
            if str(payload.get("review_stage") or "") == "qc_calibration":
                self.require_confirmed_calibration("审核前段校准证据")
            if review_status == "accepted":
                self.ensure_third_stage_acceptance_allowed(
                    payload,
                    annotation_id=annotation_id,
                )
            invalidate_qc_alignment = self.require_qc_evidence_invalidation_confirmation(
                payload,
                clear_qc_alignment_model=clear_qc_alignment_model,
                previous_review_status=str(existing.get("review_status") or "pending"),
                new_review_status=review_status,
            )
            row = self.store.upsert_review(
                annotation_id=annotation_id,
                source="manual_created",
                review_status=review_status,
                payload=payload,
                action=f"manual_annotation_{review_status}",
                window_start_min=window_start_min,
                window_end_min=window_end_min,
                time_mode=time_mode,
                invalidate_qc_alignment_model=invalidate_qc_alignment,
            )
            if invalidate_qc_alignment:
                self.invalidate_qc_model_request_reads()
                self.reset_to_automatic_qc_alignment()
            return row
        return self.review_auto_candidate(
            annotation_id,
            review_status,
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
            clear_qc_alignment_model=clear_qc_alignment_model,
        )

    def create_manual_triplet(
        self,
        g2_peak_id: str | None,
        r1_peak_id: str | None,
        ms_event_id: str,
        stage: str = "qc_calibration",
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
        lif_anchor_peak_ids: dict[str, str | None] | None = None,
        calibration_segment_id: str | None = None,
        post_qc_window_id: str | None = None,
        clear_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        g2_peak_id = optional_peak_id(g2_peak_id)
        r1_peak_id = optional_peak_id(r1_peak_id)
        ms_event_id = optional_peak_id(ms_event_id) or ""
        if stage not in {"qc_calibration", "qc_survey"}:
            raise BadRequest("Manual QC anchor can only be created in QC calibration or QC survey")
        if stage == "qc_calibration":
            self.require_confirmed_calibration("新建前段校准证据")
        config = self.project_config()
        protocol = normalize_calibration_protocol(
            config["calibration_protocol"], self.acquisition_layout
        )
        strategy = normalize_post_qc_strategy(
            config["post_qc_strategy"], self.acquisition_layout
        )
        selected_segment: dict[str, Any] | None = None
        selected_post_window: dict[str, Any] | None = None
        if stage == "qc_calibration":
            if bool(protocol.get("compatibility_mode")):
                configured_anchors = list(
                    normalize_acquisition_layout(self.acquisition_layout)[
                        "qc_anchor_channels"
                    ]
                )
            else:
                segment_key = str(calibration_segment_id or "").strip()
                selected_segment = next(
                    (
                        segment
                        for segment in protocol["segments"]
                        if str(segment["segment_id"]) == segment_key
                    ),
                    None,
                )
                if selected_segment is None:
                    raise BadRequest(
                        "Manual front calibration anchor requires calibration_segment_id"
                    )
                configured_anchors = list(selected_segment["reference_channels"])
        else:
            if strategy["mode"] == "disabled":
                raise BadRequest("当前项目后段 QC 策略为 disabled，已禁用 QC 巡检写入")
            if strategy["mode"] == "signature":
                configured_anchors = list(strategy["reference_channels"])
            else:
                selected_ms = self.ms_events[
                    self.ms_events["event_id"].eq(ms_event_id)
                ]
                if selected_ms.empty:
                    raise BadRequest(f"Unknown MS event_id: {ms_event_id}")
                ms_time = float(selected_ms.iloc[0]["time_min"])
                matching_windows = [
                    window
                    for window in strategy["windows"]
                    if float(window["start_min"]) <= ms_time <= float(window["end_min"])
                    and (
                        not post_qc_window_id
                        or str(window["window_id"]) == str(post_qc_window_id)
                    )
                ]
                if len(matching_windows) != 1:
                    raise BadRequest(
                        "Manual scheduled QC anchor 必须属于唯一配置窗口"
                    )
                selected_post_window = matching_windows[0]
                configured_anchors = list(
                    selected_post_window["reference_channels"]
                )
        normalized_anchor_ids: dict[str, str | None] | None = None
        if lif_anchor_peak_ids is not None:
            unexpected_selected = sorted(
                channel
                for channel, peak_id in lif_anchor_peak_ids.items()
                if optional_peak_id(peak_id) and channel not in configured_anchors
            )
            if unexpected_selected:
                raise BadRequest(
                    "Manual QC anchor contains channels outside the active segment/policy: "
                    + ", ".join(unexpected_selected)
                )
            normalized_anchor_ids = {
                channel: optional_peak_id(lif_anchor_peak_ids.get(channel))
                for channel in configured_anchors
            }
            g2_peak_id = normalized_anchor_ids.get("G2")
            r1_peak_id = normalized_anchor_ids.get("R1")
        allow_lif_missing = stage == "qc_survey"
        if stage == "qc_survey" and not self.frozen_time_model():
            raise BadRequest("Freeze local time model before creating QC survey anchors")
        def post_qc_lookup_bounds() -> tuple[float, float] | None:
            if window_start_min is not None and window_end_min is not None:
                return (
                    float(window_start_min) - WINDOW_CONTEXT_MARGIN_MIN,
                    float(window_end_min) + WINDOW_CONTEXT_MARGIN_MIN,
                )
            selected_ms = self.ms_events[self.ms_events["event_id"].eq(ms_event_id)]
            if selected_ms.empty:
                return None
            ms_row = selected_ms.iloc[0]
            raw_time_min = float(ms_row["time_min"])
            plot_time_min = (
                float(ms_row["time_sec"]) + self.ms_shift_sec_at(raw_time_min, "aligned")
            ) / 60.0
            margin_min = WINDOW_CONTEXT_MARGIN_MIN + POST_QC_CANDIDATE_TOL_SEC / 60.0 + 0.01
            return min(raw_time_min, plot_time_min) - margin_min, max(raw_time_min, plot_time_min) + margin_min

        if normalized_anchor_ids is not None and not allow_lif_missing:
            for group in self.alignment.get("qc_groups", {}).get("groups", []):
                if (
                    qc_anchor_peak_id_map(group) == normalized_anchor_ids
                    and str(group.get("ms_event_id")) == ms_event_id
                ):
                    review_start = window_start_min
                    review_end = window_end_min
                    if review_start is None or review_end is None:
                        group_plot_times = qc_group_plot_times(group)
                        if not group_plot_times:
                            raise BadRequest(
                                "Selected front-QC anchors have no active display-time relation"
                            )
                        review_start = min(group_plot_times)
                        review_end = max(group_plot_times)
                    return self.review_auto_candidate(
                        candidate_id_for_group(group),
                        "accepted",
                        window_start_min=float(review_start),
                        window_end_min=float(review_end),
                        time_mode=time_mode,
                        clear_qc_alignment_model=clear_qc_alignment_model,
                    )
        elif normalized_anchor_ids is not None:
            lookup_bounds = post_qc_lookup_bounds()
            for group in self.build_post_qc_candidates(*(lookup_bounds or (0.0, 0.0)), "aligned"):
                if (
                    qc_anchor_peak_id_map(group) == normalized_anchor_ids
                    and str(group.get("ms_event_id")) == ms_event_id
                ):
                    return self.review_auto_candidate(
                        post_qc_candidate_id(group),
                        "accepted",
                        window_start_min=(
                            float(window_start_min) if window_start_min is not None else (lookup_bounds or (None, None))[0]
                        ),
                        window_end_min=(
                            float(window_end_min) if window_end_min is not None else (lookup_bounds or (None, None))[1]
                        ),
                        time_mode=time_mode or "aligned",
                        clear_qc_alignment_model=clear_qc_alignment_model,
                    )
        elif not allow_lif_missing:
            for group in self.alignment.get("qc_groups", {}).get("groups", []):
                if (
                    str(group.get("g2_peak_id")) == g2_peak_id
                    and str(group.get("r1_peak_id")) == r1_peak_id
                    and str(group.get("ms_event_id")) == ms_event_id
                ):
                    return self.review_auto_candidate(
                        candidate_id_for_group(group),
                        "accepted",
                        window_start_min=window_start_min,
                        window_end_min=window_end_min,
                        time_mode=time_mode,
                        clear_qc_alignment_model=clear_qc_alignment_model,
                    )
        elif g2_peak_id and r1_peak_id:
            lookup_bounds = post_qc_lookup_bounds()
            for group in self.build_post_qc_candidates(*(lookup_bounds or (0.0, 0.0)), "aligned"):
                if (
                    str(group.get("g2_peak_id")) == g2_peak_id
                    and str(group.get("r1_peak_id")) == r1_peak_id
                    and str(group.get("ms_event_id")) == ms_event_id
                ):
                    return self.review_auto_candidate(
                        post_qc_candidate_id(group),
                        "accepted",
                        window_start_min=(
                            float(window_start_min) if window_start_min is not None else (lookup_bounds or (None, None))[0]
                        ),
                        window_end_min=(
                            float(window_end_min) if window_end_min is not None else (lookup_bounds or (None, None))[1]
                        ),
                        time_mode=time_mode or "aligned",
                        clear_qc_alignment_model=clear_qc_alignment_model,
                    )
        payload = self.payload_from_manual_triplet(
            g2_peak_id,
            r1_peak_id,
            ms_event_id,
            allow_lif_missing=allow_lif_missing,
            lif_anchor_peak_ids=normalized_anchor_ids,
            anchor_channels=configured_anchors,
            calibration_segment=selected_segment,
            post_qc_window=selected_post_window,
        )
        annotation_id = manual_annotation_id(
            g2_peak_id,
            r1_peak_id,
            ms_event_id,
            lif_anchor_peak_ids=normalized_anchor_ids,
        )
        existing = self.store.get(annotation_id)
        self.ensure_third_stage_acceptance_allowed(
            payload,
            annotation_id=annotation_id,
        )
        invalidate_qc_alignment = self.require_qc_evidence_invalidation_confirmation(
            payload,
            clear_qc_alignment_model=clear_qc_alignment_model,
            previous_review_status=str((existing or {}).get("review_status") or "pending"),
            new_review_status="accepted",
        )
        row = self.store.upsert_review(
            annotation_id=annotation_id,
            source="manual_created",
            review_status="accepted",
            payload=payload,
            action="manual_create_accept",
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
            invalidate_qc_alignment_model=invalidate_qc_alignment,
        )
        if invalidate_qc_alignment:
            self.invalidate_qc_model_request_reads()
            self.reset_to_automatic_qc_alignment()
        return row

    def create_manual_cell_pair(
        self,
        lif_channel: str,
        lif_peak_id: str,
        ms_event_id: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
    ) -> dict[str, Any]:
        with request_read_snapshot():
            return self._create_manual_cell_pair_from_request_snapshot(
                lif_channel=lif_channel,
                lif_peak_id=lif_peak_id,
                ms_event_id=ms_event_id,
                window_start_min=window_start_min,
                window_end_min=window_end_min,
                time_mode=time_mode,
            )

    def _create_manual_cell_pair_from_request_snapshot(
        self,
        lif_channel: str,
        lif_peak_id: str,
        ms_event_id: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
    ) -> dict[str, Any]:
        lif_channel = str(lif_channel).strip()
        lif_peak_id = optional_peak_id(lif_peak_id) or ""
        ms_event_id = optional_peak_id(ms_event_id) or ""
        if lif_channel not in set(self.cell_annotation_channels()):
            raise BadRequest(f"Manual cell pair requires one configured LIF peak from {', '.join(self.cell_annotation_channels())}")
        if not lif_peak_id or not ms_event_id:
            raise BadRequest("Manual cell pair requires one LIF peak and one MS760 event")
        if not self.frozen_time_model():
            raise BadRequest("Freeze local time model before creating cell annotations")
        if ms_event_id in self.accepted_qc_survey_ms_event_ids():
            raise BadRequest("This MS760 event has already been accepted as QC in QC survey; clear that QC annotation before creating a cell pair")
        payload = self.payload_from_cell_ids(lif_channel, lif_peak_id, ms_event_id)
        if window_start_min is not None and window_end_min is not None:
            for row in self.build_cell_candidates(
                float(window_start_min) - WINDOW_CONTEXT_MARGIN_MIN,
                float(window_end_min) + WINDOW_CONTEXT_MARGIN_MIN,
                "aligned",
            ):
                if (
                    str(row.get("lif_channel")) == lif_channel
                    and str(row.get("lif_peak_id")) == lif_peak_id
                    and str(row.get("ms_event_id")) == ms_event_id
                ):
                    return self.review_auto_candidate(
                        cell_candidate_id(row),
                        "accepted",
                        window_start_min=window_start_min,
                        window_end_min=window_end_min,
                        time_mode=time_mode,
                    )
        payload = {
            **payload,
            "candidate_id": None,
            "candidate_type": "manual_cell_pair",
            "review_stage": "cell_annotation",
            "confidence_mode": "manual_cell_pair",
            "selection_reason": "manual_lif_ms760_pair",
        }
        annotation_id = manual_cell_annotation_id(lif_channel, lif_peak_id, ms_event_id)
        self.ensure_third_stage_acceptance_allowed(
            payload,
            annotation_id=annotation_id,
        )
        return self.store.upsert_review(
            annotation_id=annotation_id,
            source="manual_created",
            review_status="accepted",
            payload=payload,
            action="manual_cell_pair_create_accept",
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
        )

    def clear_manual_annotation(
        self,
        annotation_id: str,
        *,
        clear_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        existing = self.store.get(annotation_id)
        if not existing:
            return self.store.hard_delete_manual(annotation_id)
        if str(existing.get("review_stage") or "") == "qc_calibration":
            self.require_confirmed_calibration("清除前段校准证据")
        invalidate_qc_alignment = self.require_qc_evidence_invalidation_confirmation(
            existing,
            clear_qc_alignment_model=clear_qc_alignment_model,
            previous_review_status=str(existing.get("review_status") or "pending"),
            new_review_status=None,
        )
        result = self.store.hard_delete_manual(
            annotation_id,
            invalidate_qc_alignment_model=invalidate_qc_alignment,
        )
        if invalidate_qc_alignment:
            self.invalidate_qc_model_request_reads()
            self.reset_to_automatic_qc_alignment()
        return result

    def enrich_qc_candidate(
        self,
        group: dict[str, Any],
        *,
        post_qc: bool,
        stored_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        generated_id = post_qc_candidate_id(group) if post_qc else candidate_id_for_group(group)
        stored = stored_review or self.store.get(generated_id)
        annotation_id = str(stored.get("annotation_id")) if stored else generated_id
        active_version = str(self.active_time_model().get("time_model_version", ""))
        frozen = self.frozen_time_model()
        stored_version = str(stored.get("time_model_version", "")) if stored else ""
        strategy = self.project_config().get("post_qc_strategy", {}) if post_qc else {}
        current_strategy_hash = (
            str(self.project_config().get("post_qc_strategy_hash") or "")
            if post_qc
            else ""
        )
        stored_strategy_hash = (
            str(stored.get("post_qc_strategy_hash") or "") if stored else ""
        )
        stale_time_model = bool(post_qc and stored and stored_version != active_version)
        stale_strategy = bool(
            post_qc
            and stored
            and not strategy.get("compatibility_mode")
            and stored_strategy_hash != current_strategy_hash
        )
        if stale_time_model or stale_strategy:
            review_status = "pending"
        else:
            review_status = str(stored.get("review_status")) if stored else "pending"
        generated_type = (
            "qc_survey_post_10p5"
            if post_qc and bool(strategy.get("compatibility_mode"))
            else f"qc_survey_{strategy.get('mode')}"
            if post_qc
            else "qc_calibration_segment_anchor"
            if group.get("calibration_segment_id")
            else "qc_calibration_anchor_0_10p5"
        )
        return {
            **group,
            **self.time_model_payload_fields(),
            "annotation_id": annotation_id,
            "candidate_id": generated_id,
            "candidate_type": (
                str(stored.get("candidate_type"))
                if stored and stored.get("candidate_type") and not stale_strategy
                else generated_type
            ),
            "source": str(stored.get("source")) if stored else "auto_candidate",
            "review_status": review_status,
            "exportable": review_status == "accepted",
            "review_enabled": (not post_qc) or bool(frozen),
            "post_qc_strategy_hash": (
                current_strategy_hash if post_qc else None
            ),
            "stale_review_status": (
                str(stored.get("review_status"))
                if stored and (stale_time_model or stale_strategy)
                else None
            ),
            "stale_time_model_version": stored_version if stale_time_model else None,
            "stale_post_qc_strategy_hash": stored_strategy_hash if stale_strategy else None,
        }

    def build_post_qc_candidates(self, context_start_min: float, context_end_min: float, time_mode: str) -> list[dict[str, Any]]:
        config = self.project_config()
        qc_end = float(config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN))
        if time_mode != "aligned" or context_end_min <= qc_end:
            return []
        if not self.frozen_time_model():
            return []
        strategy = normalize_post_qc_strategy(
            config.get("post_qc_strategy"), self.acquisition_layout
        )
        if strategy["mode"] == "disabled":
            return []
        model = self.active_time_model()
        pair_offset = self.alignment.get("qc_groups", {}).get("lif_anchor_b_minus_anchor_a_offset_sec")
        if pair_offset is None:
            pair_offset = self.alignment.get("qc_groups", {}).get("lif_r1_minus_g2_offset_sec")
        if pair_offset is None:
            pair_offset = 0.0
        ms_shift_sec = 0.0
        if context_end_min >= float(model.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)):
            ms_shift_sec = float(model.get("ms_local_delta_sec", 0.0) or 0.0)
        if bool(strategy.get("compatibility_mode")):
            groups = qc_triplets_for_range(
                self.lif_peaks,
                self.ms_events,
                context_start_min=context_start_min,
                context_end_min=context_end_min,
                qc_calibration_end_min=qc_end,
                green_shift_sec=float(self.alignment["green_to_ms_shift_sec"]),
                red_shift_sec=float(self.alignment["red_to_ms_shift_sec"]),
                ms_shift_sec=ms_shift_sec,
                pair_offset_sec=float(pair_offset),
                tolerance_sec=POST_QC_CANDIDATE_TOL_SEC,
                axis_shifts_sec=self.alignment.get("axis_shifts_sec"),
                channel_time_axes=self.alignment.get("channel_time_axes"),
                qc_anchor_channels=self.alignment.get("qc_anchor_channels"),
            )
        else:
            current_strategy_hash = str(config.get("post_qc_strategy_hash") or "")
            if strategy["mode"] == "signature":
                requested_ranges = [
                    {
                        "start_min": max(float(context_start_min), qc_end),
                        "end_min": float(context_end_min),
                        "reference_channels": list(strategy["reference_channels"]),
                        "window_id": None,
                    }
                ]
            else:
                requested_ranges = [
                    {
                        "start_min": max(
                            float(context_start_min), qc_end, float(window["start_min"])
                        ),
                        "end_min": min(
                            float(context_end_min), float(window["end_min"])
                        ),
                        "reference_channels": list(window["reference_channels"]),
                        "window_id": str(window["window_id"]),
                    }
                    for window in strategy["windows"]
                ]
            annotation_start = float(
                model.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
            )
            axis_shifts = dict(self.alignment.get("axis_shifts_sec") or {})
            channel_axes = dict(
                self.alignment.get("channel_time_axes")
                or normalize_acquisition_layout(self.acquisition_layout)["channel_time_axes"]
            )
            groups = []
            seen_relations: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
            for requested in requested_ranges:
                range_start = float(requested["start_min"])
                range_end = float(requested["end_min"])
                if range_end < range_start:
                    continue
                split_policies: list[tuple[bool, float]] = []
                if range_start < annotation_start:
                    split_policies.append((False, 0.0))
                if range_end >= annotation_start:
                    split_policies.append(
                        (
                            True,
                            float(model.get("ms_local_delta_sec", 0.0) or 0.0),
                        )
                    )
                for post_annotation, split_delta in split_policies:
                    matched = multi_anchor_groups_for_range(
                        self.lif_peaks,
                        self.ms_events,
                        anchor_channels=list(requested["reference_channels"]),
                        channel_time_axes=channel_axes,
                        axis_shifts_sec=axis_shifts,
                        context_start_min=range_start,
                        context_end_min=range_end,
                        minimum_raw_time_min=qc_end,
                        ms_shift_sec=split_delta,
                        tolerance_sec=POST_QC_CANDIDATE_TOL_SEC,
                    )
                    for group in matched:
                        # The raw MS event time owns the delta regime.  Calling
                        # the matcher over the full requested range preserves
                        # valid near-boundary LIF evidence, while this strict
                        # partition assigns annotation_start itself to the
                        # frozen post delta exactly once.
                        group_is_post = (
                            float(group.get("ms_time_min")) >= annotation_start
                        )
                        if group_is_post != post_annotation:
                            continue
                        group["post_qc_strategy_mode"] = str(strategy["mode"])
                        group["post_qc_window_id"] = requested["window_id"]
                        group["post_qc_strategy_hash"] = current_strategy_hash
                        relation = qc_relation_key(group)
                        if relation is not None and relation in seen_relations:
                            continue
                        if relation is not None:
                            seen_relations.add(relation)
                        groups.append(group)
            groups.sort(
                key=lambda item: (
                    float(item.get("ms_plot_time_min", item.get("ms_time_min", 0.0))),
                    str(item.get("post_qc_window_id") or ""),
                )
            )
            for rank, group in enumerate(groups, start=1):
                group["rank"] = rank
        allowed_ids = self.cell_event_map_event_ids()
        accepted_cell_ids = self.accepted_cell_annotation_ms_event_ids()
        groups = [
            group
            for group in groups
            if (allowed_ids is None or str(group.get("ms_event_id")) in allowed_ids)
            and str(group.get("ms_event_id")) not in accepted_cell_ids
        ]
        return [self.enrich_qc_candidate(group, post_qc=True) for group in groups]

    def enrich_cell_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        annotation_id = cell_candidate_id(row)
        stored = self.store.get(annotation_id)
        active_version = str(self.active_time_model().get("time_model_version", ""))
        stored_version = str(stored.get("time_model_version", "")) if stored else ""
        if stored and stored_version != active_version:
            review_status = "pending"
        else:
            review_status = str(stored.get("review_status")) if stored else "pending"
        return {
            **row,
            **self.time_model_payload_fields(),
            "annotation_id": annotation_id,
            "candidate_id": annotation_id,
            "candidate_type": str(row.get("candidate_type") or "cell_high_confidence"),
            "source": "auto_candidate",
            "review_status": review_status,
            "exportable": review_status == "accepted",
            "review_enabled": bool(self.frozen_time_model()),
            "stale_review_status": str(stored.get("review_status")) if stored and stored_version != active_version else None,
            "stale_time_model_version": stored_version if stored and stored_version != active_version else None,
        }

    def build_cell_candidates(self, context_start_min: float, context_end_min: float, time_mode: str) -> list[dict[str, Any]]:
        frozen = self.frozen_time_model()
        if not frozen:
            return []
        annotation_start = float(frozen.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN))
        if time_mode != "aligned" or context_end_min < annotation_start:
            return []
        ms_shift_sec = float(frozen.get("ms_local_delta_sec", 0.0) or 0.0)
        excluded_ms_event_ids = self.accepted_qc_survey_ms_event_ids()
        rows: list[dict[str, Any]] = []
        for channel in self.cell_annotation_channels():
            rows.extend(
                high_confidence_cell_pairs(
                    self.lif_peaks,
                    self.ms_events,
                    channel=channel,
                    context_start_min=context_start_min,
                    context_end_min=context_end_min,
                    shift_sec=self.channel_shift_sec(channel, "aligned"),
                    ms_shift_sec=ms_shift_sec,
                    annotation_start_min=annotation_start,
                    excluded_ms_event_ids=excluded_ms_event_ids,
                    label=self.cell_label_for_channel(channel),
                )
            )
        rows.sort(key=lambda item: (float(item["ms_plot_time_min"]), str(item["lif_channel"])))
        allowed_ids = self.cell_event_map_event_ids()
        rows = [
            row
            for row in rows
            if allowed_ids is None or str(row.get("ms_event_id")) in allowed_ids
        ]
        rows_by_ms: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            rows_by_ms.setdefault(str(row.get("ms_event_id") or ""), []).append(row)
        for event_rows in rows_by_ms.values():
            channels = {str(row.get("lif_channel") or "") for row in event_rows}
            if len(channels) <= 1:
                continue
            alternatives = [
                {
                    "lif_channel": str(item.get("lif_channel") or ""),
                    "lif_peak_id": str(item.get("lif_peak_id") or ""),
                    "label": str(item.get("label") or "cell"),
                    "abs_residual_sec": float(item.get("abs_residual_sec", 0.0) or 0.0),
                }
                for item in event_rows
            ]
            alternatives.sort(
                key=lambda item: (item["abs_residual_sec"], item["lif_channel"])
            )
            for row in event_rows:
                row.update(
                    {
                        "candidate_type": "cell_cross_channel_ambiguous",
                        "cross_channel_candidate_conflict": True,
                        "arbitration_status": "manual_required",
                        "arbitration_reason": "multiple_cell_channels_match_same_ms_event",
                        "cross_channel_alternatives": alternatives,
                        "selection_reason": "manual_cross_channel_arbitration_required",
                    }
                )
        enriched = [
            self.enrich_cell_candidate(row)
            for row in rows
        ]
        accepted_cell_ids = self.accepted_cell_annotation_ms_event_ids()
        return [
            row
            for row in enriched
            if str(row.get("ms_event_id")) not in accepted_cell_ids
            or str(row.get("review_status")) == "accepted"
        ]

    def accept_pending_auto_candidates_in_window(
        self,
        start_min: float,
        window_min: float,
        time_mode: str,
        stage: str = "qc_calibration",
        clear_qc_alignment_model: bool = False,
    ) -> dict[str, Any]:
        if stage == "cell_annotation":
            raise BadRequest("Cell candidates require individual review")
        if stage not in {"qc_calibration", "qc_survey"}:
            raise BadRequest("stage must be qc_calibration or qc_survey")
        if stage == "qc_survey" and not self.frozen_time_model():
            raise BadRequest("Freeze local time model before accepting post-calibration candidates")
        window = self.window(start_min=start_min, window_min=window_min, time_mode=time_mode)
        source_key = {
            "qc_calibration": "alignment_groups",
            "qc_survey": "post_qc_candidates",
        }[stage]
        prepared: list[tuple[str, dict[str, Any]]] = []
        accepted = []
        skipped = []
        for group in window.get(source_key, []):
            block_reason = qc_group_batch_accept_block_reason(
                group,
                window_start_min=float(window["start_min"]),
                window_end_min=float(window["end_min"]),
            )
            if block_reason:
                skipped.append({"annotation_id": group.get("annotation_id"), "reason": block_reason})
                continue
            annotation_id = str(group["annotation_id"])
            prepared.append(
                (
                    annotation_id,
                    self.payload_from_auto_candidate_id(
                        annotation_id,
                        window_start_min=float(window["start_min"]),
                        window_end_min=float(window["end_min"]),
                    ),
                )
            )
        invalidate_qc_alignment = bool(
            prepared
            and stage == "qc_calibration"
            and self.store.qc_alignment_model()
        )
        if invalidate_qc_alignment and not clear_qc_alignment_model:
            raise BadRequest(
                "修改 QC 校正证据会清除已应用的 QC 对齐和下游 time model；请确认后重新重算 QC 对齐"
            )
        try:
            for index, (annotation_id, annotation_payload) in enumerate(prepared):
                row = self.store.upsert_review(
                    annotation_id=annotation_id,
                    source="auto_candidate",
                    review_status="accepted",
                    payload=annotation_payload,
                    action="auto_candidate_accepted",
                    window_start_min=float(window["start_min"]),
                    window_end_min=float(window["end_min"]),
                    time_mode=str(window["time_mode"]),
                    invalidate_qc_alignment_model=invalidate_qc_alignment and index == 0,
                )
                accepted.append(row)
        finally:
            if invalidate_qc_alignment and not self.store.qc_alignment_model():
                self.invalidate_qc_model_request_reads()
                self.reset_to_automatic_qc_alignment()
        return {
            "accepted_count": len(accepted),
            "skipped_count": len(skipped),
            "accepted_annotation_ids": [row["annotation_id"] for row in accepted],
            "skipped": skipped,
            "window_start_min": window["start_min"],
            "window_end_min": window["end_min"],
            "time_mode": window["time_mode"],
            "stage": stage,
        }

    def window_annotations(
        self,
        window_start_min: float,
        window_end_min: float,
        *,
        context_start_min: float | None = None,
        context_end_min: float | None = None,
    ) -> list[dict[str, Any]]:
        context_start = float(window_start_min) if context_start_min is None else float(context_start_min)
        context_end = float(window_end_min) if context_end_min is None else float(context_end_min)
        rows = []
        for row in self.store.records():
            status = str(row.get("review_status", "pending"))
            if status == "pending":
                continue
            stage = self.manual_annotation_stage(row)
            if stage in {"qc_survey", "cell_annotation"}:
                frozen = self.frozen_time_model()
                active_version = str(frozen.get("time_model_version", "")) if frozen else ""
                row_version = str(row.get("time_model_version") or "")
                if not active_version or row_version != active_version:
                    continue
            if stage == "qc_survey" and not self.qc_survey_matches_current_strategy(row):
                continue
            dynamic_plot_times = row.get("lif_anchor_plot_times_min")
            lif_plot_times = [row.get("lif_plot_time_min")]
            if isinstance(dynamic_plot_times, dict):
                lif_plot_times.extend(dynamic_plot_times.values())
            else:
                lif_plot_times.extend([row.get("g2_plot_time_min"), row.get("r1_plot_time_min")])
            if saved_relation_belongs_to_window(
                ms_plot_time_min=row.get("ms_plot_time_min"),
                lif_plot_times_min=lif_plot_times,
                window_start_min=float(window_start_min),
                window_end_min=float(window_end_min),
                context_start_min=context_start,
                context_end_min=context_end,
            ):
                rows.append(row)
        rows.sort(key=lambda item: float(item.get("ms_plot_time_min", 0.0)))
        return rows

    def manual_annotation_stage(
        self,
        row: dict[str, Any],
        *,
        annotation_start_min: float | None = None,
    ) -> str:
        explicit = str(row.get("review_stage") or "")
        if explicit in {"qc_calibration", "qc_survey", "cell_annotation"}:
            return explicit
        if str(row.get("candidate_type", "")).startswith("cell") or row.get("candidate_type") == "manual_cell_pair":
            return "cell_annotation"
        if row.get("candidate_type") == "manual_qc_anchor_partial":
            return "qc_survey"
        ms_time = row.get("ms_time_min")
        try:
            ms_time_float = float(ms_time)
        except (TypeError, ValueError):
            return "qc_calibration"
        annotation_start = (
            float(annotation_start_min)
            if annotation_start_min is not None
            else float(self.project_config().get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN))
        )
        return "qc_survey" if ms_time_float >= annotation_start else "qc_calibration"

    def qc_survey_matches_current_strategy(
        self,
        row: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> bool:
        config = config or self.project_config()
        strategy = normalize_post_qc_strategy(
            config.get("post_qc_strategy"), self.acquisition_layout
        )
        if strategy["mode"] == "disabled":
            return False
        candidate_type = self.infer_candidate_type(row)
        if strategy.get("compatibility_mode"):
            if not (
                candidate_type == "qc_survey_post_10p5"
                or candidate_type.startswith("manual_qc")
            ):
                return False
            try:
                ms_time = float(row.get("ms_time_min"))
            except (TypeError, ValueError):
                return False
            annotation_start = float(
                config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
            )
            return ms_time >= annotation_start
        if not candidate_type.startswith("qc_survey_"):
            return False
        return bool(row.get("post_qc_strategy_hash")) and str(
            row.get("post_qc_strategy_hash")
        ) == str(config.get("post_qc_strategy_hash") or "")

    def is_qc_survey_annotation(self, row: dict[str, Any]) -> bool:
        if str(row.get("review_status")) != "accepted":
            return False
        frozen = self.frozen_time_model()
        active_version = str(frozen.get("time_model_version", "")) if frozen else ""
        row_version = str(row.get("time_model_version") or "")
        if not active_version or row_version != active_version:
            return False
        return self.qc_survey_matches_current_strategy(row)

    def accepted_qc_survey_ms_event_ids(self) -> set[str]:
        ids: set[str] = set()
        for row in self.store.records():
            if not self.is_qc_survey_annotation(row):
                continue
            ms_event_id = row.get("ms_event_id")
            if ms_event_id:
                ids.add(str(ms_event_id))
        return ids

    def is_cell_annotation(self, row: dict[str, Any]) -> bool:
        if str(row.get("review_status")) != "accepted":
            return False
        frozen = self.frozen_time_model()
        active_version = str(frozen.get("time_model_version", "")) if frozen else ""
        if not active_version or str(row.get("time_model_version") or "") != active_version:
            return False
        return self.annotation_review_stage(row) == "cell_annotation"

    def accepted_cell_annotation_ms_event_ids(self) -> set[str]:
        return {
            str(row["ms_event_id"])
            for row in self.store.records()
            if self.is_cell_annotation(row) and row.get("ms_event_id")
        }

    def accepted_qc_survey_anchors_for_window(
        self,
        context_start_min: float,
        context_end_min: float,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self.store.records():
            if not self.is_qc_survey_annotation(row):
                continue
            ms_plot_time = row.get("ms_plot_time_min")
            if not isinstance(ms_plot_time, (int, float)):
                continue
            if context_start_min <= float(ms_plot_time) <= context_end_min:
                rows.append(row)
        rows.sort(key=lambda item: float(item.get("ms_plot_time_min", 0.0)))
        return rows

    def window(
        self,
        start_min: float,
        window_min: float,
        time_mode: str = "aligned",
        preview_ms_delta_sec: float | None = None,
        lif_signal_mode: str = DEFAULT_LIF_SIGNAL_MODE,
        include_weak_lif_peaks: bool = False,
    ) -> dict[str, Any]:
        with request_read_snapshot():
            return self._window_from_request_snapshot(
                start_min=start_min,
                window_min=window_min,
                time_mode=time_mode,
                preview_ms_delta_sec=preview_ms_delta_sec,
                lif_signal_mode=lif_signal_mode,
                include_weak_lif_peaks=include_weak_lif_peaks,
            )

    def _window_from_request_snapshot(
        self,
        start_min: float,
        window_min: float,
        time_mode: str = "aligned",
        preview_ms_delta_sec: float | None = None,
        lif_signal_mode: str = DEFAULT_LIF_SIGNAL_MODE,
        include_weak_lif_peaks: bool = False,
    ) -> dict[str, Any]:
        if not math.isfinite(float(start_min)):
            raise BadRequest("start_min must be a finite number")
        if not math.isfinite(float(window_min)):
            raise BadRequest("window_min must be a finite number")
        if time_mode not in {"raw", "aligned"}:
            raise BadRequest("time_mode must be raw or aligned")
        lif_signal_mode = normalize_lif_signal_mode(lif_signal_mode)
        lif_trace_y_col = "raw" if lif_signal_mode == "raw" else "signal"
        lif_peak_y_col = "raw" if lif_signal_mode == "raw" else "height"
        meta = self.meta()
        time_min_min = float(meta["time_min_min"])
        time_min_max = float(meta["time_min_max"])
        window_min = max(0.25, min(float(window_min), 15.0))
        max_start = max(time_min_min, time_min_max - window_min)
        start_min = min(max(float(start_min), time_min_min), max_start)
        end_min = start_min + window_min
        context_start_min = max(time_min_min, start_min - WINDOW_CONTEXT_MARGIN_MIN)
        context_end_min = min(time_min_max, end_min + WINDOW_CONTEXT_MARGIN_MIN)

        trace_parts = []
        for channel, sub in self.lif_traces.groupby("channel", sort=False):
            shift_min = self.channel_shift_sec(str(channel), time_mode) / 60.0
            part = sub.copy()
            part["plot_time_min"] = part["time_min"] + shift_min
            part["applied_shift_sec"] = shift_min * 60.0
            trace_parts.append(part[part["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")])
        trace_window = pd.concat(trace_parts, ignore_index=True) if trace_parts else pd.DataFrame()

        peak_parts = []
        active_peak_detection = self.active_lif_peak_detection()
        for channel, sub in self.lif_peaks.groupby("channel", sort=False):
            shift_min = self.channel_shift_sec(str(channel), time_mode) / 60.0
            part = sub.copy()
            if "peak_tier" not in part.columns:
                part["peak_tier"] = "core"
            if "detector_version" not in part.columns:
                part["detector_version"] = int(
                    active_peak_detection["detector_version"]
                )
            if "detector_config_hash" not in part.columns:
                part["detector_config_hash"] = lif_peak_detection_hash(
                    active_peak_detection
                )
            if not bool(include_weak_lif_peaks):
                part = automatic_lif_peak_evidence(part).copy()
            part["raw_time_min"] = part["time_min"]
            part["raw_time_sec"] = part["time_sec"]
            part["plot_time_min"] = part["time_min"] + shift_min
            part["plot_time_sec"] = part["time_sec"] + shift_min * 60.0
            part["applied_shift_sec"] = shift_min * 60.0
            peak_parts.append(part[part["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")])
        peaks_window = (
            pd.concat(peak_parts, ignore_index=True).sort_values(["plot_time_min", "channel"])
            if peak_parts
            else pd.DataFrame()
        )
        ms_model = self.active_time_model()
        if preview_ms_delta_sec is not None and abs(float(preview_ms_delta_sec)) > LOCAL_DELTA_MAX_ABS_SEC:
            raise BadRequest(f"preview_ms_delta_sec absolute value must be <= {LOCAL_DELTA_MAX_ABS_SEC} sec")
        ms_delta_sec = float(ms_model.get("ms_local_delta_sec", 0.0) or 0.0)
        if preview_ms_delta_sec is not None:
            ms_delta_sec = float(preview_ms_delta_sec)
        ms_delta_min = ms_delta_sec / 60.0 if time_mode == "aligned" else 0.0
        annotation_start = float(ms_model.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN))
        scan_source = self.ms_scan.copy()
        scan_source["plot_time_min"] = scan_source["scan_start_time_min"] + np.where(
            scan_source["scan_start_time_min"].astype(float) >= annotation_start,
            ms_delta_min,
            0.0,
        )
        scan_source["applied_shift_sec"] = np.where(
            scan_source["scan_start_time_min"].astype(float) >= annotation_start,
            ms_delta_min * 60.0,
            0.0,
        )
        scan_window = scan_source[
            scan_source["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")
        ].copy()
        events_source = self.ms_events.copy()
        events_source["raw_time_min"] = events_source["time_min"]
        events_source["plot_time_min"] = events_source["time_min"] + np.where(
            events_source["time_min"].astype(float) >= annotation_start,
            ms_delta_min,
            0.0,
        )
        events_source["applied_shift_sec"] = np.where(
            events_source["time_min"].astype(float) >= annotation_start,
            ms_delta_min * 60.0,
            0.0,
        )
        cell_event_ids = self.cell_event_map_event_ids()
        events_source["in_cell_event_map"] = (
            True
            if cell_event_ids is None
            else events_source["event_id"].astype(str).isin(cell_event_ids)
        )
        events_window = events_source[
            events_source["plot_time_min"].between(context_start_min, context_end_min, inclusive="both")
        ].sort_values("plot_time_min").copy()
        events_window["raw_time_min"] = events_window["time_min"]

        lif_traces = {}
        for channel, sub in trace_window.groupby("channel", sort=False):
            lif_traces[str(channel)] = xy_records(sub, "plot_time_min", lif_trace_y_col)

        if not peaks_window.empty:
            peaks_window = peaks_window.copy()
            peaks_window["display_y"] = peaks_window[lif_peak_y_col]

        ms_760 = scan_window[["plot_time_min", "pc34_760_max_intensity"]].copy()
        ms_782 = scan_window[["plot_time_min", "qc_782_max_intensity"]].copy()

        peak_cols = [
            "peak_id",
            "channel",
            "label",
            "detector",
            "phase",
            "time_min",
            "time_sec",
            "raw_time_min",
            "raw_time_sec",
            "plot_time_min",
            "plot_time_sec",
            "applied_shift_sec",
            "raw",
            "height",
            "display_y",
            "prominence",
            "snr",
            "width_sec",
            "area",
            "nearest_gap_sec",
            "close_peak_risk",
            "merge_risk",
            "parent_raw_peak_ids",
            "peak_tier",
            "detector_version",
            "detector_config_hash",
            "local_snr",
            "template_similarity",
        ]
        event_cols = [
            "event_id",
            "event_strategy",
            "scan_id",
            "time_min",
            "time_sec",
            "raw_time_min",
            "plot_time_min",
            "applied_shift_sec",
            "apex_intensity",
            "peak_prominence",
            "peak_width_sec",
            "pc34_760_apex",
            "qc_782_apex",
            "tic_apex",
            "ratio_760_782_max_pseudo1",
            "collision_risk_high",
            "low_quality_scan_window",
            "nearest_event_gap_sec",
            "array_length_apex",
            "in_cell_event_map",
        ]

        alignment_groups = []
        if time_mode == "aligned":
            annotation_start = float(
                self.project_config().get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)
            )
            qc_review_rows = [
                row
                for row in self.store.records()
                if self.manual_annotation_stage(
                    row,
                    annotation_start_min=annotation_start,
                ) == "qc_calibration"
            ]
            reconciled_groups = reconcile_qc_calibration_groups(
                self.alignment.get("qc_groups", {}).get("groups", []),
                qc_review_rows,
            )
            for group, stored_review in reconciled_groups:
                if front_qc_group_belongs_to_window(
                    group,
                    start_min,
                    end_min,
                    context_margin_min=WINDOW_CONTEXT_MARGIN_MIN,
                ):
                    candidate = self.enrich_qc_candidate(
                        group,
                        post_qc=False,
                        stored_review=stored_review,
                    )
                    block_reason = qc_group_batch_accept_block_reason(
                        candidate,
                        window_start_min=start_min,
                        window_end_min=end_min,
                    )
                    candidate["batch_accept_block_reason"] = block_reason
                    candidate["batch_accept_eligible"] = block_reason is None
                    alignment_groups.append(candidate)

        post_qc_candidates = [
            candidate
            for candidate in self.build_post_qc_candidates(context_start_min, context_end_min, time_mode)
            if (plot_times := qc_group_plot_times(candidate))
            and all(start_min <= t <= end_min for t in plot_times)
        ]
        for candidate in post_qc_candidates:
            block_reason = qc_group_batch_accept_block_reason(
                candidate,
                window_start_min=start_min,
                window_end_min=end_min,
            )
            candidate["batch_accept_block_reason"] = block_reason
            candidate["batch_accept_eligible"] = block_reason is None
        cell_candidates = [
            candidate
            for candidate in self.build_cell_candidates(context_start_min, context_end_min, time_mode)
            if all(
                start_min <= float(candidate[key]) <= end_min
                for key in ("lif_plot_time_min", "ms_plot_time_min")
            )
        ]
        cell_qc_anchors = (
            self.accepted_qc_survey_anchors_for_window(start_min, end_min)
            if time_mode == "aligned"
            else []
        )

        represented_manual_ids = {
            str(group.get("annotation_id"))
            for group in alignment_groups
            if group.get("source") == "manual_created" and group.get("annotation_id")
        }
        annotations = [
            row
            for row in self.window_annotations(
                start_min,
                end_min,
                context_start_min=context_start_min,
                context_end_min=context_end_min,
            )
            if str(row.get("annotation_id")) not in represented_manual_ids
        ]
        annotation_counts = {status: 0 for status in REVIEW_STATUSES}
        for group in alignment_groups:
            status = str(group.get("review_status", "pending"))
            if status in annotation_counts:
                annotation_counts[status] += 1
        for row in annotations:
            if row.get("source") != "manual_created":
                continue
            if self.manual_annotation_stage(row) != "qc_calibration":
                continue
            status = str(row.get("review_status", "pending"))
            if status in annotation_counts:
                annotation_counts[status] += 1
        post_qc_counts = {status: 0 for status in REVIEW_STATUSES}
        for group in post_qc_candidates:
            status = str(group.get("review_status", "pending"))
            if status in post_qc_counts:
                post_qc_counts[status] += 1
        for row in annotations:
            if row.get("source") != "manual_created":
                continue
            if self.manual_annotation_stage(row) != "qc_survey":
                continue
            status = str(row.get("review_status", "pending"))
            if status in post_qc_counts:
                post_qc_counts[status] += 1
        cell_counts = {status: 0 for status in REVIEW_STATUSES}
        for group in cell_candidates:
            status = str(group.get("review_status", "pending"))
            if status in cell_counts:
                cell_counts[status] += 1

        third_stage_lif_peak_ids: set[str] = set()
        for row in [*post_qc_candidates, *cell_candidates, *cell_qc_anchors, *annotations]:
            event_id = str(row.get("ms_event_id") or "")
            if cell_event_ids is not None and event_id not in cell_event_ids:
                continue
            for peak_id in qc_anchor_peak_id_map(row).values():
                if peak_id:
                    third_stage_lif_peak_ids.add(str(peak_id))
            if row.get("lif_peak_id"):
                third_stage_lif_peak_ids.add(str(row["lif_peak_id"]))
        for row in annotations:
            if row.get("source") != "manual_created":
                continue
            if self.manual_annotation_stage(row) != "cell_annotation":
                continue
            status = str(row.get("review_status", "pending"))
            if status in cell_counts:
                cell_counts[status] += 1

        return {
            "start_min": start_min,
            "end_min": end_min,
            "window_min": window_min,
            "context_margin_min": WINDOW_CONTEXT_MARGIN_MIN,
            "context_start_min": context_start_min,
            "context_end_min": context_end_min,
            "time_mode": time_mode,
            "display_options": {
                "lif_signal_mode": lif_signal_mode,
                "lif_trace_y_col": lif_trace_y_col,
                "lif_peak_y_col": lif_peak_y_col,
                "include_weak_lif_peaks": bool(include_weak_lif_peaks),
            },
            "alignment": self.alignment,
            "project_config": self.project_config(),
            "time_model": self.active_time_model(),
            "preview_ms_delta_sec": clean_value(preview_ms_delta_sec),
            "alignment_groups": alignment_groups,
            "post_qc_candidates": post_qc_candidates,
            "cell_candidates": cell_candidates,
            "cell_qc_anchors": cell_qc_anchors,
            "annotations": annotations,
            "annotation_counts": annotation_counts,
            "post_qc_counts": post_qc_counts,
            "cell_counts": cell_counts,
            "third_stage_lif_peak_ids": sorted(third_stage_lif_peak_ids),
            "annotation_store": self.store.summary(),
            "lif_traces": lif_traces,
            "lif_peaks": records(peaks_window, peak_cols),
            "ms_traces": {
                "pc34_760_linear": xy_records(ms_760, "plot_time_min", "pc34_760_max_intensity"),
                "qc_782_linear": xy_records(ms_782, "plot_time_min", "qc_782_max_intensity"),
            },
            "ms_events": records(events_window, event_cols),
            "counts": {
                "lif_trace_points_returned": int(
                    trace_window["plot_time_min"].between(start_min, end_min, inclusive="both").sum()
                ),
                "lif_peaks": int(
                    peaks_window["plot_time_min"].between(start_min, end_min, inclusive="both").sum()
                ),
                "ms_scan_points_returned": int(
                    scan_window["plot_time_min"].between(start_min, end_min, inclusive="both").sum()
                ),
                "ms_events": int(
                    events_window["plot_time_min"].between(start_min, end_min, inclusive="both").sum()
                ),
            },
        }


@dataclass(frozen=True)
class BootstrapAppData:
    project: ProjectPaths
    load_error: str
    project_selected: bool = False

    def project_config(self) -> dict[str, Any]:
        return {
            "qc_calibration_end_min": QC_SHIFT_WINDOW_MIN,
            "sample_valve_switch_min": DEFAULT_SAMPLE_VALVE_SWITCH_MIN,
            "annotation_start_min": DEFAULT_ANNOTATION_START_MIN,
            "local_delta_seed_window_min": DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN,
        }

    def active_time_model(self) -> dict[str, Any]:
        return {
            "time_model_name": "not_loaded",
            "time_model_version": "",
            "status": "not_loaded",
            "green_to_ms_shift_sec": 0.0,
            "red_to_ms_shift_sec": 0.0,
            "ms_local_delta_sec": 0.0,
        }

    def meta(self) -> dict[str, Any]:
        meta = {
            "bootstrap": True,
            "load_error": self.load_error,
            "root": str(self.project.project_dir) if self.project_selected else "",
            "project": None,
            "default_window_min": DEFAULT_WINDOW_MIN,
            "time_min_min": 0.0,
            "time_min_max": DEFAULT_WINDOW_MIN,
            "lif_trace_rows": 0,
            "lif_peak_rows": 0,
            "ms_event_rows": 0,
            "ms_scan_rows": 0,
            "lif_channels": [],
            "channel_identity_prior": infer_channel_identity_prior(self.project.raw_data_dir),
            "inputs": {
                "lif_traces": display_path(self.project.lif_traces_path, self.project.project_dir),
                "lif_peaks": display_path(self.project.lif_peaks_path, self.project.project_dir),
                "ms_events": display_path(self.project.ms_events_path, self.project.project_dir),
                "ms_scan_summary": display_path(self.project.ms_scan_path, self.project.project_dir),
            },
            "input_policy": "等待导入 2-4 个 LIF 原始文件、1 个 MS 原始文件和事件坐标 CSV；坐标源只读取白名单三列，不读取作者标签/h5ad/manual/V2/archive 输入。",
            "alignment": {"green_to_ms_shift_sec": 0.0, "red_to_ms_shift_sec": 0.0, "ms_shift_sec": 0.0},
            "project_config": self.project_config(),
            "time_model": self.active_time_model(),
            "annotation_store": {"counts": {"pending": 0, "accepted": 0, "rejected": 0}, "total": 0},
            "write_token": WRITE_TOKEN,
        }
        if self.project_selected:
            meta["project"] = {
                "project_dir": str(self.project.project_dir),
                "raw_data_dir": str(self.project.raw_data_dir),
                "annotation_db_path": str(self.project.annotation_db_path),
            }
        return meta

    def window(
        self,
        start_min: float,
        window_min: float,
        time_mode: str = "aligned",
        preview_ms_delta_sec: float | None = None,
        lif_signal_mode: str = DEFAULT_LIF_SIGNAL_MODE,
        include_weak_lif_peaks: bool = False,
    ) -> dict[str, Any]:
        window_min = max(0.25, min(float(window_min), 15.0))
        lif_signal_mode = normalize_lif_signal_mode(lif_signal_mode)
        lif_trace_y_col = "raw" if lif_signal_mode == "raw" else "signal"
        lif_peak_y_col = "raw" if lif_signal_mode == "raw" else "height"
        return {
            "start_min": 0.0,
            "end_min": window_min,
            "window_min": window_min,
            "context_margin_min": WINDOW_CONTEXT_MARGIN_MIN,
            "context_start_min": 0.0,
            "context_end_min": window_min,
            "time_mode": time_mode if time_mode in {"raw", "aligned"} else "aligned",
            "display_options": {
                "lif_signal_mode": lif_signal_mode,
                "lif_trace_y_col": lif_trace_y_col,
                "lif_peak_y_col": lif_peak_y_col,
                "include_weak_lif_peaks": bool(include_weak_lif_peaks),
            },
            "alignment": self.meta()["alignment"],
            "project_config": self.project_config(),
            "time_model": self.active_time_model(),
            "preview_ms_delta_sec": clean_value(preview_ms_delta_sec),
            "alignment_groups": [],
            "post_qc_candidates": [],
            "cell_candidates": [],
            "cell_qc_anchors": [],
            "annotations": [],
            "annotation_counts": {"pending": 0, "accepted": 0, "rejected": 0},
            "post_qc_counts": {"pending": 0, "accepted": 0, "rejected": 0},
            "cell_counts": {"pending": 0, "accepted": 0, "rejected": 0},
            "annotation_store": self.meta()["annotation_store"],
            "lif_traces": {},
            "lif_peaks": [],
            "ms_traces": {"pc34_760_linear": [], "qc_782_linear": []},
            "ms_events": [],
            "counts": {
                "lif_trace_points_returned": 0,
                "lif_peaks": 0,
                "ms_scan_points_returned": 0,
                "ms_events": 0,
            },
        }

    def unsupported(self, *args: Any, **kwargs: Any) -> None:
        raise BadRequest("当前尚未导入项目；请先使用“导入项目”生成前处理中间表。")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return self.unsupported


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LMA Studio</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #1b1f27;
      --muted: #667085;
      --line: #d7dce3;
      --axis: #8a94a6;
      --ms760: #1f5f99;
      --ms782: #2a7d67;
      --warn: #b42318;
      --shadow: 0 10px 26px rgba(20, 26, 36, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Arial, Helvetica, sans-serif;
    }
    header {
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 18px;
      background: #111827;
      color: #fff;
    }
    h1 {
      margin: 0;
      font-size: 17px;
      font-weight: 700;
      letter-spacing: 0;
      white-space: nowrap;
    }
    .header-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      min-width: 0;
    }
    .header-export-hint {
      color: #cbd5e1;
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .header-export-button {
      height: 34px;
      border-color: #fff;
      background: #fff;
      color: #111827;
      white-space: nowrap;
    }
    .header-secondary-button {
      height: 34px;
      border-color: #475467;
      background: #1f2937;
      color: #fff;
      white-space: nowrap;
    }
    .header-secondary-button[data-unavailable="true"] {
      border-color: #667085;
      background: #344054;
      color: #d0d5dd;
      cursor: help;
    }
    .policy {
      color: #667085;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: nowrap;
    }
    .plot-controls {
      padding: 0 0 9px;
      margin-bottom: 8px;
      border-bottom: 1px solid #edf0f4;
      flex-wrap: wrap;
    }
    .plot-controls label {
      display: flex;
      align-items: center;
      gap: 5px;
      min-width: 0;
    }
    .plot-controls select {
      min-width: 104px;
    }
    button, input, select {
      height: 34px;
      border: 1px solid #aab3c2;
      border-radius: 6px;
      background: #fff;
      color: #1b1f27;
      padding: 0 10px;
      font: inherit;
    }
    button {
      cursor: pointer;
      min-width: 38px;
      font-weight: 700;
    }
    button:active { transform: translateY(1px); }
    input { width: 92px; }
    main {
      display: grid;
      grid-template-columns: 220px minmax(760px, 1fr);
      gap: 14px;
      padding: 14px;
      min-height: calc(100dvh - 64px);
      align-items: start;
    }
    aside, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    aside {
      padding: 12px;
      overflow: auto;
    }
    .plot-panel {
      padding: 10px 12px 12px;
      min-width: 0;
      overflow: hidden;
      position: sticky;
      top: 78px;
      align-self: start;
    }
    .side-title {
      margin: 0 0 10px;
      font-size: 13px;
      font-weight: 700;
      color: #2b3442;
    }
    .metric {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      padding: 7px 0;
      border-top: 1px solid #edf0f4;
      color: var(--muted);
      font-size: 12px;
    }
    .metric span {
      min-width: 0;
    }
    .metric strong {
      color: var(--ink);
      font-size: 12px;
      text-align: right;
      white-space: nowrap;
    }
    .legend {
      display: grid;
      gap: 7px;
      margin-top: 8px;
    }
    .legend-row {
      display: flex;
      align-items: center;
      gap: 8px;
      color: #384252;
      font-size: 12px;
    }
    .swatch {
      width: 18px;
      height: 3px;
      border-radius: 2px;
      background: #444;
      flex: 0 0 auto;
    }
    .detail {
      height: 100%;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    .detail-content {
      min-height: 0;
      overflow: auto;
      border-top: 1px solid #edf0f4;
      padding-top: 8px;
    }
    .empty {
      color: var(--muted);
      font-size: 12px;
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 6px;
      margin: 8px 0 10px;
    }
    .status-pill {
      border: 1px solid #d7dce3;
      border-radius: 6px;
      padding: 5px 6px;
      font-size: 11px;
      color: #475467;
      background: #fff;
      text-align: center;
    }
    .stage-tabs {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      margin: 8px 0 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid #edf0f4;
    }
    .stage-tab {
      width: 100%;
      height: 32px;
      text-align: left;
      border-color: #d7dce3;
      background: #fff;
      color: #344054;
    }
    .stage-tab.active {
      background: #111827;
      border-color: #111827;
      color: #fff;
    }
    .stage-note {
      color: #667085;
      font-size: 12px;
      margin: 0 0 8px;
      line-height: 1.35;
    }
    .segmented {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 2px;
      padding: 2px;
      margin: 0 0 9px;
      border: 1px solid #d7dce3;
      border-radius: 7px;
      background: #f2f4f7;
    }
    .segmented.two {
      grid-template-columns: repeat(2, 1fr);
    }
    .segmented button {
      height: 27px;
      min-width: 0;
      padding: 0 6px;
      border: 0;
      background: transparent;
      color: #475467;
      font-size: 11px;
    }
    .segmented button.active {
      background: #fff;
      color: #111827;
      box-shadow: 0 1px 3px rgba(16,24,40,.12);
    }
    .status-pill strong {
      display: block;
      color: #111827;
      font-size: 13px;
    }
    .candidate-list {
      display: grid;
      gap: 6px;
      max-height: 260px;
      overflow: auto;
      border-top: 1px solid #edf0f4;
      padding-top: 8px;
    }
    .candidate-row {
      border: 1px solid #d7dce3;
      border-radius: 6px;
      padding: 7px;
      background: #fff;
      font-size: 12px;
    }
    .candidate-row.selected {
      border-color: #111827;
      box-shadow: inset 0 0 0 1px #111827;
    }
    .candidate-row.rejected {
      opacity: 0.58;
      background: #f6f7f9;
    }
    .row-title {
      display: flex;
      justify-content: space-between;
      gap: 6px;
      font-weight: 700;
      color: #111827;
    }
    .row-sub {
      color: #667085;
      margin-top: 3px;
      line-height: 1.35;
    }
    .row-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
      min-width: 0;
    }
    .row-actions button, .small-button {
      height: 28px;
      min-width: 0;
      max-width: 100%;
      padding: 0 8px;
      font-size: 12px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .row-actions button {
      flex: 1 1 64px;
      white-space: normal;
      line-height: 1.15;
    }
    .small-button {
      white-space: nowrap;
    }
    .small-button.secondary {
      background: #f8fafc;
      color: #344054;
    }
    .checkbox-row {
      display: flex;
      align-items: center;
      gap: 7px;
      color: #475467;
      font-size: 12px;
      margin-top: 8px;
    }
    .checkbox-row input {
      width: auto;
      height: auto;
    }
    .manual-box {
      border-top: 1px solid #edf0f4;
      padding-top: 8px;
      color: #475467;
      font-size: 12px;
    }
    .config-grid {
      display: grid;
      grid-template-columns: 1fr 74px;
      gap: 6px;
      align-items: center;
      color: #475467;
      font-size: 12px;
    }
    .config-grid input {
      width: 74px;
      height: 28px;
      padding: 0 6px;
    }
    .detector-config-grid {
      display: grid;
      grid-template-columns: 124px minmax(0, 1fr);
      gap: 8px 12px;
      align-items: start;
      color: #475467;
      font-size: 12px;
    }
    .detector-config-grid output {
      min-width: 0;
      overflow-wrap: anywhere;
      user-select: text;
      line-height: 1.45;
    }
    .detector-standard-card {
      border: 1px solid #d0d5dd;
      border-radius: 8px;
      padding: 9px 11px;
      background: #f8fafc;
      color: #344054;
      line-height: 1.45;
    }
    .config-save-status {
      min-height: 18px;
      margin-top: 10px;
      color: #667085;
      font-size: 12px;
      line-height: 1.4;
    }
    .config-save-status.success {
      color: #067647;
    }
    .config-save-status.error {
      color: #b42318;
    }
    .config-save-status.warning {
      color: #b54708;
    }
    .delta-slider {
      width: 100%;
      height: auto;
      padding: 0;
    }
    #deltaReadout,
    #freezeDelta {
      white-space: nowrap;
    }
    .manual-selection {
      display: grid;
      gap: 4px;
      margin: 6px 0;
    }
    .manual-mode-on {
      background: #111827;
      color: #fff;
      border-color: #111827;
    }
    .kv {
      display: grid;
      grid-template-columns: 112px 1fr;
      gap: 6px;
      padding: 4px 0;
      border-bottom: 1px solid #f0f2f5;
      font-size: 12px;
      word-break: break-word;
    }
    .kv span:first-child { color: var(--muted); }
    #chart {
      width: 100%;
      height: calc(100dvh - 166px);
      min-height: 560px;
      display: block;
      border: 1px solid #dfe3ea;
      border-radius: 6px;
      background: #fff;
    }
    .window-readout {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .window-readout strong { color: var(--ink); font-size: 13px; }
    #chart .peak-time-label,
    #chart .amplitude-label,
    #chart .track-label,
    #chart .axis-label {
      pointer-events: none;
      user-select: none;
    }
    .tooltip {
      position: fixed;
      z-index: 20;
      max-width: min(420px, calc(100vw - 28px));
      pointer-events: none;
      background: rgba(17, 24, 39, 0.96);
      color: #fff;
      border-radius: 6px;
      padding: 8px 9px;
      font-size: 12px;
      box-shadow: 0 10px 26px rgba(0, 0, 0, 0.20);
      display: none;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .interaction-hint {
      position: fixed;
      left: 50%;
      bottom: 24px;
      z-index: 60;
      max-width: min(420px, calc(100vw - 28px));
      padding: 8px 12px;
      border-radius: 7px;
      background: rgba(17, 24, 39, 0.94);
      color: #fff;
      font-size: 12px;
      line-height: 1.35;
      box-shadow: 0 8px 22px rgba(15, 23, 42, 0.22);
      opacity: 0;
      pointer-events: none;
      transform: translate(-50%, 8px);
      transition: opacity .14s ease, transform .14s ease;
    }
    .interaction-hint.show {
      opacity: 1;
      transform: translate(-50%, 0);
    }
    .context-menu {
      position: fixed;
      z-index: 30;
      min-width: 132px;
      display: none;
      background: #fff;
      border: 1px solid #cfd6e2;
      border-radius: 6px;
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.18);
      padding: 5px;
    }
    .context-menu-title {
      padding: 5px 7px 6px;
      color: #667085;
      font-size: 11px;
      border-bottom: 1px solid #edf0f4;
      margin-bottom: 4px;
      white-space: nowrap;
      max-width: 260px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .context-menu button {
      display: block;
      width: 100%;
      height: 30px;
      border: 0;
      background: transparent;
      text-align: left;
      padding: 0 8px;
      color: #111827;
      font-size: 12px;
      font-weight: 700;
      border-radius: 4px;
      cursor: pointer;
    }
    .context-menu button:hover {
      background: #f2f4f7;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      z-index: 40;
      display: none;
      align-items: flex-start;
      justify-content: center;
      padding: 72px 16px 24px;
      background: rgba(17, 24, 39, 0.42);
    }
    .modal-backdrop.open {
      display: flex;
    }
    .modal {
      width: min(760px, 100%);
      max-height: calc(100dvh - 104px);
      overflow: auto;
      border: 1px solid #cfd6e2;
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 18px 46px rgba(15, 23, 42, 0.24);
      padding: 16px;
    }
    .modal.import-modal {
      width: min(1240px, 100%);
      padding: 20px 24px 22px;
    }
    .modal-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid #edf0f4;
      margin-bottom: 12px;
    }
    .modal-head > button {
      flex: 0 0 auto;
      white-space: nowrap;
    }
    .modal-title {
      margin: 0;
      font-size: 16px;
      font-weight: 700;
    }
    .import-grid {
      display: grid;
      grid-template-columns: 128px minmax(0, 1fr);
      gap: 9px 10px;
      align-items: center;
    }
    .import-grid label {
      color: #475467;
      font-size: 12px;
    }
    .import-grid input {
      width: 100%;
    }
    .path-picker-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 64px;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }
    .path-picker-row input {
      width: 100%;
      min-width: 0;
    }
    .path-picker-button {
      width: 64px;
      height: 34px;
      white-space: nowrap;
    }
    .attach-map-panel {
      margin-top: 14px;
      padding: 14px;
      border: 1px solid #d7deea;
      border-radius: 8px;
      background: #f8fafc;
      color: #344054;
    }
    .attach-map-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 4px;
    }
    .attach-map-heading .side-title {
      margin: 0;
      color: #111827;
      font-size: 14px;
    }
    .attach-map-badge {
      flex: 0 0 auto;
      padding: 2px 7px;
      border: 1px solid #f0c36a;
      border-radius: 999px;
      background: #fffaeb;
      color: #93370d;
      font-size: 10px;
      font-weight: 700;
    }
    .attach-map-copy,
    .attach-map-requirements {
      margin: 4px 0 10px;
      color: #667085;
      font-size: 12px;
      line-height: 1.5;
    }
    .attach-map-label {
      display: block;
      margin: 0 0 5px;
      color: #344054;
      font-size: 12px;
      font-weight: 700;
    }
    .attach-map-panel .path-picker-row {
      grid-template-columns: minmax(0, 1fr) 88px;
    }
    .attach-map-panel .path-picker-button {
      width: 88px;
    }
    .attach-map-panel input {
      text-overflow: ellipsis;
    }
    .attach-map-requirements code {
      color: #344054;
      font-size: 11px;
    }
    .attach-map-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 10px;
    }
    .attach-map-actions .small-button {
      flex: 0 0 auto;
    }
    .attach-map-ready {
      color: #667085;
      font-size: 11px;
    }
    .lif-input-table {
      min-width: 0;
    }
    .lif-input-head,
    .lif-input-row {
      display: grid;
      grid-template-columns: 44px 70px 86px 116px 132px 72px minmax(160px, 1fr) 32px;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }
    .lif-input-head {
      padding: 0 0 6px;
      border-bottom: 1px solid #e4e7ec;
      color: #475467;
      font-size: 11px;
      font-weight: 700;
    }
    .lif-input-head small {
      display: block;
      margin-top: 2px;
      color: #98a2b3;
      font-size: 10px;
      font-weight: 400;
      line-height: 1.25;
    }
    .lif-input-row {
      padding-top: 8px;
    }
    .lif-input-slot {
      color: #344054;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .lif-input-row input,
    .lif-input-row select {
      width: 100%;
      min-width: 0;
    }
    .lif-field {
      min-width: 0;
    }
    .lif-axis-auto {
      min-height: 34px;
      display: flex;
      align-items: center;
      padding: 5px 8px;
      border: 1px solid #d7dce3;
      border-radius: 6px;
      background: #f2f4f7;
      color: #344054;
      font-size: 11px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .lif-mobile-label {
      display: none;
    }
    .lif-file-picker {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 64px;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }
    .lif-use-options {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .lif-use-options label {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      white-space: nowrap;
      font-size: 11px;
    }
    .lif-use-options input {
      width: 14px;
      height: 14px;
      margin: 0;
    }
    .lif-remove {
      width: 32px;
      min-width: 32px;
      padding: 0;
      color: #b42318;
    }
    .lif-import-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 8px;
    }
    .qc-anchor-field {
      min-width: 0;
    }
    .qc-anchor-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-height: 34px;
      align-items: center;
    }
    .qc-anchor-option {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-width: 88px;
      height: 34px;
      margin: 0;
      padding: 0 10px;
      border: 1px solid #b7c2d4;
      border-radius: 6px;
      color: #344054;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .import-grid .qc-anchor-option input {
      flex: 0 0 16px;
      width: 16px;
      height: 16px;
      margin: 0;
      padding: 0;
    }
    .qc-anchor-option:has(input:checked) {
      border-color: #475467;
      background: #f2f4f7;
      color: #111827;
    }
    .qc-anchor-empty {
      color: #98a2b3;
      font-size: 12px;
    }
    .qc-anchor-rule {
      margin-top: 5px;
      color: #667085;
      font-size: 11px;
      line-height: 1.35;
    }
    .protocol-editor {
      display: grid;
      gap: 8px;
      min-width: 0;
    }
    .protocol-row {
      display: grid;
      grid-template-columns: 34px 130px 84px 84px minmax(190px, 1fr) 92px 32px;
      gap: 7px;
      align-items: end;
      padding: 8px;
      border: 1px solid #d7dce3;
      border-radius: 7px;
      background: #f8fafc;
    }
    .protocol-row > strong:first-child {
      align-self: center;
    }
    .modal.import-modal .protocol-row {
      grid-template-columns: 34px 150px 116px 116px minmax(190px, 1fr) 104px 32px;
    }
    .protocol-row input,
    .protocol-row select {
      width: 100%;
      min-width: 0;
    }
    .protocol-channel-options {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px 10px;
      min-height: 34px;
      align-self: end;
    }
    .protocol-time-field {
      display: grid;
      gap: 4px;
      min-width: 0;
      color: #667085;
      font-size: 10px;
      font-weight: 700;
      line-height: 1.2;
    }
    .protocol-time-field > span {
      display: block;
      padding-left: 2px;
      white-space: nowrap;
    }
    .protocol-time-field > input {
      width: 100%;
    }
    .protocol-channel-options label,
    .protocol-confirm {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      white-space: nowrap;
      font-size: 11px;
    }
    .protocol-confirm {
      min-height: 34px;
      align-self: end;
    }
    .protocol-channel-options input,
    .protocol-confirm input {
      width: 14px;
      height: 14px;
      margin: 0;
    }
    .protocol-segment-status {
      grid-column: 1 / -1;
      margin: 0;
      color: #667085;
      font-size: 11px;
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: normal;
    }
    .import-section {
      margin-top: 14px;
      padding: 12px 16px 14px;
      border-top: 1px solid #e4e7ec;
    }
    .import-section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin: 0 0 7px;
      color: #111827;
      font-size: 13px;
      font-weight: 700;
    }
    .policy-fields {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
      gap: 8px;
    }
    .policy-fields label {
      display: grid;
      gap: 4px;
      min-width: 0;
      color: #475467;
      font-size: 11px;
      font-weight: 700;
      line-height: 1.35;
    }
    .policy-fields input,
    .policy-fields select {
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
    }
    .project-template-options {
      margin-bottom: 10px;
      border: 1px solid #b7c2d4;
      border-radius: 7px;
      background: #f8fafc;
    }
    .project-template-options summary {
      padding: 9px 10px;
      color: #344054;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .project-template-body {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 10px 10px;
    }
    .project-template-body span {
      color: #667085;
      font-size: 11px;
    }
    .coordinate-source-help {
      margin-top: 4px;
      color: #667085;
      font-size: 11px;
      line-height: 1.35;
    }
    .mode-options {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .mode-option {
      display: flex;
      align-items: center;
      gap: 7px;
      min-height: 36px;
      padding: 7px 9px;
      border: 1px solid #b7c2d4;
      border-radius: 6px;
      color: #344054;
      font-size: 12px;
    }
    .mode-option input {
      width: auto;
      height: auto;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      border-top: 1px solid #edf0f4;
      margin-top: 12px;
      padding-top: 12px;
    }
    .loading {
      opacity: 0.45;
    }
    .bootstrap-screen {
      display: none;
      min-height: calc(100vh - 64px);
      align-items: center;
      justify-content: center;
      padding: 28px 16px;
    }
    body.bootstrap-mode main {
      display: none;
    }
    body.bootstrap-mode .bootstrap-screen {
      display: flex;
    }
    body.bootstrap-mode .header-actions {
      display: none;
    }
    .bootstrap-panel {
      width: min(520px, 100%);
      border: 1px solid #d7deea;
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 14px 34px rgba(15, 23, 42, 0.08);
      padding: 22px;
    }
    .bootstrap-panel h2 {
      margin: 0 0 8px;
      font-size: 20px;
      line-height: 1.25;
      color: #111827;
    }
    .bootstrap-panel p {
      margin: 0;
      color: #667085;
      font-size: 13px;
      line-height: 1.55;
    }
    .bootstrap-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 18px;
    }
    .bootstrap-actions button {
      height: 42px;
      font-size: 14px;
    }
    @media (max-width: 1100px) {
      header {
        height: auto;
        min-height: 72px;
        align-items: flex-start;
        flex-direction: column;
        padding: 10px 12px;
      }
      .header-actions {
        width: 100%;
        justify-content: space-between;
      }
      .header-export-hint {
        white-space: normal;
      }
      main {
        grid-template-columns: 1fr;
      }
      #chart {
        height: 680px;
      }
    }
    @media (max-width: 760px) {
      .modal-backdrop {
        padding: 12px 8px;
      }
      .modal {
        max-height: calc(100dvh - 24px);
        padding: 12px;
      }
      .modal.import-modal {
        padding: 14px;
      }
      .import-grid {
        grid-template-columns: minmax(0, 1fr);
        gap: 7px;
      }
      .import-grid > label {
        margin-top: 5px;
        font-weight: 700;
      }
      .lif-input-head {
        display: none;
      }
      .lif-input-row {
        grid-template-columns: 42px minmax(0, 1fr) minmax(0, 1fr) 32px;
        grid-template-areas:
          "slot channel identity remove"
          ". detector axis ."
          ". use use ."
          ". file file .";
      }
      .lif-input-slot {
        grid-area: slot;
      }
      .lif-channel {
        grid-area: channel;
      }
      .lif-identity {
        grid-area: identity;
      }
      .lif-detector {
        grid-area: detector;
      }
      .lif-axis {
        grid-area: axis;
      }
      .lif-use-options {
        grid-area: use;
      }
      .lif-file-picker {
        grid-area: file;
      }
      .lif-remove {
        grid-area: remove;
      }
      .lif-mobile-label {
        display: block;
        margin-bottom: 3px;
        color: #667085;
        font-size: 10px;
        font-weight: 700;
        line-height: 1.2;
      }
      .lif-file-picker .lif-mobile-label {
        grid-column: 1 / -1;
        margin-bottom: -4px;
      }
      .protocol-row {
        grid-template-columns: 34px minmax(0, 1fr) minmax(0, 1fr) 32px;
      }
      .modal.import-modal .protocol-row {
        grid-template-columns: 34px minmax(0, 1fr) minmax(0, 1fr) 32px;
      }
      .protocol-row > *:nth-child(5),
      .protocol-row > *:nth-child(6) {
        grid-column: 2 / 4;
      }
      .policy-fields {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>LMA Studio</h1>
    </div>
    <div class="header-actions">
      <button id="openImportProject" class="header-secondary-button">新建项目</button>
      <button id="openExistingProject" class="header-secondary-button">打开项目</button>
      <button id="openConfigProject" class="header-secondary-button">配置</button>
      <button id="openUmap" class="header-secondary-button" data-unavailable="true" title="当前项目尚未配置事件坐标 CSV">UMAP（未配置）</button>
      <span id="exportHint" class="header-export-hint">全部事件均导出；未标注为 unknown，前段 QC anchor 留在审计库</span>
      <button id="exportAcceptedCsv" class="header-export-button">导出细胞/质控主 CSV</button>
    </div>
  </header>

  <section id="bootstrapScreen" class="bootstrap-screen" aria-labelledby="bootstrapTitle">
    <div class="bootstrap-panel">
      <h2 id="bootstrapTitle">请选择项目</h2>
      <p id="bootstrapStatus">当前未加载标注项目。</p>
      <div class="bootstrap-actions">
        <button id="bootstrapNewProject" class="header-secondary-button">新建项目</button>
        <button id="bootstrapOpenProject" class="header-secondary-button">打开项目</button>
      </div>
    </div>
  </section>

  <main>
    <aside>
      <p class="side-title">当前窗口</p>
      <div class="metric"><span>范围</span><strong id="range">-</strong></div>
      <div class="metric"><span>LIF 峰</span><strong id="lifPeakCount">-</strong></div>
      <div class="metric"><span>MS 事件</span><strong id="msEventCount">-</strong></div>
      <div class="metric"><span>LIF 点数</span><strong id="lifPointCount">-</strong></div>
      <div class="metric"><span>MS scan 点数</span><strong id="msPointCount">-</strong></div>
      <p class="side-title" style="margin-top:18px;">任务阶段</p>
      <div class="stage-tabs">
        <button class="stage-tab active" data-stage="qc_calibration" title="前段参考校准">Calibration</button>
        <button class="stage-tab" data-stage="local_calibration" title="后段 MS 时间差校正">MS Δt</button>
        <button class="stage-tab" data-stage="event_annotation" title="事件标注与后段 QC">Events / QC</button>
      </div>
      <div id="stageNote" class="stage-note">逐段审核前段参考峰，用于确认各组 LIF 信号相对 MS 只需做时间平移。</div>
      <div id="eventFilter" class="segmented" style="display:none;" aria-label="事件类型筛选">
        <button type="button" class="active" data-event-filter="all">All</button>
        <button type="button" data-event-filter="qc">QC</button>
        <button type="button" data-event-filter="cell">Cells</button>
      </div>
      <div id="qcRefitPanel" class="manual-box" style="display:none; margin-top:8px;">
        <button id="previewQcRefit" class="small-button" style="width:100%;">用已接受参考峰预览重算</button>
        <div id="qcRefitStats" class="empty" style="margin-top:6px;">尚未生成重算预览。</div>
        <button id="applyQcRefit" class="small-button secondary" style="width:100%; margin-top:7px;" disabled>应用参考峰时间校正</button>
      </div>
      <div id="localDeltaPanel" class="manual-box" style="display:none; margin-top:8px;">
        <button id="estimateDelta" class="small-button" style="width:100%;" title="Estimate the MS time offset from unlabeled peak timing">Estimate MS Δt</button>
        <div id="deltaBaseSummary" class="empty" style="margin-top:6px;">基础时间平移：-</div>
        <div class="metric"><span>当前 MS 时间差</span><strong id="deltaReadout">0.00 sec</strong></div>
        <input id="deltaSlider" class="delta-slider" type="range" min="-20" max="20" step="0.25" value="0" />
        <div class="row-actions">
          <button id="deltaMinus" class="small-button secondary">-0.25 sec</button>
          <button id="deltaPlus" class="small-button secondary">+0.25 sec</button>
        </div>
        <button id="freezeDelta" class="small-button" style="width:100%; margin-top:7px;">确认并锁定 MS 时间差</button>
        <div id="deltaStats" class="empty" style="margin-top:6px;">未加载预览。</div>
      </div>
      <p class="side-title" style="margin-top:18px;">轨道</p>
      <div id="trackLegend" class="legend"></div>
      <p class="side-title" style="margin-top:18px;">已加载</p>
      <div id="loaded" class="empty">-</div>
      <div id="baseTimePanel">
        <p id="baseTimeTitle" class="side-title" style="margin-top:18px;">自动时间校正</p>
        <div class="metric"><span id="modeMetricLabel">模式</span><strong id="modeLabel">-</strong></div>
        <div id="axisShiftMetrics"></div>
        <div id="msDeltaMetric" class="metric"><span>MS 后段时间差</span><strong id="msDeltaShift">-</strong></div>
        <div class="metric"><span id="matchMetricLabel">参考峰组</span><strong id="matchCount">-</strong></div>
      </div>
      <div id="reviewPanel">
        <p class="side-title" style="margin-top:18px;">人工审核</p>
        <div class="status-grid">
          <div class="status-pill"><strong id="pendingCount">0</strong>待审</div>
          <div class="status-pill"><strong id="acceptedCount">0</strong>已接受</div>
          <div class="status-pill"><strong id="rejectedCount">0</strong>已拒绝</div>
        </div>
        <label class="checkbox-row">
          <input id="showRejected" type="checkbox" />
          显示已拒绝候选
        </label>
        <label id="crossChannelConflictControl" class="checkbox-row" style="display:none;">
          <input id="showCrossChannelConflicts" type="checkbox" />
          <span id="crossChannelConflictHint">Show conflicts</span>
        </label>
        <button id="acceptWindow" class="small-button" style="width:100%; margin:8px 0 2px;">接受本窗口待审自动候选</button>
        <div id="acceptWindowHint" class="empty" style="margin:0 0 6px;">将接受 0 条</div>
        <div id="reviewHelp" class="empty" style="margin:4px 0 8px;">
          残差 = MS760 时间减去 LIF 参考峰校正后的组合时间，单位为秒；越接近 0 表示时间对齐越好。
        </div>
        <div id="candidateList" class="candidate-list"></div>
      </div>
      <div id="manualPanel">
        <p id="manualPanelTitle" class="side-title" style="margin-top:18px;">手动参考峰关系</p>
        <div class="manual-box">
          <div id="manualAnnotationKind" class="segmented two" style="display:none;" aria-label="手工标注类型">
            <button type="button" class="active" data-manual-kind="qc">QC anchor</button>
            <button type="button" data-manual-kind="cell">Cell pair</button>
          </div>
          <button id="manualMode" class="small-button secondary">Select peaks</button>
          <button id="clearManual" class="small-button secondary">Clear</button>
          <div class="manual-selection">
            <div id="manualLifRow" style="display:none;">LIF: <strong id="manualLIF">-</strong></div>
            <div id="manualAnchorRows"></div>
            <div>MS760: <strong id="manualMS">-</strong></div>
          </div>
          <button id="createManual" class="small-button">Save pair</button>
        <div id="manualHelp" class="empty" style="margin-top:6px;">开启后依次点击项目配置的 LIF 参考峰和对应的 MS760 峰。</div>
        </div>
      </div>
    </aside>

    <section class="plot-panel">
      <div class="controls plot-controls">
        <button id="prev" title="上一窗口" aria-label="上一窗口">&#8592;</button>
        <button id="next" title="下一窗口" aria-label="下一窗口">&#8594;</button>
        <label>
          <span class="policy">Start</span>
          <input id="start" type="number" step="0.1" value="0" />
        </label>
        <label>
          <span class="policy">Window</span>
          <input id="widthDisplay" type="number" min="0.25" max="15" step="0.05" value="2.5" title="可输入 0.25–15 min" />
        </label>
        <label>
          <span class="policy">Time</span>
          <select id="timeMode">
            <option value="aligned" selected>Aligned</option>
            <option value="raw">Raw</option>
          </select>
        </label>
        <label>
          <span class="policy">Y</span>
          <select id="yAxisMode">
            <option value="full" selected>Full</option>
            <option value="robust">Zoom</option>
          </select>
        </label>
        <label>
          <span class="policy">Labels</span>
          <select id="peakLabelMode">
            <option value="auto" selected>Auto</option>
            <option value="all">All</option>
            <option value="hidden">Off</option>
          </select>
        </label>
        <label id="showWeakLifPeaksLabel" class="checkbox-row" style="margin:0; white-space:nowrap;" title="仅在事件标注段用于人工细胞配对；不参与自动匹配">
          <input id="showWeakLifPeaks" type="checkbox" />
          Weak peaks
        </label>
        <button id="go">Show</button>
      </div>
      <div class="window-readout">
        <strong id="title">同步 2.5 min 窗口</strong>
        <span id="windowPolicy">峰圆点全部保留；时间标签默认自动精简，悬停任意圆点可查看精确原始时间(min)</span>
      </div>
      <svg id="chart" role="img" aria-label="Synchronized LIF and MS tracks"></svg>
    </section>
  </main>
  <div id="tooltip" class="tooltip"></div>
  <div id="interactionHint" class="interaction-hint" role="status" aria-live="polite"></div>
  <div id="lineContextMenu" class="context-menu"></div>
  <div id="importModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="importTitle">
    <div class="modal import-modal">
      <div class="modal-head">
        <div>
          <p id="importTitle" class="modal-title">新建标注项目</p>
          <div class="empty">配置 2–4 个 LIF 通道的样本含义与信号颜色；软件会自动安排共享采集时间基准。随后核对前段参考窗口，并单独选择是否进行后段质控巡检。</div>
        </div>
        <button id="closeImportProject" class="small-button secondary">关闭</button>
      </div>
      <details id="importProjectTemplates" class="project-template-options">
        <summary>可选：实验配置模板</summary>
        <div class="project-template-body">
          <button id="applyLinLskExample" type="button" class="small-button secondary">应用 Lin− / LSK 示例</button>
          <span>Lin− / LSK 示例配置：G1/LSK → G2/Lin−，两者自动共享绿色信号时间轴；事件起点 24 min；不进行后段质控巡检。参考窗口仍须由你确认。</span>
        </div>
      </details>
      <div class="import-grid">
        <label>原始文件保存方式</label>
        <div class="mode-options">
          <label class="mode-option"><input type="radio" name="rawInputMode" value="external_reference" checked /> 外部引用</label>
          <label class="mode-option"><input type="radio" name="rawInputMode" value="copy_into_project" /> 复制到项目</label>
        </div>
        <label for="importProjectDir">项目保存路径</label>
        <div class="path-picker-row">
          <input id="importProjectDir" type="text" placeholder="选择项目保存路径" />
          <button class="small-button secondary path-picker-button" aria-label="选择项目保存路径" data-picker-target="importProjectDir" data-picker-kind="directory" data-picker-title="选择项目保存路径">选择</button>
        </div>
        <label>LIF 输入</label>
        <div class="lif-input-table">
          <div class="lif-input-head" aria-hidden="true">
            <span>输入</span>
            <span>通道<small>G1–R2</small></span>
            <span>信号颜色<small>绿色 / 红色</small></span>
            <span>采集时间基准<small>按信号颜色自动设置</small></span>
            <span>样本标签<small>细胞用途必填</small></span>
            <span>科学角色<small>细胞标注</small></span>
            <span>LIF 原始文件</span>
            <span></span>
          </div>
          <div id="importLifRows"></div>
          <div class="lif-import-actions">
            <button id="addImportLif" type="button" class="small-button secondary">＋ 添加 LIF</button>
            <span id="importRoleSummary" class="qc-anchor-rule">细胞 0 · 共 0 个 LIF</span>
          </div>
        </div>
        <div id="importDualRoleHelp" class="qc-anchor-rule" style="grid-column:2;">同一通道的 QC anchor 与 Cell pair 角色相互独立，可同时启用。</div>
        <label for="importMs">MS 文件</label>
        <div class="path-picker-row">
          <input id="importMs" type="text" placeholder="选择 MS 原始文件" />
          <button class="small-button secondary path-picker-button" aria-label="选择 MS 原始文件" data-picker-target="importMs" data-picker-kind="file" data-picker-role="ms" data-picker-title="选择 MS 原始文件">选择</button>
        </div>
        <label for="importCellEventMap">事件坐标 CSV</label>
        <div>
          <div class="path-picker-row">
            <input id="importCellEventMap" type="text" placeholder="选择包含三列必需坐标的 CSV" />
            <button class="small-button secondary path-picker-button" aria-label="选择单细胞事件坐标 CSV" data-picker-target="importCellEventMap" data-picker-kind="file" data-picker-role="cell_event_map" data-picker-title="选择单细胞事件坐标 CSV">选择</button>
          </div>
          <div class="coordinate-source-help">必须包含 scan_start_time、UMAP1、UMAP2；CellNumber、batch、Type 等其他列可以保留，导入时会忽略。</div>
        </div>
        <span>LIF 峰识别方式</span>
        <div id="importLifPeakDetectorStandard" class="detector-standard-card">
          <strong>自适应双层峰识别（自动配置）</strong>
          <div class="coordinate-source-help">高置信峰是自动流程唯一使用的证据；弱候选峰结合局部噪声与峰形筛选，仅供人工细胞配对，不参与自动校准、质量巡检、时间差估计、候选生成或时间模型训练。人工明确配对并接受后才可导出。</div>
        </div>
      </div>
      <section class="import-section" aria-labelledby="calibrationProtocolTitle">
        <div class="import-section-title">
          <span id="calibrationProtocolTitle">前段分段校准参考窗口（项目级参数）</span>
          <span class="row-actions" style="margin:0;">
            <button id="suggestImportWindows" type="button" class="small-button secondary">分析已选 LIF 并建议窗口</button>
            <button id="addImportSegment" type="button" class="small-button secondary">＋ 添加参考段</button>
          </span>
        </div>
        <div class="qc-anchor-rule">QC anchor 由各段勾选通道决定（仅绿色通道 / 仅红色通道 / 红绿联合）；CART 可在同一段选择 G2+R1。可以先创建草稿查看原始峰形；所有边界确认前，时间校准与后续阶段保持锁定。</div>
        <div id="importSuggestionStatus" class="qc-anchor-rule">尚未分析原始峰形。</div>
        <div id="importCalibrationSegments" class="protocol-editor"></div>
      </section>
      <section class="import-section" aria-labelledby="postQcPolicyTitle">
        <div class="import-section-title"><span id="postQcPolicyTitle">事件起点与后段质控巡检</span></div>
        <div class="policy-fields">
          <label>事件标注起点 (min)
            <input id="importAnnotationStart" type="number" min="0" step="0.1" value="40" />
          </label>
          <label>自动估计时间差的范围 (min)
            <input id="importSeedWindow" type="number" min="0.1" step="0.5" value="2.5" />
          </label>
          <label>Post-run QC
            <select id="importPostQcMode">
              <option value="disabled" selected>Off</option>
              <option value="signature">QC signature</option>
              <option value="scheduled_windows">Scheduled windows</option>
            </select>
          </label>
          <label id="importPostQcChannelsLabel">QC channels
            <select id="importPostQcChannels" multiple size="2" aria-label="后段巡检参考通道"></select>
          </label>
        </div>
        <div id="importPostQcHint" class="qc-anchor-rule"></div>
        <div id="importScheduledQcPanel" style="display:none; margin-top:8px;">
          <div class="import-section-title">
            <span>Scheduled windows</span>
            <button id="addImportScheduledQc" type="button" class="small-button secondary">＋ Window</button>
          </div>
          <div id="importScheduledQcWindows" class="protocol-editor"></div>
        </div>
      </section>
      <div id="importHint" class="empty" style="margin-top:10px;"></div>
      <div class="modal-actions">
        <button id="runImportProject" class="small-button">生成草稿并进入项目</button>
      </div>
    </div>
  </div>
  <div id="openProjectModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="openProjectTitle">
    <div class="modal">
      <div class="modal-head">
        <div>
          <p id="openProjectTitle" class="modal-title">打开已有项目</p>
          <div class="empty">当前版本只打开使用现行峰识别标准建立的项目。旧标准项目不会被修改；请在新的空目录中重新选择原始输入并重跑预处理。</div>
        </div>
        <button id="closeOpenProject" class="small-button secondary">关闭</button>
      </div>
      <div class="import-grid">
        <label for="openProjectDir">项目目录</label>
        <div class="path-picker-row">
          <input id="openProjectDir" type="text" placeholder="/path/to/existing_project" />
          <button class="small-button secondary path-picker-button" aria-label="选择已有项目目录" data-picker-target="openProjectDir" data-picker-kind="directory" data-picker-title="选择已有项目目录">选择</button>
        </div>
      </div>
      <div id="openProjectHint" class="empty" style="margin-top:10px;">项目目录应包含完整的项目说明、中间表和标注数据库；旧标准项目的人工标注不会自动迁移。</div>
      <div class="modal-actions">
        <button id="openAsNewStandardProject" class="small-button secondary">改用现行标准新建项目</button>
        <button id="runOpenProject" class="small-button">打开项目</button>
      </div>
    </div>
  </div>
  <div id="projectConfigModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="projectConfigTitle">
    <div class="modal">
      <div class="modal-head">
        <div>
          <p id="projectConfigTitle" class="modal-title">配置</p>
          <div class="empty">设置前段参考窗口、事件起点与后段质控巡检。修改已锁定时间模型所依赖的参数时，软件会先说明哪些结果需要重算。</div>
        </div>
        <button id="closeConfigProject" class="small-button secondary">关闭</button>
      </div>
      <div id="timeConfigPanel" class="config-grid">
        <span>前段参考结束 (min)</span><input id="cfgQcEnd" type="number" step="0.1" />
        <span>事件标注起点(min)</span><input id="cfgAnnotationStart" type="number" step="0.1" />
        <span>自动估计时间差的范围(min)</span><input id="cfgSeedWindow" type="number" step="0.5" />
      </div>
      <section class="import-section">
        <div class="import-section-title"><span>LIF 峰识别规则（项目创建时固定）</span></div>
        <div class="detector-config-grid">
          <span>识别方式</span><output id="cfgLifPeakStandard">-</output>
          <span>识别规则</span><output id="cfgLifPeakDetectorDetails">-</output>
        </div>
        <div class="qc-anchor-rule">识别规则与项目中间表固定绑定，本页只读，不会把其他规则静默套用到已有峰表。技术审计信息保存在项目说明文件中。</div>
      </section>
      <section id="cfgProtocolPanel" class="import-section">
        <div class="import-section-title"><span>前段分段参考窗口</span></div>
        <div class="qc-anchor-rule">通道与顺序保持不变；可先保存待确认边界。全部勾选“边界已确认”后，才会计算前段校准并解锁后段阶段。</div>
        <div id="cfgCalibrationSegments" class="protocol-editor"></div>
      </section>
      <section id="cfgPostQcPanel" class="import-section">
        <div class="import-section-title"><span>Post-run QC</span><button id="cfgAddScheduledQc" type="button" class="small-button secondary">＋ Window</button></div>
        <div class="policy-fields">
          <label>Mode
            <select id="cfgPostQcMode">
              <option value="disabled">Off</option>
              <option value="signature">QC signature</option>
              <option value="scheduled_windows">Scheduled windows</option>
            </select>
          </label>
          <label id="cfgPostQcChannelsLabel">QC channels
            <select id="cfgPostQcChannels" multiple size="2"></select>
          </label>
        </div>
        <div id="cfgPostQcHint" class="qc-anchor-rule"></div>
        <div id="cfgScheduledQcWindows" class="protocol-editor" style="margin-top:8px;"></div>
      </section>
      <div id="attachMapPanel" class="attach-map-panel" style="display:none;">
        <div class="attach-map-heading">
          <p class="side-title">为旧项目附加 UMAP 事件坐标</p>
          <span class="attach-map-badge">仅可附加一次</span>
        </div>
        <p class="attach-map-copy">
          选择与当前 MS 数据对应的 CSV。软件会先校验全部时间匹配，成功后才写入项目并启用 UMAP；
          校验失败不会改变项目。
        </p>
        <label class="attach-map-label" for="attachCellEventMap">事件坐标 CSV</label>
        <div class="path-picker-row">
          <input id="attachCellEventMap" type="text" autocomplete="off" spellcheck="false" aria-describedby="attachMapRequirements" placeholder="请选择与当前项目对应的 .csv 文件" />
          <button class="small-button secondary path-picker-button" aria-label="选择待附加的事件坐标 CSV" data-picker-target="attachCellEventMap" data-picker-kind="file" data-picker-role="cell_event_map" data-picker-title="选择待附加的事件坐标 CSV">选择 CSV</button>
        </div>
        <div id="attachMapRequirements" class="attach-map-requirements">
          必需列：<code>scan_start_time</code>、<code>UMAP1</code>、<code>UMAP2</code>；
          其他列会被忽略。<span id="attachMapProjectName"></span>
        </div>
        <div class="attach-map-actions">
          <button id="attachMap" type="button" class="small-button" disabled>校验并附加到当前项目</button>
          <span id="attachMapReady" class="attach-map-ready">尚未选择文件</span>
        </div>
      </div>
      <div id="configSaveStatus" class="config-save-status" role="status" aria-live="polite"></div>
      <div class="modal-actions">
        <button id="saveConfig" class="small-button">保存项目配置</button>
      </div>
    </div>
  </div>

  <script>
    const state = {
      meta: null,
      start: 0,
      width: 2.5,
      timeMode: 'aligned',
      yAxisMode: 'full',
      peakLabelMode: 'auto',
      current: null,
      selectedCandidateId: null,
      showRejected: false,
      showCrossChannelConflicts: false,
      showWeakLifPeaks: false,
      manualMode: false,
      stage: 'qc_calibration',
      eventFilter: 'all',
      manualAnnotationKind: 'qc',
      previewDeltaSec: null,
      localDeltaPreview: null,
      qcRefitPreview: null,
      manual: { anchors: {}, LIF: null, MS760: null },
      requestSeq: 0,
      actionBusy: false,
      importCreating: false,
      configSaveBusy: false,
      importRows: [],
      nextImportRowId: 1,
      importSegments: [],
      nextImportSegmentId: 1,
      importScheduledQcWindows: [],
      nextImportScheduledQcId: 1,
      importSignatureChannels: [],
      importSuggestionRevision: 0,
      configProtocolDraft: null,
      configPostQcDraft: null
    };
    const stateChannel = ('BroadcastChannel' in window)
      ? new BroadcastChannel('lma-studio-state-v1')
      : null;
    const MIN_WINDOW_MIN = 0.25;
    const MAX_WINDOW_MIN = 15.0;
    const colors = { G1: '#2f6fed', G2: '#176b45', R2: '#b95d18', R1: '#6f4bb8', ms760: '#1f5f99', ms782: '#2a7d67' };
    const fallbackLifTracks = [
      { key: 'lif_g2', label: 'LIF G2 / Day0', kind: 'lif', channels: ['G2'] },
      { key: 'lif_r1', label: 'LIF R1 / Day9', kind: 'lif', channels: ['R1'] },
      { key: 'lif_r2', label: 'LIF R2 / Day3', kind: 'lif', channels: ['R2'] },
    ];

    function colorForChannel(channel) {
      if (colors[channel]) return colors[channel];
      const palette = ['#0f766e', '#7c3aed', '#b45309', '#be123c', '#0369a1', '#4d7c0f'];
      let hash = 0;
      String(channel || '').split('').forEach(ch => { hash = ((hash * 31) + ch.charCodeAt(0)) >>> 0; });
      return palette[hash % palette.length];
    }

    function tracksForCurrentProject() {
      if (state.meta?.bootstrap || !state.meta?.project) return [];
      const layoutChannels = state.meta?.acquisition_layout?.lif_channels || [];
      const qcChannels = new Set(calibrationReferenceChannels());
      const relevantChannels = new Set(
        layoutChannels
          .filter(row => qcChannels.has(row.channel) || row.use_for_cell_annotation !== false)
          .map(row => row.channel)
      );
      const visibleChannels = state.stage === 'qc_calibration'
        ? qcChannels
        : relevantChannels;
      const lifTracks = layoutChannels.length
        ? layoutChannels.filter(row => visibleChannels.has(row.channel)).map((row) => ({
            key: `lif_${String(row.channel).toLowerCase()}`,
            label: `LIF ${row.channel}${row.identity_prior ? ' / ' + row.identity_prior : ''}`,
            kind: 'lif',
            channels: [row.channel],
          }))
        : fallbackLifTracks;
      return [
        ...lifTracks,
        { key: 'ms760', label: 'MS 760 / PC34', kind: 'ms', trace: 'pc34_760_linear' },
        { key: 'ms782', label: 'MS 782 / QC', kind: 'ms', trace: 'qc_782_linear' },
      ];
    }

    function channelDisplayLabel(channel) {
      const row = (state.meta?.acquisition_layout?.lif_channels || [])
        .find(item => item.channel === channel);
      return row?.identity_prior ? `${channel}/${row.identity_prior}` : String(channel || 'LIF');
    }

    function colorForTrack(track) {
      if (track.kind === 'lif') return colorForChannel(track.channels[0]);
      return track.trace === 'pc34_760_linear' ? colors.ms760 : colors.ms782;
    }

    function renderTrackLegend() {
      const legend = el('trackLegend');
      if (!legend) return;
      legend.innerHTML = tracksForCurrentProject().map(track => (
        `<div class="legend-row"><span class="swatch" style="background:${colorForTrack(track)}"></span>${escapeText(track.label)}</div>`
      )).join('');
    }

    function calibrationProtocol() {
      return state.current?.project_config?.calibration_protocol
        || state.meta?.project_config?.calibration_protocol
        || state.meta?.calibration_protocol
        || state.current?.alignment?.calibration_protocol
        || null;
    }

    function calibrationBoundariesConfirmed() {
      const protocol = calibrationProtocol();
      const segments = protocol?.segments || [];
      return Boolean(
        segments.length
        && protocol?.boundaries_confirmed === true
        && segments.every(segment => segment.boundaries_confirmed === true)
      );
    }

    function calibrationReferenceChannels() {
      const protocol = calibrationProtocol();
      if (Array.isArray(protocol?.reference_channels) && protocol.reference_channels.length) return protocol.reference_channels;
      return state.meta?.acquisition_layout?.qc_anchor_channels
        || state.current?.alignment?.qc_anchor_channels
        || ['G2', 'R1'];
    }

    function activeCalibrationSegment() {
      const segments = calibrationProtocol()?.segments || [];
      if (!segments.length) return null;
      const start = Number(state.current?.start_min ?? state.start ?? 0);
      const end = Number(state.current?.end_min ?? (start + state.width));
      return segments.find(segment => Number(segment.end_min) >= start && Number(segment.start_min) <= end)
        || segments.reduce((nearest, segment) => {
          if (!nearest) return segment;
          const distance = Math.min(Math.abs(Number(segment.start_min) - start), Math.abs(Number(segment.end_min) - start));
          const nearestDistance = Math.min(Math.abs(Number(nearest.start_min) - start), Math.abs(Number(nearest.end_min) - start));
          return distance < nearestDistance ? segment : nearest;
        }, null);
    }

    function qcAnchorChannels(row = null) {
      if (Array.isArray(row?.anchor_channels) && row.anchor_channels.length) return row.anchor_channels;
      if (row?.lif_anchor_peak_ids && typeof row.lif_anchor_peak_ids === 'object') return Object.keys(row.lif_anchor_peak_ids);
      if (state.stage === 'qc_calibration') {
        const segment = activeCalibrationSegment();
        if (Array.isArray(segment?.reference_channels) && segment.reference_channels.length) return segment.reference_channels;
      }
      if (state.stage === 'event_annotation') {
        const strategy = state.current?.project_config?.post_qc_strategy || state.meta?.project_config?.post_qc_strategy || {};
        if (Array.isArray(strategy.reference_channels) && strategy.reference_channels.length) return strategy.reference_channels;
      }
      return calibrationReferenceChannels();
    }

    function qcAnchorPeakIds(row) {
      const dynamic = row?.lif_anchor_peak_ids;
      if (dynamic && typeof dynamic === 'object' && !Array.isArray(dynamic)) return dynamic;
      const result = {};
      qcAnchorChannels(row).forEach((channel, index) => {
        const channelValue = row?.[`${String(channel).toLowerCase()}_peak_id`];
        const legacyValue = index === 0 ? row?.anchor_a_peak_id : (index === 1 ? row?.anchor_b_peak_id : null);
        result[channel] = channelValue || legacyValue || null;
      });
      return result;
    }

    function qcAnchorTimes(row, mode = 'plot') {
      const key = mode === 'raw' ? 'lif_anchor_raw_times_min' : 'lif_anchor_plot_times_min';
      const dynamic = row?.[key];
      if (dynamic && typeof dynamic === 'object' && !Array.isArray(dynamic)) return dynamic;
      const result = {};
      qcAnchorChannels(row).forEach((channel, index) => {
        const channelValue = row?.[`${String(channel).toLowerCase()}_${mode}_time_min`];
        const legacyValue = index === 0
          ? row?.[`anchor_a_${mode}_time_min`]
          : (index === 1 ? row?.[`anchor_b_${mode}_time_min`] : null);
        result[channel] = channelValue ?? legacyValue ?? null;
      });
      return result;
    }

    function qcAnchorTimeText(row) {
      const rawTimes = qcAnchorTimes(row, 'raw');
      return [
        ...qcAnchorChannels(row).map(channel => `${channel} ${fmtMaybe(rawTimes[channel], 3)}`),
        `MS760 ${fmtMaybe(row?.ms_time_min, 3)}`,
      ].join(' · ');
    }

    function resetManualSelection() {
      state.manual = { anchors: {}, LIF: null, MS760: null };
    }

    const el = (id) => document.getElementById(id);
    let interactionHintTimer = null;

    function showInteractionHint(message) {
      const hint = el('interactionHint');
      if (!hint) return;
      hint.textContent = String(message || '');
      hint.classList.add('show');
      if (interactionHintTimer !== null) window.clearTimeout(interactionHintTimer);
      interactionHintTimer = window.setTimeout(() => {
        hint.classList.remove('show');
        interactionHintTimer = null;
      }, 2200);
    }

    function fmt(n, digits = 2) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '';
      return Number(n).toFixed(digits);
    }

    function fmtMaybe(n, digits = 3) {
      const text = fmt(n, digits);
      return text || 'NA';
    }

    function stageWindowWidth() {
      const fallback = Number(state.meta?.default_window_min || 2.5);
      if (state.stage !== 'local_calibration') return fallback;
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      const configured = Number(cfg.local_delta_seed_window_min);
      const width = Number.isFinite(configured) && configured > 0 ? configured : fallback;
      return Math.max(0.25, Math.min(15.0, width));
    }

    function applyStageWindowWidth() {
      const current = Number(state.width);
      state.width = Number.isFinite(current) && current >= MIN_WINDOW_MIN && current <= MAX_WINDOW_MIN
        ? current
        : stageWindowWidth();
      el('widthDisplay').value = fmt(state.width, 2);
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      const seedWidth = Number(cfg.local_delta_seed_window_min || 2.5);
      el('windowPolicy').textContent = state.stage === 'local_calibration'
        ? `浏览宽度可在 0.25–15 min 内调整；自动估计 MS 时间差的取证范围仍由项目配置决定（当前 ${fmt(seedWidth, 2)} min）。边界额外载入 ±0.08 min。`
        : '浏览宽度可在 0.25–15 min 内调整；边界额外载入 ±0.08 min，各轨道刻度和峰旁数字仍为原始时间(min)。';
    }

    function syncWindowWidthFromControl() {
      const input = el('widthDisplay');
      const requested = Number(input.value);
      if (!Number.isFinite(requested) || requested < MIN_WINDOW_MIN || requested > MAX_WINDOW_MIN) {
        alert(`窗口宽度必须在 ${MIN_WINDOW_MIN}–${MAX_WINDOW_MIN} min 之间。`);
        input.focus();
        return false;
      }
      state.width = requested;
      input.value = fmt(state.width, 2);
      return true;
    }

    function eventGridWindowStart(eventTime) {
      const projectMin = Number(state.meta?.time_min_min || 0);
      const projectMax = Number(state.meta?.time_min_max || eventTime);
      const width = Number(state.width || state.meta?.default_window_min || 2.5);
      const grid = width;
      if (
        !Number.isFinite(eventTime)
        || !Number.isFinite(projectMin)
        || !Number.isFinite(projectMax)
        || !Number.isFinite(width)
        || width <= 0
        || !Number.isFinite(grid)
        || grid <= 0
      ) return projectMin;
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const annotationStart = Number(tm.annotation_start_min || 40);
      const deltaMin = Number(tm.ms_local_delta_sec || 0) / 60;
      const alignedEventTime = (
        Number.isFinite(annotationStart)
        && Number.isFinite(deltaMin)
        && eventTime >= annotationStart
      ) ? eventTime + deltaMin : eventTime;
      const epsilon = grid * 1e-10;
      const snapped = Math.floor((alignedEventTime + epsilon) / grid) * grid;
      const maxStart = Math.max(projectMin, projectMax - width);
      const clamped = Math.max(projectMin, Math.min(maxStart, snapped));
      return Math.round(clamped * 1e9) / 1e9;
    }

    const WINDOW_CONTEXT_MARGIN_MIN = 0.08;

    function relationLifPlotTimes(row) {
      const numeric = value => {
        if (value === null || value === undefined || value === '') return null;
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
      };
      const values = [numeric(row?.lif_plot_time_min)];
      const dynamic = row?.lif_anchor_plot_times_min;
      if (dynamic && typeof dynamic === 'object') {
        values.push(...Object.values(dynamic).map(numeric));
      } else {
        values.push(numeric(row?.g2_plot_time_min), numeric(row?.r1_plot_time_min));
      }
      return values.filter(value => value !== null);
    }

    function relationBelongsToDisplayWindow(row, start, end) {
      const rawMs = row?.ms_plot_time_min;
      const ms = rawMs === null || rawMs === undefined || rawMs === '' ? NaN : Number(rawMs);
      const lif = relationLifPlotTimes(row);
      const all = [...(Number.isFinite(ms) ? [ms] : []), ...lif];
      if (!all.length || !all.every(value => (
        value >= start - WINDOW_CONTEXT_MARGIN_MIN
        && value <= end + WINDOW_CONTEXT_MARGIN_MIN
      ))) return false;
      const inMain = value => value >= start && value <= end;
      if (!Number.isFinite(ms)) return lif.some(inMain);
      if (inMain(ms)) return true;
      if (!lif.some(inMain)) return false;
      const width = end - start;
      const ownerStart = ms < start ? start - width : end;
      const ownerEnd = ownerStart + width;
      const ownerCanDrawAll = all.every(value => (
        value >= ownerStart - WINDOW_CONTEXT_MARGIN_MIN
        && value <= ownerEnd + WINDOW_CONTEXT_MARGIN_MIN
      ));
      return !ownerCanDrawAll;
    }

    function relationDisplayWindowStart(row) {
      const currentStart = Number(state.current?.start_min);
      const currentEnd = Number(state.current?.end_min);
      if (
        [currentStart, currentEnd].every(Number.isFinite)
        && relationBelongsToDisplayWindow(row, currentStart, currentEnd)
      ) return currentStart;

      const msRaw = Number(row?.ms_time_min);
      const width = Number(state.width || state.meta?.default_window_min || 2.5);
      if (!Number.isFinite(msRaw) || !Number.isFinite(width) || width <= 0) return currentStart;
      const primary = eventGridWindowStart(msRaw);
      const projectMin = Number(state.meta?.time_min_min || 0);
      const projectMax = Number(state.meta?.time_min_max || primary + width);
      const maxStart = Math.max(projectMin, projectMax - width);
      const clamp = value => Math.round(
        Math.max(projectMin, Math.min(maxStart, value)) * 1e9
      ) / 1e9;
      const candidates = [primary, clamp(primary - width), clamp(primary + width)];
      for (const candidate of [...new Set(candidates)]) {
        if (relationBelongsToDisplayWindow(row, candidate, candidate + width)) return candidate;
      }
      return primary;
    }

    function fmtAxis(n) {
      const value = Number(n);
      if (!Number.isFinite(value)) return '';
      const abs = Math.abs(value);
      if (abs >= 100000 || (abs > 0 && abs < 0.001)) return value.toExponential(2);
      if (abs >= 1000) return value.toFixed(0);
      if (abs >= 10) return value.toFixed(1);
      return value.toFixed(3);
    }

    function escapeText(value) {
      return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    async function fetchJson(url) {
      const res = await fetch(url);
      if (!res.ok) throw new Error(await responseErrorMessage(res));
      return res.json();
    }

    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Annotation-Write-Token': state.meta.write_token
        },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(await responseErrorMessage(res));
      return res.json();
    }

    async function postCsv(url, payload) {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Annotation-Write-Token': state.meta.write_token
        },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(await responseErrorMessage(res));
      const text = await res.text();
      const disposition = res.headers.get('Content-Disposition') || '';
      const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
      const asciiMatch = disposition.match(/filename="([^"]+)"/);
      const headerName = res.headers.get('X-Export-Filename') || '';
      const filename = utf8Match
        ? decodeURIComponent(utf8Match[1])
        : headerName
          ? decodeURIComponent(headerName)
          : (asciiMatch ? asciiMatch[1] : 'accepted_annotations.csv');
      return { text, filename };
    }

    function user_facing_error_message(value) {
      const raw = String(value || '').trim();
      if (!raw) return '操作未完成，请检查当前页面中的设置后重试。';
      if (/lif_peak_detection|detector(?:_config_hash|_version)?|peak_tier|\bv(?:0\.3|1|2)\b/i.test(raw)) {
        return '项目的峰识别设置无效或不完整。请保留原项目不变，并在新的空目录中重新选择原始 LIF、MS 和事件坐标 CSV。';
      }
      if (/preview_hash|protocol_hash|\bhash\b/i.test(raw)) {
        return '当前预览或项目设置已经变化，请重新打开对应步骤并生成新的预览。';
      }
      if (/must be numeric/i.test(raw)) {
        if (/annotation_start_min/i.test(raw)) return '事件标注起点必须填写为数字。';
        if (/start_min/i.test(raw) && /window_min/i.test(raw) && /preview_ms_delta_sec/i.test(raw)) {
          return '开始时间、窗口宽度和预览 MS 时间差必须填写为数字。';
        }
        return '时间参数必须填写为数字。';
      }
      const replacements = [
        [/post_qc_strategy/gi, '后段质控巡检设置'],
        [/calibration_protocol/gi, '前段参考设置'],
        [/qc_anchor_channels/gi, '前段参考通道'],
        [/scheduled_windows/gi, '按指定时间窗口巡检'],
        [/signature/gi, '按参考通道巡检'],
        [/disabled/gi, '不进行后段巡检'],
        [/green_axis/gi, '绿色信号时间轴'],
        [/red_axis/gi, '红色信号时间轴'],
        [/lif_anchor_peak_ids/gi, 'LIF 参考峰'],
        [/anchor_peak_id/gi, '参考峰'],
        [/frozen time-axis model/gi, '已锁定的时间校正结果'],
        [/frozen time model/gi, '已锁定的时间校正结果'],
        [/draft time model/gi, '尚未锁定的时间校正结果'],
        [/time[-_]axis/gi, '采集时间基准'],
        [/\bfrozen\b/gi, '已锁定'],
        [/\bdraft\b/gi, '草稿'],
        [/\banchor\b/gi, '参考峰'],
        [/\bdelta\b/gi, 'MS 时间差'],
      ];
      let translated = raw;
      replacements.forEach(([pattern, label]) => { translated = translated.replace(pattern, label); });
      if (/detector|\bhash\b|calibration_protocol|post_qc_strategy|qc_anchor_channels|signature|scheduled_windows|disabled|green_axis|red_axis|preview_hash|\banchor\b|\bdelta\b|\bfrozen\b|\bdraft\b|time[-_]axis|peak_tier|\bv(?:0\.3|1|2)\b/i.test(translated)) {
        return '操作未完成。请检查当前页面中的项目设置和所选记录；如果刚修改过设置，请重新生成预览后再试。';
      }
      return translated;
    }

    async function responseErrorMessage(res) {
      const text = await res.text();
      try {
        const parsed = JSON.parse(text);
        if (parsed && parsed.error) return user_facing_error_message(parsed.error);
      } catch (err) {
        // Fall back to raw response text below.
      }
      return user_facing_error_message(text || `${res.status} ${res.statusText}`);
    }

    function syncBootstrapMode() {
      document.body.classList.toggle('bootstrap-mode', Boolean(state.meta?.bootstrap));
    }

    function syncUmapButtonState() {
      const button = el('openUmap');
      const available = Boolean(state.meta?.cell_event_map?.available);
      button.dataset.unavailable = available ? 'false' : 'true';
      button.textContent = available ? 'UMAP' : 'UMAP（未配置）';
      button.title = available
        ? '打开独立 UMAP 事件地图'
        : '当前项目尚未附加事件坐标 CSV；点击查看配置说明';
    }

    function applyLoadedProjectMeta(projectMeta) {
      state.meta = projectMeta;
      state.current = null;
      syncBootstrapMode();
      state.start = Math.max(0, state.meta.time_min_min);
      state.timeMode = 'aligned';
      state.stage = 'qc_calibration';
      state.eventFilter = 'all';
      state.manualAnnotationKind = 'qc';
      state.showCrossChannelConflicts = false;
      el('showCrossChannelConflicts').checked = false;
      state.showWeakLifPeaks = false;
      const weakToggle = el('showWeakLifPeaks');
      const detector = state.meta?.lif_peak_detection || {};
      const weakAvailable = Number(detector.detector_version || 1) === 2
        && detector.weak?.enabled === true;
      weakToggle.checked = false;
      weakToggle.disabled = !weakAvailable;
      el('showWeakLifPeaksLabel').title = weakAvailable
        ? '弱候选峰只供人工复核；仅在事件标注的细胞手工配对模式可点击'
        : '当前项目的峰识别配置异常；请停止标注并检查项目文件';
      applyStageWindowWidth();
      state.selectedCandidateId = null;
      state.previewDeltaSec = null;
      state.qcRefitPreview = null;
      resetManualSelection();
      el('timeMode').value = state.timeMode;
      el('yAxisMode').value = state.yAxisMode;
      el('peakLabelMode').value = state.peakLabelMode;
      syncUmapButtonState();
      el('loaded').innerHTML = [
        `LIF trace 行数: ${state.meta.lif_trace_rows.toLocaleString()}`,
        `LIF 峰数: ${state.meta.lif_peak_rows.toLocaleString()}`,
        `MS 事件数: ${state.meta.ms_event_rows.toLocaleString()}`,
        `MS scan 数: ${state.meta.ms_scan_rows.toLocaleString()}`
      ].map(escapeText).join('<br>');
      notifyStateChannel('project-changed');
    }

    function notifyStateChannel(type = 'annotation-changed') {
      if (!stateChannel) return;
      stateChannel.postMessage({
        type,
        project_id: state.meta?.project_id || '',
        map_sha256: state.meta?.cell_event_map?.sha256 || '',
      });
    }

    function downloadTextFile(filename, text, contentType) {
      const blob = new Blob([text], { type: contentType });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }

    function automaticTimeAxisForDetector(detector) {
      const normalized = String(detector || '').trim().toLowerCase();
      if (normalized === 'green') return 'green_axis';
      if (normalized === 'red') return 'red_axis';
      return '';
    }

    function physicalTimeAxisLabel(detector) {
      const normalized = String(detector || '').trim().toLowerCase();
      if (normalized === 'green') return '绿色信号共享时间轴（自动）';
      if (normalized === 'red') return '红色信号共享时间轴（自动）';
      return '选择信号颜色后自动设置';
    }

    function newImportLifRow(initial = {}) {
      const detector = String(initial.detector || '').trim().toLowerCase();
      const row = {
        id: state.nextImportRowId++,
        path: String(initial.path || ''),
        channel: String(initial.channel || ''),
        identity_prior: String(initial.identity_prior || ''),
        detector,
        time_axis: automaticTimeAxisForDetector(detector),
        use_for_cell_annotation: Boolean(initial.use_for_cell_annotation),
      };
      return row;
    }

    function importLifRows() {
      return state.importRows.map((row, index) => {
        const detector = String(row.detector || '').trim().toLowerCase();
        return {
          key: `lif_${index + 1}`,
          path: String(row.path || '').trim(),
          channel: String(row.channel || '').trim().toUpperCase(),
          identity_prior: String(row.identity_prior || '').trim(),
          detector,
          time_axis: automaticTimeAxisForDetector(detector),
          use_for_cell_annotation: Boolean(row.use_for_cell_annotation),
        };
      });
    }

    function renderImportLifRows() {
      const box = el('importLifRows');
      if (!box) return;
      box.innerHTML = state.importRows.map((row, index) => `
        <div class="lif-input-row" data-import-row-id="${row.id}">
          <span class="lif-input-slot">LIF ${index + 1}</span>
          <div class="lif-field lif-channel">
            <span class="lif-mobile-label">LIF 通道</span>
            <select data-import-field="channel" aria-label="LIF ${index + 1} 通道">
              <option value="">选择</option>
              ${['G1','G2','R1','R2'].map(channel => `<option value="${channel}"${row.channel === channel ? ' selected' : ''}>${channel}</option>`).join('')}
            </select>
          </div>
          <div class="lif-field lif-detector">
            <span class="lif-mobile-label">信号颜色</span>
            <select data-import-field="detector" aria-label="LIF ${index + 1} 信号颜色">
              <option value="">选择</option>
              <option value="green"${row.detector === 'green' ? ' selected' : ''}>绿色</option>
              <option value="red"${row.detector === 'red' ? ' selected' : ''}>红色</option>
            </select>
          </div>
          <div class="lif-field lif-axis">
            <span class="lif-mobile-label">采集时间基准</span>
            <div class="lif-axis-auto" aria-label="LIF ${index + 1} 自动采集时间基准">${escapeText(physicalTimeAxisLabel(row.detector))}</div>
          </div>
          <div class="lif-field lif-identity">
            <span class="lif-mobile-label">样本标签</span>
            <input data-import-field="identity_prior" type="text" value="${escapeText(row.identity_prior)}" placeholder="${row.use_for_cell_annotation ? '细胞用途必填' : '可留空'}" aria-label="LIF ${index + 1} 样本标签" />
          </div>
          <div class="lif-use-options">
            <label><input data-import-field="use_for_cell_annotation" type="checkbox"${row.use_for_cell_annotation ? ' checked' : ''} /> 细胞</label>
          </div>
          <div class="lif-file-picker">
            <span class="lif-mobile-label">LIF 原始文件</span>
            <input id="importLifPath${row.id}" data-import-field="path" type="text" value="${escapeText(row.path)}" placeholder="选择 LIF 原始文件" aria-label="LIF ${index + 1} 文件路径" />
            <button type="button" class="small-button secondary path-picker-button" aria-label="选择 LIF ${index + 1} 原始文件" data-picker-target="importLifPath${row.id}" data-picker-kind="file" data-picker-role="lif" data-picker-title="选择 LIF ${index + 1} 原始文件">选择</button>
          </div>
          <button type="button" class="small-button secondary lif-remove" data-remove-import-row="${row.id}" aria-label="删除 LIF ${index + 1}"${state.importRows.length <= 2 ? ' disabled' : ''}>×</button>
        </div>
      `).join('');
      refreshImportProtocolOptions();
    }

    function duplicateImportLifPathMessage() {
      const seen = new Map();
      for (const row of importLifRows()) {
        if (!row.path) continue;
        const key = row.path.replaceAll('\\', '/').replace(/\/+/g, '/').toLowerCase();
        if (seen.has(key)) {
          return `LIF 原始文件路径不能重复：${seen.get(key)} 和 ${row.channel || row.key} 选择了同一文件。`;
        }
        seen.set(key, row.channel || row.key);
      }
      return '';
    }

    function refreshImportProtocolOptions() {
      const rows = importLifRows();
      const validChannels = new Set(rows.map(row => row.channel).filter(Boolean));
      state.importSegments.forEach(segment => {
        segment.reference_channels = (segment.reference_channels || []).filter(channel => validChannels.has(channel));
      });
      state.importScheduledQcWindows.forEach(windowRow => {
        windowRow.reference_channels = (windowRow.reference_channels || []).filter(channel => validChannels.has(channel));
      });
      state.importSignatureChannels = state.importSignatureChannels.filter(channel => validChannels.has(channel));
      const cell = rows.filter(row => row.use_for_cell_annotation).length;
      el('importRoleSummary').textContent = `细胞 ${cell} · 共 ${rows.length} 个 LIF`;
      el('addImportLif').disabled = rows.length >= 4;
      renderImportSegments();
      renderImportPostQcControls();
    }

    function newImportSegment(initial = {}) {
      return {
        id: state.nextImportSegmentId++,
        segment_id: String(initial.segment_id || ''),
        population_label: String(initial.population_label || ''),
        start_min: initial.start_min ?? '',
        end_min: initial.end_min ?? '',
        reference_channels: Array.from(initial.reference_channels || []),
        boundaries_confirmed: Boolean(initial.boundaries_confirmed),
        suggestion_status: String(initial.suggestion_status || ''),
      };
    }

    function newImportScheduledQcWindow(initial = {}) {
      return {
        id: state.nextImportScheduledQcId++,
        window_id: String(initial.window_id || ''),
        start_min: initial.start_min ?? '',
        end_min: initial.end_min ?? '',
        reference_channels: Array.from(initial.reference_channels || []),
      };
    }

    function importChannelOptions(selected = []) {
      const selectedSet = new Set(selected);
      const channels = importLifRows().map(row => row.channel).filter(Boolean);
      if (!channels.length) return '<span class="qc-anchor-empty">请先配置通道</span>';
      return channels.map(channel => `
        <label><input type="checkbox" data-segment-channel="${escapeText(channel)}"${selectedSet.has(channel) ? ' checked' : ''} /> ${escapeText(channel)}</label>
      `).join('');
    }

    function calibrationSuggestionStatusLabel(status) {
      const labels = {
        suggested: '已生成建议，待用户确认',
        ambiguous: '峰形证据有歧义，请人工定界',
        missing: '未找到足够峰形证据',
        missing_evidence: '未找到足够峰形证据',
        evidence_available: '已找到峰形证据，待形成有序边界',
        order_conflict: '峰形顺序与参考段顺序冲突',
      };
      return labels[String(status || '')] || String(status || '');
    }

    function invalidateImportCalibrationConfirmations(message = 'LIF 输入已变化，旧窗口建议已失效，请重新分析并确认。', bumpRevision = true) {
      if (bumpRevision) state.importSuggestionRevision += 1;
      state.importSegments.forEach(segment => {
        segment.boundaries_confirmed = false;
        segment.suggestion_status = message;
      });
      renderImportSegments();
      const status = el('importSuggestionStatus');
      if (status) status.textContent = message;
    }

    function renderImportSegments() {
      const box = el('importCalibrationSegments');
      if (!box) return;
      box.innerHTML = state.importSegments.map((segment, index) => `
        <div class="protocol-row" data-import-segment-id="${segment.id}">
          <strong>#${index + 1}</strong>
          <input data-segment-field="population_label" type="text" value="${escapeText(segment.population_label)}" placeholder="群体/参考段名称" aria-label="参考段 ${index + 1} 群体名称" />
          <label class="protocol-time-field"><span>开始时间 (min)</span><input data-segment-field="start_min" type="number" min="0" step="0.1" value="${escapeText(segment.start_min)}" aria-label="参考段 ${index + 1} 开始时间" /></label>
          <label class="protocol-time-field"><span>结束时间 (min)</span><input data-segment-field="end_min" type="number" min="0" step="0.1" value="${escapeText(segment.end_min)}" aria-label="参考段 ${index + 1} 结束时间" /></label>
          <div class="protocol-channel-options">${importChannelOptions(segment.reference_channels)}</div>
          <label class="protocol-confirm" title="全部参考段确认后解锁校准"><input data-segment-field="boundaries_confirmed" type="checkbox"${segment.boundaries_confirmed ? ' checked' : ''} /> 边界已确认</label>
          <button type="button" class="small-button secondary lif-remove" data-remove-import-segment="${segment.id}" aria-label="删除参考段 ${index + 1}"${state.importSegments.length <= 1 ? ' disabled' : ''}>×</button>
          ${segment.suggestion_status ? `<small class="protocol-segment-status" role="status">${escapeText(calibrationSuggestionStatusLabel(segment.suggestion_status))}</small>` : ''}
        </div>
      `).join('');
      const allConfirmed = state.importSegments.length > 0
        && state.importSegments.every(segment => segment.boundaries_confirmed === true);
      const createButton = el('runImportProject');
      if (createButton) {
        createButton.disabled = state.importCreating;
        if (state.importCreating) {
          if (!createButton.textContent.includes('正在创建')) createButton.textContent = '正在创建…';
        } else {
          createButton.textContent = allConfirmed ? '生成并进入项目' : '生成草稿并进入项目';
        }
      }
    }

    function renderImportScheduledQcWindows() {
      const box = el('importScheduledQcWindows');
      if (!box) return;
      box.innerHTML = state.importScheduledQcWindows.map((windowRow, index) => `
        <div class="protocol-row" data-import-scheduled-id="${windowRow.id}">
          <strong>#${index + 1}</strong>
          <span>QC window</span>
          <label class="protocol-time-field"><span>开始时间 (min)</span><input data-scheduled-field="start_min" type="number" min="0" step="0.1" value="${escapeText(windowRow.start_min)}" /></label>
          <label class="protocol-time-field"><span>结束时间 (min)</span><input data-scheduled-field="end_min" type="number" min="0" step="0.1" value="${escapeText(windowRow.end_min)}" /></label>
          <div class="protocol-channel-options">${importChannelOptions(windowRow.reference_channels).replaceAll('data-segment-channel', 'data-scheduled-channel')}</div>
          <span></span>
          <button type="button" class="small-button secondary lif-remove" data-remove-import-scheduled="${windowRow.id}" aria-label="删除定时 QC 窗口 ${index + 1}">×</button>
        </div>
      `).join('');
    }

    function renderImportPostQcControls() {
      const mode = el('importPostQcMode')?.value || 'disabled';
      const channels = importLifRows().map(row => row.channel).filter(Boolean);
      const select = el('importPostQcChannels');
      if (select) {
        const selected = new Set(state.importSignatureChannels);
        select.innerHTML = channels.map(channel => `<option value="${escapeText(channel)}"${selected.has(channel) ? ' selected' : ''}>${escapeText(channel)}</option>`).join('');
        select.disabled = mode !== 'signature';
      }
        if (el('importPostQcChannelsLabel')) {
          el('importPostQcChannelsLabel').style.display = mode === 'signature' ? 'grid' : 'none';
          el('importPostQcChannelsLabel').style.opacity = '1';
      }
      if (el('importScheduledQcPanel')) el('importScheduledQcPanel').style.display = mode === 'scheduled_windows' ? 'block' : 'none';
      if (el('importPostQcHint')) el('importPostQcHint').textContent = postQcModeHelp(mode);
      renderImportScheduledQcWindows();
    }

    function calibrationProtocolPayload() {
      return {
        protocol_version: 1,
        segments: state.importSegments.map((segment, index) => ({
          segment_id: String(segment.segment_id || segment.population_label || `reference_${index + 1}`)
            .trim().replace(/\s+/g, '_').toLowerCase(),
          order: index + 1,
          start_min: Number(segment.start_min),
          end_min: Number(segment.end_min),
          reference_channels: Array.from(segment.reference_channels || []),
          population_label: String(segment.population_label || '').trim(),
          boundaries_confirmed: Boolean(segment.boundaries_confirmed),
        })),
      };
    }

    function calibrationSuggestionSegmentsPayload() {
      return state.importSegments.map((segment, index) => ({
        segment_id: String(segment.segment_id || segment.population_label || `reference_${index + 1}`)
          .trim().replace(/\s+/g, '_').toLowerCase(),
        order: index + 1,
        reference_channels: Array.from(segment.reference_channels || []),
        population_label: String(segment.population_label || '').trim(),
      }));
    }

    async function suggestImportCalibrationWindows() {
      if (state.actionBusy) return;
      const requestRevision = state.importSuggestionRevision;
      const button = el('suggestImportWindows');
      const status = el('importSuggestionStatus');
      const oldText = button.textContent;
      state.actionBusy = true;
      button.disabled = true;
      button.textContent = '正在分析峰形…';
      status.textContent = '正在只读分析已选 LIF 原始文件；不会创建项目或修改原始数据。';
      try {
        refreshImportProtocolOptions();
        const lifInputs = importLifRows();
        if (lifInputs.length < 2 || lifInputs.length > 4) throw new Error('窗口建议需要 2–4 个 LIF 输入。');
        if (lifInputs.some(row => !row.path || !row.channel)) throw new Error('请先为每个 LIF 输入选择文件并填写通道。');
        if (new Set(lifInputs.map(row => row.channel)).size !== lifInputs.length) throw new Error('LIF 通道名不能重复。');
        const duplicatePathMessage = duplicateImportLifPathMessage();
        if (duplicatePathMessage) throw new Error(duplicatePathMessage);
        const segments = calibrationSuggestionSegmentsPayload();
        if (!segments.length || segments.some(row => !row.reference_channels.length)) {
          throw new Error('请先为每个参考段选择至少一个参考通道。');
        }
        const annotationStart = Number(el('importAnnotationStart').value);
        if (!Number.isFinite(annotationStart) || annotationStart <= 0) throw new Error('请先填写大于 0 的事件标注起点。');
        const result = await postJson('/api/suggest-calibration-windows', {
          lif_inputs: lifInputs,
          segments,
          annotation_start_min: annotationStart,
        });
        if (requestRevision !== state.importSuggestionRevision) {
          status.textContent = 'LIF 输入在分析期间已变化，本次旧建议已丢弃；请按当前文件重新分析。';
          return;
        }
        const byId = new Map((result.segments || []).map(row => [String(row.segment_id), row]));
        state.importSegments.forEach((segment, index) => {
          const requestId = segments[index].segment_id;
          const suggestion = byId.get(requestId) || result.segments?.[index] || {};
          if (suggestion.suggested_start_min !== null && suggestion.suggested_start_min !== undefined && suggestion.suggested_start_min !== '' && Number.isFinite(Number(suggestion.suggested_start_min))) {
            segment.start_min = Number(suggestion.suggested_start_min);
          }
          if (suggestion.suggested_end_min !== null && suggestion.suggested_end_min !== undefined && suggestion.suggested_end_min !== '' && Number.isFinite(Number(suggestion.suggested_end_min))) {
            segment.end_min = Number(suggestion.suggested_end_min);
          }
          segment.boundaries_confirmed = false;
          segment.suggestion_status = calibrationSuggestionStatusLabel(
            suggestion.status || '未获得建议'
          );
        });
        renderImportSegments();
        const summaries = (result.channel_summaries || [])
          .map(row => `${row.channel} ${Number(row.merged_peak_count || 0).toLocaleString()} 峰`)
          .join(' · ');
        const warnings = (result.warnings || []).join(' ');
        status.textContent = [
          result.can_apply_suggestions ? '已回填建议边界；可先创建草稿并在项目轨迹中核对。所有边界保持待确认，确认前不会用于校准。' : '证据不足，未形成可完整应用的有序方案。',
          summaries,
          warnings,
        ].filter(Boolean).join(' ');
      } catch (err) {
        status.textContent = `峰形建议失败：${err.message}`;
        alert(`峰形建议失败: ${err.message}`);
      } finally {
        button.disabled = false;
        button.textContent = oldText;
        state.actionBusy = false;
      }
    }

    function postQcStrategyPayload() {
      const mode = el('importPostQcMode').value;
      if (mode === 'disabled') return { mode };
      if (mode === 'signature') return {
        mode,
        reference_channels: Array.from(el('importPostQcChannels').selectedOptions).map(option => option.value),
      };
      return {
        mode,
        windows: state.importScheduledQcWindows.map((windowRow, index) => ({
          window_id: String(windowRow.window_id || `post_qc_${index + 1}`).trim(),
          start_min: Number(windowRow.start_min),
          end_min: Number(windowRow.end_min),
          reference_channels: Array.from(windowRow.reference_channels || []),
        })),
      };
    }

    let activeModal = null;
    let modalReturnFocus = null;

    function modalFocusableElements(modal) {
      return Array.from(modal.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      )).filter(node => node.getClientRects().length > 0);
    }

    function setModalVisibility(modalId, open, preferredFocusId = null) {
      const modal = el(modalId);
      modal.classList.toggle('open', open);
      if (open) {
        modalReturnFocus = document.activeElement;
        activeModal = modal;
        window.requestAnimationFrame(() => {
          const preferred = preferredFocusId ? el(preferredFocusId) : null;
          const target = preferred || modalFocusableElements(modal)[0];
          if (target) target.focus();
        });
        return;
      }
      if (activeModal !== modal) return;
      activeModal = null;
      const returnTarget = modalReturnFocus;
      modalReturnFocus = null;
      if (returnTarget && document.contains(returnTarget)) returnTarget.focus();
    }

    function closeActiveModal() {
      if (!activeModal) return;
      if (activeModal.id === 'importModal') setImportModal(false);
      if (activeModal.id === 'openProjectModal') setOpenProjectModal(false);
      if (activeModal.id === 'projectConfigModal') setProjectConfigModal(false);
    }

    function setImportModal(open) {
      if (!open && state.importCreating) return;
      if (open) {
        state.importSuggestionRevision += 1;
        [
          'importProjectDir',
          'importMs',
          'importCellEventMap',
        ].forEach((targetId) => {
          el(targetId).value = '';
        });
        state.importRows = [newImportLifRow(), newImportLifRow()];
        state.importSegments = [newImportSegment()];
        state.importScheduledQcWindows = [];
        state.importSignatureChannels = [];
        el('importAnnotationStart').value = '40';
        el('importSeedWindow').value = '2.5';
        el('importPostQcMode').value = 'disabled';
        const templateOptions = el('importProjectTemplates');
        if (templateOptions) templateOptions.open = false;
        renderImportLifRows();
        renderImportSegments();
        renderImportPostQcControls();
        el('importSuggestionStatus').textContent = '尚未分析原始峰形。';
        const externalMode = document.querySelector('input[name="rawInputMode"][value="external_reference"]');
        if (externalMode) externalMode.checked = true;
        el('importHint').textContent = '';
      }
      setModalVisibility('importModal', open, 'importProjectDir');
    }

    function setOpenProjectModal(open) {
      if (open) {
        const projectDir = state.meta?.project?.project_dir || '';
        el('openProjectDir').placeholder = projectDir || '/path/to/existing_project';
        el('openProjectHint').textContent = '只支持使用现行峰识别标准建立的项目。旧标准项目不会被修改；请改用新的空目录重建。';
      }
      setModalVisibility('openProjectModal', open, 'openProjectDir');
    }

    function setProjectConfigModal(open) {
      if (!open && state.configSaveBusy) return;
      if (open) {
        renderConfigInputs(true);
        setConfigSaveStatus('');
        const attachAllowed = Boolean(state.meta?.cell_event_map?.attach_allowed);
        el('attachMapPanel').style.display = attachAllowed ? 'block' : 'none';
        el('attachCellEventMap').value = '';
        el('attachCellEventMap').title = '';
        const projectName = state.meta?.project?.project_dir
          ? state.meta.project.project_dir.split(/[\\/]/).filter(Boolean).pop()
          : '';
        el('attachMapProjectName').textContent = projectName ? ` 当前项目：${projectName}` : '';
        updateAttachMapControls();
      }
      setModalVisibility('projectConfigModal', open);
    }

    async function openUmapWindow() {
      if (!state.meta?.cell_event_map?.available) {
        setProjectConfigModal(true);
        if (state.meta?.cell_event_map?.attach_allowed) {
          setConfigSaveStatus(
            '当前项目尚未附加事件坐标 CSV，因此 UMAP 暂不可用。请在上方选择 CSV，再点击“校验并附加到当前项目”。',
            'warning'
          );
          window.setTimeout(() => el('attachCellEventMap').focus(), 0);
        } else {
          setConfigSaveStatus(
            '当前项目没有事件坐标 map，且缺少可附加 map 的项目清单。UMAP 暂不可用；旧标注工作流仍可继续使用。',
            'warning'
          );
        }
        return;
      }
      try {
        if (window.pywebview?.api?.open_umap_window) {
          await window.pywebview.api.open_umap_window();
        } else {
          window.open('/umap', 'lma-umap');
        }
      } catch (err) {
        alert(`无法打开 UMAP 窗口: ${err.message || err}`);
      }
    }

    function selectedRawInputMode() {
      const checked = document.querySelector('input[name="rawInputMode"]:checked');
      return checked ? checked.value : 'external_reference';
    }

    async function init() {
      state.meta = await fetchJson('/api/meta');
      syncBootstrapMode();
      state.start = Math.max(0, state.meta.time_min_min);
      applyStageWindowWidth();
      el('start').value = state.start.toFixed(2);
      el('timeMode').value = state.timeMode;
      el('yAxisMode').value = state.yAxisMode;
      el('peakLabelMode').value = state.peakLabelMode;
      syncUmapButtonState();
      const loadedLines = state.meta.bootstrap ? [
        '等待新建或打开项目',
        '请选择新建项目或打开已有项目。'
      ] : [
        `LIF trace 行数: ${state.meta.lif_trace_rows.toLocaleString()}`,
        `LIF 峰数: ${state.meta.lif_peak_rows.toLocaleString()}`,
        `MS 事件数: ${state.meta.ms_event_rows.toLocaleString()}`,
        `MS scan 数: ${state.meta.ms_scan_rows.toLocaleString()}`
      ];
      el('loaded').innerHTML = loadedLines.map(escapeText).join('<br>');
      el('bootstrapStatus').textContent = state.meta.bootstrap
        ? '当前未加载项目。请选择新建项目或打开已有项目。'
        : `已加载项目: ${state.meta.project.project_dir}`;
      renderConfigInputs();
      await loadWindow();
      if (!state.meta.bootstrap) notifyStateChannel('project-changed');
    }

    async function loadWindow() {
      const seq = ++state.requestSeq;
      hideLineContextMenu();
      document.body.classList.add('loading');
      if (!calibrationBoundariesConfirmed()) {
        const stageChanged = state.stage !== 'qc_calibration';
        state.stage = 'qc_calibration';
        state.timeMode = 'raw';
        state.previewDeltaSec = null;
        state.localDeltaPreview = null;
        if (stageChanged) {
          const firstSegment = calibrationProtocol()?.segments?.[0];
          state.start = Math.max(
            Number(state.meta?.time_min_min || 0),
            Number(firstSegment?.start_min || 0)
          );
        }
        applyStageWindowWidth();
        el('timeMode').value = 'raw';
      }
      let url = `/api/window?start_min=${encodeURIComponent(state.start)}&window_min=${encodeURIComponent(state.width)}&time_mode=${encodeURIComponent(state.timeMode)}`;
      url += `&include_weak_lif_peaks=${state.showWeakLifPeaks ? 'true' : 'false'}`;
      if (state.stage === 'local_calibration' && state.previewDeltaSec !== null) {
        url += `&preview_ms_delta_sec=${encodeURIComponent(state.previewDeltaSec)}`;
      }
      const payload = await fetchJson(url);
      if (seq !== state.requestSeq) return;
      state.current = payload;
      state.start = state.current.start_min;
      state.width = state.current.window_min;
      el('widthDisplay').value = fmt(state.width, 2);
      state.meta.project_config = payload.project_config || state.meta.project_config;
      state.meta.time_model = payload.time_model || state.meta.time_model;
      el('start').value = state.start.toFixed(2);
      if (state.stage === 'local_calibration' && calibrationBoundariesConfirmed()) {
        await loadLocalDeltaPreview(state.previewDeltaSec);
      }
      updateMetrics();
      draw();
      renderCandidateList();
      renderManualSelection();
      updateAcceptWindowButton();
      document.body.classList.remove('loading');
    }

    function updateMetrics() {
      const w = state.current;
      renderTrackLegend();
      el('range').textContent = `${fmt(w.start_min)}-${fmt(w.end_min)} min`;
      el('lifPeakCount').textContent = w.counts.lif_peaks;
      el('msEventCount').textContent = w.counts.ms_events;
      el('lifPointCount').textContent = w.counts.lif_trace_points_returned.toLocaleString();
      el('msPointCount').textContent = w.counts.ms_scan_points_returned.toLocaleString();
      el('title').textContent = `${w.time_mode === 'aligned' ? '校正后' : '原始'}同步 ${fmt(w.window_min, 1)} min 窗口: ${fmt(w.start_min)}-${fmt(w.end_min)} min`;
      updateTimeModelPanel();
      const counts = stageCounts();
      el('pendingCount').textContent = counts.pending || 0;
      el('acceptedCount').textContent = counts.accepted || 0;
      el('rejectedCount').textContent = counts.rejected || 0;
      el('stageNote').textContent = stageNote();
      renderConfigInputs();
      renderQcRefitPanel();
      renderLocalDeltaPanel();
      renderStagePanels();
      updateExportHint();
      document.querySelectorAll('.stage-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.stage === state.stage);
      });
    }

    function renderCurrentState() {
      if (!state.current) return;
      updateMetrics();
      draw();
      renderCandidateList();
      renderManualSelection();
      updateAcceptWindowButton();
    }

    function updateExportHint() {
      el('exportHint').textContent = '全部事件均导出；未标注为 unknown，前段 QC anchor 留在审计库';
    }

    function postQcModeLabel(mode) {
      if (mode === 'disabled') return 'Off';
      if (mode === 'signature') return 'QC signature';
      if (mode === 'scheduled_windows') return 'Scheduled windows';
      return 'Post-run QC';
    }

    function postQcModeHelp(mode) {
      if (mode === 'signature') {
        return 'QC 时间未知或不规律：在整个事件标注段寻找所选 QC 通道组合。';
      }
      if (mode === 'scheduled_windows') {
        return 'QC 时间已知：仅在填写的时间窗口内寻找，减少误匹配。';
      }
      return '后段没有再次注入 QC 时使用；Lin− / LSK 示例选择 Off。';
    }

    function physicalAxisName(axis) {
      const normalized = String(axis || '').trim().toLowerCase();
      if (normalized === 'green_axis') return '绿色信号时间轴';
      if (normalized === 'red_axis') return '红色信号时间轴';
      return '共享信号时间轴';
    }

    function referenceModeLabel(mode) {
      const normalized = String(mode || '').trim().toLowerCase();
      if (normalized === 'green_only') return '仅绿色通道';
      if (normalized === 'red_only') return '仅红色通道';
      if (normalized === 'red_green') return '红绿联合';
      return '组合参考';
    }

    function calibrationSegmentDisplayName(segment) { const label = String(segment?.population_label || '').trim(); return label || '未命名参考段'; }

    function stageNote() {
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const frozen = tm.status === 'frozen';
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      const anchors = qcAnchorChannels();
      if (!calibrationBoundariesConfirmed()) {
        return '参考段边界待确认：当前只显示原始峰形。请在“配置”中核对并确认全部边界，随后才会计算前段校准并解锁后段阶段。';
      }
      if (state.stage === 'local_calibration') return '使用标注起点后的无身份标签峰，只估计 MS 的局部时间差；此步骤不会写入人工标注。';
      if (state.stage === 'event_annotation') {
        const strategy = cfg.post_qc_strategy || {};
        const policy = strategy.mode === 'disabled'
          ? '不进行后段巡检，只显示细胞候选'
          : `${postQcModeLabel(strategy.mode)}（${(strategy.reference_channels || []).join('/') || '按窗口设置'}）`;
        return frozen
        ? `审核事件坐标表范围内的事件；${policy}；多个通道指向同一 MS 事件时必须人工选择唯一关系。`
        : '请先在“后段时间差校正”中确认并锁定 MS 时间差，再进入事件标注。';
      }
      const segment = activeCalibrationSegment();
      if (!segment) return '当前项目未找到可显示的校准参考段。';
      return `参考段 #${segment.order} ${calibrationSegmentDisplayName(segment)}：${fmt(segment.start_min, 2)}-${fmt(segment.end_min, 2)} min；审核 ${anchors.join('/')} 与 MS 是否可通过单一时间平移对齐。`;
    }

    function timeModelDisplayName(tm) {
      if (tm.status === 'calibration_boundaries_unconfirmed') return '边界待确认';
      const status = tm.status === 'frozen' ? '已锁定' : (tm.status === 'exploratory' ? '试算' : '草稿');
      const delta = Number(tm.ms_local_delta_sec || 0);
      return `${status} ${delta >= 0 ? '+' : ''}${fmt(delta, 2)}s`;
    }

    function configuredPhysicalAxes() {
      const axes = (state.meta?.acquisition_layout?.lif_channels || [])
        .map(row => String(row.time_axis || '').trim())
        .filter(Boolean);
      return Array.from(new Set(axes));
    }

    function renderAxisShiftMetrics(windowState, physicalAxes, suffix) {
      const box = el('axisShiftMetrics');
      if (!box) return;
      const alignment = windowState?.alignment || {};
      const shifts = alignment.axis_shifts_sec || {};
      const calibrationBlocked = alignment.status === 'calibration_boundaries_unconfirmed';
      box.innerHTML = physicalAxes.map(axis => {
        let value = shifts[axis];
        if (!Number.isFinite(Number(value)) && axis === 'green_axis') value = alignment.green_to_ms_shift_sec;
        if (!Number.isFinite(Number(value)) && axis === 'red_axis') value = alignment.red_to_ms_shift_sec;
        const display = !calibrationBlocked && Number.isFinite(Number(value)) ? `${fmt(Number(value), 2)} sec` : '未估计';
        return `<div class="metric" data-physical-axis="${escapeText(axis)}"><span>${escapeText(physicalAxisName(axis))} ${escapeText(suffix)}</span><strong>${escapeText(display)}</strong></div>`;
      }).join('');
    }

    function updateTimeModelPanel() {
      const w = state.current;
      const tm = w?.time_model || state.meta?.time_model || {};
      const totalGroups = (w.alignment.qc_groups && w.alignment.qc_groups.groups ? w.alignment.qc_groups.groups.length : 0);
      const visibleGroups = (w.alignment_groups || []).length;
      const physicalAxes = configuredPhysicalAxes();
      if (state.stage === 'qc_calibration') {
        renderAxisShiftMetrics(w, physicalAxes, '平移');
        el('baseTimeTitle').textContent = '自动时间校正';
        el('modeMetricLabel').textContent = '模式';
        el('modeLabel').textContent = calibrationBoundariesConfirmed()
          ? (w.time_mode === 'aligned' ? '校正后' : '原始')
          : '仅原始浏览';
        el('msDeltaMetric').style.display = 'none';
        el('matchMetricLabel').textContent = '参考峰组';
        el('matchCount').textContent = w.time_mode === 'aligned' ? `${visibleGroups}/${totalGroups}` : '-';
        return;
      }
      renderAxisShiftMetrics(w, physicalAxes, '基础平移');
      el('baseTimeTitle').textContent = '当前时间模型';
      el('modeMetricLabel').textContent = '状态';
      el('modeLabel').textContent = tm.status === 'calibration_boundaries_unconfirmed'
        ? '边界待确认'
        : (tm.status === 'frozen' ? '已锁定' : '草稿');
      el('msDeltaMetric').style.display = 'grid';
      el('msDeltaShift').textContent = `${fmt(tm.ms_local_delta_sec || 0, 2)} sec`;
      el('matchMetricLabel').textContent = '时间模型';
      el('matchCount').textContent = timeModelDisplayName(tm);
      el('matchCount').title = '前段基础平移与 MS 后段时间差的组合结果';
    }

    function stageCounts() {
      if (!state.current) return { pending: 0, accepted: 0, rejected: 0 };
      if (state.stage === 'local_calibration') {
        return { pending: 0, accepted: 0, rejected: 0 };
      }
      if (state.stage === 'event_annotation') {
        const qc = state.current.post_qc_counts || {};
        const cell = state.current.cell_counts || {};
        const conflictPending = (state.current.cell_candidates || [])
          .filter(isPendingCrossChannelConflict).length;
        const visibleCell = {
          ...cell,
          pending: Math.max(0, Number(cell.pending || 0) - conflictPending),
        };
        if (state.eventFilter === 'qc') return qc;
        if (state.eventFilter === 'cell') return visibleCell;
        return {
          pending: Number(qc.pending || 0) + Number(visibleCell.pending || 0),
          accepted: Number(qc.accepted || 0) + Number(visibleCell.accepted || 0),
          rejected: Number(qc.rejected || 0) + Number(visibleCell.rejected || 0),
        };
      }
      return state.current.annotation_counts || {};
    }

    function extent(values) {
      let min = Infinity, max = -Infinity;
      values.forEach(v => {
        const y = Number(v);
        if (Number.isFinite(y)) {
          min = Math.min(min, y);
          max = Math.max(max, y);
        }
      });
      if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
      if (min === max) return [min - 1, max + 1];
      const pad = (max - min) * 0.08;
      return [min - pad, max + pad];
    }

    function quantile(sorted, q) {
      if (!sorted.length) return NaN;
      const pos = (sorted.length - 1) * q;
      const lo = Math.floor(pos);
      const hi = Math.ceil(pos);
      if (lo === hi) return sorted[lo];
      return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
    }

    function robustExtent(values) {
      const finite = values.map(Number).filter(Number.isFinite).sort((a, b) => a - b);
      if (finite.length < 8) return extent(values);
      const min = quantile(finite, 0.01);
      const max = quantile(finite, 0.995);
      if (!Number.isFinite(min) || !Number.isFinite(max) || min >= max) return extent(values);
      const pad = (max - min) * 0.08;
      return [min - pad, max + pad];
    }

    function yAxisExtent(values) {
      return state.yAxisMode === 'robust' ? robustExtent(values) : extent(values);
    }

    function clampForAxis(y, yMin, yMax) {
      const value = Number(y);
      if (!Number.isFinite(value)) return yMin;
      if (state.yAxisMode !== 'robust') return value;
      return Math.max(yMin, Math.min(yMax, value));
    }

    function lifPeakY(peak) {
      const displayY = Number(peak.display_y);
      if (Number.isFinite(displayY)) return displayY;
      const fallback = Number(peak.height);
      return Number.isFinite(fallback) ? fallback : 0;
    }

    function linePath(points, xScale, yScale) {
      if (!points.length) return '';
      return points.map((p, i) => `${i === 0 ? 'M' : 'L'}${xScale(p.x).toFixed(2)},${yScale(p.y).toFixed(2)}`).join(' ');
    }

    function isPendingCrossChannelConflict(row) {
      return row?.cross_channel_candidate_conflict === true
        && String(row?.review_status || 'pending') === 'pending';
    }

    function pendingCrossChannelConflictGroups() {
      if (state.stage !== 'event_annotation' || state.eventFilter === 'qc') return [];
      const groups = new Map();
      (state.current?.cell_candidates || [])
        .filter(row => row?.cross_channel_candidate_conflict === true)
        .forEach(row => {
          const key = String(row.ms_event_id || '');
          if (!key) return;
          if (!groups.has(key)) groups.set(key, []);
          groups.get(key).push(row);
        });
      return Array.from(groups.entries())
        .filter(([, rows]) => rows.some(isPendingCrossChannelConflict))
        .map(([msEventId, rows]) => ({
          ms_event_id: msEventId,
          rows: rows.sort((a, b) => (
            Number(a.abs_residual_sec || 0) - Number(b.abs_residual_sec || 0)
            || String(a.lif_channel || '').localeCompare(String(b.lif_channel || ''))
          )),
        }))
        .sort((a, b) => Number(a.rows[0]?.ms_plot_time_min || 0) - Number(b.rows[0]?.ms_plot_time_min || 0));
    }

    function visibleCellCandidates() {
      return (state.current?.cell_candidates || []).filter(row => (
        !isPendingCrossChannelConflict(row) || state.showCrossChannelConflicts
      ));
    }

    function renderCrossChannelConflictControl() {
      const groups = pendingCrossChannelConflictGroups();
      const control = el('crossChannelConflictControl');
      const visible = groups.length > 0;
      control.style.display = visible ? 'flex' : 'none';
      if (!visible) return;
      const n = groups.length;
      el('crossChannelConflictHint').textContent = state.showCrossChannelConflicts
        ? `Show conflicts (${n})`
        : `${n} ambiguous event${n === 1 ? '' : 's'} hidden`;
    }

    function candidateRows() {
      let rows = [];
      if (state.stage === 'local_calibration') {
        rows = [];
      } else if (state.stage === 'event_annotation') {
        rows = [
          ...(state.current?.post_qc_candidates || []),
          ...visibleCellCandidates().filter(row => !isPendingCrossChannelConflict(row)),
        ].filter(eventRowMatchesFilter);
      } else {
        rows = [...(state.current?.alignment_groups || [])];
      }
      const manualRows = (state.current?.annotations || [])
        .filter(row => row.source === 'manual_created')
        .filter(row => manualBelongsToStage(row, state.stage))
        .filter(row => state.stage !== 'event_annotation' || eventRowMatchesFilter(row))
        .map(row => ({
          ...row,
          rank: '人工',
          candidate_id: row.annotation_id
        }));
      const combined = (state.stage === 'qc_calibration' || state.stage === 'event_annotation') ? [...rows, ...manualRows] : rows;
      return combined
        .filter(row => state.showRejected || row.review_status !== 'rejected')
        .sort((a, b) => Number(a.ms_plot_time_min || a.ms_time_min || 0) - Number(b.ms_plot_time_min || b.ms_time_min || 0));
    }

    function manualBelongsToStage(row, stage) {
      const explicit = row.review_stage || '';
      if (stage === 'event_annotation') {
        return ['qc_survey', 'cell_annotation'].includes(explicit)
          || eventRowKind(row) === 'qc'
          || eventRowKind(row) === 'cell';
      }
      if (explicit === 'qc_calibration' || explicit === 'qc_survey' || explicit === 'cell_annotation') return explicit === stage;
      if (stage === 'cell_annotation' && (row.candidate_type === 'manual_cell_pair' || String(row.candidate_type || '').startsWith('cell'))) return true;
      if (stage === 'qc_survey' && row.candidate_type === 'manual_qc_anchor_partial') return true;
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      const annotationStart = Number(cfg.annotation_start_min || 40);
      const qcEnd = Number(cfg.qc_calibration_end_min || 10.5);
      const msTime = Number(row.ms_time_min);
      if (stage === 'qc_survey') return Number.isFinite(msTime) && msTime >= annotationStart;
      if (stage === 'qc_calibration') return Number.isFinite(msTime) && msTime <= qcEnd;
      return false;
    }

    function eventRowKind(row) {
      const explicit = String(row?.review_stage || '');
      const type = String(row?.candidate_type || '');
      if (explicit === 'cell_annotation' || type === 'manual_cell_pair' || type.startsWith('cell')) return 'cell';
      if (explicit === 'qc_survey' || type.startsWith('qc_survey_') || type.startsWith('manual_qc')) return 'qc';
      return '';
    }

    function eventRowMatchesFilter(row) {
      return state.eventFilter === 'all' || eventRowKind(row) === state.eventFilter;
    }

    function qcCandidatesForCurrentStage() {
      if (!state.current) return [];
      if (state.stage !== 'qc_calibration') return [];
      return state.current.alignment_groups || [];
    }

    function candidateInsideMainWindow(row) {
      const start = Number(state.current?.start_min);
      const end = Number(state.current?.end_min);
      const anchorTimes = qcAnchorTimes(row, 'plot');
      const values = [
        ...qcAnchorChannels(row).map(channel => anchorTimes[channel]),
        row.ms_plot_time_min
      ].filter(value => value !== null && value !== undefined && value !== '').map(Number);
      return values.length >= 2 && values.every(value => Number.isFinite(value) && value >= start && value <= end);
    }

    function candidateBatchBlockReason(row) {
      if (row.batch_accept_block_reason !== undefined) return row.batch_accept_block_reason;
      if (row.review_enabled === false) return 'review_disabled';
      if (row.source !== 'auto_candidate') return 'not_auto_candidate';
      if (row.review_status !== 'pending') return `status_${row.review_status}`;
      if (row.axis_coherent === false) return 'axis_incoherent';
      if (row.complete_anchor_set === false) return 'partial_anchor_set';
      if (Number(row.conflict_count || 0) > 0) return 'conflicting_anchor_set';
      const tolerance = Number(row.match_tolerance_sec || 4);
      if (Math.abs(Number(row.composite_to_ms_residual_sec || 0)) > tolerance) return 'composite_residual_out_of_tolerance';
      if (Number.isFinite(Number(row.max_abs_axis_to_ms_residual_sec))
          && Number(row.max_abs_axis_to_ms_residual_sec) > tolerance) return 'axis_residual_out_of_tolerance';
      if (!candidateInsideMainWindow(row)) return 'outside_main_window';
      return null;
    }

    function batchBlockText(reason, row = {}) {
      const tolerance = fmt(Number(row.match_tolerance_sec || 4), 1);
      const labels = {
        conflicting_anchor_set: '附近有多个可匹配峰',
        partial_anchor_set: 'LIF 通道不完整',
        axis_incoherent: '各通道时间不一致',
        composite_residual_out_of_tolerance: `偏差超过 ${tolerance} sec`,
        axis_residual_out_of_tolerance: `偏差超过 ${tolerance} sec`,
        outside_main_window: '部分峰不在主窗口',
        review_disabled: '当前阶段尚未解锁'
      };
      return labels[reason] || '不符合批量接受条件';
    }

    function pendingAutoCandidatesInMainWindow() {
      return qcCandidatesForCurrentStage().filter(row => (
        row.review_enabled !== false
        && row.source === 'auto_candidate'
        && row.review_status === 'pending'
        && candidateInsideMainWindow(row)
      ));
    }

    function batchAcceptableAutoCandidatesInMainWindow() {
      return pendingAutoCandidatesInMainWindow().filter(row => !candidateBatchBlockReason(row));
    }

    function candidateNeedsIndividualReview(row) {
      const reason = candidateBatchBlockReason(row);
      return row.source === 'auto_candidate'
        && row.review_status === 'pending'
        && Boolean(reason)
        && reason !== 'outside_main_window';
    }

    function updateAcceptWindowButton() {
      const pending = pendingAutoCandidatesInMainWindow().length;
      const n = batchAcceptableAutoCandidatesInMainWindow().length;
      const individual = Math.max(0, pending - n);
      el('acceptWindow').textContent = `批量接受唯一匹配（${n}）`;
      if (state.stage === 'event_annotation' || state.stage === 'local_calibration') {
        if (state.stage === 'local_calibration') {
          el('acceptWindowHint').textContent = '后段时间差校正只生成预览，不会写入人工标注';
        } else {
          el('acceptWindowHint').textContent = '事件标注阶段的 QC / 细胞候选均需逐条确认';
        }
        el('acceptWindow').disabled = true;
        return;
      }
      const tm = state.current?.time_model || state.meta?.time_model || {};
      el('acceptWindowHint').textContent = `待审 ${pending}：可批量 ${n}${individual ? `，需逐条 ${individual}` : ''}`;
      el('acceptWindow').disabled = n === 0;
    }

    function renderConfigInputs(resetDraft = false) {
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      if (!cfg) return;
      el('cfgQcEnd').value = fmt(cfg.qc_calibration_end_min ?? 10.5, 1);
      el('cfgAnnotationStart').value = fmt(cfg.annotation_start_min ?? 40.0, 1);
      el('cfgSeedWindow').value = fmt(cfg.local_delta_seed_window_min ?? 2.5, 1);
      const detector = cfg.lif_peak_detection || state.meta?.lif_peak_detection || {};
      el('cfgLifPeakStandard').textContent = '自适应双层峰识别';
      const core = detector.core || {};
      const weak = detector.weak || {};
      el('cfgLifPeakDetectorDetails').textContent = `高置信峰阈值 ≥ ${fmt(core.prominence_snr_min, 2)}σ；弱候选峰阈值 ≥ ${fmt(weak.prominence_snr_min, 2)}σ；峰形相似度 ≥ ${fmt(weak.template_similarity_min, 2)}；局部噪声窗口 ${fmt(weak.local_noise_block_sec, 1)} s；至少 ${weak.min_core_template_peaks ?? '-'} 个高置信峰且平均 ${fmt(weak.min_core_rate_per_min, 2)} 个/分钟；弱候选峰仅供人工细胞配对，不参与自动流程`;
      const protocol = cfg.calibration_protocol || null;
      const legacy = Boolean(protocol?.compatibility_mode);
      el('cfgQcEnd').disabled = true;
      el('cfgQcEnd').title = legacy
        ? '此项目的参考窗口按原规则只读显示；如需改变结构，请用原始输入在新目录中重建项目。'
        : '由最后一个已确认参考段的结束时间自动计算。';
      el('cfgProtocolPanel').style.display = protocol ? 'block' : 'none';
      if (resetDraft || !state.configProtocolDraft) {
        state.configProtocolDraft = protocol ? JSON.parse(JSON.stringify(protocol)) : null;
      }
      if (resetDraft || !state.configPostQcDraft) {
        state.configPostQcDraft = JSON.parse(JSON.stringify(cfg.post_qc_strategy || { mode: 'disabled' }));
      }
      renderConfigProtocolEditor();
      renderConfigPostQcEditor();
    }

    function renderConfigProtocolEditor() {
      const box = el('cfgCalibrationSegments');
      const protocol = state.configProtocolDraft;
      if (!box || !protocol) return;
      const legacy = Boolean(protocol.compatibility_mode);
      const legacyNotice = legacy
        ? '<div class="qc-anchor-rule">此项目的前段参考窗口按原规则只读显示；软件不会静默改写。</div>'
        : '';
      box.innerHTML = legacyNotice + (protocol.segments || []).map((segment, index) => `
        <div class="protocol-row" data-cfg-segment-index="${index}">
          <strong>#${segment.order}</strong>
          <span><strong>${escapeText(calibrationSegmentDisplayName(segment))}</strong><br><small>${escapeText((segment.reference_channels || []).join('/'))}</small></span>
          <label class="protocol-time-field"><span>开始时间 (min)</span><input data-cfg-segment-field="start_min" type="number" min="0" step="0.1" value="${escapeText(segment.start_min)}" aria-label="${escapeText(calibrationSegmentDisplayName(segment))} 开始时间"${legacy ? ' disabled' : ''} /></label>
          <label class="protocol-time-field"><span>结束时间 (min)</span><input data-cfg-segment-field="end_min" type="number" min="0" step="0.1" value="${escapeText(segment.end_min)}" aria-label="${escapeText(calibrationSegmentDisplayName(segment))} 结束时间"${legacy ? ' disabled' : ''} /></label>
          <span>${escapeText(referenceModeLabel(segment.reference_mode))} · ${(segment.time_axes || []).map(axis => escapeText(physicalAxisName(axis))).join('/')}</span>
          <label class="protocol-confirm" title="全部参考段确认后解锁校准"><input data-cfg-segment-field="boundaries_confirmed" type="checkbox"${segment.boundaries_confirmed ? ' checked' : ''}${legacy ? ' disabled' : ''} /> 边界已确认</label>
          <span></span>
        </div>
      `).join('');
    }

    function configChannelCheckboxes(selected = []) {
      const selectedSet = new Set(selected);
      const channels = (state.meta?.acquisition_layout?.lif_channels || []).map(row => row.channel);
      return channels.map(channel => `<label><input type="checkbox" data-cfg-scheduled-channel="${escapeText(channel)}"${selectedSet.has(channel) ? ' checked' : ''} /> ${escapeText(channel)}</label>`).join('');
    }

    function renderConfigPostQcEditor() {
      const strategy = state.configPostQcDraft || { mode: 'disabled' };
      const mode = strategy.mode || 'disabled';
      el('cfgPostQcMode').value = mode;
      const channels = (state.meta?.acquisition_layout?.lif_channels || []).map(row => row.channel);
      const selected = new Set(strategy.reference_channels || []);
      el('cfgPostQcChannels').innerHTML = channels.map(channel => `<option value="${escapeText(channel)}"${selected.has(channel) ? ' selected' : ''}>${escapeText(channel)}</option>`).join('');
      el('cfgPostQcChannels').disabled = mode !== 'signature';
      el('cfgPostQcChannelsLabel').style.display = mode === 'signature' ? 'grid' : 'none';
      el('cfgPostQcChannelsLabel').style.opacity = '1';
      el('cfgPostQcHint').textContent = postQcModeHelp(mode);
      el('cfgAddScheduledQc').style.display = mode === 'scheduled_windows' ? 'block' : 'none';
      const box = el('cfgScheduledQcWindows');
      box.style.display = mode === 'scheduled_windows' ? 'grid' : 'none';
      box.innerHTML = (strategy.windows || []).map((windowRow, index) => `
        <div class="protocol-row" data-cfg-scheduled-index="${index}">
          <strong>#${index + 1}</strong>
          <span>QC window</span>
          <label class="protocol-time-field"><span>开始时间 (min)</span><input data-cfg-scheduled-field="start_min" type="number" min="0" step="0.1" value="${escapeText(windowRow.start_min ?? '')}" /></label>
          <label class="protocol-time-field"><span>结束时间 (min)</span><input data-cfg-scheduled-field="end_min" type="number" min="0" step="0.1" value="${escapeText(windowRow.end_min ?? '')}" /></label>
          <div class="protocol-channel-options">${configChannelCheckboxes(windowRow.reference_channels)}</div>
          <span></span>
          <button type="button" class="small-button secondary lif-remove" data-remove-cfg-scheduled="${index}" aria-label="删除定时 QC 窗口 ${index + 1}">×</button>
        </div>
      `).join('');
    }

    function renderLocalDeltaPanel() {
      const visible = state.stage === 'local_calibration';
      el('localDeltaPanel').style.display = visible ? 'block' : 'none';
      if (!visible) return;
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const activeDelta = Number(tm.ms_local_delta_sec || 0);
      const displayDelta = state.previewDeltaSec === null ? activeDelta : Number(state.previewDeltaSec);
      const previewChanged = Math.abs(displayDelta - activeDelta) > 1e-9;
      const statusLabel = tm.status === 'frozen' && previewChanged
        ? '预览'
        : (tm.status === 'frozen' ? '已锁定' : '草稿');
      el('deltaReadout').textContent = `${fmt(displayDelta, 2)} sec (${escapeText(statusLabel)})`;
      el('freezeDelta').textContent = tm.status === 'frozen' ? (previewChanged ? '重新锁定时间差' : '时间差已锁定') : '锁定 MS 时间差';
      el('freezeDelta').disabled = tm.status === 'frozen' && !previewChanged;
      const align = state.current?.alignment || state.meta?.alignment || {};
      const configuredAxes = configuredPhysicalAxes();
      const fallbackShifts = configuredAxes.map(axis => {
        const shift = axis === 'green_axis' ? align.green_to_ms_shift_sec : align.red_to_ms_shift_sec;
        return [axis, shift];
      });
      const shiftEntries = Object.entries(align.axis_shifts_sec || {});
      const axisSummary = (shiftEntries.length ? shiftEntries : fallbackShifts)
        .map(([axis, shift]) => `${physicalAxisName(axis)} ${fmt(shift, 2)} sec`)
        .join('；') || '尚未估计';
      el('deltaBaseSummary').textContent = `基础时间平移：${axisSummary}；MS 后段时间差可单独调节`;
      el('deltaSlider').value = String(Math.max(-20, Math.min(20, displayDelta)));
      const p = state.localDeltaPreview;
      if (!p) {
        el('deltaStats').textContent = '未加载预览。';
        return;
      }
      const recommendationText = p.recommendation_status === 'ambiguous'
        ? '自动建议存在并列解'
        : (p.recommendation_status === 'insufficient_evidence' ? '自动建议证据不足' : '');
      el('deltaStats').textContent = [
        `证据 ${p.evidence_count || 0} 条`,
        p.complete_anchor_set_count !== undefined ? `完整参考峰组 ${p.complete_anchor_set_count || 0} 条` : '',
        `冲突 ${p.conflict_count || 0}`,
        `median |残差| ${fmt(p.median_abs_residual_sec, 3)} sec`,
        `p90 |残差| ${fmt(p.p90_abs_residual_sec, 3)} sec`,
        `取证范围 ${fmt(p.seed_start_min, 2)}-${fmt(p.seed_end_min, 2)} min`,
        recommendationText,
        previewChanged ? '尚未锁定，仅为预览' : '当前结果已保存'
      ].filter(Boolean).join('；');
    }

    function renderQcRefitPanel() {
      const visible = state.stage === 'qc_calibration';
      el('qcRefitPanel').style.display = visible ? 'block' : 'none';
      if (!visible) return;
      const calibrationReady = calibrationBoundariesConfirmed();
      el('previewQcRefit').disabled = !calibrationReady || state.actionBusy;
      if (!calibrationReady) {
        el('applyQcRefit').disabled = true;
        el('qcRefitStats').textContent = '参考段边界待确认；请先查看原始峰形，并在“配置”中确认全部边界。';
        return;
      }
      const preview = state.qcRefitPreview;
      const active = state.current?.alignment?.qc_alignment_model
        || state.meta?.alignment?.qc_alignment_model
        || state.current?.project_config?.qc_alignment_model
        || state.meta?.project_config?.qc_alignment_model;
      const button = el('applyQcRefit');
      button.disabled = !preview || state.actionBusy;
      if (preview) {
        const axes = Object.entries(preview.axes || {}).map(([axis, details]) => (
          `${physicalAxisName(axis)}：${fmt(details.previous_shift_sec, 2)} → ${fmt(details.shift_sec, 2)} sec `
          + `(${details.inlier_count || 0}/${details.evidence_count || 0} 条)`
        ));
        el('qcRefitStats').textContent = [
          ...axes,
          `使用 ${preview.used_annotation_count || 0} 条已接受人工记录`,
          `冲突 ${preview.conflict_count || 0} 条`,
          '预览尚未应用'
        ].join('；');
        return;
      }
      if (active) {
        const axes = Object.entries(active.axis_shifts_sec || {}).map(([axis, shift]) => `${physicalAxisName(axis)} ${fmt(shift, 2)} sec`);
        el('qcRefitStats').textContent = `已应用参考峰时间校正：${axes.join('；')}`;
        return;
      }
      el('qcRefitStats').textContent = '尚未生成重算预览。';
    }

    function renderStagePanels() {
      const local = state.stage === 'local_calibration';
      const qcCalibration = state.stage === 'qc_calibration';
      const eventAnnotation = state.stage === 'event_annotation';
      let cellMode = eventAnnotation && state.manualAnnotationKind === 'cell';
      const calibrationReady = calibrationBoundariesConfirmed();
      const frozen = (state.current?.time_model || state.meta?.time_model || {}).status === 'frozen';
      const postQcMode = String(
        state.current?.project_config?.post_qc_strategy?.mode
        || state.meta?.project_config?.post_qc_strategy?.mode
        || 'disabled'
      );
      const postQcEnabled = postQcMode !== 'disabled';
      if (eventAnnotation && !postQcEnabled && state.eventFilter === 'qc') state.eventFilter = 'all';
      if (eventAnnotation && !postQcEnabled && state.manualAnnotationKind === 'qc') {
        state.manualAnnotationKind = 'cell';
        cellMode = true;
      }
      el('qcRefitPanel').style.display = qcCalibration ? 'block' : 'none';
      el('baseTimePanel').style.display = local ? 'none' : 'block';
      el('reviewPanel').style.display = (!calibrationReady || local || (eventAnnotation && !frozen)) ? 'none' : 'block';
      el('manualPanel').style.display = calibrationReady && (qcCalibration || (eventAnnotation && frozen)) ? 'block' : 'none';
      if (calibrationReady && (qcCalibration || (eventAnnotation && frozen))) {
        el('reviewPanel').parentNode.insertBefore(el('manualPanel'), el('reviewPanel'));
      }
      document.querySelectorAll('.stage-tab').forEach(button => {
        button.disabled = !calibrationReady && button.dataset.stage !== 'qc_calibration';
        button.title = button.disabled ? '请先在配置中确认全部前段参考边界' : '';
      });
      el('eventFilter').style.display = eventAnnotation ? 'grid' : 'none';
      el('manualAnnotationKind').style.display = eventAnnotation ? 'grid' : 'none';
      el('manualLifRow').style.display = cellMode ? 'block' : 'none';
      el('manualPanelTitle').textContent = eventAnnotation ? '手动事件关系' : '手动参考峰关系';
      el('createManual').textContent = cellMode ? 'Save pair' : 'Save anchor';
      el('acceptWindow').style.display = eventAnnotation ? 'none' : 'block';
      document.querySelectorAll('[data-event-filter]').forEach(button => {
        button.classList.toggle('active', button.dataset.eventFilter === state.eventFilter);
        if (button.dataset.eventFilter === 'qc') {
          button.disabled = !postQcEnabled;
          button.title = postQcEnabled ? postQcModeLabel(postQcMode) : '本项目不进行后段巡检';
        }
      });
      document.querySelectorAll('[data-manual-kind]').forEach(button => {
        button.classList.toggle('active', button.dataset.manualKind === state.manualAnnotationKind);
        if (button.dataset.manualKind === 'qc') {
          button.disabled = !postQcEnabled;
          button.title = postQcEnabled ? postQcModeLabel(postQcMode) : '本项目不进行后段巡检';
        }
      });
      if (eventAnnotation) {
        el('reviewHelp').textContent = postQcEnabled
          ? `${postQcModeLabel(postQcMode)}；质控与细胞候选都只使用事件坐标表中的事件，均需逐条确认；同一 MS 事件只能接受一个跨通道关系。`
          : '本项目不进行后段巡检；当前只显示细胞候选。同一 MS 事件出现跨通道冲突时，必须人工选择唯一关系。';
      } else {
        const anchors = qcAnchorChannels();
        el('reviewHelp').textContent = `残差 = MS760 时间 - ${anchors.join('/')} 按时间轴聚合后的组合时间，单位 sec；越接近 0 表示时间对齐越好。`;
      }
      const anchors = qcAnchorChannels();
      el('manualHelp').textContent = eventAnnotation
        ? (cellMode
            ? 'Cell pair：选 1 个 LIF 峰 + 1 个事件坐标 CSV 内的 MS760；灰色 MS 仅供查看。'
            : `质控参考关系：必须选择 MS760；并从 ${anchors.join('/')} 中至少选择一个 LIF 峰，缺失通道会明确记为空值。`)
        : `前段时间校正：选择 ${anchors.join('/')} 中能够覆盖全部信号时间轴的峰，以及对应的 MS760 峰。`;
    }

    function setConfigSaveStatus(message, type = '') {
      const status = el('configSaveStatus');
      status.textContent = message;
      status.className = `config-save-status${type ? ` ${type}` : ''}`;
    }

    function updateAttachMapControls() {
      const input = el('attachCellEventMap');
      const value = input.value.trim();
      const filename = value.split(/[\\/]/).filter(Boolean).pop() || '';
      input.title = value;
      el('attachMap').disabled = Boolean(state.actionBusy) || !value;
      el('attachMapReady').textContent = value
        ? `已选择：${filename}`
        : '尚未选择文件';
    }

    async function saveProjectConfig() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      state.configSaveBusy = true;
      const button = el('saveConfig');
      const closeButton = el('closeConfigProject');
      const oldText = button.textContent;
      button.disabled = true;
      closeButton.disabled = true;
      button.textContent = '保存中...';
      setConfigSaveStatus('正在保存项目时间节点...');
      try {
        const calibrationWasReady = calibrationBoundariesConfirmed();
        const currentCfg = state.current?.project_config || state.meta?.project_config || {};
        const payload = {
          qc_calibration_end_min: Number(el('cfgQcEnd').value),
          annotation_start_min: Number(el('cfgAnnotationStart').value),
          local_delta_seed_window_min: Number(el('cfgSeedWindow').value),
          post_qc_strategy: JSON.parse(JSON.stringify(state.configPostQcDraft || { mode: 'disabled' }))
        };
        if (state.configProtocolDraft && !state.configProtocolDraft.compatibility_mode) {
          payload.calibration_protocol = JSON.parse(JSON.stringify(state.configProtocolDraft));
          payload.calibration_protocol.segments = payload.calibration_protocol.segments.map((segment, index) => ({
            ...segment,
            order: index + 1,
            start_min: Number(segment.start_min),
            end_min: Number(segment.end_min),
            boundaries_confirmed: Boolean(segment.boundaries_confirmed)
          }));
          payload.qc_calibration_end_min = Math.max(...payload.calibration_protocol.segments.map(segment => segment.end_min));
        }
        const currentPostQcComparable = JSON.parse(JSON.stringify(currentCfg.post_qc_strategy || { mode: 'disabled' }));
        const proposedPostQcComparable = JSON.parse(JSON.stringify(payload.post_qc_strategy));
        delete currentPostQcComparable.compatibility_mode;
        delete proposedPostQcComparable.compatibility_mode;
        const postQcStrategyChanged = JSON.stringify(currentPostQcComparable) !== JSON.stringify(proposedPostQcComparable);
        if (postQcStrategyChanged) {
          delete payload.post_qc_strategy.compatibility_mode;
          const postQcOk = window.confirm(
            '修改后段 QC 策略后，已有人工后段 QC 标注会完整保留为历史记录，但对当前策略失效；当前候选和主 CSV 将按新策略重算。是否继续？'
          );
          if (!postQcOk) {
            setConfigSaveStatus('未保存，后段 QC 策略保持不变。');
            return;
          }
        }
        const tm = state.current?.time_model || state.meta?.time_model || {};
        const qcEndChanged = Math.abs(
          Number(currentCfg.qc_calibration_end_min ?? 0) - Number(payload.qc_calibration_end_min ?? 0)
        ) > 1e-9;
        const clearsQcAlignment = qcEndChanged && Boolean(currentCfg.qc_alignment_model);
        const protocolChanged = Boolean(payload.calibration_protocol)
          && JSON.stringify(payload.calibration_protocol) !== JSON.stringify(currentCfg.calibration_protocol);
        const clearsProtocolAlignment = protocolChanged && Boolean(currentCfg.qc_alignment_model);
        const changedFrozenConfig = tm.status === 'frozen' && [
          'qc_calibration_end_min',
          'annotation_start_min',
          'local_delta_seed_window_min'
        ].some(key => Math.abs(Number(currentCfg[key] ?? 0) - Number(payload[key] ?? 0)) > 1e-9)
          || (tm.status === 'frozen' && protocolChanged);
        if (changedFrozenConfig || clearsQcAlignment || clearsProtocolAlignment) {
          const effects = [];
          if (clearsQcAlignment || clearsProtocolAlignment) effects.push('已应用的前段参考峰时间校正');
          if (changedFrozenConfig) effects.push('当前已锁定的后段时间模型');
          const ok = window.confirm(`修改这些项目级校准参数会使${effects.join('和')}失效。已有人工标注会保留为历史记录，但 MS 后段时间差与事件候选必须重算。是否继续？`);
          if (!ok) {
            setConfigSaveStatus('未保存，项目时间节点保持不变。');
            return;
          }
          if (changedFrozenConfig) payload.clear_frozen_time_model = true;
          if (clearsQcAlignment || clearsProtocolAlignment) payload.clear_qc_alignment_model = true;
        }
        const result = await postJson('/api/project-config', payload);
        state.meta.project_config = result.project_config;
        state.meta.time_model = result.time_model;
        if (state.current) {
          state.current.project_config = result.project_config;
          state.current.time_model = result.time_model;
        }
        state.configProtocolDraft = result.project_config.calibration_protocol
          ? JSON.parse(JSON.stringify(result.project_config.calibration_protocol))
          : null;
        state.configPostQcDraft = JSON.parse(JSON.stringify(result.project_config.post_qc_strategy || { mode: 'disabled' }));
        if (qcEndChanged) state.qcRefitPreview = null;
        const cfg = result.project_config;
        const calibrationBecameReady = !calibrationWasReady && calibrationBoundariesConfirmed();
        const annotationStart = Number(cfg.annotation_start_min || state.start);
        const qcEnd = Number(cfg.qc_calibration_end_min || 10.5);
        if (!calibrationBoundariesConfirmed()) {
          state.stage = 'qc_calibration';
          state.timeMode = 'raw';
          state.previewDeltaSec = null;
          state.localDeltaPreview = null;
          state.qcRefitPreview = null;
          const firstSegment = cfg.calibration_protocol?.segments?.[0];
          state.start = Math.max(
            Number(state.meta?.time_min_min || 0),
            Number(firstSegment?.start_min || 0)
          );
          el('timeMode').value = 'raw';
        } else if (calibrationBecameReady) {
          const firstSegment = cfg.calibration_protocol?.segments?.[0];
          state.stage = 'qc_calibration';
          state.timeMode = 'aligned';
          state.start = Math.max(
            Number(state.meta?.time_min_min || 0),
            Number(firstSegment?.start_min || 0)
          );
          el('timeMode').value = 'aligned';
        } else if (state.stage === 'local_calibration') {
          state.start = annotationStart;
        } else if (state.stage === 'event_annotation' && Number(state.start) < annotationStart) {
          state.start = annotationStart;
        } else if (state.stage === 'qc_calibration' && Number(state.start) > qcEnd) {
          state.start = 0;
        }
        applyStageWindowWidth();
        const calibrationReadyMessage = calibrationBecameReady
          ? ' QC anchor 候选已生成；关闭配置后即可审核。'
          : '';
        const savedMessage = `已保存：前段参考结束 ${String(Number(cfg.qc_calibration_end_min))} min；事件起点 ${String(Number(cfg.annotation_start_min))} min；自动估计时间差范围 ${String(Number(cfg.local_delta_seed_window_min))} min；${postQcModeLabel(cfg.post_qc_strategy?.mode || 'disabled')}。${calibrationReadyMessage}`;
        setConfigSaveStatus(
          result.warning ? `${savedMessage} ${result.warning}` : savedMessage,
          result.warning ? 'warning' : 'success'
        );
        try {
          await loadWindow();
        } catch (refreshErr) {
          const syncWarning = result.warning ? `${result.warning} ` : '';
          setConfigSaveStatus(`${savedMessage} ${syncWarning}图窗刷新失败：${refreshErr.message}`, 'warning');
          console.error(refreshErr);
        }
      } catch (err) {
        setConfigSaveStatus(`保存失败：${err.message}`, 'error');
        alert(`保存项目时间节点失败: ${err.message}`);
      } finally {
        button.disabled = false;
        closeButton.disabled = false;
        button.textContent = oldText;
        state.configSaveBusy = false;
        state.actionBusy = false;
      }
    }

    async function previewQcAlignmentRefit() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      renderQcRefitPanel();
      try {
        const result = await postJson('/api/qc-alignment-refit-preview', {});
        state.qcRefitPreview = result.preview;
        renderQcRefitPanel();
      } catch (err) {
        state.qcRefitPreview = null;
        renderQcRefitPanel();
        alert(`参考峰时间校正预览失败：${err.message}`);
      } finally {
        state.actionBusy = false;
        renderQcRefitPanel();
      }
    }

    async function applyQcAlignmentRefit() {
      if (state.actionBusy || !state.qcRefitPreview) return;
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const frozen = tm.status === 'frozen';
      const consequence = frozen
        ? '应用后会清除当前已锁定的后段时间模型，并要求重新进行后段时间差校正。'
        : '应用后会重置 MS 后段时间差，并创建新的时间模型草稿。';
      if (!confirm(`应用当前已接受参考峰得到的基础时间校正？${consequence}`)) return;
      state.actionBusy = true;
      renderQcRefitPanel();
      try {
        const result = await postJson('/api/qc-alignment-refit', {
          preview_hash: state.qcRefitPreview.preview_hash,
          clear_frozen_time_model: frozen
        });
        state.meta.alignment = result.alignment;
        state.meta.project_config = result.project_config;
        state.meta.time_model = result.time_model;
        state.qcRefitPreview = null;
        await loadWindow();
        alert(result.warning ? `参考峰时间校正已应用。${result.warning}` : '参考峰时间校正已应用；请重新进行后段时间差校正。');
      } catch (err) {
        alert(`应用参考峰时间校正失败：${err.message}`);
      } finally {
        state.actionBusy = false;
        renderQcRefitPanel();
      }
    }

    async function loadLocalDeltaPreview(deltaSec = null) {
      const suffix = deltaSec === null ? '' : `?delta_sec=${encodeURIComponent(deltaSec)}`;
      state.localDeltaPreview = await fetchJson(`/api/local-delta-preview${suffix}`);
      renderLocalDeltaPanel();
    }

    async function estimateLocalDelta() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      try {
        const tm = state.current?.time_model || state.meta?.time_model || {};
        if (tm.status === 'frozen') {
          const result = await postJson('/api/estimate-local-delta-preview', {});
          const recommendationStatus = String(result.preview.recommendation_status || 'recommended');
          if (recommendationStatus !== 'recommended') {
            state.localDeltaPreview = result.preview;
            renderLocalDeltaPanel();
            alert(recommendationStatus === 'ambiguous'
              ? '自动估计存在多个近似最优平移，未改变当前预览；请结合轨道图使用滑块人工确认。'
              : '自动估计的多通道 QC 证据不足，未改变当前预览；请扩大后段预校准取证范围或使用滑块人工确认。');
            return;
          }
          state.previewDeltaSec = Number(result.preview.delta_sec);
          state.localDeltaPreview = result.preview;
          await loadWindow();
          return;
        }
        const result = await postJson('/api/estimate-local-delta', {});
        state.meta.time_model = result.time_model;
        state.localDeltaPreview = result.preview;
        state.previewDeltaSec = null;
        await loadWindow();
      } catch (err) {
        alert(`自动估计失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    async function updateDeltaPreview(deltaSec) {
      if (state.actionBusy) return;
      state.actionBusy = true;
      try {
        state.previewDeltaSec = Number(deltaSec);
        await loadWindow();
      } catch (err) {
        alert(`预览 MS 时间差失败：${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    async function freezeLocalDelta() {
      if (state.actionBusy) return;
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const activeDelta = Number(tm.ms_local_delta_sec || 0);
      const desiredDelta = state.previewDeltaSec === null ? activeDelta : Number(state.previewDeltaSec);
      const previewChanged = Math.abs(desiredDelta - activeDelta) > 1e-9;
      if (tm.status === 'frozen' && !previewChanged) return;
      const actionText = tm.status === 'frozen' ? '重新锁定当前预览时间差' : '锁定当前 MS 后段时间差';
      if (!confirm(`${actionText}？锁定后才会解锁后段质控巡检和细胞标注候选。`)) return;
      state.actionBusy = true;
      try {
        if (previewChanged) {
          await postJson('/api/local-delta-draft', { delta_sec: desiredDelta });
        }
        const result = await postJson('/api/freeze-local-delta', {});
        state.meta.time_model = result.time_model;
        state.localDeltaPreview = result.preview;
        state.previewDeltaSec = null;
        await loadWindow();
      } catch (err) {
        alert(`锁定 MS 时间差失败：${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    function statusText(status) {
      if (status === 'accepted') return '已接受';
      if (status === 'rejected') return '已拒绝';
      if (status === 'preview') return '预览';
      return '待审';
    }

    function sourceText(source) {
      if (source === 'preview') return '预览';
      return source === 'manual_created' ? '人工' : '自动';
    }

    function rowId(row) {
      return row.annotation_id || row.candidate_id || '';
    }

    function rowSummary(row) {
      const isCell = String(row.candidate_type || '').startsWith('cell') || row.candidate_type === 'manual_cell_pair';
      if (isCell) return `${channelDisplayLabel(row.lif_channel)} ${fmt(row.lif_raw_time_min, 3)} → MS760 ${fmt(row.ms_time_min, 3)}`;
      return `${qcAnchorTimeText(row)} min`;
    }

    function contextActions(row) {
      if (state.stage === 'qc_calibration' && !calibrationBoundariesConfirmed()) return [];
      if (!row || row.review_enabled === false || row.source === 'preview') return [];
      const actions = [
        { action: 'accepted', label: '接受' },
        { action: 'rejected', label: '拒绝' }
      ];
      if (row.source === 'auto_candidate') actions.push({ action: 'pending', label: '待审' });
      if (row.source === 'manual_created') actions.push({ action: 'clear_manual', label: '清除' });
      return actions;
    }

    function hideLineContextMenu() {
      const menu = el('lineContextMenu');
      if (menu) menu.style.display = 'none';
    }

    function showLineContextMenu(row, ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (state.manualMode) return;
      if (state.actionBusy) return;
      const id = rowId(row);
      const actions = contextActions(row);
      if (!id || !actions.length) return;
      hideTooltip();
      state.selectedCandidateId = id;
      renderCandidateList();
      draw();
      const menu = el('lineContextMenu');
      menu.innerHTML = `
        <div class="context-menu-title">${escapeText(sourceText(row.source))} · ${escapeText(statusText(row.review_status))}<br>${escapeText(rowSummary(row))}</div>
        ${actions.map(item => `<button data-action="${escapeText(item.action)}">${escapeText(item.label)}</button>`).join('')}
      `;
      menu.querySelectorAll('button[data-action]').forEach(button => {
        button.addEventListener('click', async (clickEv) => {
          clickEv.stopPropagation();
          const action = button.dataset.action;
          hideLineContextMenu();
          if (action === 'clear_manual') {
            await clearManualAnnotation(id);
          } else {
            await reviewCandidate(id, action);
          }
        });
      });
      menu.style.display = 'block';
      menu.style.left = '0px';
      menu.style.top = '0px';
      const rect = menu.getBoundingClientRect();
      const left = Math.max(8, Math.min(ev.clientX, window.innerWidth - rect.width - 8));
      const top = Math.max(8, Math.min(ev.clientY, window.innerHeight - rect.height - 8));
      menu.style.left = `${left}px`;
      menu.style.top = `${top}px`;
    }

    function attachLineContextMenu(line, row) {
      line.addEventListener('contextmenu', (ev) => showLineContextMenu(row, ev));
    }

    function attachLineInteractions(line, row, onSelect) {
      attachHover(line);
      line.addEventListener('click', () => {
        if (state.manualMode) return;
        onSelect();
      });
      attachLineContextMenu(line, row);
    }

    function appendLineWithHitTarget(svg, line, row, onSelect) {
      attachLineInteractions(line, row, onSelect);
      svg.appendChild(line);
      const hit = line.cloneNode(false);
      hit.setAttribute('stroke', 'rgba(0,0,0,0.001)');
      hit.setAttribute('stroke-width', '14');
      hit.setAttribute('stroke-dasharray', '');
      hit.setAttribute('opacity', '0');
      hit.setAttribute('pointer-events', 'stroke');
      hit.setAttribute('cursor', 'pointer');
      hit.__detail = line.__detail;
      attachLineInteractions(hit, row, onSelect);
      svg.appendChild(hit);
    }

    function renderCandidateList() {
      const box = el('candidateList');
      const rows = candidateRows();
      const conflictGroups = state.showCrossChannelConflicts
        ? pendingCrossChannelConflictGroups()
        : [];
      renderCrossChannelConflictControl();
      if (!rows.length && !conflictGroups.length) {
        box.innerHTML = '<div class="empty">当前窗口没有可显示候选。</div>';
        return;
      }
      const regularRowsHtml = rows.map(row => {
        const id = row.annotation_id || row.candidate_id;
        const selected = id === state.selectedCandidateId ? ' selected' : '';
        const rejected = row.review_status === 'rejected' ? ' rejected' : '';
        const isCell = String(row.candidate_type || '').startsWith('cell') || row.candidate_type === 'manual_cell_pair';
        const times = isCell
          ? `${channelDisplayLabel(row.lif_channel)} ${fmt(row.lif_raw_time_min, 3)} → MS760 ${fmt(row.ms_time_min, 3)}`
          : qcAnchorTimeText(row);
        const deviation = decisionDeviationSec(row);
        const canReview = row.review_enabled !== false && row.source !== 'preview';
        const displayStatus = row.review_enabled === false && row.review_status === 'pending' ? 'preview' : row.review_status;
        const reviewNote = candidateNeedsIndividualReview(row) ? ' · 需逐条审核' : '';
        const actions = canReview ? `
              <button data-action="accepted" data-id="${escapeText(id)}">接受</button>
              <button data-action="rejected" data-id="${escapeText(id)}">拒绝</button>
              ${row.source === 'auto_candidate' ? `<button data-action="pending" data-id="${escapeText(id)}">待审</button>` : ''}
              ${row.source === 'manual_created' ? `<button data-action="clear_manual" data-id="${escapeText(id)}">清除</button>` : ''}
        ` : '<span class="empty">仅供预览</span>';
        return `
          <div class="candidate-row${selected}${rejected}" data-candidate-id="${escapeText(id)}">
            <div class="row-title"><span>${escapeText(sourceText(row.source))} #${escapeText(row.rank ?? '')}</span><span>${escapeText(statusText(displayStatus))}</span></div>
            <div class="row-sub">${escapeText(times)} min<br>${deviation === null ? '偏差 -' : `偏差 ${escapeText(fmt(deviation, 3))} sec`}${escapeText(reviewNote)}</div>
            <div class="row-actions">${actions}</div>
          </div>
        `;
      }).join('');
      const conflictRowsHtml = conflictGroups.map(group => {
        const selected = group.rows.some(row => rowId(row) === state.selectedCandidateId) ? ' selected' : '';
        const first = group.rows[0] || {};
        const alternatives = group.rows.map(row => (
          `${channelDisplayLabel(row.lif_channel)} Δ${fmt(decisionDeviationSec(row), 3)}s`
        )).join(' · ');
        const actions = group.rows.map(row => `
          <button data-conflict-candidate-id="${escapeText(rowId(row))}">Use ${escapeText(row.lif_channel)}</button>
        `).join('');
        return `
          <div class="candidate-row conflict-group${selected}" data-conflict-event-id="${escapeText(group.ms_event_id)}">
            <div class="row-title"><span>Ambiguous event</span><span>Review only</span></div>
            <div class="row-sub">MS760 ${escapeText(fmt(first.ms_time_min, 3))} min<br>${escapeText(alternatives)}</div>
            <div class="row-actions">${actions}<button data-hide-conflicts="true">Hide</button></div>
          </div>
        `;
      }).join('');
      box.innerHTML = regularRowsHtml + conflictRowsHtml;
      box.querySelectorAll('.candidate-row').forEach(node => {
        node.addEventListener('click', (ev) => {
          if (ev.target && ev.target.dataset && ev.target.dataset.action) return;
          if (!node.dataset.candidateId) return;
          state.selectedCandidateId = node.dataset.candidateId;
          renderCandidateList();
          draw();
        });
      });
      box.querySelectorAll('button[data-action]').forEach(button => {
        button.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          if (button.dataset.action === 'clear_manual') {
            await clearManualAnnotation(button.dataset.id);
          } else {
            await reviewCandidate(button.dataset.id, button.dataset.action);
          }
        });
      });
      box.querySelectorAll('button[data-conflict-candidate-id]').forEach(button => {
        button.addEventListener('click', async (ev) => {
          ev.stopPropagation();
          await reviewCandidate(button.dataset.conflictCandidateId, 'accepted');
        });
      });
      box.querySelectorAll('button[data-hide-conflicts]').forEach(button => {
        button.addEventListener('click', (ev) => {
          ev.stopPropagation();
          state.showCrossChannelConflicts = false;
          el('showCrossChannelConflicts').checked = false;
          state.selectedCandidateId = null;
          renderCurrentState();
        });
      });
    }

    function confirmQcEvidenceInvalidation(previousReviewStatus, newReviewStatus) {
      if (state.stage !== 'qc_calibration') return {};
      if ((previousReviewStatus === 'accepted') === (newReviewStatus === 'accepted')) return {};
      const active = state.current?.alignment?.qc_alignment_model
        || state.meta?.alignment?.qc_alignment_model
        || state.current?.project_config?.qc_alignment_model
        || state.meta?.project_config?.qc_alignment_model;
      if (!active) return {};
      const ok = confirm(
        '修改前段参考峰记录会清除已应用的时间校正和后续时间模型。'
        + '完成修改后必须重新预览并应用前段时间校正，再进行后段时间差校正。是否继续？'
      );
      return ok ? { clear_qc_alignment_model: true } : null;
    }

    async function reviewCandidate(annotationId, reviewStatus) {
      if (state.actionBusy) return;
      const currentRow = candidateRows().find(row => rowId(row) === annotationId);
      const invalidation = confirmQcEvidenceInvalidation(currentRow?.review_status || 'pending', reviewStatus);
      if (invalidation === null) return;
      state.actionBusy = true;
      showInteractionHint(
        reviewStatus === 'accepted' ? 'Accepting…'
          : reviewStatus === 'rejected' ? 'Rejecting…'
            : 'Updating…'
      );
      try {
        await postJson('/api/review', {
          annotation_id: annotationId,
          review_status: reviewStatus,
          window_start_min: state.current.start_min,
          window_end_min: state.current.end_min,
          time_mode: state.current.time_mode,
          ...invalidation
        });
        state.qcRefitPreview = null;
        await loadWindow();
        showInteractionHint(
          reviewStatus === 'accepted' ? 'Accepted'
            : reviewStatus === 'rejected' ? 'Rejected'
              : 'Updated'
        );
        notifyStateChannel();
      } catch (err) {
        alert(`审核写入失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    async function clearManualAnnotation(annotationId) {
      if (state.actionBusy) return;
      if (!confirm('清除这条人工误选记录？这会删除当前记录和它的 audit 事件。')) return;
      const currentRow = candidateRows().find(row => rowId(row) === annotationId);
      const invalidation = confirmQcEvidenceInvalidation(currentRow?.review_status || 'pending', null);
      if (invalidation === null) return;
      state.actionBusy = true;
      try {
        await postJson('/api/clear-manual', { annotation_id: annotationId, ...invalidation });
        if (state.selectedCandidateId === annotationId) state.selectedCandidateId = null;
        state.qcRefitPreview = null;
        await loadWindow();
        notifyStateChannel();
      } catch (err) {
        alert(`清除失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    async function exportAcceptedCsv() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      const button = el('exportAcceptedCsv');
      const hint = el('exportHint');
      const oldText = button.textContent;
      button.textContent = '导出中...';
        hint.textContent = '导出中...';
      try {
        const result = await postCsv('/api/export-accepted-csv', {});
        downloadTextFile(result.filename, result.text, 'text/csv;charset=utf-8');
        hint.textContent = '主 CSV 已导出；未标注事件为 unknown，前段 QC anchor 留在审计库';
      } catch (err) {
        hint.textContent = '导出失败';
        alert(`导出失败: ${err.message}`);
      } finally {
        button.textContent = oldText;
        state.actionBusy = false;
      }
    }

    async function importProject() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      state.importCreating = true;
      const button = el('runImportProject');
      const closeButton = el('closeImportProject');
      const startedAt = Date.now();
      let creationKind = '项目';
      let progressTimer = null;
      const updateCreationFeedback = () => {
        const elapsedSec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
        button.textContent = `正在创建… ${elapsedSec}s`;
        el('importHint').textContent = `正在创建${creationKind}：读取原始文件并生成中间表。已等待 ${elapsedSec} 秒，请勿重复点击或关闭软件。`;
      };
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      closeButton.disabled = true;
      updateCreationFeedback();
      progressTimer = window.setInterval(updateCreationFeedback, 1000);
      try {
        await new Promise(resolve => window.requestAnimationFrame(() => resolve()));
        refreshImportProtocolOptions();
        const lifInputs = importLifRows();
        const channels = lifInputs.map(row => row.channel);
        if (lifInputs.length < 2 || lifInputs.length > 4) {
          throw new Error('LIF 输入必须为 2–4 个。');
        }
        if (lifInputs.some(row => !row.path || !row.channel || !row.detector || !row.time_axis)) {
          throw new Error('请为每个 LIF 输入选择文件，并填写通道和信号颜色。');
        }
        if (new Set(channels).size !== channels.length) {
          throw new Error('LIF 通道名不能重复。');
        }
        const duplicatePathMessage = duplicateImportLifPathMessage();
        if (duplicatePathMessage) {
          throw new Error(duplicatePathMessage);
        }
        const cellRows = lifInputs.filter(row => row.use_for_cell_annotation);
        if (!cellRows.length) {
          throw new Error('至少一个 LIF 通道必须用于细胞标注。');
        }
        const missingCellLabels = cellRows.filter(row => !row.identity_prior).map(row => row.channel);
        if (missingCellLabels.length) {
          throw new Error(`用于细胞标注的通道必须填写样本标签：${missingCellLabels.join('、')}。`);
        }
        const calibrationProtocol = calibrationProtocolPayload();
        if (!calibrationProtocol.segments.length) throw new Error('至少配置一个前段校准参考段。');
        let previousEnd = null;
        calibrationProtocol.segments.forEach((segment, index) => {
          if (!Number.isFinite(segment.start_min) || !Number.isFinite(segment.end_min) || segment.end_min <= segment.start_min) {
            throw new Error(`参考段 #${index + 1} 必须填写有效的开始和结束时间。`);
          }
          if (previousEnd !== null && segment.start_min < previousEnd) {
            throw new Error(`参考段 #${index + 1} 与前一段重叠或顺序错误。`);
          }
          if (!segment.reference_channels.length) throw new Error(`参考段 #${index + 1} 至少选择一个参考通道。`);
          previousEnd = segment.end_min;
        });
        const axesByChannel = Object.fromEntries(lifInputs.map(row => [row.channel, row.time_axis]));
        const requiredAxes = new Set(cellRows.map(row => row.time_axis));
        const coveredAxes = new Set(calibrationProtocol.segments.flatMap(segment => segment.reference_channels.map(channel => axesByChannel[channel])));
        const missingAxes = Array.from(requiredAxes).filter(axis => !coveredAxes.has(axis));
        if (missingAxes.length) throw new Error('前段参考段尚未覆盖所有用于细胞标注的信号颜色。');
        const annotationStart = Number(el('importAnnotationStart').value);
        const seedWindow = Number(el('importSeedWindow').value);
        if (!Number.isFinite(annotationStart) || annotationStart < Number(previousEnd)) {
          throw new Error('事件标注起点必须不早于最后一个校准参考段的结束时间。');
        }
        if (!Number.isFinite(seedWindow) || seedWindow <= 0) throw new Error('自动估计 MS 时间差的范围必须大于 0。');
        const postQcStrategy = postQcStrategyPayload();
        if (!el('importProjectDir').value.trim() || !el('importMs').value.trim() || !el('importCellEventMap').value.trim()) {
          throw new Error('请选择项目保存路径、MS 原始文件和事件坐标 CSV。');
        }
        const unconfirmedCount = calibrationProtocol.segments.filter(segment => !segment.boundaries_confirmed).length;
        if (unconfirmedCount) {
          creationKind = `边界待确认草稿（${unconfirmedCount} 段）`;
          updateCreationFeedback();
        }
        const result = await postJson('/api/import-project', {
          project_dir: el('importProjectDir').value,
          ms_path: el('importMs').value,
          raw_input_mode: selectedRawInputMode(),
          lif_inputs: lifInputs,
          calibration_protocol: calibrationProtocol,
          annotation_start_min: annotationStart,
          local_delta_seed_window_min: seedWindow,
          post_qc_strategy: postQcStrategy,
          cell_event_map_path: el('importCellEventMap').value,
        });
        applyLoadedProjectMeta(result.meta);
        await loadWindow();
        if (progressTimer !== null) {
          window.clearInterval(progressTimer);
          progressTimer = null;
        }
        state.importCreating = false;
        setImportModal(false);
      } catch (err) {
        el('importHint').textContent = `导入失败: ${err.message}`;
        alert(`导入失败: ${err.message}`);
      } finally {
        if (progressTimer !== null) window.clearInterval(progressTimer);
        state.importCreating = false;
        button.removeAttribute('aria-busy');
        button.disabled = false;
        closeButton.disabled = false;
        state.actionBusy = false;
        renderImportSegments();
      }
    }

    async function openExistingProject() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      const button = el('runOpenProject');
      const oldText = button.textContent;
      button.textContent = '打开中...';
      el('openProjectHint').textContent = '正在读取项目数据与人工标注，请稍候。';
      try {
        const result = await postJson('/api/open-project', {
          project_dir: el('openProjectDir').value
        });
        applyLoadedProjectMeta(result.meta);
        await loadWindow();
        setOpenProjectModal(false);
      } catch (err) {
        el('openProjectHint').textContent = `打开失败: ${err.message}`;
        alert(`打开失败: ${err.message}`);
      } finally {
        button.textContent = oldText;
        state.actionBusy = false;
      }
    }

    async function attachCellEventMap() {
      if (state.actionBusy) return;
      const sourcePath = el('attachCellEventMap').value.trim();
      if (!sourcePath) {
        setConfigSaveStatus('请选择事件坐标 CSV。', 'error');
        return;
      }
      if (!window.confirm('软件会读取必需坐标列并一次性绑定到当前项目；绑定后不能直接替换。继续？')) return;
      state.actionBusy = true;
      const button = el('attachMap');
      const oldText = button.textContent;
      button.disabled = true;
      button.textContent = '校验中…';
      setConfigSaveStatus('正在校验 CSV 坐标与当前 MS 事件的一对一关系…');
      try {
        const result = await postJson('/api/attach-cell-event-map', { source_path: sourcePath });
        state.meta = result.meta;
        state.current = null;
        syncUmapButtonState();
        el('attachMapPanel').style.display = 'none';
        await loadWindow();
        notifyStateChannel('map-attached');
        const rowCount = Number(result.meta?.cell_event_map?.row_count || 0);
        setConfigSaveStatus(
          `已成功附加 ${rowCount.toLocaleString()} 个事件坐标点，UMAP 已启用。`,
          'success'
        );
      } catch (err) {
        setConfigSaveStatus(`附加失败：${err.message}`, 'error');
        alert(`附加事件坐标失败: ${err.message}`);
      } finally {
        button.textContent = oldText;
        state.actionBusy = false;
        updateAttachMapControls();
      }
    }

    function pickerInitialDir(targetId) {
      const currentValue = el(targetId).value.trim();
      if (currentValue) {
        const normalized = currentValue.replaceAll('\\\\', '/');
        const slash = normalized.lastIndexOf('/');
        return slash > 0 ? currentValue.slice(0, slash) : currentValue;
      }
      if (targetId === 'importProjectDir') return state.meta?.project?.project_dir || '';
      if (targetId === 'openProjectDir') return state.meta?.project?.project_dir || '';
      return state.meta?.project?.raw_data_dir || state.meta?.project?.project_dir || '';
    }

    async function selectImportPath(button) {
      if (state.actionBusy) return;
      const targetId = button.dataset.pickerTarget;
      const target = el(targetId);
      const attachPicker = targetId === 'attachCellEventMap';
      const oldText = button.textContent;
      button.disabled = true;
      button.textContent = '选择中';
      if (attachPicker) {
        setConfigSaveStatus('请在弹出的系统窗口中选择事件坐标 CSV。');
      } else {
        el('importHint').textContent = '请在弹出的系统窗口中选择路径。';
      }
      try {
        const result = await postJson('/api/select-path', {
          kind: button.dataset.pickerKind,
          title: button.dataset.pickerTitle || '选择路径',
          file_role: button.dataset.pickerRole || '',
          initial_dir: pickerInitialDir(targetId)
        });
        if (!result.cancelled && result.path) {
          target.value = result.path;
          target.dispatchEvent(new Event('input', { bubbles: true }));
          target.focus();
          if (attachPicker) {
            const filename = result.path.split(/[\\/]/).filter(Boolean).pop() || result.path;
            setConfigSaveStatus(
              `已选择 ${filename}。点击“校验并附加到当前项目”后才会写入项目。`
            );
          } else {
            el('importHint').textContent = duplicateImportLifPathMessage();
          }
        } else {
          if (attachPicker) {
            setConfigSaveStatus('已取消选择；项目未发生变化。');
          } else {
            el('importHint').textContent = '已取消选择；原路径保持不变。';
          }
        }
      } catch (err) {
        if (attachPicker) {
          setConfigSaveStatus(`选择路径失败：${err.message}`, 'error');
        } else {
          el('importHint').textContent = `选择路径失败: ${err.message}`;
        }
        alert(`选择路径失败: ${err.message}`);
      } finally {
        button.textContent = oldText;
        button.disabled = false;
        if (attachPicker) updateAttachMapControls();
      }
    }

    async function acceptWindowPendingAutoCandidates() {
      const n = batchAcceptableAutoCandidatesInMainWindow().length;
      if (state.actionBusy || n === 0) return;
      if (!confirm(`接受当前主窗口内 ${n} 条唯一匹配候选？已接受、已拒绝和需逐条审核的候选不会被修改。`)) return;
      const invalidation = confirmQcEvidenceInvalidation('pending', 'accepted');
      if (invalidation === null) return;
      state.actionBusy = true;
      try {
        const response = await postJson('/api/accept-window', {
          start_min: state.current.start_min,
          window_min: state.current.window_min,
          time_mode: state.current.time_mode,
          stage: state.stage,
          ...invalidation
        });
        state.qcRefitPreview = null;
        await loadWindow();
        notifyStateChannel();
        const accepted = Number(response.result?.accepted_count || 0);
        const skipped = Number(response.result?.skipped_count || 0);
        if (accepted !== n || skipped > 0) {
          alert(`批量审核结果：接受 ${accepted} 条，跳过 ${skipped} 条。窗口状态可能已发生变化，请按当前列表继续审核。`);
        }
      } catch (err) {
        alert(`批量接受失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    function renderManualSelection() {
      const cell = state.stage === 'event_annotation' && state.manualAnnotationKind === 'cell';
      const anchors = qcAnchorChannels();
      el('manualMode').classList.toggle('manual-mode-on', state.manualMode);
      el('manualMode').textContent = state.manualMode ? 'Selecting' : 'Select peaks';
      el('manualLIF').textContent = state.manual.LIF ? `${state.manual.LIF.channel} ${state.manual.LIF.id} (${fmt(state.manual.LIF.time, 3)})` : '-';
      el('manualAnchorRows').innerHTML = anchors.map((channel) => {
        const selected = state.manual.anchors[channel];
        const value = selected ? `${selected.id} (${fmt(selected.time, 3)})` : '-';
        return `<div>${escapeText(channel)}: <strong>${escapeText(value)}</strong></div>`;
      }).join('');
      el('manualMS').textContent = state.manual.MS760 ? `${state.manual.MS760.id} (${fmt(state.manual.MS760.time, 3)})` : '-';
      el('manualLifRow').style.display = cell ? 'block' : 'none';
      el('manualAnchorRows').style.display = cell ? 'none' : 'block';
    }

    function selectManualPeak(kind, row) {
      const weakPeak = kind !== 'MS760'
        && String(row.peak_tier || 'core').trim().toLowerCase() === 'weak';
      if (weakPeak && state.stage !== 'event_annotation') {
        showInteractionHint('仅在事件标注段生效');
        return;
      }
      if (weakPeak && state.manualAnnotationKind !== 'cell') {
        showInteractionHint('请切换到 Cell pair');
        return;
      }
      if (!state.manualMode) {
        showInteractionHint('请先点击 Select peaks');
        return;
      }
      const cellMode = state.stage === 'event_annotation' && state.manualAnnotationKind === 'cell';
      if (cellMode && kind !== 'MS760') {
        const allowed = new Set(
          (state.meta?.acquisition_layout?.lif_channels || [])
            .filter(item => item.use_for_cell_annotation !== false)
            .map(item => item.channel)
        );
        if (allowed.size && !allowed.has(row.channel)) {
          showInteractionHint('该通道未启用 Cell pair');
          return;
        }
        state.manual.LIF = { id: row.peak_id, channel: row.channel, time: row.raw_time_min ?? row.time_min };
        state.manual.anchors = {};
      } else {
        const selected = { id: kind === 'MS760' ? row.event_id : row.peak_id, channel: row.channel, time: row.raw_time_min ?? row.time_min };
        if (kind === 'MS760') {
          state.manual.MS760 = selected;
        } else {
          const anchors = qcAnchorChannels();
          if (anchors.includes(row.channel)) state.manual.anchors[row.channel] = selected;
          state.manual.LIF = { id: row.peak_id, channel: row.channel, time: row.raw_time_min ?? row.time_min };
        }
      }
      renderManualSelection();
      draw();
    }

    function focusSavedCellRelation(row) {
      const targetStart = relationDisplayWindowStart(row);
      const currentStart = Number(state.current?.start_min);
      if (!Number.isFinite(targetStart) || Math.abs(targetStart - currentStart) <= 1e-9) return false;
      state.start = targetStart;
      el('start').value = state.start.toFixed(2);
      showInteractionHint('已保存；已转到包含完整关系的窗口');
      return true;
    }

    async function createManualTriplet() {
      if (state.actionBusy) return;
      const cell = state.stage === 'event_annotation' && state.manualAnnotationKind === 'cell';
      const qcSurvey = state.stage === 'event_annotation';
      if (cell) {
        if (!state.manual.LIF || !state.manual.MS760) {
          alert('细胞标注需选择一个 LIF 峰和一个 MS760 峰。');
          return;
        }
        state.actionBusy = true;
        try {
          const response = await postJson('/api/manual-cell-pair', {
            lif_channel: state.manual.LIF.channel,
            lif_peak_id: state.manual.LIF.id,
            ms_event_id: state.manual.MS760.id,
            window_start_min: state.current.start_min,
            window_end_min: state.current.end_min,
            time_mode: state.current.time_mode
          });
          resetManualSelection();
          state.manualMode = false;
          focusSavedCellRelation(response.annotation);
          await loadWindow();
          notifyStateChannel();
        } catch (err) {
          alert(`手动细胞二元组写入失败: ${err.message}`);
        } finally {
          state.actionBusy = false;
        }
        return;
      }
      const anchors = qcAnchorChannels();
      const selectedAnchorChannels = anchors.filter(channel => Boolean(state.manual.anchors[channel]));
      const axes = state.meta?.acquisition_layout?.channel_time_axes || state.current?.alignment?.channel_time_axes || {};
      const requiredAxes = new Set(anchors.map(channel => axes[channel] || (channel.startsWith('G') ? 'green_axis' : 'red_axis')));
      const coveredAxes = new Set(selectedAnchorChannels.map(channel => axes[channel] || (channel.startsWith('G') ? 'green_axis' : 'red_axis')));
      const coversAllAxes = Array.from(requiredAxes).every(axis => coveredAxes.has(axis));
      if (!state.manual.MS760 || (qcSurvey ? selectedAnchorChannels.length === 0 : !coversAllAxes)) {
        alert(qcSurvey
          ? `后段质控巡检需要选择 MS760，并至少选择 ${anchors.join('/')} 中的一个峰。`
          : `前段时间校正需要选择 MS760，并用 ${anchors.join('/')} 中的峰覆盖全部信号时间轴。`);
        return;
      }
      const selectedAnchorIds = Object.fromEntries(
        anchors.map(channel => [channel, state.manual.anchors[channel]?.id || null])
      );
      const matchingRow = candidateRows().find((row) => {
        if (String(row.ms_event_id || '') !== String(state.manual.MS760.id || '')) return false;
        const rowAnchors = qcAnchorPeakIds(row);
        return anchors.every(channel => (rowAnchors[channel] || null) === selectedAnchorIds[channel]);
      });
      const invalidation = confirmQcEvidenceInvalidation(matchingRow?.review_status || 'pending', 'accepted');
      if (invalidation === null) return;
      state.actionBusy = true;
      try {
        await postJson('/api/manual-triplet', {
          lif_anchor_peak_ids: selectedAnchorIds,
          ms_event_id: state.manual.MS760.id,
          stage: qcSurvey ? 'qc_survey' : 'qc_calibration',
          calibration_segment_id: qcSurvey ? null : activeCalibrationSegment()?.segment_id,
          window_start_min: state.current.start_min,
          window_end_min: state.current.end_min,
          time_mode: state.current.time_mode,
          ...invalidation
        });
        state.qcRefitPreview = null;
        resetManualSelection();
        state.manualMode = false;
        await loadWindow();
        notifyStateChannel();
      } catch (err) {
        alert(`手动参考峰关系保存失败：${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    function setAttrs(node, attrs) {
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
      return node;
    }

    function svgEl(name, attrs = {}) {
      return setAttrs(document.createElementNS('http://www.w3.org/2000/svg', name), attrs);
    }

    function visibleThirdStageLifPeakIds() {
      const ids = new Set();
      if (state.stage !== 'event_annotation' || !state.meta?.cell_event_map?.available) return null;
      const rows = [];
      const allowedEventIds = new Set(
        (state.current?.ms_events || [])
          .filter(event => event.in_cell_event_map === true)
          .map(event => String(event.event_id))
      );
      if (state.eventFilter !== 'cell') {
        rows.push(...(state.current?.post_qc_candidates || []));
        rows.push(...(state.current?.cell_qc_anchors || []));
      }
      if (state.eventFilter !== 'qc') rows.push(...visibleCellCandidates());
      (state.current?.annotations || [])
        .filter(row => allowedEventIds.has(String(row.ms_event_id || '')))
        .filter(row => eventRowMatchesFilter(row))
        .forEach(row => rows.push(row));
      rows.forEach(row => {
        Object.values(qcAnchorPeakIds(row)).filter(Boolean).forEach(id => ids.add(String(id)));
        if (row.lif_peak_id) ids.add(String(row.lif_peak_id));
      });
      return ids;
    }

    function automaticPeakLabelIds(rows, idFor, timeFor, scoreFor, start, end, plotWidth) {
      if (state.peakLabelMode === 'hidden') return new Set();
      const visibleRows = rows.filter(row => {
        const time = Number(timeFor(row));
        return Number.isFinite(time) && time >= start && time <= end;
      });
      if (state.peakLabelMode === 'all') {
        return new Set(visibleRows.map(row => String(idFor(row))));
      }
      const targetCount = Math.max(6, Math.min(20, Math.floor(plotWidth / 95)));
      if (visibleRows.length <= targetCount) {
        return new Set(visibleRows.map(row => String(idFor(row))));
      }
      const bins = Array.from({ length: targetCount }, () => null);
      const span = Math.max(1e-9, end - start);
      visibleRows.forEach(row => {
        const time = Number(timeFor(row));
        const binIndex = Math.min(
          targetCount - 1,
          Math.max(0, Math.floor(((time - start) / span) * targetCount))
        );
        const rawScore = Number(scoreFor(row));
        const score = Number.isFinite(rawScore) ? rawScore : 0;
        const current = bins[binIndex];
        if (!current || score > current.score) bins[binIndex] = { row, score };
      });
      return new Set(
        bins.filter(Boolean).map(item => String(idFor(item.row)))
      );
    }

    function updatePeakLabelPolicyText() {
      const policy = el('windowPolicy');
      if (!policy) return;
      if (state.peakLabelMode === 'hidden') {
        policy.textContent = '峰圆点全部保留；时间数字已隐藏，悬停任意圆点可查看精确原始时间(min)';
      } else if (state.peakLabelMode === 'all') {
        policy.textContent = '正在尽量显示全部峰时间，高密度窗口可能拥挤；悬停圆点可查看精确信息';
      } else {
        policy.textContent = '峰圆点全部保留；每个时间分区只标注一个显著峰，悬停任意圆点可查看精确原始时间(min)';
      }
    }

    function draw() {
      const svg = el('chart');
      const rect = svg.getBoundingClientRect();
      const width = Math.max(720, rect.width || 900);
      const height = Math.max(560, rect.height || 620);
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg.innerHTML = '';

      const margin = { left: 190, right: 104, top: 18, bottom: 34 };
      const gap = 18;
      const perTrackAxisH = 22;
      const tracks = tracksForCurrentProject();
      const trackH = (height - margin.top - margin.bottom - gap * (tracks.length - 1)) / tracks.length;
      const plotW = width - margin.left - margin.right;
      const x0 = margin.left;
      const x1 = width - margin.right;
      const start = state.current.start_min;
      const end = state.current.end_min;
      const xScale = (x) => x0 + ((x - start) / (end - start)) * plotW;
      const contextMarginMin = Math.max(0, Number(state.current.context_margin_min || 0));
      const contextPadPx = Math.min(86, (contextMarginMin / Math.max(1e-6, end - start)) * plotW);
      const amplitudeLabelX = x0 - contextPadPx - 16;
      const markerPositions = {};
      const thirdStagePeakIds = visibleThirdStageLifPeakIds();
      const restrictThirdStageHits = thirdStagePeakIds !== null;
      const cellAnnotationChannels = new Set(
        (state.meta?.acquisition_layout?.lif_channels || [])
          .filter(item => item.use_for_cell_annotation !== false)
          .map(item => String(item.channel))
      );
      const manualCellSelectionActive = state.stage === 'event_annotation'
        && state.manualAnnotationKind === 'cell'
        && state.manualMode;
      const peakInsideMainWindow = p => {
        const time = Number(p.plot_time_min);
        return Number.isFinite(time) && time >= start && time <= end;
      };
      const peakCellRoleEnabled = p => cellAnnotationChannels.size === 0
        || cellAnnotationChannels.has(String(p.channel));
      updatePeakLabelPolicyText();

      const bg = svgEl('rect', { x: 0, y: 0, width, height, fill: '#fff' });
      svg.appendChild(bg);

      tracks.forEach((track, idx) => {
        const top = margin.top + idx * (trackH + gap);
        const bottom = top + trackH;
        const signalBottom = bottom - perTrackAxisH;
        const mid = top + (signalBottom - top) / 2;
        const series = [];
        if (track.kind === 'lif') {
          track.channels.forEach(ch => (state.current.lif_traces[ch] || []).forEach(p => series.push(p.y)));
          (state.current.lif_peaks || [])
            .filter(p => track.channels.includes(p.channel))
            .forEach(p => series.push(lifPeakY(p)));
        } else {
          (state.current.ms_traces[track.trace] || []).forEach(p => series.push(p.y));
        }
        const [yMin, yMax] = yAxisExtent(series);
        const yScale = (y) => signalBottom - ((clampForAxis(y, yMin, yMax) - yMin) / (yMax - yMin)) * (signalBottom - top);
        const trackShiftMin = trackShiftSec(track) / 60.0;
        const labelBoxes = [];

        svg.appendChild(svgEl('rect', { x: x0, y: top, width: plotW, height: trackH, fill: idx % 2 ? '#fbfcfd' : '#ffffff' }));
        svg.appendChild(svgEl('line', { x1: x0, y1: signalBottom, x2: x1, y2: signalBottom, stroke: '#d7dce3', 'stroke-width': 1 }));
        svg.appendChild(svgEl('text', { x: 16, y: mid + 4, fill: '#344054', 'font-size': 12, 'font-weight': 700, class: 'track-label', 'pointer-events': 'none' })).textContent = track.label;
        const yMaxText = fmtAxis(yMax);
        const yMinText = fmtAxis(yMin);
        reserveLabelBox(labelBoxes, yMaxText, amplitudeLabelX, top + 12, 'end');
        reserveLabelBox(labelBoxes, yMinText, amplitudeLabelX, signalBottom - 4, 'end');
        svg.appendChild(svgEl('text', {
          x: amplitudeLabelX,
          y: top + 12,
          fill: '#667085',
          'font-size': 10,
          'text-anchor': 'end',
          class: 'amplitude-label',
          'pointer-events': 'none',
          'paint-order': 'stroke',
          stroke: '#ffffff',
          'stroke-width': 3,
          'stroke-linejoin': 'round'
        })).textContent = yMaxText;
        svg.appendChild(svgEl('text', {
          x: amplitudeLabelX,
          y: signalBottom - 4,
          fill: '#667085',
          'font-size': 10,
          'text-anchor': 'end',
          class: 'amplitude-label',
          'pointer-events': 'none',
          'paint-order': 'stroke',
          stroke: '#ffffff',
          'stroke-width': 3,
          'stroke-linejoin': 'round'
        })).textContent = yMinText;

        drawTrackTimeAxis(svg, xScale, start, end, x0, x1, top, signalBottom, bottom, trackShiftMin);

        if (track.kind === 'lif') {
          track.channels.forEach(ch => {
            const points = state.current.lif_traces[ch] || [];
            svg.appendChild(svgEl('path', {
              d: linePath(points, xScale, yScale),
              fill: 'none',
              stroke: colorForChannel(ch),
              'stroke-width': 1.15,
              'vector-effect': 'non-scaling-stroke'
            }));
          });
          const trackPeaks = state.current.lif_peaks
            .filter(p => track.channels.includes(p.channel));
          const peakIsInteractive = p => {
            const weakPeak = String(p.peak_tier || 'core').trim().toLowerCase() === 'weak';
            if (weakPeak) {
              return state.showWeakLifPeaks
                && state.stage === 'event_annotation'
                && state.manualAnnotationKind === 'cell'
                && peakInsideMainWindow(p)
                && peakCellRoleEnabled(p);
            }
            if (manualCellSelectionActive && peakInsideMainWindow(p) && peakCellRoleEnabled(p)) return true;
            return !restrictThirdStageHits
              || thirdStagePeakIds.has(String(p.peak_id));
          };
          const labelIds = automaticPeakLabelIds(
            trackPeaks.filter(peakIsInteractive),
            p => p.peak_id,
            p => p.plot_time_min,
            p => Number(p.prominence ?? p.height ?? 0),
            start,
            end,
            plotW
          );
          trackPeaks.forEach(p => {
              const peakY = lifPeakY(p);
              const interactive = peakIsInteractive(p);
              const weakPeak = String(p.peak_tier || 'core').trim().toLowerCase() === 'weak';
              const manuallySelected = String(state.manual.LIF?.id || '') === String(p.peak_id)
                || Object.values(state.manual.anchors || {}).some(item => String(item?.id || '') === String(p.peak_id));
              const c = svgEl('circle', {
                cx: xScale(p.plot_time_min),
                cy: yScale(peakY),
                r: manuallySelected ? 5.2 : (p.close_peak_risk || p.merge_risk ? 4.5 : (weakPeak ? 3.8 : 3.4)),
                fill: weakPeak ? '#fff' : colorForChannel(p.channel),
                stroke: manuallySelected ? '#111827' : (p.close_peak_risk || p.merge_risk ? '#b42318' : (weakPeak ? colorForChannel(p.channel) : '#fff')),
                'stroke-width': manuallySelected ? 2.6 : (weakPeak ? 1.8 : 1.3),
                'stroke-dasharray': weakPeak ? '2 1' : '',
                class: manuallySelected ? 'peak-marker manual-selected-peak' : 'peak-marker'
              });
              if (weakPeak) c.setAttribute('pointer-events', 'none');
              if (interactive && !weakPeak) {
                c.setAttribute('tabindex', '0');
                c.__detail = { kind: 'lif_peak', type: 'LIF 峰', data: p };
                attachHover(c);
                c.addEventListener('click', () => {
                  selectManualPeak('LIF', p);
                });
              } else if (!weakPeak) {
                c.setAttribute('opacity', '.42');
                c.setAttribute('pointer-events', 'none');
              } else if (!interactive) {
                c.setAttribute('opacity', '.58');
                c.setAttribute('pointer-events', 'none');
              }
              svg.appendChild(c);
              if (weakPeak && state.showWeakLifPeaks) {
                const activateWeakPeak = () => {
                  if (state.stage !== 'event_annotation') {
                    showInteractionHint('仅在事件标注段生效');
                    return;
                  }
                  selectManualPeak('LIF', p);
                };
                const weakHit = svgEl('circle', {
                  class: 'weak-peak-hit-target',
                  cx: xScale(p.plot_time_min),
                  cy: yScale(peakY),
                  r: 9,
                  fill: 'transparent',
                  role: 'button',
                  tabindex: '0',
                  'aria-label': `${channelDisplayLabel(p.channel)} 弱候选峰 ${fmtMaybe(p.raw_time_min ?? p.time_min, 3)} min`,
                  'pointer-events': 'all',
                  cursor: state.stage === 'event_annotation' ? 'pointer' : 'help'
                });
                weakHit.__detail = { kind: 'lif_peak', type: 'LIF 弱候选峰', data: p };
                attachHover(weakHit);
                weakHit.addEventListener('click', activateWeakPeak);
                weakHit.addEventListener('keydown', (ev) => {
                  if (ev.key === 'Enter' || ev.key === ' ') {
                    ev.preventDefault();
                    activateWeakPeak();
                  }
                });
                svg.appendChild(weakHit);
              }
              if (interactive && labelIds.has(String(p.peak_id))) {
                markerPositions[`lif:${p.peak_id}`] = { x: xScale(p.plot_time_min), y: yScale(peakY), channel: p.channel };
                addTimeLabel(svg, fmt(p.raw_time_min ?? p.time_min, 3), xScale(p.plot_time_min), yScale(peakY), top, signalBottom, x1, colorForChannel(p.channel), labelBoxes, state.peakLabelMode === 'all');
              } else if (interactive) {
                markerPositions[`lif:${p.peak_id}`] = { x: xScale(p.plot_time_min), y: yScale(peakY), channel: p.channel };
              }
            });
        } else {
          const trace = state.current.ms_traces[track.trace] || [];
          svg.appendChild(svgEl('path', {
            d: linePath(trace, xScale, yScale),
            fill: 'none',
            stroke: track.trace === 'pc34_760_linear' ? colors.ms760 : colors.ms782,
            'stroke-width': 1.15,
            'vector-effect': 'non-scaling-stroke'
          }));
          const eventIsInteractive = e => state.stage !== 'event_annotation'
            || !state.meta?.cell_event_map?.available
            || e.in_cell_event_map === true;
          const labelIds = automaticPeakLabelIds(
            state.current.ms_events.filter(eventIsInteractive),
            e => e.event_id,
            e => e.plot_time_min,
            e => track.trace === 'pc34_760_linear' ? e.pc34_760_apex : e.qc_782_apex,
            start,
            end,
            plotW
          );
          state.current.ms_events.forEach(e => {
            const raw = track.trace === 'pc34_760_linear' ? e.pc34_760_apex : e.qc_782_apex;
            const y = Math.max(0, Number(raw || 0));
            const interactive = eventIsInteractive(e);
            const c = svgEl('circle', {
              cx: xScale(e.plot_time_min),
              cy: yScale(y),
              r: e.low_quality_scan_window || e.collision_risk_high ? 4.7 : 3.5,
              fill: track.trace === 'pc34_760_linear' ? colors.ms760 : colors.ms782,
              stroke: e.low_quality_scan_window || e.collision_risk_high ? '#b42318' : '#fff',
              'stroke-width': 1.3,
              class: 'peak-marker'
            });
            if (interactive) {
              c.setAttribute('tabindex', '0');
              c.__detail = {
                kind: track.trace === 'pc34_760_linear' ? 'ms760_peak' : 'ms782_peak',
                type: track.trace === 'pc34_760_linear' ? 'MS 760 峰' : 'MS 782 峰',
                data: e
              };
              attachHover(c);
              if (track.trace === 'pc34_760_linear') {
                c.addEventListener('click', () => selectManualPeak('MS760', e));
              }
            } else {
              c.setAttribute('opacity', '.38');
              c.setAttribute('tabindex', '0');
              c.setAttribute('cursor', 'help');
              c.__detail = {
                kind: track.trace === 'pc34_760_linear' ? 'ms760_peak' : 'ms782_peak',
                type: track.trace === 'pc34_760_linear'
                  ? 'MS 760（未在事件坐标 CSV）'
                  : 'MS 782（未在事件坐标 CSV）',
                data: e
              };
              attachHover(c);
              if (track.trace === 'pc34_760_linear') {
                const explainUnmappedMs = () => showInteractionHint('不在事件坐标 CSV，不能用于 Cell pair');
                c.addEventListener('click', explainUnmappedMs);
                c.addEventListener('keydown', (ev) => {
                  if (ev.key === 'Enter' || ev.key === ' ') {
                    ev.preventDefault();
                    explainUnmappedMs();
                  }
                });
              }
            }
            svg.appendChild(c);
            if (interactive && track.trace === 'pc34_760_linear') {
              markerPositions[`ms760:${e.event_id}`] = { x: xScale(e.plot_time_min), y: yScale(y) };
            }
            if (interactive && labelIds.has(String(e.event_id))) {
              addTimeLabel(svg, fmt(e.raw_time_min ?? e.time_min, 3), xScale(e.plot_time_min), yScale(y), top, signalBottom, x1, track.trace === 'pc34_760_linear' ? colors.ms760 : colors.ms782, labelBoxes, state.peakLabelMode === 'all');
            }
          });
        }
      });
      if (state.stage === 'event_annotation') {
        if (state.eventFilter !== 'cell') {
          drawPostQcCandidates(svg, markerPositions);
          drawManualAnnotations(svg, markerPositions);
        }
        if (state.eventFilter !== 'qc') {
          drawCellCandidates(svg, markerPositions);
          drawManualCellAnnotations(svg, markerPositions);
        }
      } else {
        drawAlignmentGroups(svg, markerPositions);
        drawManualAnnotations(svg, markerPositions);
      }
      bringPeakMarkersToFront(svg);
      bringPeakLabelsToFront(svg);
    }

    function trackShiftSec(track) {
      if (!state.current || state.current.time_mode !== 'aligned') return 0;
      if (track.kind === 'ms') {
        const tm = state.current.time_model || {};
        if (state.current.start_min >= Number(tm.annotation_start_min || 40)) return Number(tm.ms_local_delta_sec || 0);
        return 0;
      }
      if (track.kind !== 'lif') return 0;
      const channel = track.channels[0];
      const axes = state.current.alignment.channel_time_axes || state.meta?.acquisition_layout?.channel_time_axes || {};
      const axis = axes[channel] || (String(channel).startsWith('G') ? 'green_axis' : 'red_axis');
      const axisShifts = state.current.alignment.axis_shifts_sec || {};
      if (axisShifts[axis] !== undefined) return Number(axisShifts[axis] || 0);
      if (axis === 'green_axis') return Number(state.current.alignment.green_to_ms_shift_sec || 0);
      if (axis === 'red_axis') return Number(state.current.alignment.red_to_ms_shift_sec || 0);
      return 0;
    }

    function qcAnchorMarkerPoints(row, markerPositions) {
      const peakIds = qcAnchorPeakIds(row);
      const configuredChannels = qcAnchorChannels(row);
      const anchors = configuredChannels
        .map(channel => {
          if (!peakIds[channel]) return null;
          const marker = markerPositions[`lif:${peakIds[channel]}`];
          return marker ? { ...marker, channel } : null;
        })
        .filter(Boolean);
      const ms = markerPositions[`ms760:${row.ms_event_id}`];
      return { anchors, ms, configuredCount: configuredChannels.length };
    }

    function appendQcConnectorPolyline(svg, markerGroup, row, detail, style, onSelect) {
      if (!markerGroup.ms || markerGroup.anchors.length < 1) return;
      const partial = row.complete_anchor_set === false || row.candidate_type === 'manual_qc_anchor_partial';
      if (!partial && markerGroup.anchors.length !== markerGroup.configuredCount) return;
      const points = [
        ...markerGroup.anchors.slice().sort((left, right) => left.y - right.y),
        markerGroup.ms
      ];
      const line = svgEl('polyline', {
        points: points.map(point => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' '),
        fill: 'none',
        stroke: style.stroke,
        'stroke-width': style.width,
        'stroke-dasharray': style.dash,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
        opacity: style.opacity,
        'pointer-events': 'visibleStroke',
        cursor: 'pointer'
      });
      line.__detail = detail;
      appendLineWithHitTarget(svg, line, row, onSelect);
    }

    function drawAlignmentGroups(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.alignment_groups || []).forEach(group => {
        if (group.review_status === 'rejected') return;
        const markerGroup = qcAnchorMarkerPoints(group, markerPositions);
        const lineStyle = candidateLineStyle(group);
        appendQcConnectorPolyline(svg, markerGroup, group, { kind: 'qc_candidate', type: 'QC 候选', data: group }, lineStyle, () => {
          state.selectedCandidateId = group.annotation_id || group.candidate_id;
          renderCandidateList();
          draw();
        });
      });
    }

    function drawPostQcCandidates(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.post_qc_candidates || []).forEach(group => {
        if (group.review_status === 'rejected') return;
        const markerGroup = qcAnchorMarkerPoints(group, markerPositions);
        const lineStyle = candidateLineStyle(group);
        appendQcConnectorPolyline(svg, markerGroup, group, { kind: 'qc_survey_candidate', type: 'QC 巡检候选', data: group }, lineStyle, () => {
          state.selectedCandidateId = group.annotation_id || group.candidate_id;
          renderCandidateList();
          draw();
        });
      });
    }

    function drawCellCandidates(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      visibleCellCandidates().forEach(row => {
        if (row.review_status === 'rejected') return;
        const lif = markerPositions[`lif:${row.lif_peak_id}`];
        const ms = markerPositions[`ms760:${row.ms_event_id}`];
        if (!lif || !ms) return;
        const selected = (row.annotation_id || row.candidate_id) === state.selectedCandidateId;
        const accepted = row.review_status === 'accepted';
        const baseColor = row.cross_channel_candidate_conflict && !accepted ? '#d97706' : colorForChannel(row.lif_channel);
        const line = svgEl('line', {
          x1: lif.x.toFixed(2),
          y1: lif.y.toFixed(2),
          x2: ms.x.toFixed(2),
          y2: ms.y.toFixed(2),
          stroke: baseColor,
          'stroke-width': accepted ? 1.05 : (selected ? 1.8 : 1.25),
          'stroke-dasharray': accepted ? '' : '6 4',
          opacity: accepted ? 0.40 : 0.68,
          'pointer-events': 'visibleStroke',
          cursor: 'pointer'
        });
        line.__detail = {
          kind: 'cell_candidate',
          type: row.cross_channel_candidate_conflict ? '跨通道歧义候选（需人工仲裁）' : '细胞候选',
          data: row
        };
        appendLineWithHitTarget(svg, line, row, () => {
          state.selectedCandidateId = row.annotation_id || row.candidate_id;
          renderCandidateList();
          draw();
        });
      });
    }

    function drawManualAnnotations(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.annotations || [])
        .filter(row => row.source === 'manual_created')
        .filter(row => manualBelongsToStage(row, state.stage))
        .filter(row => state.stage !== 'event_annotation' || eventRowKind(row) === 'qc')
        .forEach(row => {
          if (row.review_status === 'rejected') return;
          const markerGroup = qcAnchorMarkerPoints(row, markerPositions);
          const style = candidateLineStyle(row);
          const detail = {
            kind: 'manual_qc',
            type: (row.complete_anchor_set === false || row.candidate_type === 'manual_qc_anchor_partial') ? '人工 QC（部分通道）' : '人工 QC',
            data: row
          };
          appendQcConnectorPolyline(svg, markerGroup, row, detail, style, () => {
            state.selectedCandidateId = row.annotation_id;
            renderCandidateList();
            draw();
          });
        });
    }

    function drawManualCellAnnotations(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.annotations || [])
        .filter(row => row.source === 'manual_created')
        .filter(row => manualBelongsToStage(row, 'cell_annotation'))
        .forEach(row => {
          if (row.review_status === 'rejected') return;
          const lif = markerPositions[`lif:${row.lif_peak_id}`];
          const ms = markerPositions[`ms760:${row.ms_event_id}`];
          if (!lif || !ms) return;
          const selected = row.annotation_id === state.selectedCandidateId;
          const baseColor = colorForChannel(row.lif_channel);
          const style = candidateLineStyle(row);
          const line = svgEl('line', {
            x1: lif.x.toFixed(2),
            y1: lif.y.toFixed(2),
            x2: ms.x.toFixed(2),
            y2: ms.y.toFixed(2),
            stroke: baseColor,
            'stroke-width': selected ? 1.65 : style.width,
            'stroke-dasharray': style.dash,
            opacity: row.review_status === 'accepted' ? 0.42 : style.opacity,
            'pointer-events': 'visibleStroke',
            cursor: 'pointer'
          });
          line.__detail = { kind: 'manual_cell', type: '人工细胞标注', data: row };
          appendLineWithHitTarget(svg, line, row, () => {
            state.selectedCandidateId = row.annotation_id;
            renderCandidateList();
            draw();
          });
        });
    }

    function isAcceptedQcSurveyRow(row) {
      if (row.review_status !== 'accepted') return false;
      const type = String(row.candidate_type || '');
      return type.startsWith('qc_survey_') || type.startsWith('manual_qc');
    }

    function drawAcceptedQcSurveyAnnotations(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.cell_qc_anchors || [])
        .filter(isAcceptedQcSurveyRow)
        .forEach(row => {
          const markerGroup = qcAnchorMarkerPoints(row, markerPositions);
          const selected = (row.annotation_id || row.candidate_id) === state.selectedCandidateId;
          const style = {
            stroke: '#111827',
            width: selected ? 1.75 : 1.05,
            dash: '',
            opacity: selected ? 0.55 : 0.28
          };
          appendQcConnectorPolyline(svg, markerGroup, row, { kind: 'accepted_qc_survey', type: 'QC 巡检', data: row }, style, () => {
            state.selectedCandidateId = row.annotation_id || row.candidate_id;
            renderCandidateList();
            draw();
          });
        });
    }

    function candidateLineStyle(row) {
      const selected = (row.annotation_id || row.candidate_id) === state.selectedCandidateId;
      if (row.review_status === 'accepted') {
        return { stroke: '#111827', width: 1.05, dash: '', opacity: 0.34 };
      }
      return { stroke: '#111827', width: selected ? 1.9 : 1.35, dash: '6 4', opacity: selected ? 0.76 : 0.56 };
    }

    function drawTrackTimeAxis(svg, xScale, start, end, left, right, top, signalBottom, bottom, shiftMin) {
      const axisY = signalBottom;
      svg.appendChild(svgEl('line', { x1: left, y1: axisY, x2: right, y2: axisY, stroke: '#98a2b3', 'stroke-width': 1 }));
      const widthMin = end - start;
      const step = widthMin <= 3.1 ? 0.5 : widthMin <= 6 ? 1.0 : 2.0;
      const rawStart = start - shiftMin;
      const rawEnd = end - shiftMin;
      const first = Math.ceil(rawStart / step) * step;
      for (let rawTick = first; rawTick <= rawEnd + 1e-9; rawTick += step) {
        const x = xScale(rawTick + shiftMin);
        if (x < left - 1 || x > right + 1) continue;
        svg.appendChild(svgEl('line', { x1: x, y1: top, x2: x, y2: signalBottom, stroke: '#edf0f4', 'stroke-width': 1 }));
        svg.appendChild(svgEl('line', { x1: x, y1: axisY, x2: x, y2: axisY + 5, stroke: '#667085', 'stroke-width': 1 }));
        svg.appendChild(svgEl('text', {
          x,
          y: Math.min(axisY + 17, bottom - 3),
          fill: '#475467',
          'font-size': 10,
          'text-anchor': 'middle',
          class: 'axis-label',
          'pointer-events': 'none',
          'paint-order': 'stroke',
          stroke: '#ffffff',
          'stroke-width': 2,
          'stroke-linejoin': 'round'
        })).textContent = fmt(rawTick, 1);
      }
      svg.appendChild(svgEl('text', {
        x: right + 36,
        y: Math.min(axisY + 17, bottom - 3),
        fill: '#344054',
        'font-size': 10,
        'text-anchor': 'start',
        class: 'axis-label',
        'pointer-events': 'none',
        'paint-order': 'stroke',
        stroke: '#ffffff',
        'stroke-width': 2,
        'stroke-linejoin': 'round'
      })).textContent = 'min';
    }

    function boxesOverlap(a, b) {
      return a.x1 < b.x2 && a.x2 > b.x1 && a.y1 < b.y2 && a.y2 > b.y1;
    }

    function reserveLabelBox(labelBoxes, text, x, y, anchor) {
      const estimatedW = Math.max(18, String(text).length * 6.2);
      labelBoxes.push({
        x1: anchor === 'end' ? x - estimatedW : x,
        x2: anchor === 'end' ? x : x + estimatedW,
        y1: y - 10,
        y2: y + 3
      });
    }

    function bringPeakLabelsToFront(svg) {
      svg.querySelectorAll('.peak-time-label, .amplitude-label').forEach(node => svg.appendChild(node));
    }

    function bringPeakMarkersToFront(svg) {
      svg.querySelectorAll('.peak-marker').forEach(node => svg.appendChild(node));
      svg.querySelectorAll('.weak-peak-hit-target').forEach(node => svg.appendChild(node));
    }

    function addTimeLabel(svg, text, x, y, top, bottom, right, color, labelBoxes, allowOverlap = false) {
      const anchor = x > right - 52 ? 'end' : 'start';
      const labelX = anchor === 'end' ? x - 5 : x + 5;
      const estimatedW = Math.max(26, String(text).length * 6.2);
      const x1 = anchor === 'end' ? labelX - estimatedW : labelX;
      const x2 = anchor === 'end' ? labelX : labelX + estimatedW;
      const lanes = [-7, 13, -20, 26, -33, 39];
      let labelY = Math.min(Math.max(y - 7, top + 10), bottom - 3);
      let chosen = null;
      for (const offset of lanes) {
        const candidateY = Math.min(Math.max(y + offset, top + 10), bottom - 3);
        const box = { x1, x2, y1: candidateY - 10, y2: candidateY + 3 };
        if (!labelBoxes.some(existing => boxesOverlap(box, existing))) {
          labelY = candidateY;
          chosen = box;
          break;
        }
      }
      if (!chosen) {
        if (!allowOverlap) return false;
        chosen = { x1, x2, y1: labelY - 10, y2: labelY + 3 };
      }
      labelBoxes.push(chosen);
      svg.appendChild(svgEl('text', {
        x: labelX,
        y: labelY,
        fill: color,
        'font-size': 10,
        'font-weight': 700,
        class: 'peak-time-label',
        'pointer-events': 'none',
        'text-anchor': anchor,
        'paint-order': 'stroke',
        stroke: '#ffffff',
        'stroke-width': 3,
        'stroke-linejoin': 'round'
      })).textContent = text;
      return true;
    }

    function attachHover(node) {
      node.addEventListener('mousemove', (ev) => showDetail(node.__detail, ev.clientX, ev.clientY));
      node.addEventListener('mouseenter', (ev) => showDetail(node.__detail, ev.clientX, ev.clientY));
      node.addEventListener('mouseleave', hideTooltip);
      node.addEventListener('focus', (ev) => showDetail(node.__detail, window.innerWidth - 380, 90));
    }

    function reviewDetailStatus(row) {
      if (row.review_enabled === false && row.review_status === 'pending') return '预览';
      return statusText(row.review_status);
    }

    function decisionDeviationSec(row) {
      const values = [
        row.max_abs_axis_to_ms_residual_sec,
        row.abs_composite_to_ms_residual_sec,
        row.abs_residual_sec,
        row.composite_to_ms_residual_sec,
        row.residual_sec
      ].filter(value => value !== null && value !== undefined && value !== '')
        .map(Number).filter(Number.isFinite).map(Math.abs);
      return values.length ? values[0] : null;
    }

    function qcHoverLines(detail) {
      const row = detail.data;
      const deviation = decisionDeviationSec(row);
      const lines = [
        `${detail.type} · ${reviewDetailStatus(row)}`,
        deviation === null ? '' : `偏差 ${fmt(deviation, 2)} sec`
      ];
      if (row.review_status === 'pending') {
        const reason = candidateBatchBlockReason(row);
        if (reason && reason !== 'outside_main_window') lines.push(`需逐条审核：${batchBlockText(reason, row)}`);
      }
      return lines.filter(Boolean);
    }

    function cellHoverLines(detail) {
      const row = detail.data;
      const deviation = decisionDeviationSec(row);
      return [
        `${channelDisplayLabel(row.lif_channel)} ${detail.type} · ${reviewDetailStatus(row)}`,
        deviation === null ? '' : `偏差 ${fmt(deviation, 2)} sec`
      ].filter(Boolean);
    }

    function detailText(detail) {
      const d = detail.data;
      if (['qc_candidate', 'qc_survey_candidate', 'manual_qc', 'accepted_qc_survey'].includes(detail.kind)) {
        return qcHoverLines(detail).join('\n');
      }
      if (['cell_candidate', 'manual_cell'].includes(detail.kind)) {
        return cellHoverLines(detail).join('\n');
      }
      if (detail.kind === 'lif_peak') {
        const lines = [
          `LIF ${channelDisplayLabel(d.channel)} 峰`,
          `${fmtMaybe(d.raw_time_min ?? d.time_min, 3)} min`,
          Number.isFinite(Number(d.snr)) ? `SNR ${fmt(d.snr, 1)}` : '',
          d.close_peak_risk || d.merge_risk ? '相邻峰较近' : ''
        ];
        return lines.filter(Boolean).join('\n');
      }
      const isMs760 = detail.kind === 'ms760_peak';
      const intensity = isMs760 ? d.pc34_760_apex : d.qc_782_apex;
      return [
        detail.type,
        `${fmtMaybe(d.raw_time_min ?? d.time_min, 3)} min`,
        Number.isFinite(Number(intensity)) ? `强度 ${fmt(intensity, 1)}` : '',
        d.collision_risk_high || d.low_quality_scan_window ? '相邻事件或信号质量需注意' : ''
      ].filter(Boolean).join('\n');
    }

    function showDetail(detail, x, y) {
      const txt = detailText(detail);
      const tt = el('tooltip');
      tt.textContent = txt;
      tt.style.display = 'block';
      tt.style.left = '0px';
      tt.style.top = '0px';
      const ttWidth = Math.min(tt.offsetWidth || 360, window.innerWidth - 28);
      const ttHeight = Math.min(tt.offsetHeight || 220, window.innerHeight - 28);
      const left = Math.max(14, Math.min(x + 14, window.innerWidth - ttWidth - 14));
      const top = Math.max(14, Math.min(y + 14, window.innerHeight - ttHeight - 14));
      tt.style.left = `${left}px`;
      tt.style.top = `${top}px`;

      const rows = txt.split('\n').slice(1).map(line => {
        const i = line.indexOf(':');
        if (i < 0) return `<div class="kv"><span></span><strong>${escapeText(line)}</strong></div>`;
        return `<div class="kv"><span>${escapeText(line.slice(0, i))}</span><strong>${escapeText(line.slice(i + 2))}</strong></div>`;
      }).join('');
      const detailEl = el('detail');
      if (detailEl) {
        detailEl.innerHTML = `<p class="side-title">${escapeText(detail.type)}</p>${rows}`;
      }
    }

    function hideTooltip() {
      el('tooltip').style.display = 'none';
    }

    el('prev').addEventListener('click', async () => {
      state.start = Math.max(state.meta.time_min_min, state.start - state.width);
      await loadWindow();
    });
    el('next').addEventListener('click', async () => {
      state.start = Math.min(state.meta.time_min_max - state.width, state.start + state.width);
      await loadWindow();
    });
    el('go').addEventListener('click', async () => {
      if (!syncWindowWidthFromControl()) return;
      state.start = Number(el('start').value || 0);
      await loadWindow();
    });
    el('start').addEventListener('keydown', async (ev) => {
      if (ev.key === 'Enter') {
        if (!syncWindowWidthFromControl()) return;
        state.start = Number(el('start').value || 0);
        await loadWindow();
      }
    });
    el('widthDisplay').addEventListener('keydown', async (ev) => {
      if (ev.key === 'Enter') {
        if (!syncWindowWidthFromControl()) return;
        state.start = Number(el('start').value || state.start || 0);
        await loadWindow();
      }
    });
    el('timeMode').addEventListener('change', async () => {
      state.timeMode = el('timeMode').value;
      await loadWindow();
    });
    el('yAxisMode').addEventListener('change', () => {
      state.yAxisMode = el('yAxisMode').value;
      if (state.current) draw();
    });
    el('peakLabelMode').addEventListener('change', () => {
      state.peakLabelMode = el('peakLabelMode').value;
      if (state.current) draw();
    });
    document.querySelectorAll('.stage-tab').forEach(button => {
      button.addEventListener('click', async () => {
        hideLineContextMenu();
        state.stage = button.dataset.stage;
        state.showCrossChannelConflicts = false;
        el('showCrossChannelConflicts').checked = false;
        resetManualSelection();
        state.manualMode = false;
        applyStageWindowWidth();
        state.selectedCandidateId = null;
        state.previewDeltaSec = null;
        const cfg = state.current?.project_config || state.meta?.project_config || {};
        const annotationStart = Number(cfg.annotation_start_min || state.start);
        if (state.stage === 'event_annotation' || state.stage === 'local_calibration') {
          state.timeMode = 'aligned';
          el('timeMode').value = state.timeMode;
        }
        if (state.stage === 'local_calibration') {
          state.start = annotationStart;
        } else if (state.stage === 'event_annotation' && Number(state.start) < annotationStart) {
          state.start = annotationStart;
        }
        if (state.stage === 'qc_calibration' && Number(state.start) > Number(cfg.qc_calibration_end_min || 10.5)) {
          state.start = Math.max(0, Number(state.meta?.time_min_min || 0));
        }
        if (state.stage === 'local_calibration' || state.stage === 'event_annotation' || state.stage === 'qc_calibration') {
          await loadWindow();
          return;
        }
      });
    });
    el('showRejected').addEventListener('change', () => {
      hideLineContextMenu();
      state.showRejected = el('showRejected').checked;
      renderCandidateList();
      draw();
    });
    el('showCrossChannelConflicts').addEventListener('change', () => {
      hideLineContextMenu();
      state.showCrossChannelConflicts = el('showCrossChannelConflicts').checked;
      state.selectedCandidateId = null;
      renderCurrentState();
    });
    el('showWeakLifPeaks').addEventListener('change', async () => {
      hideLineContextMenu();
      state.showWeakLifPeaks = el('showWeakLifPeaks').checked;
      await loadWindow();
    });
    el('manualMode').addEventListener('click', () => {
      hideLineContextMenu();
      state.manualMode = !state.manualMode;
      renderManualSelection();
      draw();
    });
    el('clearManual').addEventListener('click', () => {
      resetManualSelection();
      renderManualSelection();
    });
    el('createManual').addEventListener('click', createManualTriplet);
    el('exportAcceptedCsv').addEventListener('click', exportAcceptedCsv);
    el('openImportProject').addEventListener('click', () => setImportModal(true));
    el('openExistingProject').addEventListener('click', () => setOpenProjectModal(true));
    el('openConfigProject').addEventListener('click', () => setProjectConfigModal(true));
    el('openUmap').addEventListener('click', openUmapWindow);
    el('bootstrapNewProject').addEventListener('click', () => setImportModal(true));
    el('bootstrapOpenProject').addEventListener('click', () => setOpenProjectModal(true));
    el('closeImportProject').addEventListener('click', () => setImportModal(false));
    el('closeOpenProject').addEventListener('click', () => setOpenProjectModal(false));
    el('openAsNewStandardProject').addEventListener('click', () => {
      setOpenProjectModal(false);
      setImportModal(true);
    });
    el('closeConfigProject').addEventListener('click', () => setProjectConfigModal(false));
    el('openProjectModal').addEventListener('click', (ev) => {
      if (ev.target === el('openProjectModal')) setOpenProjectModal(false);
    });
    el('projectConfigModal').addEventListener('click', (ev) => {
      if (ev.target === el('projectConfigModal')) setProjectConfigModal(false);
    });
    document.addEventListener('click', (event) => {
      const picker = event.target.closest('[data-picker-target]');
      if (picker) {
        event.preventDefault();
        selectImportPath(picker);
        return;
      }
      const remove = event.target.closest('[data-remove-import-row]');
      if (remove) {
        if (state.importRows.length <= 2) return;
        const rowId = Number(remove.dataset.removeImportRow);
        state.importRows = state.importRows.filter(row => row.id !== rowId);
        invalidateImportCalibrationConfirmations('LIF 通道已删除，旧窗口建议已失效，请重新分析并确认。');
        renderImportLifRows();
        return;
      }
      const removeSegment = event.target.closest('[data-remove-import-segment]');
      if (removeSegment) {
        if (state.importSegments.length <= 1) return;
        const segmentId = Number(removeSegment.dataset.removeImportSegment);
        state.importSegments = state.importSegments.filter(row => row.id !== segmentId);
        state.importSuggestionRevision += 1;
        renderImportSegments();
        return;
      }
      const removeScheduled = event.target.closest('[data-remove-import-scheduled]');
      if (removeScheduled) {
        const scheduledId = Number(removeScheduled.dataset.removeImportScheduled);
        state.importScheduledQcWindows = state.importScheduledQcWindows.filter(row => row.id !== scheduledId);
        renderImportScheduledQcWindows();
        return;
      }
      const removeCfgScheduled = event.target.closest('[data-remove-cfg-scheduled]');
      if (removeCfgScheduled && state.configPostQcDraft) {
        const index = Number(removeCfgScheduled.dataset.removeCfgScheduled);
        state.configPostQcDraft.windows = (state.configPostQcDraft.windows || []).filter((_row, rowIndex) => rowIndex !== index);
        renderConfigPostQcEditor();
      }
    });
    el('importLifRows').addEventListener('input', event => {
      const rowElement = event.target.closest('[data-import-row-id]');
      const field = event.target.dataset.importField;
      if (!rowElement || !field) return;
      const row = state.importRows.find(item => item.id === Number(rowElement.dataset.importRowId));
      if (!row) return;
      const previousValue = row[field];
      row[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
      if (['path', 'channel', 'detector'].includes(field) && row[field] !== previousValue) {
        state.importSuggestionRevision += 1;
        invalidateImportCalibrationConfirmations(
          'LIF 文件、通道或信号颜色已变化，旧窗口建议已失效，请重新分析并确认。',
          false
        );
      }
      if (field === 'channel') {
        const channel = String(row.channel || '').toUpperCase();
        row.detector = channel.startsWith('G') ? 'green' : channel.startsWith('R') ? 'red' : row.detector;
        row.time_axis = automaticTimeAxisForDetector(row.detector);
        renderImportLifRows();
      } else if (field === 'detector') {
        row.time_axis = automaticTimeAxisForDetector(row.detector);
        renderImportLifRows();
      } else if (field === 'use_for_cell_annotation') renderImportLifRows();
      else refreshImportProtocolOptions();
    });
    el('importCalibrationSegments').addEventListener('input', event => {
      const rowElement = event.target.closest('[data-import-segment-id]');
      if (!rowElement) return;
      const segment = state.importSegments.find(item => item.id === Number(rowElement.dataset.importSegmentId));
      if (!segment) return;
      const channel = event.target.dataset.segmentChannel;
      if (channel) {
        state.importSuggestionRevision += 1;
        const selected = new Set(segment.reference_channels);
        if (event.target.checked) selected.add(channel); else selected.delete(channel);
        segment.reference_channels = Array.from(selected);
        segment.boundaries_confirmed = false;
        segment.suggestion_status = '通道已修改，待重新核对';
        const confirmation = rowElement.querySelector('[data-segment-field="boundaries_confirmed"]');
        if (confirmation) confirmation.checked = false;
        return;
      }
      const field = event.target.dataset.segmentField;
      if (field) {
        if (field !== 'boundaries_confirmed') state.importSuggestionRevision += 1;
        segment[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
        if (field === 'start_min' || field === 'end_min') {
          segment.boundaries_confirmed = false;
          segment.suggestion_status = '边界已编辑，待确认';
          const confirmation = rowElement.querySelector('[data-segment-field="boundaries_confirmed"]');
          if (confirmation) confirmation.checked = false;
        } else if (field === 'boundaries_confirmed') {
          segment.suggestion_status = event.target.checked ? '用户已确认' : '待确认';
        }
      }
    });
    el('importScheduledQcWindows').addEventListener('input', event => {
      const rowElement = event.target.closest('[data-import-scheduled-id]');
      if (!rowElement) return;
      const windowRow = state.importScheduledQcWindows.find(item => item.id === Number(rowElement.dataset.importScheduledId));
      if (!windowRow) return;
      const channel = event.target.dataset.scheduledChannel;
      if (channel) {
        const selected = new Set(windowRow.reference_channels);
        if (event.target.checked) selected.add(channel); else selected.delete(channel);
        windowRow.reference_channels = Array.from(selected);
        return;
      }
      const field = event.target.dataset.scheduledField;
      if (field) windowRow[field] = event.target.value;
    });
    el('addImportLif').addEventListener('click', () => {
      if (state.importRows.length >= 4) return;
      state.importRows.push(newImportLifRow());
      invalidateImportCalibrationConfirmations('新增 LIF 通道后，旧窗口建议已失效，请重新分析并确认。');
      renderImportLifRows();
    });
    el('suggestImportWindows').addEventListener('click', suggestImportCalibrationWindows);
    el('addImportSegment').addEventListener('click', () => {
      state.importSegments.push(newImportSegment());
      state.importSuggestionRevision += 1;
      renderImportSegments();
    });
    el('addImportScheduledQc').addEventListener('click', () => {
      state.importScheduledQcWindows.push(newImportScheduledQcWindow());
      renderImportScheduledQcWindows();
    });
    el('importPostQcMode').addEventListener('change', renderImportPostQcControls);
    el('importPostQcChannels').addEventListener('change', () => {
      state.importSignatureChannels = Array.from(el('importPostQcChannels').selectedOptions).map(option => option.value);
    });
    el('cfgCalibrationSegments').addEventListener('input', event => {
      const rowElement = event.target.closest('[data-cfg-segment-index]');
      const field = event.target.dataset.cfgSegmentField;
      if (!rowElement || !field || !state.configProtocolDraft) return;
      const segment = state.configProtocolDraft.segments[Number(rowElement.dataset.cfgSegmentIndex)];
      if (!segment) return;
      segment[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
      if (field === 'start_min' || field === 'end_min') {
        segment.boundaries_confirmed = false;
        const confirmation = rowElement.querySelector('[data-cfg-segment-field="boundaries_confirmed"]');
        if (confirmation) confirmation.checked = false;
      }
      if (field === 'end_min') {
        const values = state.configProtocolDraft.segments.map(row => Number(row.end_min));
        if (values.every(Number.isFinite)) el('cfgQcEnd').value = fmt(Math.max(...values), 1);
      }
    });
    el('cfgPostQcMode').addEventListener('change', () => {
      const mode = el('cfgPostQcMode').value;
      const previous = state.configPostQcDraft || {};
      if (mode === 'disabled') state.configPostQcDraft = { mode: 'disabled', reference_channels: [], windows: [] };
      if (mode === 'signature') state.configPostQcDraft = {
        mode: 'signature',
        reference_channels: previous.reference_channels || [],
        windows: []
      };
      if (mode === 'scheduled_windows') state.configPostQcDraft = {
        mode: 'scheduled_windows',
        reference_channels: [],
        windows: previous.windows || []
      };
      renderConfigPostQcEditor();
    });
    el('cfgPostQcChannels').addEventListener('change', () => {
      if (!state.configPostQcDraft) return;
      state.configPostQcDraft.reference_channels = Array.from(el('cfgPostQcChannels').selectedOptions).map(option => option.value);
    });
    el('cfgAddScheduledQc').addEventListener('click', () => {
      if (!state.configPostQcDraft || state.configPostQcDraft.mode !== 'scheduled_windows') return;
      state.configPostQcDraft.windows = state.configPostQcDraft.windows || [];
      state.configPostQcDraft.windows.push({
        window_id: `post_qc_${state.configPostQcDraft.windows.length + 1}`,
        start_min: '',
        end_min: '',
        reference_channels: []
      });
      renderConfigPostQcEditor();
    });
    el('cfgScheduledQcWindows').addEventListener('input', event => {
      const rowElement = event.target.closest('[data-cfg-scheduled-index]');
      if (!rowElement || !state.configPostQcDraft) return;
      const windowRow = state.configPostQcDraft.windows?.[Number(rowElement.dataset.cfgScheduledIndex)];
      if (!windowRow) return;
      const channel = event.target.dataset.cfgScheduledChannel;
      if (channel) {
        const selected = new Set(windowRow.reference_channels || []);
        if (event.target.checked) selected.add(channel); else selected.delete(channel);
        windowRow.reference_channels = Array.from(selected);
        return;
      }
      const field = event.target.dataset.cfgScheduledField;
      if (field) windowRow[field] = event.target.value;
    });
    el('applyLinLskExample').addEventListener('click', () => {
      state.importSuggestionRevision += 1;
      state.importRows = [
        newImportLifRow({ channel: 'G1', identity_prior: 'LSK', detector: 'green', use_for_cell_annotation: true }),
        newImportLifRow({ channel: 'G2', identity_prior: 'Lin−', detector: 'green', use_for_cell_annotation: true }),
      ];
      state.importSegments = [
        newImportSegment({ segment_id: 'lsk_reference', population_label: 'LSK', reference_channels: ['G1'] }),
        newImportSegment({ segment_id: 'lin_reference', population_label: 'Lin−', reference_channels: ['G2'] }),
      ];
      state.importScheduledQcWindows = [];
      state.importSignatureChannels = [];
      el('importAnnotationStart').value = '24';
      el('importSeedWindow').value = '2.5';
      el('importPostQcMode').value = 'disabled';
      renderImportLifRows();
      renderImportSegments();
      renderImportPostQcControls();
      el('importSuggestionStatus').textContent = '选择 G1/G2 原始文件后，可点击“分析已选 LIF 并建议窗口”；建议不会自动确认。';
      el('importHint').textContent = '已应用 Lin− / LSK 示例角色；G1/G2 将自动共享绿色信号时间轴。请选择原始文件，并根据本项目峰形核对、确认两个参考段边界。';
    });
    document.querySelectorAll('[data-event-filter]').forEach(button => {
      button.addEventListener('click', () => {
        state.eventFilter = button.dataset.eventFilter;
        state.selectedCandidateId = null;
        renderCurrentState();
      });
    });
    document.querySelectorAll('[data-manual-kind]').forEach(button => {
      button.addEventListener('click', () => {
        state.manualAnnotationKind = button.dataset.manualKind;
        resetManualSelection();
        renderCurrentState();
      });
    });
    el('runImportProject').addEventListener('click', importProject);
    el('runOpenProject').addEventListener('click', openExistingProject);
    el('acceptWindow').addEventListener('click', acceptWindowPendingAutoCandidates);
    el('saveConfig').addEventListener('click', saveProjectConfig);
    el('attachMap').addEventListener('click', attachCellEventMap);
    el('attachCellEventMap').addEventListener('input', updateAttachMapControls);
    el('previewQcRefit').addEventListener('click', previewQcAlignmentRefit);
    el('applyQcRefit').addEventListener('click', applyQcAlignmentRefit);
    el('estimateDelta').addEventListener('click', estimateLocalDelta);
    el('freezeDelta').addEventListener('click', freezeLocalDelta);
    el('deltaMinus').addEventListener('click', () => updateDeltaPreview(Number(el('deltaSlider').value || 0) - 0.25));
    el('deltaPlus').addEventListener('click', () => updateDeltaPreview(Number(el('deltaSlider').value || 0) + 0.25));
    el('deltaSlider').addEventListener('change', () => updateDeltaPreview(Number(el('deltaSlider').value || 0)));
    document.addEventListener('click', hideLineContextMenu);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') {
        if (activeModal) {
          ev.preventDefault();
          closeActiveModal();
          return;
        }
        hideLineContextMenu();
        return;
      }
      if (ev.key !== 'Tab' || !activeModal) return;
      const focusable = modalFocusableElements(activeModal);
      if (!focusable.length) {
        ev.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
      }
    });
    window.addEventListener('scroll', hideLineContextMenu, true);
    window.addEventListener('resize', () => { hideLineContextMenu(); if (state.current) draw(); });
    if (stateChannel) {
      stateChannel.addEventListener('message', async event => {
        const message = event.data || {};
        if (message.type !== 'focus-event') return;
        if (String(message.project_id || '') !== String(state.meta?.project_id || '')) return;
        if (String(message.map_sha256 || '') !== String(state.meta?.cell_event_map?.sha256 || '')) return;
        const eventTime = Number(message.scan_start_time);
        if (!Number.isFinite(eventTime)) return;
        if (!calibrationBoundariesConfirmed()) {
          alert('参考段边界尚未全部确认；当前草稿只能浏览原始峰形，暂不能从 UMAP 跳转到事件标注。');
          return;
        }
        state.stage = 'event_annotation';
        state.eventFilter = 'all';
        state.timeMode = 'aligned';
        applyStageWindowWidth();
        state.start = eventGridWindowStart(eventTime);
        el('timeMode').value = state.timeMode;
        await loadWindow();
        const matching = candidateRows().find(row => String(row.ms_event_id || '') === String(message.ms_event_id || ''));
        state.selectedCandidateId = matching ? rowId(matching) : null;
        renderCandidateList();
        draw();
      });
    }
    init().catch(err => {
      alert(`页面加载失败: ${err.message}`);
      console.error(err);
    });
  </script>
</body>
</html>
"""


class AnnotationHandler(BaseHTTPRequestHandler):
    data: AppData | BootstrapAppData
    path_dialog: Callable[..., dict[str, Any]] = staticmethod(choose_native_path)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def expected_origins(self) -> set[str]:
        host, port = self.server.server_address[:2]
        return {f"http://{host}:{port}", f"http://localhost:{port}"}

    def request_is_local(self) -> bool:
        allowed_origins = self.expected_origins()
        allowed_hosts = {urlparse(origin).netloc for origin in allowed_origins}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        return host in allowed_hosts and (not origin or origin in allowed_origins)

    def send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_security_headers()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, payload: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/html") -> None:
        raw = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_security_headers()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_csv_download(self, payload: str, filename: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = payload.encode("utf-8-sig")
        ascii_filename = re.sub(r'[^A-Za-z0-9._ -]+', "_", filename).strip(" ._") or "accepted_annotations.csv"
        self.send_response(status)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_security_headers()
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("X-Export-Filename", quote(filename))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 100_000:
            raise BadRequest("Request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BadRequest("Invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise BadRequest("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        if not self.request_is_local():
            self.send_json({"error": "该请求不是从本机应用发出的，已安全阻止。"}, HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_text(HTML)
                return
            if parsed.path == "/umap":
                self.send_text(UMAP_HTML)
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/api/meta":
                self.send_json(self.data.meta())
                return
            if parsed.path == "/api/cell-event-map":
                if not isinstance(self.data, AppData):
                    raise BadRequest("请先打开项目")
                self.send_json(self.data.projected_cell_event_map_state())
                return
            if parsed.path == "/api/cell-event-map-revision":
                if not isinstance(self.data, AppData):
                    raise BadRequest("请先打开项目")
                self.send_json(self.data.cell_event_map_revision())
                return
            if parsed.path == "/api/window":
                query = parse_qs(parsed.query)
                try:
                    start_min = float(query.get("start_min", ["0"])[0])
                    window_min = float(query.get("window_min", [str(DEFAULT_WINDOW_MIN)])[0])
                    preview_value = query.get("preview_ms_delta_sec", [None])[0]
                    preview_ms_delta_sec = None if preview_value in {None, ""} else float(preview_value)
                except ValueError as exc:
                    raise BadRequest("start_min, window_min, and preview_ms_delta_sec must be numeric") from exc
                time_mode = str(query.get("time_mode", ["aligned"])[0])
                lif_signal_mode = str(query.get("lif_signal_mode", [DEFAULT_LIF_SIGNAL_MODE])[0])
                include_weak_lif_peaks = str(
                    query.get("include_weak_lif_peaks", ["false"])[0]
                ).strip().lower() in {"1", "true", "yes", "on"}
                self.send_json(
                    self.data.window(
                        start_min=start_min,
                        window_min=window_min,
                        time_mode=time_mode,
                        preview_ms_delta_sec=preview_ms_delta_sec,
                        lif_signal_mode=lif_signal_mode,
                        include_weak_lif_peaks=include_weak_lif_peaks,
                    )
                )
                return
            if parsed.path == "/api/project-config":
                self.send_json({"project_config": self.data.project_config(), "time_model": self.data.active_time_model()})
                return
            if parsed.path == "/api/local-delta-preview":
                query = parse_qs(parsed.query)
                delta_value = query.get("delta_sec", [None])[0]
                delta = None if delta_value in {None, ""} else float(delta_value)
                self.send_json(self.data.local_delta_preview(delta))
                return
            if parsed.path == "/api/annotations":
                self.send_json({"summary": self.data.store.summary(), "records": self.data.store.records()})
                return
            self.send_json({"error": "未找到请求的页面或操作。"}, HTTPStatus.NOT_FOUND)
        except BadRequest as exc:
            self.send_json(
                {"error": user_facing_error_message(exc)},
                HTTPStatus.BAD_REQUEST,
            )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            LOGGER.info("Client disconnected before GET response completed: %s", parsed.path)
        except Exception as exc:
            LOGGER.exception("Unhandled GET request failure for %s", parsed.path)
            self.send_json(
                {"error": user_facing_error_message(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        if not self.request_is_local():
            self.send_json({"error": "该请求不是从本机应用发出的，已安全阻止。"}, HTTPStatus.FORBIDDEN)
            return
        activity = getattr(self.server, "request_activity", None)
        if activity is None:
            self._do_POST()
            return
        with activity.track(urlparse(self.path).path):
            self._do_POST()

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if self.headers.get("X-Annotation-Write-Token") != WRITE_TOKEN:
                raise BadRequest("Missing or invalid annotation write token")
            payload = self.read_json()
            if parsed.path == "/api/review":
                annotation_id = str(payload.get("annotation_id", ""))
                review_status = str(payload.get("review_status", ""))
                if not annotation_id:
                    raise BadRequest("annotation_id is required")
                if review_status not in REVIEW_STATUSES:
                    raise BadRequest(f"review_status must be one of {sorted(REVIEW_STATUSES)}")
                row = self.data.review_annotation(
                    annotation_id,
                    review_status,
                    window_start_min=clean_value(payload.get("window_start_min")),
                    window_end_min=clean_value(payload.get("window_end_min")),
                    time_mode=str(payload.get("time_mode", "")) or None,
                    clear_qc_alignment_model=bool(payload.get("clear_qc_alignment_model")),
                )
                self.send_json({"ok": True, "annotation": row, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/project-config":
                config = self.data.update_project_config(payload)
                self.send_json(
                    {
                        "ok": True,
                        "project_config": config,
                        "time_model": getattr(self.data, "_project_config_update_time_model", {"status": "unavailable"}),
                        "warning": str(getattr(self.data, "_project_config_update_warning", "")),
                    }
                )
                return
            if parsed.path == "/api/qc-alignment-refit-preview":
                self.send_json({"ok": True, "preview": self.data.qc_alignment_refit_preview()})
                return
            if parsed.path == "/api/qc-alignment-refit":
                result = self.data.save_qc_alignment_refit(
                    str(payload.get("preview_hash") or ""),
                    clear_frozen_time_model=bool(payload.get("clear_frozen_time_model")),
                )
                self.send_json({"ok": True, **result})
                return
            if parsed.path == "/api/estimate-local-delta":
                model = self.data.estimate_local_delta_model()
                self.send_json({"ok": True, "time_model": model, "preview": self.data.local_delta_preview()})
                return
            if parsed.path == "/api/estimate-local-delta-preview":
                self.send_json({"ok": True, "time_model": self.data.active_time_model(), "preview": self.data.estimate_local_delta_preview()})
                return
            if parsed.path == "/api/local-delta-draft":
                try:
                    delta_sec = float(payload.get("delta_sec", 0.0))
                except (TypeError, ValueError) as exc:
                    raise BadRequest("delta_sec must be numeric") from exc
                model = self.data.update_local_delta_draft(delta_sec)
                self.send_json({"ok": True, "time_model": model, "preview": self.data.local_delta_preview()})
                return
            if parsed.path == "/api/freeze-local-delta":
                model = self.data.freeze_local_delta_model()
                self.send_json({"ok": True, "time_model": model, "preview": self.data.local_delta_preview()})
                return
            if parsed.path == "/api/manual-triplet":
                anchor_map_payload = payload.get("lif_anchor_peak_ids")
                anchor_map = None
                if isinstance(anchor_map_payload, dict):
                    anchor_map = {
                        str(channel).strip().upper(): optional_peak_id(peak_id)
                        for channel, peak_id in anchor_map_payload.items()
                        if str(channel).strip()
                    }
                row = self.data.create_manual_triplet(
                    optional_peak_id(payload.get("anchor_a_peak_id")) or optional_peak_id(payload.get("g2_peak_id")),
                    optional_peak_id(payload.get("anchor_b_peak_id")) or optional_peak_id(payload.get("r1_peak_id")),
                    optional_peak_id(payload.get("ms_event_id")) or "",
                    stage=str(payload.get("stage", "qc_calibration")),
                    window_start_min=clean_value(payload.get("window_start_min")),
                    window_end_min=clean_value(payload.get("window_end_min")),
                    time_mode=str(payload.get("time_mode", "")) or None,
                    lif_anchor_peak_ids=anchor_map,
                    calibration_segment_id=(
                        str(payload.get("calibration_segment_id") or "") or None
                    ),
                    post_qc_window_id=(
                        str(payload.get("post_qc_window_id") or "") or None
                    ),
                    clear_qc_alignment_model=bool(payload.get("clear_qc_alignment_model")),
                )
                self.send_json({"ok": True, "annotation": row, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/manual-cell-pair":
                row = self.data.create_manual_cell_pair(
                    str(payload.get("lif_channel", "")),
                    optional_peak_id(payload.get("lif_peak_id")) or "",
                    optional_peak_id(payload.get("ms_event_id")) or "",
                    window_start_min=clean_value(payload.get("window_start_min")),
                    window_end_min=clean_value(payload.get("window_end_min")),
                    time_mode=str(payload.get("time_mode", "")) or None,
                )
                self.send_json({"ok": True, "annotation": row, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/clear-manual":
                annotation_id = str(payload.get("annotation_id", ""))
                if not annotation_id:
                    raise BadRequest("annotation_id is required")
                row = self.data.clear_manual_annotation(
                    annotation_id,
                    clear_qc_alignment_model=bool(payload.get("clear_qc_alignment_model")),
                )
                self.send_json({"ok": True, "cleared": row, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/accept-window":
                try:
                    start_min = float(payload.get("start_min", 0.0))
                    window_min = float(payload.get("window_min", DEFAULT_WINDOW_MIN))
                except (TypeError, ValueError) as exc:
                    raise BadRequest("start_min and window_min must be numeric") from exc
                time_mode = str(payload.get("time_mode", "aligned"))
                stage = str(payload.get("stage", "qc_calibration"))
                result = self.data.accept_pending_auto_candidates_in_window(
                    start_min=start_min,
                    window_min=window_min,
                    time_mode=time_mode,
                    stage=stage,
                    clear_qc_alignment_model=bool(payload.get("clear_qc_alignment_model")),
                )
                self.send_json({"ok": True, "result": result, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/export-accepted-csv":
                result = self.data.export_accepted_annotations_csv()
                self.send_csv_download(result["csv_text"], result["filename"])
                return
            if parsed.path == "/api/select-path":
                kind = str(payload.get("kind", "")).strip()
                result = self.__class__.path_dialog(
                    kind=kind,
                    title=str(payload.get("title", "")).strip(),
                    initial_dir=str(payload.get("initial_dir", "")).strip(),
                    file_role=str(payload.get("file_role", "")).strip(),
                )
                self.send_json(result)
                return
            if parsed.path == "/api/attach-cell-event-map":
                if not isinstance(self.data, AppData):
                    raise BadRequest("请先打开项目")
                source_path = str(payload.get("source_path", "")).strip()
                if not source_path:
                    raise BadRequest("source_path is required")
                new_data = self.data.attach_cell_event_map(Path(source_path))
                self.__class__.data = new_data
                self.send_json(
                    {
                        "ok": True,
                        "meta": new_data.meta(),
                        "cell_event_map": new_data.cell_event_map_revision(),
                    }
                )
                return
            if parsed.path == "/api/suggest-calibration-windows":
                lif_inputs_payload = payload.get("lif_inputs")
                segments_payload = payload.get("segments")
                if not isinstance(lif_inputs_payload, list):
                    raise BadRequest("lif_inputs must be a list")
                if not isinstance(segments_payload, list):
                    raise BadRequest("segments must be a list")
                try:
                    annotation_start_min = float(payload.get("annotation_start_min"))
                except (TypeError, ValueError) as exc:
                    raise BadRequest("annotation_start_min must be numeric") from exc
                result = suggest_calibration_windows_from_raw_inputs(
                    lif_inputs_payload,
                    segments_payload,
                    annotation_start_min=annotation_start_min,
                )
                self.send_json({"ok": True, **result})
                return
            if parsed.path == "/api/import-project":
                lif_inputs_payload = payload.get("lif_inputs")
                uses_dynamic_lif_inputs = isinstance(lif_inputs_payload, list) and bool(lif_inputs_payload)
                if uses_dynamic_lif_inputs:
                    calibration_protocol_payload = payload.get("calibration_protocol")
                    anchor_channels_payload = payload.get("qc_anchor_channels")
                    if not isinstance(calibration_protocol_payload, dict) and (
                        not isinstance(anchor_channels_payload, list) or not anchor_channels_payload
                    ):
                        raise BadRequest("新项目必须明确提供 calibration_protocol")
                    required = ["project_dir", "ms_path", "cell_event_map_path"]
                else:
                    required = [
                        "project_dir",
                        "lif_g2_path",
                        "lif_r1_path",
                        "lif_r2_path",
                        "ms_path",
                        "cell_event_map_path",
                    ]
                missing = [key for key in required if not str(payload.get(key, "")).strip()]
                if missing:
                    raise BadRequest(f"缺少导入路径字段: {', '.join(missing)}")
                if uses_dynamic_lif_inputs:
                    lif_inputs = []
                    for index, item in enumerate(lif_inputs_payload, start=1):
                        if not isinstance(item, dict):
                            raise BadRequest("lif_inputs entries must be objects")
                        lif_inputs.append(
                            {
                                "key": str(item.get("key") or f"lif_{index}"),
                                "path": Path(str(item.get("path", ""))),
                                "channel": str(item.get("channel", "")),
                                "identity_prior": str(item.get("identity_prior", "")),
                                "time_axis": str(item.get("time_axis", "")),
                                "detector": str(item.get("detector", "")),
                                "use_for_cell_annotation": bool(
                                    item.get("use_for_cell_annotation")
                                ),
                            }
                        )
                    new_data = AppData.create_project_from_raw_inputs(
                        project_dir=Path(str(payload["project_dir"])),
                        ms_path=Path(str(payload["ms_path"])),
                        raw_input_mode=str(payload.get("raw_input_mode", RAW_INPUT_MODE_EXTERNAL)),
                        lif_inputs=lif_inputs,
                        qc_anchor_channels=(
                            list(anchor_channels_payload)
                            if isinstance(anchor_channels_payload, list)
                            else None
                        ),
                        calibration_protocol=(
                            calibration_protocol_payload
                            if isinstance(calibration_protocol_payload, dict)
                            else None
                        ),
                        post_qc_strategy=(
                            payload.get("post_qc_strategy")
                            if isinstance(payload.get("post_qc_strategy"), dict)
                            else None
                        ),
                        lif_peak_detection=(
                            payload.get("lif_peak_detection")
                            if isinstance(payload.get("lif_peak_detection"), dict)
                            else None
                        ),
                        annotation_start_min=(
                            float(payload.get("annotation_start_min"))
                            if payload.get("annotation_start_min") is not None
                            else None
                        ),
                        local_delta_seed_window_min=float(
                            payload.get(
                                "local_delta_seed_window_min",
                                DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN,
                            )
                        ),
                        cell_event_map_path=Path(str(payload["cell_event_map_path"])),
                    )
                else:
                    new_data = AppData.create_project_from_raw_inputs(
                        project_dir=Path(str(payload["project_dir"])),
                        lif_g2_path=Path(str(payload["lif_g2_path"])),
                        lif_r1_path=Path(str(payload["lif_r1_path"])),
                        lif_r2_path=Path(str(payload["lif_r2_path"])),
                        ms_path=Path(str(payload["ms_path"])),
                        g2_identity=str(payload.get("g2_identity", "Day0")),
                        r1_identity=str(payload.get("r1_identity", "Day9")),
                        r2_identity=str(payload.get("r2_identity", "Day3")),
                        raw_input_mode=str(payload.get("raw_input_mode", RAW_INPUT_MODE_EXTERNAL)),
                        lif_peak_detection=(
                            payload.get("lif_peak_detection")
                            if isinstance(payload.get("lif_peak_detection"), dict)
                            else None
                        ),
                        cell_event_map_path=Path(str(payload["cell_event_map_path"])),
                    )
                self.__class__.data = new_data
                self.send_json({"ok": True, "meta": new_data.meta()})
                return
            if parsed.path == "/api/open-project":
                project_dir = str(payload.get("project_dir", "")).strip()
                if not project_dir:
                    raise BadRequest("project_dir is required")
                project = ProjectPaths.from_args(project_dir=project_dir)
                new_data = AppData.load(project)
                self.__class__.data = new_data
                self.send_json({"ok": True, "meta": new_data.meta()})
                return
            self.send_json({"error": "未找到请求的页面或操作。"}, HTTPStatus.NOT_FOUND)
        except BadRequest as exc:
            self.send_json(
                {"error": user_facing_error_message(exc)},
                HTTPStatus.BAD_REQUEST,
            )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            LOGGER.info("Client disconnected before POST response completed: %s", parsed.path)
        except Exception as exc:
            LOGGER.exception("Unhandled POST request failure for %s", parsed.path)
            self.send_json(
                {"error": user_facing_error_message(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


class RequestActivity:
    """Tracks active write requests so the desktop shell can block unsafe exit."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_paths: dict[str, int] = {}

    @contextlib.contextmanager
    def track(self, path: str):
        with self._lock:
            self._active_paths[path] = self._active_paths.get(path, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._active_paths.get(path, 1) - 1
                if remaining > 0:
                    self._active_paths[path] = remaining
                else:
                    self._active_paths.pop(path, None)

    def active_paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active_paths))

    def is_busy(self) -> bool:
        return bool(self.active_paths())


class LocalHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[AnnotationHandler]) -> None:
        self.request_activity = RequestActivity()
        super().__init__(server_address, handler_class)


def initial_app_data(
    project: ProjectPaths,
    *,
    project_selected: bool,
) -> AppData | BootstrapAppData:
    if not project_selected:
        LOGGER.info("No project loaded; starting at the project selection screen")
        return BootstrapAppData(project=project, load_error="", project_selected=False)
    try:
        return AppData.load(project)
    except FileNotFoundError as exc:
        LOGGER.warning("Preprocessing inputs are not loaded yet: %s", exc)
        return BootstrapAppData(project=project, load_error=str(exc), project_selected=True)


def create_http_server(
    host: str,
    port: int,
    data: AppData | BootstrapAppData,
    *,
    path_dialog: Callable[..., dict[str, Any]] = choose_native_path,
) -> LocalHTTPServer:
    class BoundAnnotationHandler(AnnotationHandler):
        pass

    BoundAnnotationHandler.data = data
    BoundAnnotationHandler.path_dialog = staticmethod(path_dialog)
    return LocalHTTPServer((host, port), BoundAnnotationHandler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"运行本地 {APP_DISPLAY_NAME} 浏览器应用。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--project-dir", default=None, help="项目根目录；默认使用当前 scMetab 仓库。")
    parser.add_argument("--raw-data-dir", default=None, help="原始数据目录；当前 MVP 仅记录路径，不直接读取作者 CSV/h5ad。")
    parser.add_argument("--annotation-db", default=None, help="人工标注 SQLite 路径；默认 annotation_app/annotations/annotation.sqlite。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_was_selected = any([args.project_dir, args.raw_data_dir, args.annotation_db])
    project = ProjectPaths.from_args(
        project_dir=args.project_dir,
        raw_data_dir=args.raw_data_dir,
        annotation_db=args.annotation_db,
    )
    data = initial_app_data(project, project_selected=project_was_selected)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "WARNING: this MVP has no authentication. Prefer --host 127.0.0.1; "
            f"current host {args.host!r} may expose data beyond this machine.",
            flush=True,
        )
    server = create_http_server(args.host, args.port, data)
    if isinstance(data, AppData):
        print(f"Loaded annotation preprocessing inputs from {project.project_dir}")
        print(f"Annotation DB: {project.annotation_db_path}")
    print(f"Open http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
