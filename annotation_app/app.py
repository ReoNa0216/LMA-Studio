#!/usr/bin/env python3
"""Local browser application for human-assisted LIF-MS annotation.

The app loads first-principles preprocessing tables, slices synchronized LIF/MS
windows, records human review decisions in SQLite, and exports accepted
annotations. It deliberately does not read author CSV, h5ad, manual labels, or
V2 outputs for candidate generation or export.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import numpy as np
import pandas as pd


IS_FROZEN = bool(getattr(sys, "frozen", False))


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
APP_VERSION = "annotation_app_mvp1_review_sqlite"
APP_DISPLAY_NAME = "LMA Studio"

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
LOCAL_DELTA_SEARCH_MIN_SEC = -20.0
LOCAL_DELTA_SEARCH_MAX_SEC = 20.0
LOCAL_DELTA_SEARCH_STEP_SEC = 0.25
LOCAL_DELTA_MATCH_TOL_SEC = 1.50
LOCAL_DELTA_MAX_ABS_SEC = 20.0
LOCAL_DELTA_ABS_PRIOR_WEIGHT = 0.50
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
PROJECT_SCHEMA_VERSION = 1
ACQUISITION_LAYOUT_VERSION = 1
RAW_INPUT_MODE_COPY = "copy_into_project"
RAW_INPUT_MODE_EXTERNAL = "external_reference"
RAW_INPUT_MODES = {RAW_INPUT_MODE_COPY, RAW_INPUT_MODE_EXTERNAL}
REQUIRED_INTERMEDIATE_TABLES = {
    "lif_traces": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet",
    "lif_peaks": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet",
    "ms_events": "data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet",
    "ms_scan_summary": "data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet",
}
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
) -> dict[str, Any]:
    identities = identities or {}
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
                    "detector": str(item.get("detector") or detector_from_time_axis(time_axis)),
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
    if len(lif_channels) != 3:
        raise BadRequest("项目必须配置正好 3 个 LIF 通道")
    channels = [row["channel"] for row in lif_channels]
    if len(set(channels)) != len(channels):
        raise BadRequest("LIF 通道名不能重复")
    anchors = list(qc_anchor_channels or ((layout or {}).get("qc_anchor_channels") if isinstance(layout, dict) else []) or ["G2", "R1"])
    anchors = [str(ch).strip().upper() for ch in anchors if str(ch).strip()]
    if len(anchors) != 2 or len(set(anchors)) != 2:
        raise BadRequest("QC anchor 必须选择两个不同的 LIF 通道")
    missing = [ch for ch in anchors if ch not in channels]
    if missing:
        raise BadRequest(f"QC anchor 通道不在 LIF 通道配置中: {', '.join(missing)}")
    axis_by_channel = {row["channel"]: row["time_axis"] for row in lif_channels}
    return {
        "layout_version": ACQUISITION_LAYOUT_VERSION,
        "lif_channels": lif_channels,
        "qc_anchor_channels": anchors,
        "channel_time_axes": axis_by_channel,
    }


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


def write_project_manifest(
    *,
    project_dir: Path,
    raw_input_mode: str,
    raw_inputs: dict[str, dict[str, Any]],
    channel_identity_prior: dict[str, str],
    intermediate_tables: dict[str, dict[str, Any]] | None = None,
    acquisition_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    binding = project_table_binding(intermediate_tables) if intermediate_tables else {}
    layout = normalize_acquisition_layout(acquisition_layout, identities=channel_identity_prior)
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
        "intermediate_tables": intermediate_tables or {},
        "project_table_binding": binding,
        "annotation_db": {
            "path": "annotation_app/annotations/annotation.sqlite",
            "schema_version": PROJECT_SCHEMA_VERSION,
        },
        "channel_identity_prior": prior_values,
        "updated_at": now_iso(),
    }
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    mode = normalize_raw_input_mode(raw_input_mode)
    if lif_inputs is None:
        lif_inputs = [
            {"key": "lif_g2", "path": raw_paths["lif_g2"], "channel": "G2", "identity_prior": identities.get("G2", "Day0")},
            {"key": "lif_r1", "path": raw_paths["lif_r1"], "channel": "R1", "identity_prior": identities.get("R1", "Day9")},
            {"key": "lif_r2", "path": raw_paths["lif_r2"], "channel": "R2", "identity_prior": identities.get("R2", "Day3")},
        ]
    layout_input = {
        "lif_channels": [
            {
                "input_id": f"{str(item.get('key') or f'lif_{idx}').strip()}_raw",
                "channel": str(item.get("channel") or "").strip().upper(),
                "identity_prior": str(item.get("identity_prior") or identities.get(str(item.get("channel") or "").strip().upper(), "")),
                "time_axis": str(item.get("time_axis") or default_time_axis_for_channel(str(item.get("channel") or ""))),
            }
            for idx, item in enumerate(lif_inputs, start=1)
        ],
        "qc_anchor_channels": list(qc_anchor_channels or ["G2", "R1"]),
    }
    layout = normalize_acquisition_layout(layout_input, identities=identities)
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
                "LIF trace physical QC and peak calling",
                ".csv",
                Path(item["path"]),
            )
        )
    specs.append(("ms", "ms_raw_txt", "raw_ms_spectra", "", "", "MS", "MS trace physical QC and event calling", ".txt", raw_paths["ms"]))
    rows: list[dict[str, Any]] = []
    manifest_inputs: dict[str, dict[str, Any]] = {}
    for key, input_id, input_class, channel, label, detector, role, default_suffix, raw_path in specs:
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
                "encoding_or_format": "ASCII mzML-like text export" if key == "ms" else "UTF-16 LE tab-delimited text",
            }
        )
        manifest_inputs[key] = {
            "path": path_value,
            "path_mode": mode,
            "original_source_path": str(source_path.resolve()),
        }
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


def candidate_id_for_group(group: dict[str, Any]) -> str:
    return f"auto_qc:{group['g2_peak_id']}:{group['r1_peak_id']}:{group['ms_event_id']}"


def post_qc_candidate_id(group: dict[str, Any]) -> str:
    return f"post_qc:{group['g2_peak_id']}:{group['r1_peak_id']}:{group['ms_event_id']}"


def cell_candidate_id(row: dict[str, Any]) -> str:
    return f"cell:{row['lif_channel']}:{row['lif_peak_id']}:{row['ms_event_id']}"


def manual_annotation_id(g2_peak_id: str | None, r1_peak_id: str | None, ms_event_id: str) -> str:
    g2_key = g2_peak_id or MISSING_PEAK_SYMBOL
    r1_key = r1_peak_id or MISSING_PEAK_SYMBOL
    digest = hashlib.sha1(f"{g2_key}|{r1_key}|{ms_event_id}".encode("utf-8")).hexdigest()[:10]
    return f"manual_qc:{digest}"


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


def load_preprocessing_runner(script_name: str):
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


def manifest_entry_path(project_dir: Path, entry: dict[str, Any]) -> Path:
    raw_path = str(entry.get("path", "")).strip()
    if not raw_path:
        raise BadRequest(f"{PROJECT_MANIFEST_FILENAME} contains an entry without path")
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (project_dir / path).resolve()


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


def validate_project_manifest_against_files(project_dir: Path, manifest: dict[str, Any] | None) -> None:
    if not manifest:
        return
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
        raw_db_path = Path(str(annotation_db["path"])).expanduser()
        annotation_db_path = raw_db_path.resolve() if raw_db_path.is_absolute() else (project.project_dir / raw_db_path).resolve()
    return replace(
        project,
        annotation_db_path=annotation_db_path,
        lif_traces_path=manifest_entry_path(project.project_dir, tables["lif_traces"]),
        lif_peaks_path=manifest_entry_path(project.project_dir, tables["lif_peaks"]),
        ms_events_path=manifest_entry_path(project.project_dir, tables["ms_events"]),
        ms_scan_path=manifest_entry_path(project.project_dir, tables["ms_scan_summary"]),
    )


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
        "schema_version": PROJECT_SCHEMA_VERSION,
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
                for key in ["lif_peak_id", "g2_peak_id", "r1_peak_id", "r2_peak_id"]:
                    value = optional_peak_id(payload.get(key))
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

    def __init__(self, db_path: Path = DEFAULT_ANNOTATION_DB_PATH) -> None:
        self.db_path = db_path
        self.legacy_state_path = self.db_path.parent / "annotation_state.json"
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_legacy_json_if_needed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

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

    def record_input_manifest(self, paths: dict[str, Path]) -> None:
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
                        display_path(path),
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

    def update_project_config(self, updates: dict[str, Any], *, clear_frozen_time_model: bool = False) -> dict[str, Any]:
        allowed = {
            "qc_calibration_end_min",
            "annotation_start_min",
            "local_delta_seed_window_min",
        }
        current_config = self.project_config()
        cleaned: dict[str, float] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise BadRequest(f"{key} must be numeric") from exc
            if not math.isfinite(number) or number < 0:
                raise BadRequest(f"{key} must be a finite non-negative number")
            cleaned[key] = number
        if not cleaned:
            return current_config
        proposed_config = {**current_config, **cleaned}
        if float(proposed_config["annotation_start_min"]) < float(proposed_config["qc_calibration_end_min"]):
            raise BadRequest("annotation_start_min must be >= qc_calibration_end_min")
        frozen = self.active_time_model()
        sensitive_changes = {
            key: {"old": float(current_config.get(key, 0.0)), "new": float(proposed_config[key])}
            for key in TIME_MODEL_CONFIG_KEYS
            if key in cleaned and abs(float(current_config.get(key, 0.0)) - float(proposed_config[key])) > 1e-9
        }
        clear_active_frozen = bool(frozen and frozen.get("status") == "frozen" and sensitive_changes)
        if clear_active_frozen and not clear_frozen_time_model:
            raise BadRequest("修改 QC/后段时间节点会清除当前已冻结 time model；请确认后重新进行后段局部校正")
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if clear_active_frozen:
                conn.execute(
                    "UPDATE time_models SET is_active = 0, updated_at = ? WHERE is_active = 1 AND status = 'frozen'",
                    (timestamp,),
                )
                self._insert_time_model_audit_row(
                    conn,
                    action="clear_frozen_time_model_for_project_config_update",
                    time_model_version=str(frozen.get("time_model_version")),
                    payload={"updates": cleaned, "sensitive_changes": sensitive_changes, "previous_time_model": frozen},
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
                    "cleared_frozen_time_model": clear_active_frozen,
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

    def ensure_draft_time_model(self, base_model_name: str) -> dict[str, Any]:
        existing = self.active_time_model()
        if existing:
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
        }
        return self.upsert_time_model(payload, action="default_draft_time_model")

    def upsert_time_model(self, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
        timestamp = now_iso()
        version = str(payload.get("time_model_version") or f"tm_{uuid.uuid4().hex[:12]}")
        status = str(payload.get("status", "draft"))
        if status not in {"draft", "frozen", "exploratory"}:
            raise BadRequest("time model status must be draft, frozen, or exploratory")
        if bool(payload.get("contains_cell_labels", False)):
            raise BadRequest("local delta time model must not contain cell labels")
        row = {
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
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
        return self.active_time_model() or row

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
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)).fetchone()
            return self._decode_annotation_row(row) if row else None

    def records(self) -> list[dict[str, Any]]:
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

    def hard_delete_manual(self, annotation_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM annotations WHERE annotation_id = ?", (annotation_id,)).fetchone()
            if not row:
                return {"annotation_id": annotation_id, "deleted": False, "reason": "not_found"}
            current = self._decode_annotation_row(row)
            if current.get("source") != "manual_created":
                raise BadRequest("Only manual_created annotations can be cleared")
            conn.execute("DELETE FROM annotations WHERE annotation_id = ?", (annotation_id,))
            conn.execute("DELETE FROM audit_events WHERE annotation_id = ?", (annotation_id,))
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
    ) -> dict[str, Any]:
        if review_status not in REVIEW_STATUSES:
            raise BadRequest(f"review_status must be one of {sorted(REVIEW_STATUSES)}")
        if source not in ANNOTATION_SOURCES:
            raise BadRequest(f"source must be one of {sorted(ANNOTATION_SOURCES)}")
        timestamp = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
        return current or {
            "annotation_id": annotation_id,
            "source": source,
            "review_status": "pending",
            "exportable": False,
        }


class BadRequest(ValueError):
    pass


def display_phase_from_time_min(time_min: pd.Series | np.ndarray) -> np.ndarray:
    t = np.asarray(time_min, dtype=float)
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
    pairs: list[tuple[float, int, int, float]] = []
    shifted = lif_times_sec + shift_sec
    for lif_idx, value in enumerate(shifted):
        left = int(np.searchsorted(ms_times_sec, value - tolerance_sec, side="left"))
        right = int(np.searchsorted(ms_times_sec, value + tolerance_sec, side="right"))
        for ms_idx in range(left, right):
            residual = float(ms_times_sec[ms_idx] - value)
            pairs.append((abs(residual), lif_idx, ms_idx, residual))
    pairs.sort(key=lambda item: (item[0], item[1], item[2]))

    used_lif: set[int] = set()
    used_ms: set[int] = set()
    out: list[tuple[int, int, float]] = []
    for _, lif_idx, ms_idx, residual in pairs:
        if lif_idx in used_lif or ms_idx in used_ms:
            continue
        used_lif.add(lif_idx)
        used_ms.add(ms_idx)
        out.append((lif_idx, ms_idx, residual))
    out.sort(key=lambda item: lif_times_sec[item[0]])
    return out


def estimate_channel_shift(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    channel: str,
    qc_calibration_end_min: float = QC_SHIFT_WINDOW_MIN,
) -> dict[str, Any]:
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
) -> list[dict[str, Any]]:
    """Match dense QC components without letting weak near-neighbors steal anchors."""
    ms_times = ms["time_sec"].to_numpy(float)
    pair_to_ms: dict[int, set[int]] = {}
    ms_to_pair: dict[int, set[int]] = {}
    for pair_idx, row in enumerate(pair_rows):
        composite = float(row[4])
        left = int(np.searchsorted(ms_times, composite - QC_GROUP_MATCH_TOL_SEC, side="left"))
        right = int(np.searchsorted(ms_times, composite + QC_GROUP_MATCH_TOL_SEC, side="right"))
        for ms_idx in range(left, right):
            residual = float(ms_times[ms_idx] - composite)
            if abs(residual) <= QC_GROUP_MATCH_TOL_SEC + QC_COMPONENT_SELECT_EPS:
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

        local_matches = []
        for pair_local, ms_local in enumerate(range(selected_count)):
            pair_idx = selected_pairs[pair_local]
            ms_idx = selected_ms_indices[ms_local]
            residual = float(ms_times[ms_idx] - float(pair_rows[pair_idx][4]))
            if abs(residual) <= QC_GROUP_MATCH_TOL_SEC + QC_COMPONENT_SELECT_EPS:
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
            out.append(
                {
                    "pair_idx": pair_idx,
                    "ms_idx": ms_idx,
                    "residual_sec": float(residual),
                    "component_pair_count": len(component_pair_list),
                    "component_ms_count": len(component_ms_list),
                    "selection_reason": selection_reason,
                    "alternative_ms_event_ids": [],
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
) -> dict[str, Any]:
    qc_end = float(qc_calibration_end_min)
    anchors = [str(ch).strip().upper() for ch in (qc_anchor_channels or ["G2", "R1"])]
    if len(anchors) != 2 or len(set(anchors)) != 2:
        raise BadRequest("QC anchor 必须包含两个不同 LIF 通道")
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
                "lif_pair_quality_score": float(quality),
                "composite_to_ms_residual_sec": float(residual),
                "abs_composite_to_ms_residual_sec": abs(float(residual)),
                "component_pair_count": int(match["component_pair_count"]),
                "component_ms_count": int(match["component_ms_count"]),
                "selection_reason": match["selection_reason"],
                "alternative_ms_event_ids": match["alternative_ms_event_ids"],
                "skipped_pair_ids": match["skipped_pair_ids"],
                "skipped_ms_event_ids": match["skipped_ms_event_ids"],
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    qc_end = float(qc_calibration_end_min)
    anchors = [str(ch).strip().upper() for ch in (qc_anchor_channels or ["G2", "R1"])]
    channel_time_axes = channel_time_axes or {"G2": "green_axis", "G1": "green_axis", "R1": "red_axis", "R2": "red_axis"}
    axis_shifts_sec = axis_shifts_sec or {"green_axis": green_shift_sec, "red_axis": red_shift_sec}

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

    composite_times = np.asarray([row[4] for row in pair_rows], dtype=float)
    ms_times = ms["plot_time_sec"].to_numpy(float)
    matches = greedy_time_matches(composite_times, ms_times, 0.0, tolerance_sec)
    groups = []
    for rank, (pair_idx, ms_idx, residual) in enumerate(matches, start=1):
        anchor_a_row, anchor_b_row, anchor_a_plot, anchor_b_plot, composite, lif_pair_residual, quality = pair_rows[int(pair_idx)]
        m_row = ms.iloc[int(ms_idx)]
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
                "lif_pair_quality_score": float(quality),
                "composite_to_ms_residual_sec": float(residual),
                "abs_composite_to_ms_residual_sec": abs(float(residual)),
                "match_tolerance_sec": float(tolerance_sec),
                "selection_reason": "post_qc_shift_only_unique_nearest",
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
) -> list[dict[str, Any]]:
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
        label = {"G2": "Day0 cell", "R1": "Day9 cell", "R2": "Day3 cell"}.get(channel, "cell")
        rows.append(
            {
                "rank": rank,
                "lif_channel": channel,
                "lif_peak_id": str(lif_row["peak_id"]),
                "ms_event_id": str(ms_row["event_id"]),
                "scan_id": clean_value(ms_row.get("scan_id")),
                "label": label,
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
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
    for group in groups:
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
        "conflict_count": 0,
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
) -> dict[str, Any]:
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
    qc_anchor_channels: list[str] | tuple[str, str] | None = None,
) -> dict[str, Any]:
    best_pair: dict[str, Any] | None = None
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
            if best_pair is None or score_key > best_pair["score_key"]:
                best_pair = candidate
    if best_pair is not None and int(best_pair["unique_match_count"]) > 0:
        best_pair["method"] = "qc_pair_seed_window_shift_grid_search"
        best_pair["search_range_sec"] = [LOCAL_DELTA_SEARCH_MIN_SEC, LOCAL_DELTA_SEARCH_MAX_SEC]
        best_pair["search_step_sec"] = LOCAL_DELTA_SEARCH_STEP_SEC
        best_pair["match_tolerance_sec"] = POST_QC_CANDIDATE_TOL_SEC
        best_pair["contains_cell_labels"] = False
        best_pair["delta_abs_prior_weight"] = LOCAL_DELTA_ABS_PRIOR_WEIGHT
        return best_pair

    best: dict[str, Any] | None = None
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
        if abs(float(delta)) < LOCAL_DELTA_SEARCH_STEP_SEC / 2.0:
            zero_evidence = candidate
        if best is None or score_key > best["score_key"]:
            best = candidate
    assert best is not None
    if int(best["unique_match_count"]) <= 0 and zero_evidence is not None:
        best = zero_evidence
    best["method"] = "unlabeled_seed_window_shift_only_grid_search"
    best["search_range_sec"] = [LOCAL_DELTA_SEARCH_MIN_SEC, LOCAL_DELTA_SEARCH_MAX_SEC]
    best["search_step_sec"] = LOCAL_DELTA_SEARCH_STEP_SEC
    best["match_tolerance_sec"] = LOCAL_DELTA_MATCH_TOL_SEC
    best["contains_cell_labels"] = False
    return best


def estimate_shift_alignment(
    lif_peaks: pd.DataFrame,
    ms_events: pd.DataFrame,
    qc_calibration_end_min: float = QC_SHIFT_WINDOW_MIN,
    acquisition_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    qc_end = float(qc_calibration_end_min)
    layout = normalize_acquisition_layout(acquisition_layout)
    channel_time_axes = layout["channel_time_axes"]
    qc_anchor_channels = layout["qc_anchor_channels"]
    channel_configs = layout["lif_channels"]
    anchor_estimates = {
        channel: estimate_channel_shift(lif_peaks, ms_events, channel, qc_end)
        for channel in qc_anchor_channels
    }
    axis_shifts_sec: dict[str, float] = {}
    axis_sources: dict[str, str] = {}
    for channel in qc_anchor_channels:
        axis = str(channel_time_axes[channel])
        axis_shifts_sec[axis] = float(anchor_estimates[channel]["shift_sec"])
        axis_sources[axis] = channel
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
        source_estimate = anchor_estimates.get(source)
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
        "model": f"shift_only_auto_0_{qc_end:g}min_qc",
        "status": "suggestion_not_annotation",
        "description": f"0-{qc_end:g} min 全 QC 区段自动估计整体平移；QC anchor={'+'.join(qc_anchor_channels)}；同一 time_axis 的 LIF 通道共用平移，MS760/MS782 不移动。",
        "green_to_ms_shift_sec": green_shift,
        "red_to_ms_shift_sec": red_shift,
        "axis_shifts_sec": axis_shifts_sec,
        "channel_time_axes": channel_time_axes,
        "qc_anchor_channels": qc_anchor_channels,
        "r2_uses": "red_to_ms_shift_sec",
        "ms_shift_sec": 0.0,
        "channels": channel_estimates,
        "qc_groups": qc_groups,
    }


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

    @classmethod
    def load(cls, project: ProjectPaths | None = None) -> "AppData":
        project = project or ProjectPaths.from_args()
        manifest_was_missing = False
        manifest = read_project_manifest(project.project_dir)
        if manifest is None:
            manifest_was_missing = True
        validate_project_manifest_against_files(project.project_dir, manifest)
        project = project_with_manifest_paths(project, manifest)
        for path in [project.lif_traces_path, project.lif_peaks_path, project.ms_events_path, project.ms_scan_path]:
            require_file(path)
        intermediate_tables = intermediate_table_fingerprints(project)
        binding = project_table_binding(intermediate_tables)

        lif_traces = pd.read_parquet(project.lif_traces_path).sort_values(["channel", "time_min"]).reset_index(drop=True)
        lif_peaks = pd.read_parquet(project.lif_peaks_path)
        lif_peaks = lif_peaks[lif_peaks["peak_stage"].eq("merged")].sort_values(["time_min", "channel"]).reset_index(drop=True)
        lif_peaks["phase"] = display_phase_from_time_min(lif_peaks["time_min"])
        ms_events = pd.read_parquet(project.ms_events_path).sort_values("time_min").reset_index(drop=True)
        ms_scan = pd.read_parquet(project.ms_scan_path).sort_values("scan_start_time_min").reset_index(drop=True)
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
        allow_adopt = manifest_was_missing or sqlite_annotation_count(project.annotation_db_path) == 0
        if allow_adopt:
            validate_sqlite_input_manifest_against_files(project.annotation_db_path, project.project_dir, intermediate_tables)
        validate_sqlite_project_binding(project.annotation_db_path, binding, allow_adopt=allow_adopt)
        if manifest_was_missing:
            prior_values = {
                channel: info.get("identity_prior", "")
                for channel, info in channel_identity_prior.items()
            }
            write_project_manifest(
                project_dir=project.project_dir,
                raw_input_mode=RAW_INPUT_MODE_EXTERNAL,
                raw_inputs={},
                channel_identity_prior=prior_values,
                intermediate_tables=intermediate_tables,
            )
        store = AnnotationStore(project.annotation_db_path)
        project_config = store.project_config()
        alignment = estimate_shift_alignment(
            lif_peaks,
            ms_events,
            float(project_config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN)),
            acquisition_layout=acquisition_layout,
        )
        store.record_project_table_binding(binding)
        store.ensure_draft_time_model(str(alignment["model"]))
        store.record_input_manifest(
            {
                "lif_traces": project.lif_traces_path,
                "lif_peaks": project.lif_peaks_path,
                "ms_events": project.ms_events_path,
                "ms_scan_summary": project.ms_scan_path,
            }
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
        if candidate_type == "qc_survey_post_10p5" or candidate_type == "manual_qc_anchor_partial":
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

    def export_accepted_annotations_csv(self) -> dict[str, Any]:
        timestamp = now_iso()
        export_id = f"export_{timestamp.replace(':', '').replace('-', '').replace('Z', '')}_{uuid.uuid4().hex[:8]}"
        csv_filename = export_filename_for_project(self.project.project_dir)
        frozen = self.frozen_time_model()
        active_version = str(frozen.get("time_model_version", "")) if frozen else ""
        filters = {
            "review_status": "accepted",
            "exportable": True,
            "include_stages": ["qc_calibration", "qc_survey", "cell_annotation"],
            "current_time_model_only_for_post_qc_and_cell": True,
            "active_time_model_version": active_version,
            "input_policy": "first_principles_preprocessing_tables_plus_human_review",
            "label_policy": "Day labels are channel identity priors from raw filename/project config, not author CSV/h5ad labels",
        }
        rows: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in self.store.records():
            if row.get("review_status") != "accepted" or not bool(row.get("exportable")):
                continue
            stage = self.annotation_review_stage(row)
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
            rows.append(self.export_row(row, stage=stage, export_id=export_id, exported_at=timestamp))
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
        g2_raw = row.get("g2_raw_time_min")
        r1_raw = row.get("r1_raw_time_min")
        r2_raw = row.get("r2_raw_time_min")
        lif_raw = row.get("lif_raw_time_min")
        if is_cell and lif_raw is not None:
            if lif_channel == "G2":
                g2_raw = lif_raw
            elif lif_channel == "R1":
                r1_raw = lif_raw
            elif lif_channel == "R2":
                r2_raw = lif_raw
        g2_plot = row.get("g2_plot_time_min")
        r1_plot = row.get("r1_plot_time_min")
        r2_plot = row.get("r2_plot_time_min")
        lif_plot = row.get("lif_plot_time_min")
        if is_cell and lif_plot is not None:
            if lif_channel == "G2":
                g2_plot = lif_plot
            elif lif_channel == "R1":
                r1_plot = lif_plot
            elif lif_channel == "R2":
                r2_plot = lif_plot
        return {
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
            "label_source": label_source,
            "payload_label": row.get("label"),
            "evidence_role": evidence_role,
            "lif_channel": lif_channel,
            "lif_peak_id": lif_peak_id,
            "candidate_channel": lif_channel,
            "channel_identity_prior": identity_prior,
            "channel_identity_prior_source": prior.get("identity_prior_source"),
            "channel_identity_prior_file": prior.get("identity_prior_file"),
            "g2_peak_id": row.get("g2_peak_id") or (MISSING_PEAK_SYMBOL if annotation_kind == "qc_anchor" else None),
            "r1_peak_id": row.get("r1_peak_id") or (MISSING_PEAK_SYMBOL if annotation_kind == "qc_anchor" else None),
            "r2_peak_id": row.get("r2_peak_id"),
            "ms_event_id": row.get("ms_event_id"),
            "scan_id": row.get("scan_id"),
            "g2_raw_time_min": g2_raw,
            "r1_raw_time_min": r1_raw,
            "r2_raw_time_min": r2_raw,
            "lif_raw_time_min": lif_raw,
            "ms_time_min": row.get("ms_time_min"),
            "g2_plot_time_min": g2_plot,
            "r1_plot_time_min": r1_plot,
            "r2_plot_time_min": r2_plot,
            "lif_plot_time_min": lif_plot,
            "ms_plot_time_min": row.get("ms_plot_time_min"),
            "residual_sec": row.get("residual_sec"),
            "abs_residual_sec": row.get("abs_residual_sec"),
            "lif_anchor_count": row.get("lif_anchor_count"),
            "missing_lif_channels": row.get("missing_lif_channels"),
            "candidate_rank": row.get("candidate_rank"),
            "candidate_score": row.get("candidate_score"),
            "selection_reason": row.get("selection_reason"),
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
            "annotation_id", "annotation_kind", "review_stage",
            "annotation_label", "label_source", "evidence_role",
            "source", "review_status",
            "lif_channel", "channel_identity_prior", "channel_identity_prior_source",
            "lif_peak_id",
            "g2_peak_id", "r1_peak_id", "r2_peak_id", "ms_event_id", "scan_id",
            "lif_raw_time_min", "g2_raw_time_min", "r1_raw_time_min", "r2_raw_time_min", "ms_time_min",
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
        qc_anchor_channels: list[str] | tuple[str, str] | None = None,
    ) -> "AppData":
        project_dir = project_dir.expanduser().resolve()
        mode = normalize_raw_input_mode(raw_input_mode)
        if lif_inputs is None:
            if not (lif_g2_path and lif_r1_path and lif_r2_path):
                raise BadRequest("必须提供 3 个 LIF 原始文件")
            lif_inputs = [
                {"key": "lif_g2", "path": lif_g2_path, "channel": "G2", "identity_prior": g2_identity},
                {"key": "lif_r1", "path": lif_r1_path, "channel": "R1", "identity_prior": r1_identity},
                {"key": "lif_r2", "path": lif_r2_path, "channel": "R2", "identity_prior": r2_identity},
            ]
            qc_anchor_channels = qc_anchor_channels or ["G2", "R1"]
        source_raw_paths = {"ms": ms_path.expanduser().resolve()}
        for item in lif_inputs:
            key = str(item.get("key") or "").strip()
            if not key:
                raise BadRequest("每个 LIF 输入必须包含 key")
            source_raw_paths[key] = Path(item["path"]).expanduser().resolve()
        if not IS_FROZEN and project_dir == ROOT:
            raise BadRequest("项目保存路径不能使用当前代码仓库根目录；请新建独立项目目录")
        existing_outputs = [
            project_dir / REQUIRED_INTERMEDIATE_TABLES["lif_traces"],
            project_dir / REQUIRED_INTERMEDIATE_TABLES["lif_peaks"],
            project_dir / REQUIRED_INTERMEDIATE_TABLES["ms_events"],
            project_dir / REQUIRED_INTERMEDIATE_TABLES["ms_scan_summary"],
        ]
        assert_new_project_target_is_clean(project_dir, existing_outputs)
        for path in source_raw_paths.values():
            raw_file_fingerprint(path)

        project_dir.mkdir(parents=True, exist_ok=True)
        lock_dir = project_dir / "results/tables/v3"
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
        )
        if mode == RAW_INPUT_MODE_COPY:
            for key, source_path in source_raw_paths.items():
                destination = project_dir / str(manifest_raw_inputs[key]["path"])
                if destination.exists():
                    raise BadRequest(f"项目目录 raw_inputs 中已有同名文件: {display_path(destination, project_dir)}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
        raw_data_dir = project_dir / "raw_inputs"
        locked_rows = []
        input_id_to_key = {str(row["input_id"]): key for key, row in zip([str(item.get("key")) for item in lif_inputs], rows) if row["input_class"] == "raw_lif_trace"}
        input_id_to_key["ms_raw_txt"] = "ms"
        for row in rows:
            raw_path = Path(row["path"])
            full_path = raw_path if raw_path.is_absolute() else project_dir / raw_path
            fp = raw_file_fingerprint(full_path)
            manifest_raw_inputs[input_id_to_key[str(row["input_id"])]].update(fp)
            locked_rows.append(
                {
                    **row,
                    "allowed_stage": "main annotation preprocessing",
                    **fp,
                }
            )
        allowed = pd.DataFrame(locked_rows)
        # V3-01/V3-02 currently check this exact historical stage string.
        script_allowed = allowed.copy()
        script_allowed["allowed_stage"] = "V3-01~V3-06 main workflow"
        script_allowed.to_csv(lock_dir / "00_allowed_inputs.csv", index=False)
        allowed.to_csv(lock_dir / "00_imported_raw_inputs.csv", index=False)
        (project_dir / "reports").mkdir(parents=True, exist_ok=True)
        (project_dir / "reports/import_project.md").write_text(
            "\n".join(
                [
                    "# 标注项目导入记录",
                    "",
                    f"导入时间：`{now_iso()}`",
                    "",
                    "- 本导入只锁定 3 个用户配置的 LIF 原始文件和 1 个 MS 原始文件。",
                    "- 不读取作者 CSV、h5ad、manual、V2/archive 输入。",
                    "- 生成的中间表用于浏览器人工标注；后续时间校正和 annotation 由软件内人工审核完成。",
                    f"- QC anchor LIF 组合：`{acquisition_layout['qc_anchor_channels'][0]} + {acquisition_layout['qc_anchor_channels'][1]}`。",
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

        scripts = [
            "run_v3_01_lif_trace_physical_qc.py",
            "run_v3_02_ms_event_calling.py",
        ]
        log_lines = []
        for script in scripts:
            try:
                script_output = run_preprocessing_script(script, project_dir)
                log_lines.append(f"$ in-process {script} --project-dir {project_dir}\n{script_output}")
            except Exception as exc:
                log_lines.append(f"$ in-process {script} --project-dir {project_dir}\n{type(exc).__name__}: {exc}")
                (project_dir / "reports/import_preprocess.log").write_text("\n\n".join(log_lines), encoding="utf-8")
                raise BadRequest(f"前处理失败：{script}\n{str(exc)[-4000:]}") from exc
        (project_dir / "reports/import_preprocess.log").write_text("\n\n".join(log_lines), encoding="utf-8")
        intermediate_tables = {
            "lif_traces": {"path": project_relative_or_absolute(existing_outputs[0], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[0], full_hash_limit_bytes=None)},
            "lif_peaks": {"path": project_relative_or_absolute(existing_outputs[1], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[1], full_hash_limit_bytes=None)},
            "ms_events": {"path": project_relative_or_absolute(existing_outputs[2], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[2], full_hash_limit_bytes=None)},
            "ms_scan_summary": {"path": project_relative_or_absolute(existing_outputs[3], project_dir).replace("\\", "/"), **raw_file_fingerprint(existing_outputs[3], full_hash_limit_bytes=None)},
        }
        write_project_manifest(
            project_dir=project_dir,
            raw_input_mode=mode,
            raw_inputs=manifest_raw_inputs,
            channel_identity_prior=identities,
            intermediate_tables=intermediate_tables,
            acquisition_layout=acquisition_layout,
        )

        project = ProjectPaths.from_args(
            project_dir=str(project_dir),
            raw_data_dir=str(raw_data_dir),
            annotation_db=str(project_dir / "annotation_app/annotations/annotation.sqlite"),
        )
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

    def aligned_channel_shifts_sec(self) -> dict[str, float]:
        return {channel: self.channel_shift_sec(channel, "aligned") for channel in self.cell_annotation_channels()}

    def project_config(self) -> dict[str, Any]:
        config = self.store.project_config()
        return {
            "qc_calibration_end_min": float(config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN)),
            "sample_valve_switch_min": float(config.get("sample_valve_switch_min", DEFAULT_SAMPLE_VALVE_SWITCH_MIN)),
            "annotation_start_min": float(config.get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN)),
            "local_delta_seed_window_min": float(
                config.get("local_delta_seed_window_min", DEFAULT_LOCAL_DELTA_SEED_WINDOW_MIN)
            ),
        }

    def active_time_model(self) -> dict[str, Any]:
        model = self.store.active_time_model()
        if model:
            return model
        return self.store.ensure_draft_time_model(str(self.alignment["model"]))

    def frozen_time_model(self) -> dict[str, Any] | None:
        model = self.active_time_model()
        return model if str(model.get("status")) == "frozen" else None

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
        return {
            "time_model_name": str(model.get("base_model_name", self.alignment["model"])),
            "time_model_version": str(model.get("time_model_version", "")),
            "time_model_status": str(model.get("status", "draft")),
            "contains_cell_labels": bool(model.get("contains_cell_labels", False)),
            "ms_local_delta_sec": float(model.get("ms_local_delta_sec", 0.0) or 0.0),
        }

    def local_delta_anchor_pair_kwargs(self, config: dict[str, Any]) -> dict[str, Any]:
        pair_offset = self.alignment.get("qc_groups", {}).get("lif_anchor_b_minus_anchor_a_offset_sec")
        if pair_offset is None:
            pair_offset = self.alignment.get("qc_groups", {}).get("lif_r1_minus_g2_offset_sec")
        if pair_offset is None:
            return {}
        return {
            "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
            "pair_offset_sec": float(pair_offset),
            "axis_shifts_sec": self.alignment.get("axis_shifts_sec"),
            "channel_time_axes": self.alignment.get("channel_time_axes"),
            "qc_anchor_channels": self.alignment.get("qc_anchor_channels"),
        }

    def local_delta_preview(self, delta_sec: float | None = None) -> dict[str, Any]:
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
        }

    def update_local_delta_draft(self, delta_sec: float) -> dict[str, Any]:
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
        }
        return self.store.upsert_time_model(payload, action="manual_update_local_delta_draft")

    def freeze_local_delta_model(self) -> dict[str, Any]:
        model = self.active_time_model()
        if bool(model.get("contains_cell_labels", False)):
            raise BadRequest("Cannot freeze a time model that contains cell labels")
        status = "frozen"
        records = self.store.records()
        first_cell = [
            row
            for row in records
            if str(row.get("candidate_type", "")).startswith("cell")
            and str(row.get("review_status")) == "accepted"
        ]
        if first_cell:
            status = "exploratory"
        payload = {**model, "status": status, "contains_cell_labels": False}
        return self.store.upsert_time_model(payload, action=f"freeze_local_delta_{status}")

    def update_project_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        clear_frozen_time_model = bool(updates.get("clear_frozen_time_model"))
        config = self.store.update_project_config(updates, clear_frozen_time_model=clear_frozen_time_model)
        object.__setattr__(
            self,
            "alignment",
            estimate_shift_alignment(
                self.lif_peaks,
                self.ms_events,
                float(config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN)),
                acquisition_layout=self.acquisition_layout,
            ),
        )
        model = self.active_time_model()
        if str(model.get("status")) != "frozen":
            payload = {
                **model,
                "status": "draft",
                "base_model_name": str(self.alignment["model"]),
                "qc_calibration_end_min": float(config["qc_calibration_end_min"]),
                "sample_valve_switch_min": float(config["sample_valve_switch_min"]),
                "annotation_start_min": float(config["annotation_start_min"]),
                "local_delta_seed_window_min": float(config["local_delta_seed_window_min"]),
                "contains_cell_labels": False,
                "ms_local_delta_sec": 0.0 if clear_frozen_time_model else float(model.get("ms_local_delta_sec", 0.0) or 0.0),
                "max_training_time_min": float(config["annotation_start_min"]) + float(config["local_delta_seed_window_min"]),
            }
            self.store.upsert_time_model(payload, action="sync_draft_time_model_to_project_config")
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
        return {
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
            "label": "QC",
            "anchor_a_channel": group.get("anchor_a_channel") or self.alignment.get("qc_anchor_channels", ["G2", "R1"])[0],
            "anchor_b_channel": group.get("anchor_b_channel") or self.alignment.get("qc_anchor_channels", ["G2", "R1"])[1],
            "anchor_a_peak_id": group.get("anchor_a_peak_id") or group["g2_peak_id"],
            "anchor_b_peak_id": group.get("anchor_b_peak_id") or group["r1_peak_id"],
            "g2_peak_id": group["g2_peak_id"],
            "r1_peak_id": group["r1_peak_id"],
            "ms_event_id": group["ms_event_id"],
            "scan_id": scan_id,
            "g2_raw_time_min": group["g2_raw_time_min"],
            "r1_raw_time_min": group["r1_raw_time_min"],
            "anchor_a_raw_time_min": group.get("anchor_a_raw_time_min", group["g2_raw_time_min"]),
            "anchor_b_raw_time_min": group.get("anchor_b_raw_time_min", group["r1_raw_time_min"]),
            "ms_time_min": group["ms_time_min"],
            "g2_plot_time_min": group["g2_plot_time_min"],
            "r1_plot_time_min": group["r1_plot_time_min"],
            "anchor_a_plot_time_min": group.get("anchor_a_plot_time_min", group["g2_plot_time_min"]),
            "anchor_b_plot_time_min": group.get("anchor_b_plot_time_min", group["r1_plot_time_min"]),
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

    def payload_from_auto_group(self, group: dict[str, Any]) -> dict[str, Any]:
        return self.payload_from_qc_group(
            group,
            candidate_id=candidate_id_for_group(group),
            candidate_type="qc_calibration_anchor_0_10p5",
            confidence_mode="auto_qc_shift_candidate",
        )

    def payload_from_post_qc_group(self, group: dict[str, Any]) -> dict[str, Any]:
        return self.payload_from_qc_group(
            group,
            candidate_id=post_qc_candidate_id(group),
            candidate_type="qc_survey_post_10p5",
            confidence_mode="post_qc_shift_only_candidate",
        )

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
            "label": {"G2": "Day0 cell", "R1": "Day9 cell", "R2": "Day3 cell"}.get(lif_channel, "cell"),
            "lif_channel": lif_channel,
            "lif_peak_id": lif_peak_id,
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

    def payload_from_auto_candidate_id(self, annotation_id: str) -> dict[str, Any]:
        if annotation_id.startswith("auto_qc:"):
            return self.payload_from_auto_group(self.auto_group_by_id(annotation_id))
        if annotation_id.startswith("post_qc:"):
            if not self.frozen_time_model():
                raise BadRequest("Freeze local time model before reviewing QC survey candidates")
            parts = annotation_id.split(":", 3)
            if len(parts) != 4:
                raise BadRequest(f"Malformed post_qc candidate_id: {annotation_id}")
            return self.payload_from_qc_ids(parts[1], parts[2], parts[3], post_qc=True)
        if annotation_id.startswith("cell:"):
            if not self.frozen_time_model():
                raise BadRequest("Freeze local time model before reviewing cell candidates")
            parts = annotation_id.split(":", 3)
            if len(parts) != 4:
                raise BadRequest(f"Malformed cell candidate_id: {annotation_id}")
            return self.payload_from_cell_ids(parts[1], parts[2], parts[3])
        raise BadRequest(f"Unknown auto candidate_id: {annotation_id}")

    def payload_from_manual_triplet(
        self,
        g2_peak_id: str | None,
        r1_peak_id: str | None,
        ms_event_id: str,
        *,
        allow_lif_missing: bool = False,
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        payload = self.payload_from_auto_candidate_id(annotation_id)
        return self.store.upsert_review(
            annotation_id=annotation_id,
            source="auto_candidate",
            review_status=review_status,
            payload=payload,
            action=f"auto_candidate_{review_status}",
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
        )

    def review_annotation(
        self,
        annotation_id: str,
        review_status: str,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
        time_mode: str | None = None,
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
            return self.store.upsert_review(
                annotation_id=annotation_id,
                source="manual_created",
                review_status=review_status,
                payload=payload,
                action=f"manual_annotation_{review_status}",
                window_start_min=window_start_min,
                window_end_min=window_end_min,
                time_mode=time_mode,
            )
        return self.review_auto_candidate(
            annotation_id,
            review_status,
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
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
    ) -> dict[str, Any]:
        g2_peak_id = optional_peak_id(g2_peak_id)
        r1_peak_id = optional_peak_id(r1_peak_id)
        ms_event_id = optional_peak_id(ms_event_id) or ""
        if stage not in {"qc_calibration", "qc_survey"}:
            raise BadRequest("Manual QC anchor can only be created in QC calibration or QC survey")
        allow_lif_missing = stage == "qc_survey"
        if stage == "qc_survey" and not self.frozen_time_model():
            raise BadRequest("Freeze local time model before creating QC survey anchors")
        if not allow_lif_missing:
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
                    )
        elif g2_peak_id and r1_peak_id and window_start_min is not None and window_end_min is not None:
            for group in self.build_post_qc_candidates(
                float(window_start_min) - WINDOW_CONTEXT_MARGIN_MIN,
                float(window_end_min) + WINDOW_CONTEXT_MARGIN_MIN,
                "aligned",
            ):
                if (
                    str(group.get("g2_peak_id")) == g2_peak_id
                    and str(group.get("r1_peak_id")) == r1_peak_id
                    and str(group.get("ms_event_id")) == ms_event_id
                ):
                    return self.review_auto_candidate(
                        post_qc_candidate_id(group),
                        "accepted",
                        window_start_min=window_start_min,
                        window_end_min=window_end_min,
                        time_mode=time_mode,
                    )
        payload = self.payload_from_manual_triplet(
            g2_peak_id,
            r1_peak_id,
            ms_event_id,
            allow_lif_missing=allow_lif_missing,
        )
        annotation_id = manual_annotation_id(g2_peak_id, r1_peak_id, ms_event_id)
        return self.store.upsert_review(
            annotation_id=annotation_id,
            source="manual_created",
            review_status="accepted",
            payload=payload,
            action="manual_create_accept",
            window_start_min=window_start_min,
            window_end_min=window_end_min,
            time_mode=time_mode,
        )

    def create_manual_cell_pair(
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

    def clear_manual_annotation(self, annotation_id: str) -> dict[str, Any]:
        return self.store.hard_delete_manual(annotation_id)

    def enrich_qc_candidate(self, group: dict[str, Any], *, post_qc: bool) -> dict[str, Any]:
        annotation_id = post_qc_candidate_id(group) if post_qc else candidate_id_for_group(group)
        stored = self.store.get(annotation_id)
        active_version = str(self.active_time_model().get("time_model_version", ""))
        frozen = self.frozen_time_model()
        stored_version = str(stored.get("time_model_version", "")) if stored else ""
        if post_qc and stored and stored_version != active_version:
            review_status = "pending"
        else:
            review_status = str(stored.get("review_status")) if stored else "pending"
        return {
            **group,
            **self.time_model_payload_fields(),
            "annotation_id": annotation_id,
            "candidate_id": annotation_id,
            "candidate_type": "qc_survey_post_10p5" if post_qc else "qc_calibration_anchor_0_10p5",
            "source": "auto_candidate",
            "review_status": review_status,
            "exportable": review_status == "accepted",
            "review_enabled": (not post_qc) or bool(frozen),
            "stale_review_status": str(stored.get("review_status")) if post_qc and stored and stored_version != active_version else None,
            "stale_time_model_version": stored_version if post_qc and stored and stored_version != active_version else None,
        }

    def build_post_qc_candidates(self, context_start_min: float, context_end_min: float, time_mode: str) -> list[dict[str, Any]]:
        config = self.project_config()
        qc_end = float(config.get("qc_calibration_end_min", QC_SHIFT_WINDOW_MIN))
        if time_mode != "aligned" or context_end_min <= qc_end:
            return []
        if not self.frozen_time_model():
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
            "candidate_type": "cell_high_confidence",
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
                )
            )
        rows.sort(key=lambda item: (float(item["ms_plot_time_min"]), str(item["lif_channel"])))
        return [self.enrich_cell_candidate(row) for row in rows]

    def accept_pending_auto_candidates_in_window(
        self,
        start_min: float,
        window_min: float,
        time_mode: str,
        stage: str = "qc_calibration",
    ) -> dict[str, Any]:
        window = self.window(start_min=start_min, window_min=window_min, time_mode=time_mode)
        if stage not in {"qc_calibration", "qc_survey", "cell_annotation"}:
            raise BadRequest("stage must be qc_calibration, qc_survey, or cell_annotation")
        if stage in {"qc_survey", "cell_annotation"} and not self.frozen_time_model():
            raise BadRequest("Freeze local time model before accepting post-calibration candidates")
        source_key = {
            "qc_calibration": "alignment_groups",
            "qc_survey": "post_qc_candidates",
            "cell_annotation": "cell_candidates",
        }[stage]
        accepted = []
        skipped = []
        for group in window.get(source_key, []):
            if stage == "cell_annotation":
                plot_times = [float(group["lif_plot_time_min"]), float(group["ms_plot_time_min"])]
            else:
                plot_times = [
                    float(group["g2_plot_time_min"]),
                    float(group["r1_plot_time_min"]),
                    float(group["ms_plot_time_min"]),
                ]
            if not all(float(window["start_min"]) <= t <= float(window["end_min"]) for t in plot_times):
                skipped.append({"annotation_id": group.get("annotation_id"), "reason": "outside_main_window"})
                continue
            if group.get("source") != "auto_candidate":
                skipped.append({"annotation_id": group.get("annotation_id"), "reason": "not_auto_candidate"})
                continue
            if group.get("review_status") != "pending":
                skipped.append({"annotation_id": group.get("annotation_id"), "reason": f"status_{group.get('review_status')}"})
                continue
            row = self.review_auto_candidate(
                str(group["annotation_id"]),
                "accepted",
                window_start_min=float(window["start_min"]),
                window_end_min=float(window["end_min"]),
                time_mode=str(window["time_mode"]),
            )
            accepted.append(row)
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

    def window_annotations(self, context_start_min: float, context_end_min: float) -> list[dict[str, Any]]:
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
            plot_times = [
                row.get("lif_plot_time_min"),
                row.get("g2_plot_time_min"),
                row.get("r1_plot_time_min"),
                row.get("ms_plot_time_min"),
            ]
            visible_times = [float(t) for t in plot_times if isinstance(t, (int, float))]
            if visible_times and all(context_start_min <= t <= context_end_min for t in visible_times):
                rows.append(row)
        rows.sort(key=lambda item: float(item.get("ms_plot_time_min", 0.0)))
        return rows

    def manual_annotation_stage(self, row: dict[str, Any]) -> str:
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
        annotation_start = float(self.project_config().get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN))
        return "qc_survey" if ms_time_float >= annotation_start else "qc_calibration"

    def is_qc_survey_annotation(self, row: dict[str, Any]) -> bool:
        if str(row.get("review_status")) != "accepted":
            return False
        frozen = self.frozen_time_model()
        active_version = str(frozen.get("time_model_version", "")) if frozen else ""
        row_version = str(row.get("time_model_version") or "")
        if not active_version or row_version != active_version:
            return False
        candidate_type = str(row.get("candidate_type") or "")
        if candidate_type == "qc_survey_post_10p5" or candidate_type.startswith("manual_qc"):
            try:
                ms_time = float(row.get("ms_time_min"))
            except (TypeError, ValueError):
                return False
            annotation_start = float(self.project_config().get("annotation_start_min", DEFAULT_ANNOTATION_START_MIN))
            return ms_time >= annotation_start
        return False

    def accepted_qc_survey_ms_event_ids(self) -> set[str]:
        ids: set[str] = set()
        for row in self.store.records():
            if not self.is_qc_survey_annotation(row):
                continue
            ms_event_id = row.get("ms_event_id")
            if ms_event_id:
                ids.add(str(ms_event_id))
        return ids

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
        for channel, sub in self.lif_peaks.groupby("channel", sort=False):
            shift_min = self.channel_shift_sec(str(channel), time_mode) / 60.0
            part = sub.copy()
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
        ]

        alignment_groups = []
        if time_mode == "aligned":
            for group in self.alignment.get("qc_groups", {}).get("groups", []):
                plot_times = [
                    float(group["g2_plot_time_min"]),
                    float(group["r1_plot_time_min"]),
                    float(group["ms_plot_time_min"]),
                ]
                if all(context_start_min <= t <= context_end_min for t in plot_times):
                    alignment_groups.append(self.enrich_qc_candidate(group, post_qc=False))

        post_qc_candidates = self.build_post_qc_candidates(context_start_min, context_end_min, time_mode)
        cell_candidates = self.build_cell_candidates(context_start_min, context_end_min, time_mode)
        cell_qc_anchors = (
            self.accepted_qc_survey_anchors_for_window(context_start_min, context_end_min)
            if time_mode == "aligned"
            else []
        )

        annotations = self.window_annotations(context_start_min, context_end_min)
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
            "annotation_store": self.store.summary(),
            "lif_traces": lif_traces,
            "lif_peaks": records(peaks_window, peak_cols),
            "ms_traces": {
                "pc34_760_linear": xy_records(ms_760, "plot_time_min", "pc34_760_max_intensity"),
                "qc_782_linear": xy_records(ms_782, "plot_time_min", "qc_782_max_intensity"),
            },
            "ms_events": records(events_window, event_cols),
            "counts": {
                "lif_trace_points_returned": int(sum(len(v) for v in lif_traces.values())),
                "lif_peaks": int(len(peaks_window)),
                "ms_scan_points_returned": int(len(ms_760) + len(ms_782)),
                "ms_events": int(len(events_window)),
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
            "input_policy": "等待导入 3 个 LIF 原始文件和 1 个 MS 原始文件；不读取作者 CSV、h5ad、manual/V2/archive 输入。",
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
      --g2: #176b45;
      --r2: #b95d18;
      --r1: #6f4bb8;
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
      gap: 6px;
      margin-top: 6px;
    }
    .row-actions button, .small-button {
      height: 28px;
      min-width: 0;
      padding: 0 8px;
      font-size: 12px;
      font-weight: 700;
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
      max-width: min(640px, calc(100vw - 28px));
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
    .modal-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid #edf0f4;
      margin-bottom: 12px;
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
      min-width: 0;
    }
    .path-picker-button {
      width: 64px;
      height: 34px;
      white-space: nowrap;
    }
    .lif-input-grid {
      display: grid;
      grid-template-columns: 72px 96px minmax(0, 1fr) 64px;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }
    .lif-input-grid input {
      min-width: 0;
    }
    .qc-anchor-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
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
      <span id="exportHint" class="header-export-hint">导出全项目已接受标注</span>
      <button id="exportAcceptedCsv" class="header-export-button">导出已接受 CSV</button>
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
        <button class="stage-tab active" data-stage="qc_calibration">QC 校正</button>
        <button class="stage-tab" data-stage="local_calibration">后段局部校正</button>
        <button class="stage-tab" data-stage="qc_survey">QC 巡检</button>
        <button class="stage-tab" data-stage="cell_annotation">细胞标注</button>
      </div>
      <div id="stageNote" class="stage-note">前 10.5 min QC anchor 审核，用于确认 shift-only 时间校正。</div>
      <div id="localDeltaPanel" class="manual-box" style="display:none; margin-top:8px;">
        <button id="estimateDelta" class="small-button" style="width:100%;">自动估计 MS 后段平移</button>
        <div id="deltaBaseSummary" class="empty" style="margin-top:6px;">base shift: -</div>
        <div class="metric"><span>当前 delta</span><strong id="deltaReadout">0.00 sec</strong></div>
        <input id="deltaSlider" class="delta-slider" type="range" min="-20" max="20" step="0.25" value="0" />
        <div class="row-actions">
          <button id="deltaMinus" class="small-button secondary">-0.25 sec</button>
          <button id="deltaPlus" class="small-button secondary">+0.25 sec</button>
        </div>
        <button id="freezeDelta" class="small-button" style="width:100%; margin-top:7px;">冻结 delta</button>
        <div id="deltaStats" class="empty" style="margin-top:6px;">未加载预览。</div>
      </div>
      <p class="side-title" style="margin-top:18px;">轨道</p>
      <div class="legend">
        <div class="legend-row"><span class="swatch" style="background:var(--g2)"></span> LIF G2 / Day0</div>
        <div class="legend-row"><span class="swatch" style="background:var(--r1)"></span> LIF R1 / Day9</div>
        <div class="legend-row"><span class="swatch" style="background:var(--r2)"></span> LIF R2 / Day3</div>
        <div class="legend-row"><span class="swatch" style="background:var(--ms760)"></span> MS PC34 760</div>
        <div class="legend-row"><span class="swatch" style="background:var(--ms782)"></span> MS QC 782</div>
      </div>
      <p class="side-title" style="margin-top:18px;">已加载</p>
      <div id="loaded" class="empty">-</div>
      <div id="baseTimePanel">
        <p id="baseTimeTitle" class="side-title" style="margin-top:18px;">自动时间校正</p>
        <div class="metric"><span id="modeMetricLabel">模式</span><strong id="modeLabel">-</strong></div>
        <div class="metric"><span id="greenMetricLabel">G2 显示平移</span><strong id="greenShift">-</strong></div>
        <div class="metric"><span id="redMetricLabel">R1/R2 显示平移</span><strong id="redShift">-</strong></div>
        <div id="msDeltaMetric" class="metric"><span>MS 后段 delta</span><strong id="msDeltaShift">-</strong></div>
        <div class="metric"><span id="matchMetricLabel">QC 组三元组</span><strong id="matchCount">-</strong></div>
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
        <button id="acceptWindow" class="small-button" style="width:100%; margin:8px 0 2px;">接受本窗口待审自动候选</button>
        <div id="acceptWindowHint" class="empty" style="margin:0 0 6px;">将接受 0 条</div>
        <div id="reviewHelp" class="empty" style="margin:4px 0 8px;">
          残差 = MS760 时间 - QC anchor 双 LIF 校正后组合时间，单位 sec；越接近 0 表示时间对齐越好。
        </div>
        <div id="candidateList" class="candidate-list"></div>
      </div>
      <div id="manualPanel">
        <p id="manualPanelTitle" class="side-title" style="margin-top:18px;">手动 QC anchor</p>
        <div class="manual-box">
          <button id="manualMode" class="small-button secondary">选择峰</button>
          <button id="clearManual" class="small-button secondary">清空</button>
          <div class="manual-selection">
            <div id="manualLifRow" style="display:none;">LIF: <strong id="manualLIF">-</strong></div>
            <div>G2: <strong id="manualG2">-</strong></div>
            <div>R1: <strong id="manualR1">-</strong></div>
            <div id="manualR2Row" style="display:none;">R2: <strong id="manualR2">-</strong></div>
            <div>MS760: <strong id="manualMS">-</strong></div>
          </div>
          <button id="createManual" class="small-button">建立并接受</button>
        <div id="manualHelp" class="empty" style="margin-top:6px;">开启后依次点击项目配置的两个 QC anchor LIF 峰和 MS760 峰。</div>
        </div>
      </div>
    </aside>

    <section class="plot-panel">
      <div class="controls plot-controls">
        <button id="prev" title="上一窗口" aria-label="上一窗口">&#8592;</button>
        <button id="next" title="下一窗口" aria-label="下一窗口">&#8594;</button>
        <label>
          <span class="policy">图窗起点</span>
          <input id="start" type="number" step="0.1" value="0" />
        </label>
        <label>
          <span class="policy">窗口宽度</span>
          <input id="widthDisplay" type="text" value="2.5 min" readonly />
        </label>
        <label>
          <span class="policy">时间轴</span>
          <select id="timeMode">
            <option value="aligned" selected>校正后</option>
            <option value="raw">原始</option>
          </select>
        </label>
        <label>
          <span class="policy">Y轴</span>
          <select id="yAxisMode">
            <option value="full" selected>完整</option>
            <option value="robust">稳健放大</option>
          </select>
        </label>
        <button id="go">显示窗口</button>
      </div>
      <div class="window-readout">
        <strong id="title">同步 2.5 min 窗口</strong>
        <span>主窗口固定 2.5 min，边界额外载入 ±0.08 min；各轨道刻度和峰旁数字仍为原始时间(min)</span>
      </div>
      <svg id="chart" role="img" aria-label="Synchronized LIF and MS tracks"></svg>
    </section>
  </main>
  <div id="tooltip" class="tooltip"></div>
  <div id="lineContextMenu" class="context-menu"></div>
  <div id="importModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="importTitle">
    <div class="modal">
      <div class="modal-head">
        <div>
          <p id="importTitle" class="modal-title">新建标注项目</p>
          <div class="empty">选择项目保存路径，配置本项目采用的 3 个 LIF 通道、QC anchor 组合和 1 个 MS 原始文件。</div>
        </div>
        <button id="closeImportProject" class="small-button secondary">关闭</button>
      </div>
      <div class="import-grid">
        <label>Raw 数据管理</label>
        <div class="mode-options">
          <label class="mode-option"><input type="radio" name="rawInputMode" value="external_reference" checked /> 外部引用</label>
          <label class="mode-option"><input type="radio" name="rawInputMode" value="copy_into_project" /> 复制到项目</label>
        </div>
        <label for="importProjectDir">项目保存路径</label>
        <div class="path-picker-row">
          <input id="importProjectDir" type="text" />
          <button class="small-button secondary path-picker-button" data-picker-target="importProjectDir" data-picker-kind="directory" data-picker-title="选择项目保存路径">选择</button>
        </div>
        <label>LIF 1</label>
        <div class="lif-input-grid">
          <input id="importLif1Channel" type="text" value="G2" aria-label="LIF 1 通道" />
          <input id="importLif1Identity" type="text" value="Day0" aria-label="LIF 1 身份" />
          <input id="importLif1Path" type="text" aria-label="LIF 1 文件路径" />
          <button class="small-button secondary path-picker-button" data-picker-target="importLif1Path" data-picker-kind="file" data-picker-role="lif" data-picker-title="选择 LIF 1 原始文件">选择</button>
        </div>
        <label>LIF 2</label>
        <div class="lif-input-grid">
          <input id="importLif2Channel" type="text" value="R1" aria-label="LIF 2 通道" />
          <input id="importLif2Identity" type="text" value="Day9" aria-label="LIF 2 身份" />
          <input id="importLif2Path" type="text" aria-label="LIF 2 文件路径" />
          <button class="small-button secondary path-picker-button" data-picker-target="importLif2Path" data-picker-kind="file" data-picker-role="lif" data-picker-title="选择 LIF 2 原始文件">选择</button>
        </div>
        <label>LIF 3</label>
        <div class="lif-input-grid">
          <input id="importLif3Channel" type="text" value="R2" aria-label="LIF 3 通道" />
          <input id="importLif3Identity" type="text" value="Day3" aria-label="LIF 3 身份" />
          <input id="importLif3Path" type="text" aria-label="LIF 3 文件路径" />
          <button class="small-button secondary path-picker-button" data-picker-target="importLif3Path" data-picker-kind="file" data-picker-role="lif" data-picker-title="选择 LIF 3 原始文件">选择</button>
        </div>
        <label>QC anchor</label>
        <div class="qc-anchor-grid">
          <select id="importQcAnchorA" aria-label="QC anchor LIF A"></select>
          <select id="importQcAnchorB" aria-label="QC anchor LIF B"></select>
        </div>
        <label for="importMs">MS 文件</label>
        <div class="path-picker-row">
          <input id="importMs" type="text" />
          <button class="small-button secondary path-picker-button" data-picker-target="importMs" data-picker-kind="file" data-picker-role="ms" data-picker-title="选择 MS 原始文件">选择</button>
        </div>
      </div>
      <div id="importHint" class="empty" style="margin-top:10px;"></div>
      <div class="modal-actions">
        <button id="runImportProject" class="small-button">生成并进入项目</button>
      </div>
    </div>
  </div>
  <div id="openProjectModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="openProjectTitle">
    <div class="modal">
      <div class="modal-head">
        <div>
          <p id="openProjectTitle" class="modal-title">打开已有项目</p>
          <div class="empty">选择包含中间表和 annotation.sqlite 的项目目录；如果存在 lifms_project.json 会优先作为项目说明。</div>
        </div>
        <button id="closeOpenProject" class="small-button secondary">关闭</button>
      </div>
      <div class="import-grid">
        <label for="openProjectDir">项目目录</label>
        <div class="path-picker-row">
          <input id="openProjectDir" type="text" placeholder="/path/to/existing_project" />
          <button class="small-button secondary path-picker-button" data-picker-target="openProjectDir" data-picker-kind="directory" data-picker-title="选择已有项目目录">选择</button>
        </div>
      </div>
      <div id="openProjectHint" class="empty" style="margin-top:10px;">项目目录应包含 data/interim/v3 和 annotation_app/annotations/annotation.sqlite。</div>
      <div class="modal-actions">
        <button id="runOpenProject" class="small-button">打开项目</button>
      </div>
    </div>
  </div>
  <div id="projectConfigModal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="projectConfigTitle">
    <div class="modal">
      <div class="modal-head">
        <div>
          <p id="projectConfigTitle" class="modal-title">配置</p>
          <div class="empty">Acquisition / phase 时间节点</div>
        </div>
        <button id="closeConfigProject" class="small-button secondary">关闭</button>
      </div>
      <div id="timeConfigPanel" class="config-grid">
        <span>QC 结束(min)</span><input id="cfgQcEnd" type="number" step="0.1" />
        <span>标注起点(min)</span><input id="cfgAnnotationStart" type="number" step="0.1" />
        <span>预校准窗口(min)</span><input id="cfgSeedWindow" type="number" step="0.5" />
      </div>
      <div class="modal-actions">
        <button id="saveConfig" class="small-button">保存项目时间节点</button>
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
      current: null,
      selectedCandidateId: null,
      showRejected: false,
      manualMode: false,
      stage: 'qc_calibration',
      previewDeltaSec: null,
      localDeltaPreview: null,
      manual: { LIF: null, MS760: null },
      requestSeq: 0,
      actionBusy: false
    };
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
      const layoutChannels = state.meta?.acquisition_layout?.lif_channels || [];
      const lifTracks = layoutChannels.length
        ? layoutChannels.map((row) => ({
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

    function qcAnchorChannels() {
      return state.meta?.acquisition_layout?.qc_anchor_channels || state.current?.alignment?.qc_anchor_channels || ['G2', 'R1'];
    }

    function resetManualSelection() {
      state.manual = { anchorA: null, anchorB: null, LIF: null, MS760: null };
    }

    const el = (id) => document.getElementById(id);

    function fmt(n, digits = 2) {
      if (n === null || n === undefined || Number.isNaN(Number(n))) return '';
      return Number(n).toFixed(digits);
    }

    function fmtMaybe(n, digits = 3) {
      const text = fmt(n, digits);
      return text || 'NA';
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

    async function responseErrorMessage(res) {
      const text = await res.text();
      try {
        const parsed = JSON.parse(text);
        if (parsed && parsed.error) return parsed.error;
      } catch (err) {
        // Fall back to raw response text below.
      }
      return text || `${res.status} ${res.statusText}`;
    }

    function syncBootstrapMode() {
      document.body.classList.toggle('bootstrap-mode', Boolean(state.meta?.bootstrap));
    }

    function applyLoadedProjectMeta(projectMeta) {
      state.meta = projectMeta;
      syncBootstrapMode();
      state.start = Math.max(0, state.meta.time_min_min);
      state.width = state.meta.default_window_min;
      state.timeMode = 'aligned';
      state.stage = 'qc_calibration';
      state.selectedCandidateId = null;
      state.previewDeltaSec = null;
      resetManualSelection();
      el('timeMode').value = state.timeMode;
      el('yAxisMode').value = state.yAxisMode;
      el('widthDisplay').value = `${fmt(state.width, 1)} min`;
      el('loaded').innerHTML = [
        `LIF trace 行数: ${state.meta.lif_trace_rows.toLocaleString()}`,
        `LIF 峰数: ${state.meta.lif_peak_rows.toLocaleString()}`,
        `MS 事件数: ${state.meta.ms_event_rows.toLocaleString()}`,
        `MS scan 数: ${state.meta.ms_scan_rows.toLocaleString()}`
      ].map(escapeText).join('<br>');
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

    function importLifRows() {
      return [1, 2, 3].map((idx) => ({
        key: `lif_${idx}`,
        path: el(`importLif${idx}Path`).value.trim(),
        channel: el(`importLif${idx}Channel`).value.trim().toUpperCase(),
        identity_prior: el(`importLif${idx}Identity`).value.trim(),
      }));
    }

    function refreshImportQcAnchorOptions() {
      const rows = importLifRows();
      const channels = rows.map(row => row.channel).filter(Boolean);
      const fallback = channels.length >= 2 ? channels : ['G2', 'R1', 'R2'];
      const currentA = el('importQcAnchorA').value || fallback[0] || '';
      const currentB = el('importQcAnchorB').value || fallback[1] || fallback[0] || '';
      ['importQcAnchorA', 'importQcAnchorB'].forEach((targetId, idx) => {
        const select = el(targetId);
        select.innerHTML = '';
        fallback.forEach((channel) => {
          const option = document.createElement('option');
          option.value = channel;
          option.textContent = channel;
          select.appendChild(option);
        });
        const preferred = idx === 0 ? currentA : currentB;
        select.value = fallback.includes(preferred) ? preferred : (fallback[idx] || fallback[0] || '');
      });
      if (el('importQcAnchorA').value === el('importQcAnchorB').value && fallback.length > 1) {
        el('importQcAnchorB').value = fallback.find(ch => ch !== el('importQcAnchorA').value) || fallback[1];
      }
    }

    function setImportModal(open) {
      el('importModal').classList.toggle('open', open);
      if (open) {
        [
          'importProjectDir',
          'importLif1Path',
          'importLif2Path',
          'importLif3Path',
          'importMs',
        ].forEach((targetId) => {
          el(targetId).value = '';
          el(targetId).placeholder = '';
        });
        el('importLif1Channel').value = 'G2';
        el('importLif1Identity').value = 'Day0';
        el('importLif2Channel').value = 'R1';
        el('importLif2Identity').value = 'Day9';
        el('importLif3Channel').value = 'R2';
        el('importLif3Identity').value = 'Day3';
        refreshImportQcAnchorOptions();
        const externalMode = document.querySelector('input[name="rawInputMode"][value="external_reference"]');
        if (externalMode) externalMode.checked = true;
        el('importHint').textContent = '';
      }
    }

    function setOpenProjectModal(open) {
      el('openProjectModal').classList.toggle('open', open);
      if (open) {
        const projectDir = state.meta?.project?.project_dir || '';
        el('openProjectDir').placeholder = projectDir || '/path/to/existing_project';
        el('openProjectHint').textContent = '项目目录应包含 data/interim/v3 和 annotation_app/annotations/annotation.sqlite。';
      }
    }

    function setProjectConfigModal(open) {
      el('projectConfigModal').classList.toggle('open', open);
      if (open) renderConfigInputs();
    }

    function selectedRawInputMode() {
      const checked = document.querySelector('input[name="rawInputMode"]:checked');
      return checked ? checked.value : 'external_reference';
    }

    async function init() {
      state.meta = await fetchJson('/api/meta');
      syncBootstrapMode();
      state.start = Math.max(0, state.meta.time_min_min);
      state.width = state.meta.default_window_min;
      el('start').value = state.start.toFixed(2);
      el('widthDisplay').value = `${fmt(state.width, 1)} min`;
      el('timeMode').value = state.timeMode;
      el('yAxisMode').value = state.yAxisMode;
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
    }

    async function loadWindow() {
      const seq = ++state.requestSeq;
      hideLineContextMenu();
      document.body.classList.add('loading');
      let url = `/api/window?start_min=${encodeURIComponent(state.start)}&window_min=${encodeURIComponent(state.width)}&time_mode=${encodeURIComponent(state.timeMode)}`;
      if (state.stage === 'local_calibration' && state.previewDeltaSec !== null) {
        url += `&preview_ms_delta_sec=${encodeURIComponent(state.previewDeltaSec)}`;
      }
      const payload = await fetchJson(url);
      if (seq !== state.requestSeq) return;
      state.current = payload;
      state.start = state.current.start_min;
      state.meta.project_config = payload.project_config || state.meta.project_config;
      state.meta.time_model = payload.time_model || state.meta.time_model;
      el('start').value = state.start.toFixed(2);
      if (state.stage === 'local_calibration') await loadLocalDeltaPreview(state.previewDeltaSec);
      updateMetrics();
      draw();
      renderCandidateList();
      renderManualSelection();
      updateAcceptWindowButton();
      document.body.classList.remove('loading');
    }

    function updateMetrics() {
      const w = state.current;
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
      renderLocalDeltaPanel();
      renderStagePanels();
      updateExportHint();
      document.querySelectorAll('.stage-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.stage === state.stage);
      });
    }

    function updateExportHint() {
      const summary = state.current?.annotation_store || state.meta?.annotation_store || {};
      const accepted = Number(summary.counts?.accepted || 0);
      el('exportHint').textContent = `已接受 ${accepted.toLocaleString()} 条`;
    }

    function stageNote() {
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const frozen = tm.status === 'frozen';
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      const qcEnd = Number(cfg.qc_calibration_end_min || 10.5);
      const anchors = qcAnchorChannels();
      if (state.stage === 'local_calibration') return '标注起点后的未标注峰拓扑预校准：只估计 MS 后段局部平移，不产生 annotation。';
      if (state.stage === 'qc_survey') return frozen ? `冻结后段 time model 后的 QC 巡检候选：沿用 ${anchors[0]}-${anchors[1]}-MS760 anchor 线，用于后段 QC 证据巡检。` : '请先在“后段局部校正”中冻结 delta，再进入 QC 巡检。';
      if (state.stage === 'cell_annotation') return frozen ? '冻结 time model 后的高保守细胞候选：按 LIF 通道颜色连接到 MS760，默认逐条人工确认。' : '请先在“后段局部校正”中冻结 delta，再进入细胞标注。';
      return `前 ${fmt(qcEnd, 1)} min QC anchor 审核，用于确认 shift-only 时间校正。`;
    }

    function timeModelDisplayName(tm) {
      const status = tm.status === 'frozen' ? '冻结' : (tm.status === 'exploratory' ? '探索' : '草稿');
      const delta = Number(tm.ms_local_delta_sec || 0);
      return `${status} ${delta >= 0 ? '+' : ''}${fmt(delta, 2)}s`;
    }

    function updateTimeModelPanel() {
      const w = state.current;
      const tm = w?.time_model || state.meta?.time_model || {};
      const totalGroups = (w.alignment.qc_groups && w.alignment.qc_groups.groups ? w.alignment.qc_groups.groups.length : 0);
      const visibleGroups = (w.alignment_groups || []).length;
      el('greenShift').textContent = `${fmt(w.alignment.green_to_ms_shift_sec, 2)} sec`;
      el('redShift').textContent = `${fmt(w.alignment.red_to_ms_shift_sec, 2)} sec`;
      if (state.stage === 'qc_calibration') {
        el('baseTimeTitle').textContent = '自动时间校正';
        el('modeMetricLabel').textContent = '模式';
        el('modeLabel').textContent = w.time_mode === 'aligned' ? '校正后' : '原始';
        el('greenMetricLabel').textContent = 'green_axis 平移';
        el('redMetricLabel').textContent = 'red_axis 平移';
        el('msDeltaMetric').style.display = 'none';
        el('matchMetricLabel').textContent = 'QC 组三元组';
        el('matchCount').textContent = w.time_mode === 'aligned' ? `${visibleGroups}/${totalGroups}` : '-';
        return;
      }
      el('baseTimeTitle').textContent = '当前时间模型';
      el('modeMetricLabel').textContent = '状态';
      el('modeLabel').textContent = tm.status === 'frozen' ? '已冻结' : 'draft';
      el('greenMetricLabel').textContent = 'green_axis base shift';
      el('redMetricLabel').textContent = 'red_axis base shift';
      el('msDeltaMetric').style.display = 'grid';
      el('msDeltaShift').textContent = `${fmt(tm.ms_local_delta_sec || 0, 2)} sec`;
      el('matchMetricLabel').textContent = '时间模型';
      el('matchCount').textContent = timeModelDisplayName(tm);
      el('matchCount').title = String(tm.time_model_version || '');
    }

    function stageCounts() {
      if (!state.current) return { pending: 0, accepted: 0, rejected: 0 };
      if (state.stage === 'local_calibration') {
        return { pending: 0, accepted: 0, rejected: 0 };
      }
      if (state.stage === 'qc_survey') return state.current.post_qc_counts || {};
      if (state.stage === 'cell_annotation') return state.current.cell_counts || {};
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

    function candidateRows() {
      let rows = [];
      if (state.stage === 'local_calibration') {
        rows = [];
      } else if (state.stage === 'qc_survey') {
        rows = [...(state.current?.post_qc_candidates || [])];
      } else if (state.stage === 'cell_annotation') {
        rows = [...(state.current?.cell_candidates || [])];
      } else {
        rows = [...(state.current?.alignment_groups || [])];
      }
      const manualRows = (state.current?.annotations || [])
        .filter(row => row.source === 'manual_created')
        .filter(row => manualBelongsToStage(row, state.stage))
        .map(row => ({
          ...row,
          rank: '人工',
          candidate_id: row.annotation_id
        }));
      const combined = (state.stage === 'qc_calibration' || state.stage === 'qc_survey' || state.stage === 'cell_annotation') ? [...rows, ...manualRows] : rows;
      return combined
        .filter(row => state.showRejected || row.review_status !== 'rejected')
        .sort((a, b) => Number(a.ms_plot_time_min || a.ms_time_min || 0) - Number(b.ms_plot_time_min || b.ms_time_min || 0));
    }

    function manualBelongsToStage(row, stage) {
      const explicit = row.review_stage || '';
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

    function pendingAutoCandidatesInMainWindow() {
      if (!state.current) return [];
      if (state.stage === 'cell_annotation' || state.stage === 'local_calibration') return [];
      const start = Number(state.current.start_min);
      const end = Number(state.current.end_min);
      const rows = state.stage === 'qc_survey' ? (state.current.post_qc_candidates || []) : (state.current.alignment_groups || []);
      return rows.filter(row => {
        if (row.review_enabled === false) return false;
        if (row.source !== 'auto_candidate' || row.review_status !== 'pending') return false;
        const times = [row.g2_plot_time_min, row.r1_plot_time_min, row.ms_plot_time_min].map(Number);
        return times.every(t => Number.isFinite(t) && t >= start && t <= end);
      });
    }

    function updateAcceptWindowButton() {
      const n = pendingAutoCandidatesInMainWindow().length;
      if (state.stage === 'cell_annotation' || state.stage === 'local_calibration') {
        if (state.stage === 'local_calibration') {
          el('acceptWindowHint').textContent = '局部校正只做 preview，不写入 annotation';
        } else {
          el('acceptWindowHint').textContent = '细胞标注候选需逐条确认';
        }
        el('acceptWindow').disabled = true;
        return;
      }
      const tm = state.current?.time_model || state.meta?.time_model || {};
      if (state.stage === 'qc_survey' && tm.status !== 'frozen') {
        el('acceptWindowHint').textContent = 'draft preview：冻结 delta 后才允许写入 QC 巡检审核';
        el('acceptWindow').disabled = true;
        return;
      }
      el('acceptWindowHint').textContent = `将接受 ${n} 条 pending 自动候选`;
      el('acceptWindow').disabled = n === 0;
    }

    function renderConfigInputs() {
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      if (!cfg) return;
      el('cfgQcEnd').value = fmt(cfg.qc_calibration_end_min ?? 10.5, 1);
      el('cfgAnnotationStart').value = fmt(cfg.annotation_start_min ?? 40.0, 1);
      el('cfgSeedWindow').value = fmt(cfg.local_delta_seed_window_min ?? 2.5, 1);
    }

    function renderLocalDeltaPanel() {
      const visible = state.stage === 'local_calibration';
      el('localDeltaPanel').style.display = visible ? 'block' : 'none';
      if (!visible) return;
      const tm = state.current?.time_model || state.meta?.time_model || {};
      const activeDelta = Number(tm.ms_local_delta_sec || 0);
      const displayDelta = state.previewDeltaSec === null ? activeDelta : Number(state.previewDeltaSec);
      const previewChanged = Math.abs(displayDelta - activeDelta) > 1e-9;
      const statusLabel = tm.status === 'frozen' && previewChanged ? '预览' : (tm.status || 'draft');
      el('deltaReadout').textContent = `${fmt(displayDelta, 2)} sec (${escapeText(statusLabel)})`;
      el('freezeDelta').textContent = tm.status === 'frozen' ? (previewChanged ? '重新冻结' : '已冻结') : '冻结 delta';
      el('freezeDelta').disabled = tm.status === 'frozen' && !previewChanged;
      const align = state.current?.alignment || state.meta?.alignment || {};
      const axisSummary = Object.entries(align.axis_shifts_sec || {}).map(([axis, shift]) => `${axis} ${fmt(shift, 2)} sec`).join('；') || `green_axis ${fmt(align.green_to_ms_shift_sec, 2)} sec；red_axis ${fmt(align.red_to_ms_shift_sec, 2)} sec`;
      el('deltaBaseSummary').textContent = `base shift: ${axisSummary}；MS 后段 delta 单独调节`;
      el('deltaSlider').value = String(Math.max(-20, Math.min(20, displayDelta)));
      const p = state.localDeltaPreview;
      if (!p) {
        el('deltaStats').textContent = '未加载预览。';
        return;
      }
      el('deltaStats').textContent = [
        `证据 ${p.evidence_count || 0} 条`,
        `冲突 ${p.conflict_count || 0}`,
        `median |残差| ${fmt(p.median_abs_residual_sec, 3)} sec`,
        `p90 |残差| ${fmt(p.p90_abs_residual_sec, 3)} sec`,
        `seed ${fmt(p.seed_start_min, 2)}-${fmt(p.seed_end_min, 2)} min`,
        `contains_cell_labels=false`,
        previewChanged ? '未冻结预览，不写入数据库' : '当前已保存'
      ].join('；');
    }

    function renderStagePanels() {
      const local = state.stage === 'local_calibration';
      const qcCalibration = state.stage === 'qc_calibration';
      const qcSurvey = state.stage === 'qc_survey';
      const cell = state.stage === 'cell_annotation';
      const frozen = (state.current?.time_model || state.meta?.time_model || {}).status === 'frozen';
      el('baseTimePanel').style.display = local ? 'none' : 'block';
      el('reviewPanel').style.display = (local || ((qcSurvey || cell) && !frozen)) ? 'none' : 'block';
      el('manualPanel').style.display = (qcCalibration || (qcSurvey && frozen) || (cell && frozen)) ? 'block' : 'none';
      if (qcCalibration || (qcSurvey && frozen) || (cell && frozen)) {
        el('reviewPanel').parentNode.insertBefore(el('manualPanel'), el('reviewPanel'));
      }
      el('manualLifRow').style.display = cell ? 'block' : 'none';
      el('manualR2Row').style.display = 'none';
      el('manualPanelTitle').textContent = cell ? '手动细胞二元组' : '手动 QC anchor';
      el('acceptWindow').style.display = cell ? 'none' : 'block';
      if (cell) {
        el('reviewHelp').textContent = '残差 = MS760 时间 - LIF 峰校正后时间，单位 sec；候选必须逐条人工确认。';
      } else if (qcSurvey && !frozen) {
        el('reviewHelp').textContent = '当前为 draft preview：候选只用于检查 delta，冻结前不会写入 annotation。';
      } else {
        const anchors = qcAnchorChannels();
        el('reviewHelp').textContent = qcSurvey
          ? `残差 = MS760 时间 - 已选择 ${anchors[0]}/${anchors[1]} 校正后均值，单位 sec；缺失峰保存为 NA。`
          : `残差 = MS760 时间 - ${anchors[0]}/${anchors[1]} 校正后组合时间，单位 sec；越接近 0 表示时间对齐越好。`;
      }
      const anchors = qcAnchorChannels();
      el('manualHelp').textContent = qcSurvey
        ? `QC 巡检：MS760 必选；${anchors[0]}/${anchors[1]} 至少选择一个，缺失侧保存为 NA。`
        : cell
          ? '细胞标注：选择一个项目配置 LIF 峰和一个 MS760 峰，建立严格二元组。'
          : `QC 校正：必须依次选择 ${anchors[0]}、${anchors[1]}、MS760 峰。`;
    }

    async function saveProjectConfig() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      try {
        const currentCfg = state.current?.project_config || state.meta?.project_config || {};
        const payload = {
          qc_calibration_end_min: Number(el('cfgQcEnd').value),
          annotation_start_min: Number(el('cfgAnnotationStart').value),
          local_delta_seed_window_min: Number(el('cfgSeedWindow').value)
        };
        const tm = state.current?.time_model || state.meta?.time_model || {};
        const changedFrozenConfig = tm.status === 'frozen' && [
          'qc_calibration_end_min',
          'annotation_start_min',
          'local_delta_seed_window_min'
        ].some(key => Math.abs(Number(currentCfg[key] ?? 0) - Number(payload[key] ?? 0)) > 1e-9);
        if (changedFrozenConfig) {
          const ok = window.confirm('修改这些时间节点会清除当前已冻结的后段 time model。保存后需要重新进入“后段局部校正”并重新冻结 delta，旧后段候选不会再作为当前模型结果使用。是否继续？');
          if (!ok) return;
          payload.clear_frozen_time_model = true;
        }
        const result = await postJson('/api/project-config', payload);
        state.meta.project_config = result.project_config;
        state.meta.time_model = result.time_model;
        if (state.current) state.current.time_model = result.time_model;
        const cfg = result.project_config;
        const annotationStart = Number(cfg.annotation_start_min || state.start);
        const qcEnd = Number(cfg.qc_calibration_end_min || 10.5);
        if (state.stage === 'local_calibration') {
          state.start = annotationStart;
        } else if ((state.stage === 'qc_survey' || state.stage === 'cell_annotation') && Number(state.start) < annotationStart) {
          state.start = annotationStart;
        } else if (state.stage === 'qc_calibration' && Number(state.start) > qcEnd) {
          state.start = 0;
        }
        await loadWindow();
      } catch (err) {
        alert(`保存项目时间节点失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
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
        alert(`预览 delta 失败: ${err.message}`);
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
      const actionText = tm.status === 'frozen' ? '重新冻结当前预览 delta' : '冻结当前 MS 后段局部平移';
      if (!confirm(`${actionText}？冻结后才会解锁 QC 巡检写入和细胞标注候选。`)) return;
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
        alert(`冻结 delta 失败: ${err.message}`);
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
      const isCell = row.candidate_type === 'cell_high_confidence' || row.candidate_type === 'manual_cell_pair';
      if (isCell) return `${row.lif_channel || 'LIF'} ${fmt(row.lif_raw_time_min, 3)} → MS760 ${fmt(row.ms_time_min, 3)}`;
      return `${fmtMaybe(row.g2_raw_time_min, 3)} - ${fmtMaybe(row.r1_raw_time_min, 3)} - ${fmtMaybe(row.ms_time_min, 3)}`;
    }

    function contextActions(row) {
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
      if (!rows.length) {
        box.innerHTML = '<div class="empty">当前窗口没有可显示候选。</div>';
        return;
      }
      box.innerHTML = rows.map(row => {
        const id = row.annotation_id || row.candidate_id;
        const selected = id === state.selectedCandidateId ? ' selected' : '';
        const rejected = row.review_status === 'rejected' ? ' rejected' : '';
        const isCell = row.candidate_type === 'cell_high_confidence' || row.candidate_type === 'manual_cell_pair';
        const times = isCell
          ? `${row.lif_channel} ${fmt(row.lif_raw_time_min, 3)} → MS760 ${fmt(row.ms_time_min, 3)}`
          : `${fmtMaybe(row.g2_raw_time_min, 3)} - ${fmtMaybe(row.r1_raw_time_min, 3)} - ${fmtMaybe(row.ms_time_min, 3)}`;
        const residual = row.composite_to_ms_residual_sec ?? row.residual_sec;
        const canReview = row.review_enabled !== false && row.source !== 'preview';
        const displayStatus = row.review_enabled === false && row.review_status === 'pending' ? 'preview' : row.review_status;
        const actions = canReview ? `
              <button data-action="accepted" data-id="${escapeText(id)}">接受</button>
              <button data-action="rejected" data-id="${escapeText(id)}">拒绝</button>
              ${row.source === 'auto_candidate' ? `<button data-action="pending" data-id="${escapeText(id)}">待审</button>` : ''}
              ${row.source === 'manual_created' ? `<button data-action="clear_manual" data-id="${escapeText(id)}">清除</button>` : ''}
        ` : '<span class="empty">preview only</span>';
        return `
          <div class="candidate-row${selected}${rejected}" data-candidate-id="${escapeText(id)}">
            <div class="row-title"><span>${escapeText(sourceText(row.source))} #${escapeText(row.rank ?? '')}</span><span>${escapeText(statusText(displayStatus))}</span></div>
            <div class="row-sub">${escapeText(times)} min<br>残差 ${escapeText(fmt(residual, 3))} sec</div>
            <div class="row-actions">${actions}</div>
          </div>
        `;
      }).join('');
      box.querySelectorAll('.candidate-row').forEach(node => {
        node.addEventListener('click', (ev) => {
          if (ev.target && ev.target.dataset && ev.target.dataset.action) return;
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
    }

    async function reviewCandidate(annotationId, reviewStatus) {
      if (state.actionBusy) return;
      state.actionBusy = true;
      try {
        await postJson('/api/review', {
          annotation_id: annotationId,
          review_status: reviewStatus,
          window_start_min: state.current.start_min,
          window_end_min: state.current.end_min,
          time_mode: state.current.time_mode
        });
        await loadWindow();
      } catch (err) {
        alert(`审核写入失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    async function clearManualAnnotation(annotationId) {
      if (state.actionBusy) return;
      if (!confirm('清除这条人工误选记录？这会删除当前记录和它的 audit 事件。')) return;
      state.actionBusy = true;
      try {
        await postJson('/api/clear-manual', { annotation_id: annotationId });
        if (state.selectedCandidateId === annotationId) state.selectedCandidateId = null;
        await loadWindow();
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
        hint.textContent = '导出完成';
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
      const button = el('runImportProject');
      const oldText = button.textContent;
      button.textContent = '生成中...';
      el('importHint').textContent = '正在生成中间表；MS 文件较大时可能需要几分钟。';
      try {
        refreshImportQcAnchorOptions();
        const lifInputs = importLifRows();
        const channels = lifInputs.map(row => row.channel);
        if (lifInputs.some(row => !row.path || !row.channel)) {
          throw new Error('请为 3 个 LIF 输入填写文件路径和通道名。');
        }
        if (new Set(channels).size !== channels.length) {
          throw new Error('3 个 LIF 通道名不能重复。');
        }
        const qcAnchorChannels = [el('importQcAnchorA').value, el('importQcAnchorB').value];
        if (qcAnchorChannels[0] === qcAnchorChannels[1]) {
          throw new Error('QC anchor 必须选择两个不同的 LIF 通道。');
        }
        const result = await postJson('/api/import-project', {
          project_dir: el('importProjectDir').value,
          ms_path: el('importMs').value,
          raw_input_mode: selectedRawInputMode(),
          lif_inputs: lifInputs,
          qc_anchor_channels: qcAnchorChannels,
        });
        applyLoadedProjectMeta(result.meta);
        await loadWindow();
        setImportModal(false);
      } catch (err) {
        el('importHint').textContent = `导入失败: ${err.message}`;
        alert(`导入失败: ${err.message}`);
      } finally {
        button.textContent = oldText;
        state.actionBusy = false;
      }
    }

    async function openExistingProject() {
      if (state.actionBusy) return;
      state.actionBusy = true;
      const button = el('runOpenProject');
      const oldText = button.textContent;
      button.textContent = '打开中...';
      el('openProjectHint').textContent = '正在读取项目中间表和 SQLite。';
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
      const oldText = button.textContent;
      button.disabled = true;
      button.textContent = '选择中';
      el('importHint').textContent = '请在弹出的系统窗口中选择路径。';
      try {
        const result = await postJson('/api/select-path', {
          kind: button.dataset.pickerKind,
          title: button.dataset.pickerTitle || '选择路径',
          file_role: button.dataset.pickerRole || '',
          initial_dir: pickerInitialDir(targetId)
        });
        if (!result.cancelled && result.path) {
          target.value = result.path;
          target.focus();
          el('importHint').textContent = '';
        } else {
          el('importHint').textContent = '已取消选择；原路径保持不变。';
        }
      } catch (err) {
        el('importHint').textContent = `选择路径失败: ${err.message}`;
        alert(`选择路径失败: ${err.message}`);
      } finally {
        button.textContent = oldText;
        button.disabled = false;
      }
    }

    async function acceptWindowPendingAutoCandidates() {
      const n = pendingAutoCandidatesInMainWindow().length;
      if (state.actionBusy || n === 0) return;
      if (!confirm(`接受当前 2.5 min 主窗口内 ${n} 条待审自动候选？已接受、已拒绝、人工三元组不会被修改。`)) return;
      state.actionBusy = true;
      try {
        await postJson('/api/accept-window', {
          start_min: state.current.start_min,
          window_min: state.current.window_min,
          time_mode: state.current.time_mode,
          stage: state.stage
        });
        await loadWindow();
      } catch (err) {
        alert(`批量接受失败: ${err.message}`);
      } finally {
        state.actionBusy = false;
      }
    }

    function renderManualSelection() {
      const cell = state.stage === 'cell_annotation';
      const anchors = qcAnchorChannels();
      el('manualMode').classList.toggle('manual-mode-on', state.manualMode);
      el('manualMode').textContent = state.manualMode ? '选择中' : '选择峰';
      el('manualLIF').textContent = state.manual.LIF ? `${state.manual.LIF.channel} ${state.manual.LIF.id} (${fmt(state.manual.LIF.time, 3)})` : '-';
      el('manualG2').parentElement.querySelector('strong').textContent = anchors[0] || 'Anchor A';
      el('manualR1').parentElement.querySelector('strong').textContent = anchors[1] || 'Anchor B';
      el('manualG2').textContent = state.manual.anchorA ? `${state.manual.anchorA.id} (${fmt(state.manual.anchorA.time, 3)})` : '-';
      el('manualR1').textContent = state.manual.anchorB ? `${state.manual.anchorB.id} (${fmt(state.manual.anchorB.time, 3)})` : '-';
      el('manualR2').textContent = state.manual.R2 ? `${state.manual.R2.id} (${fmt(state.manual.R2.time, 3)})` : '-';
      el('manualMS').textContent = state.manual.MS760 ? `${state.manual.MS760.id} (${fmt(state.manual.MS760.time, 3)})` : '-';
      el('manualLifRow').style.display = cell ? 'block' : 'none';
      el('manualG2').parentElement.style.display = cell ? 'none' : 'block';
      el('manualR1').parentElement.style.display = cell ? 'none' : 'block';
      el('manualR2Row').style.display = 'none';
    }

    function selectManualPeak(kind, row) {
      if (!state.manualMode) return;
      if (state.stage === 'cell_annotation' && kind !== 'MS760') {
        state.manual.LIF = { id: row.peak_id, channel: row.channel, time: row.raw_time_min ?? row.time_min };
        state.manual.anchorA = null;
        state.manual.anchorB = null;
      } else {
        const selected = { id: kind === 'MS760' ? row.event_id : row.peak_id, channel: row.channel, time: row.raw_time_min ?? row.time_min };
        if (kind === 'MS760') {
          state.manual.MS760 = selected;
        } else {
          const anchors = qcAnchorChannels();
          if (row.channel === anchors[0]) state.manual.anchorA = selected;
          if (row.channel === anchors[1]) state.manual.anchorB = selected;
          state.manual.LIF = { id: row.peak_id, channel: row.channel, time: row.raw_time_min ?? row.time_min };
        }
      }
      renderManualSelection();
    }

    async function createManualTriplet() {
      if (state.actionBusy) return;
      const cell = state.stage === 'cell_annotation';
      const qcSurvey = state.stage === 'qc_survey';
      if (cell) {
        if (!state.manual.LIF || !state.manual.MS760) {
          alert('细胞标注需选择一个 LIF 峰和一个 MS760 峰。');
          return;
        }
        state.actionBusy = true;
        try {
          await postJson('/api/manual-cell-pair', {
            lif_channel: state.manual.LIF.channel,
            lif_peak_id: state.manual.LIF.id,
            ms_event_id: state.manual.MS760.id,
            window_start_min: state.current.start_min,
            window_end_min: state.current.end_min,
            time_mode: state.current.time_mode
          });
          resetManualSelection();
          state.manualMode = false;
          await loadWindow();
        } catch (err) {
          alert(`手动细胞二元组写入失败: ${err.message}`);
        } finally {
          state.actionBusy = false;
        }
        return;
      }
      const anchors = qcAnchorChannels();
      const hasAnyLif = Boolean(state.manual.anchorA || state.manual.anchorB);
      if (!state.manual.MS760 || (qcSurvey ? !hasAnyLif : (!state.manual.anchorA || !state.manual.anchorB))) {
        alert(qcSurvey ? `QC 巡检需选择 MS760，并至少选择 ${anchors[0]} 或 ${anchors[1]} 其中一个峰。` : `QC 校正需选择 ${anchors[0]}、${anchors[1]} 和 MS760 三个峰。`);
        return;
      }
      state.actionBusy = true;
      try {
        await postJson('/api/manual-triplet', {
          anchor_a_peak_id: state.manual.anchorA ? state.manual.anchorA.id : null,
          anchor_b_peak_id: state.manual.anchorB ? state.manual.anchorB.id : null,
          ms_event_id: state.manual.MS760.id,
          stage: state.stage,
          window_start_min: state.current.start_min,
          window_end_min: state.current.end_min,
          time_mode: state.current.time_mode
        });
        resetManualSelection();
        state.manualMode = false;
        await loadWindow();
      } catch (err) {
        alert(`手动 QC anchor 写入失败: ${err.message}`);
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
          state.current.lif_peaks
            .filter(p => track.channels.includes(p.channel))
            .forEach(p => {
              const peakY = lifPeakY(p);
              const c = svgEl('circle', {
                cx: xScale(p.plot_time_min),
                cy: yScale(peakY),
                r: p.close_peak_risk || p.merge_risk ? 4.5 : 3.4,
                fill: colorForChannel(p.channel),
                stroke: p.close_peak_risk || p.merge_risk ? '#b42318' : '#fff',
                'stroke-width': 1.3,
                class: 'peak-marker',
                tabindex: 0
              });
              c.__detail = { type: 'LIF 峰', data: p };
              attachHover(c);
              c.addEventListener('click', () => {
                selectManualPeak('LIF', p);
              });
              svg.appendChild(c);
              markerPositions[`lif:${p.peak_id}`] = { x: xScale(p.plot_time_min), y: yScale(peakY), channel: p.channel };
              addTimeLabel(svg, fmt(p.raw_time_min ?? p.time_min, 3), xScale(p.plot_time_min), yScale(peakY), top, signalBottom, x1, colorForChannel(p.channel), labelBoxes);
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
          state.current.ms_events.forEach(e => {
            const raw = track.trace === 'pc34_760_linear' ? e.pc34_760_apex : e.qc_782_apex;
            const y = Math.max(0, Number(raw || 0));
            const c = svgEl('circle', {
              cx: xScale(e.plot_time_min),
              cy: yScale(y),
              r: e.low_quality_scan_window || e.collision_risk_high ? 4.7 : 3.5,
              fill: track.trace === 'pc34_760_linear' ? colors.ms760 : colors.ms782,
              stroke: e.low_quality_scan_window || e.collision_risk_high ? '#b42318' : '#fff',
              'stroke-width': 1.3,
              class: 'peak-marker',
              tabindex: 0
            });
            c.__detail = { type: track.trace === 'pc34_760_linear' ? 'MS 事件 / 760' : 'MS 事件 / 782', data: e };
            attachHover(c);
            if (track.trace === 'pc34_760_linear') {
              c.addEventListener('click', () => selectManualPeak('MS760', e));
            }
            svg.appendChild(c);
            if (track.trace === 'pc34_760_linear') {
              markerPositions[`ms760:${e.event_id}`] = { x: xScale(e.plot_time_min), y: yScale(y) };
            }
            addTimeLabel(svg, fmt(e.raw_time_min ?? e.time_min, 3), xScale(e.plot_time_min), yScale(y), top, signalBottom, x1, track.trace === 'pc34_760_linear' ? colors.ms760 : colors.ms782, labelBoxes);
          });
        }
      });
      if (state.stage === 'qc_survey') {
        drawPostQcCandidates(svg, markerPositions);
        drawManualAnnotations(svg, markerPositions);
      } else if (state.stage === 'cell_annotation') {
        drawAcceptedQcSurveyAnnotations(svg, markerPositions);
        drawCellCandidates(svg, markerPositions);
        drawManualCellAnnotations(svg, markerPositions);
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

    function drawAlignmentGroups(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.alignment_groups || []).forEach(group => {
        if (group.review_status === 'rejected' && !state.showRejected) return;
        const g2 = markerPositions[`lif:${group.g2_peak_id}`];
        const r1 = markerPositions[`lif:${group.r1_peak_id}`];
        const ms = markerPositions[`ms760:${group.ms_event_id}`];
        if (!g2 || !r1 || !ms) return;
        const points = `${g2.x.toFixed(2)},${g2.y.toFixed(2)} ${r1.x.toFixed(2)},${r1.y.toFixed(2)} ${ms.x.toFixed(2)},${ms.y.toFixed(2)}`;
        const lineStyle = candidateLineStyle(group);
        const line = svgEl('polyline', {
          points,
          fill: 'none',
          stroke: lineStyle.stroke,
          'stroke-width': lineStyle.width,
          'stroke-dasharray': lineStyle.dash,
          opacity: lineStyle.opacity,
          'pointer-events': 'visibleStroke',
          cursor: 'pointer'
        });
        line.__detail = { type: 'QC 候选三元组', data: group };
        appendLineWithHitTarget(svg, line, group, () => {
          state.selectedCandidateId = group.annotation_id || group.candidate_id;
          renderCandidateList();
          draw();
        });
      });
    }

    function drawPostQcCandidates(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.post_qc_candidates || []).forEach(group => {
        if (group.review_status === 'rejected' && !state.showRejected) return;
        const g2 = markerPositions[`lif:${group.g2_peak_id}`];
        const r1 = markerPositions[`lif:${group.r1_peak_id}`];
        const ms = markerPositions[`ms760:${group.ms_event_id}`];
        if (!g2 || !r1 || !ms) return;
        const points = `${g2.x.toFixed(2)},${g2.y.toFixed(2)} ${r1.x.toFixed(2)},${r1.y.toFixed(2)} ${ms.x.toFixed(2)},${ms.y.toFixed(2)}`;
        const lineStyle = candidateLineStyle(group);
        const line = svgEl('polyline', {
          points,
          fill: 'none',
          stroke: lineStyle.stroke,
          'stroke-width': lineStyle.width,
          'stroke-dasharray': lineStyle.dash,
          opacity: lineStyle.opacity,
          'pointer-events': 'visibleStroke',
          cursor: 'pointer'
        });
        line.__detail = { type: 'QC 巡检候选三元组', data: group };
        appendLineWithHitTarget(svg, line, group, () => {
          state.selectedCandidateId = group.annotation_id || group.candidate_id;
          renderCandidateList();
          draw();
        });
      });
    }

    function drawCellCandidates(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.cell_candidates || []).forEach(row => {
        if (row.review_status === 'rejected' && !state.showRejected) return;
        const lif = markerPositions[`lif:${row.lif_peak_id}`];
        const ms = markerPositions[`ms760:${row.ms_event_id}`];
        if (!lif || !ms) return;
        const selected = (row.annotation_id || row.candidate_id) === state.selectedCandidateId;
        const baseColor = colors[row.lif_channel] || '#111827';
        const accepted = row.review_status === 'accepted';
        const rejected = row.review_status === 'rejected';
        const line = svgEl('line', {
          x1: lif.x.toFixed(2),
          y1: lif.y.toFixed(2),
          x2: ms.x.toFixed(2),
          y2: ms.y.toFixed(2),
          stroke: rejected ? '#98a2b3' : baseColor,
          'stroke-width': accepted ? 1.05 : (selected ? 1.8 : 1.25),
          'stroke-dasharray': accepted ? '' : '6 4',
          opacity: rejected ? 0.35 : (accepted ? 0.40 : 0.68),
          'pointer-events': 'visibleStroke',
          cursor: 'pointer'
        });
        line.__detail = { type: '高置信细胞候选', data: row };
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
        .forEach(row => {
          if (row.review_status === 'rejected' && !state.showRejected) return;
          const g2 = markerPositions[`lif:${row.g2_peak_id}`];
          const r1 = markerPositions[`lif:${row.r1_peak_id}`];
          const ms = markerPositions[`ms760:${row.ms_event_id}`];
          const present = [g2, r1, ms].filter(Boolean);
          if (!ms || present.length < 2) return;
          const style = candidateLineStyle(row);
          const points = present.map(p => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(' ');
          const line = svgEl('polyline', {
            points,
            fill: 'none',
            stroke: style.stroke,
            'stroke-width': style.width,
            'stroke-dasharray': style.dash,
            opacity: style.opacity,
            'pointer-events': 'visibleStroke',
            cursor: 'pointer'
          });
          line.__detail = { type: row.candidate_type === 'manual_qc_anchor_partial' ? '人工 QC anchor（二联）' : '人工 QC anchor（三元组）', data: row };
          appendLineWithHitTarget(svg, line, row, () => {
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
          if (row.review_status === 'rejected' && !state.showRejected) return;
          const lif = markerPositions[`lif:${row.lif_peak_id}`];
          const ms = markerPositions[`ms760:${row.ms_event_id}`];
          if (!lif || !ms) return;
          const selected = row.annotation_id === state.selectedCandidateId;
          const baseColor = colors[row.lif_channel] || '#111827';
          const style = candidateLineStyle(row);
          const line = svgEl('line', {
            x1: lif.x.toFixed(2),
            y1: lif.y.toFixed(2),
            x2: ms.x.toFixed(2),
            y2: ms.y.toFixed(2),
            stroke: row.review_status === 'rejected' ? '#98a2b3' : baseColor,
            'stroke-width': selected ? 1.65 : style.width,
            'stroke-dasharray': style.dash,
            opacity: row.review_status === 'accepted' ? 0.42 : style.opacity,
            'pointer-events': 'visibleStroke',
            cursor: 'pointer'
          });
          line.__detail = { type: '人工细胞二元组', data: row };
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
      if (!(type === 'qc_survey_post_10p5' || type.startsWith('manual_qc'))) return false;
      const cfg = state.current?.project_config || state.meta?.project_config || {};
      const annotationStart = Number(cfg.annotation_start_min || 40);
      const msTime = Number(row.ms_time_min);
      return Number.isFinite(msTime) && msTime >= annotationStart;
    }

    function drawAcceptedQcSurveyAnnotations(svg, markerPositions) {
      if (state.current.time_mode !== 'aligned') return;
      (state.current.cell_qc_anchors || [])
        .filter(isAcceptedQcSurveyRow)
        .forEach(row => {
          const g2 = markerPositions[`lif:${row.g2_peak_id}`];
          const r1 = markerPositions[`lif:${row.r1_peak_id}`];
          const ms = markerPositions[`ms760:${row.ms_event_id}`];
          const present = [g2, r1, ms].filter(Boolean);
          if (!ms || present.length < 2) return;
          const selected = (row.annotation_id || row.candidate_id) === state.selectedCandidateId;
          const line = svgEl('polyline', {
            points: present.map(p => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(' '),
            fill: 'none',
            stroke: '#111827',
            'stroke-width': selected ? 1.75 : 1.05,
            'stroke-dasharray': '',
            opacity: selected ? 0.55 : 0.28,
            'pointer-events': 'visibleStroke',
            cursor: 'pointer'
          });
          line.__detail = { type: '已接受 QC 巡检 anchor', data: row };
          appendLineWithHitTarget(svg, line, row, () => {
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
      if (row.review_status === 'rejected') {
        return { stroke: '#98a2b3', width: 1.0, dash: '3 5', opacity: 0.35 };
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
    }

    function addTimeLabel(svg, text, x, y, top, bottom, right, color, labelBoxes) {
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
    }

    function attachHover(node) {
      node.addEventListener('mousemove', (ev) => showDetail(node.__detail, ev.clientX, ev.clientY));
      node.addEventListener('mouseenter', (ev) => showDetail(node.__detail, ev.clientX, ev.clientY));
      node.addEventListener('mouseleave', hideTooltip);
      node.addEventListener('focus', (ev) => showDetail(node.__detail, window.innerWidth - 380, 90));
    }

    function detailText(detail) {
      const d = detail.data;
      const preferred = detail.type.includes('三元组') || detail.type.includes('二元组') || detail.type.includes('候选') || detail.type.includes('anchor') || detail.type.includes('QC 巡检') ? [
        'annotation_id', 'candidate_id', 'source', 'review_status', 'exportable',
        'candidate_type', 'lif_channel', 'lif_peak_id', 'g2_peak_id', 'r1_peak_id', 'r2_peak_id',
        'ms_event_id', 'lif_raw_time_min', 'g2_raw_time_min', 'r1_raw_time_min',
        'ms_time_min', 'lif_anchor_count', 'missing_lif_channels', 'missing_peak_symbol',
        'residual_sec', 'composite_to_ms_residual_sec',
        'abs_residual_sec', 'candidate_rank', 'selection_reason'
      ] : detail.type.startsWith('LIF') ? [
        'peak_id', 'channel', 'label', 'phase', 'time_min', 'time_sec', 'height',
        'prominence', 'snr', 'width_sec', 'area', 'nearest_gap_sec', 'close_peak_risk',
        'merge_risk', 'parent_raw_peak_ids'
      ] : [
        'event_id', 'event_strategy', 'scan_id', 'time_min', 'time_sec', 'apex_intensity',
        'peak_prominence', 'peak_width_sec', 'pc34_760_apex', 'qc_782_apex',
        'tic_apex', 'ratio_760_782_max_pseudo1', 'array_length_apex',
        'collision_risk_high', 'low_quality_scan_window', 'nearest_event_gap_sec'
      ];
      return `${detail.type}\n` + preferred
        .filter(k => d[k] !== undefined && d[k] !== null && d[k] !== '')
        .map(k => `${k}: ${typeof d[k] === 'number' ? fmt(d[k], Math.abs(d[k]) >= 100 ? 1 : 4) : d[k]}`)
        .join('\n');
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
      state.start = Number(el('start').value || 0);
      await loadWindow();
    });
    el('start').addEventListener('keydown', async (ev) => {
      if (ev.key === 'Enter') {
        state.start = Number(el('start').value || 0);
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
    document.querySelectorAll('.stage-tab').forEach(button => {
      button.addEventListener('click', async () => {
        hideLineContextMenu();
        state.stage = button.dataset.stage;
        state.selectedCandidateId = null;
        state.previewDeltaSec = null;
        const cfg = state.current?.project_config || state.meta?.project_config || {};
        const annotationStart = Number(cfg.annotation_start_min || state.start);
        if (state.stage === 'qc_survey' || state.stage === 'cell_annotation' || state.stage === 'local_calibration') {
          state.timeMode = 'aligned';
          el('timeMode').value = state.timeMode;
        }
        if (state.stage === 'local_calibration') {
          state.start = annotationStart;
        } else if ((state.stage === 'qc_survey' || state.stage === 'cell_annotation') && Number(state.start) < annotationStart) {
          state.start = annotationStart;
        }
        if (state.stage === 'qc_calibration' && Number(state.start) > Number(cfg.qc_calibration_end_min || 10.5)) {
          state.start = Math.max(0, Number(state.meta?.time_min_min || 0));
        }
        if (state.stage === 'local_calibration' || state.stage === 'qc_survey' || state.stage === 'cell_annotation' || state.stage === 'qc_calibration') {
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
    el('manualMode').addEventListener('click', () => {
      hideLineContextMenu();
      state.manualMode = !state.manualMode;
      renderManualSelection();
    });
    el('clearManual').addEventListener('click', () => {
      state.manual = { G2: null, R1: null, R2: null, LIF: null, MS760: null };
      renderManualSelection();
    });
    el('createManual').addEventListener('click', createManualTriplet);
    el('exportAcceptedCsv').addEventListener('click', exportAcceptedCsv);
    el('openImportProject').addEventListener('click', () => setImportModal(true));
    el('openExistingProject').addEventListener('click', () => setOpenProjectModal(true));
    el('openConfigProject').addEventListener('click', () => setProjectConfigModal(true));
    el('bootstrapNewProject').addEventListener('click', () => setImportModal(true));
    el('bootstrapOpenProject').addEventListener('click', () => setOpenProjectModal(true));
    el('closeImportProject').addEventListener('click', () => setImportModal(false));
    el('closeOpenProject').addEventListener('click', () => setOpenProjectModal(false));
    el('closeConfigProject').addEventListener('click', () => setProjectConfigModal(false));
    el('importModal').addEventListener('click', (ev) => {
      if (ev.target === el('importModal')) setImportModal(false);
    });
    el('openProjectModal').addEventListener('click', (ev) => {
      if (ev.target === el('openProjectModal')) setOpenProjectModal(false);
    });
    el('projectConfigModal').addEventListener('click', (ev) => {
      if (ev.target === el('projectConfigModal')) setProjectConfigModal(false);
    });
    document.querySelectorAll('[data-picker-target]').forEach(button => {
      button.addEventListener('click', () => selectImportPath(button));
    });
    [1, 2, 3].forEach((idx) => {
      el(`importLif${idx}Channel`).addEventListener('input', refreshImportQcAnchorOptions);
    });
    el('runImportProject').addEventListener('click', importProject);
    el('runOpenProject').addEventListener('click', openExistingProject);
    el('acceptWindow').addEventListener('click', acceptWindowPendingAutoCandidates);
    el('saveConfig').addEventListener('click', saveProjectConfig);
    el('estimateDelta').addEventListener('click', estimateLocalDelta);
    el('freezeDelta').addEventListener('click', freezeLocalDelta);
    el('deltaMinus').addEventListener('click', () => updateDeltaPreview(Number(el('deltaSlider').value || 0) - 0.25));
    el('deltaPlus').addEventListener('click', () => updateDeltaPreview(Number(el('deltaSlider').value || 0) + 0.25));
    el('deltaSlider').addEventListener('change', () => updateDeltaPreview(Number(el('deltaSlider').value || 0)));
    document.addEventListener('click', hideLineContextMenu);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') hideLineContextMenu();
    });
    window.addEventListener('scroll', hideLineContextMenu, true);
    window.addEventListener('resize', () => { hideLineContextMenu(); if (state.current) draw(); });
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

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, payload: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/html") -> None:
        raw = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_csv_download(self, payload: str, filename: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = payload.encode("utf-8-sig")
        ascii_filename = re.sub(r'[^A-Za-z0-9._ -]+', "_", filename).strip(" ._") or "accepted_annotations.csv"
        self.send_response(status)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
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
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_text(HTML)
                return
            if parsed.path == "/api/meta":
                self.send_json(self.data.meta())
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
                self.send_json(
                    self.data.window(
                        start_min=start_min,
                        window_min=window_min,
                        time_mode=time_mode,
                        preview_ms_delta_sec=preview_ms_delta_sec,
                        lif_signal_mode=lif_signal_mode,
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
            self.send_json({"error": f"Not found: {parsed.path}"}, HTTPStatus.NOT_FOUND)
        except BadRequest as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
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
                )
                self.send_json({"ok": True, "annotation": row, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/project-config":
                config = self.data.update_project_config(payload)
                self.send_json({"ok": True, "project_config": config, "time_model": self.data.active_time_model()})
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
                row = self.data.create_manual_triplet(
                    optional_peak_id(payload.get("anchor_a_peak_id")) or optional_peak_id(payload.get("g2_peak_id")),
                    optional_peak_id(payload.get("anchor_b_peak_id")) or optional_peak_id(payload.get("r1_peak_id")),
                    optional_peak_id(payload.get("ms_event_id")) or "",
                    stage=str(payload.get("stage", "qc_calibration")),
                    window_start_min=clean_value(payload.get("window_start_min")),
                    window_end_min=clean_value(payload.get("window_end_min")),
                    time_mode=str(payload.get("time_mode", "")) or None,
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
                row = self.data.clear_manual_annotation(annotation_id)
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
                )
                self.send_json({"ok": True, "result": result, "summary": self.data.store.summary()})
                return
            if parsed.path == "/api/export-accepted-csv":
                result = self.data.export_accepted_annotations_csv()
                self.send_csv_download(result["csv_text"], result["filename"])
                return
            if parsed.path == "/api/select-path":
                kind = str(payload.get("kind", "")).strip()
                result = choose_native_path(
                    kind=kind,
                    title=str(payload.get("title", "")).strip(),
                    initial_dir=str(payload.get("initial_dir", "")).strip(),
                    file_role=str(payload.get("file_role", "")).strip(),
                )
                self.send_json(result)
                return
            if parsed.path == "/api/import-project":
                lif_inputs_payload = payload.get("lif_inputs")
                if isinstance(lif_inputs_payload, list) and lif_inputs_payload:
                    required = ["project_dir", "ms_path"]
                else:
                    required = ["project_dir", "lif_g2_path", "lif_r1_path", "lif_r2_path", "ms_path"]
                missing = [key for key in required if not str(payload.get(key, "")).strip()]
                if missing:
                    raise BadRequest(f"缺少导入路径字段: {', '.join(missing)}")
                if isinstance(lif_inputs_payload, list) and lif_inputs_payload:
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
                            }
                        )
                    new_data = AppData.create_project_from_raw_inputs(
                        project_dir=Path(str(payload["project_dir"])),
                        ms_path=Path(str(payload["ms_path"])),
                        raw_input_mode=str(payload.get("raw_input_mode", RAW_INPUT_MODE_EXTERNAL)),
                        lif_inputs=lif_inputs,
                        qc_anchor_channels=list(payload.get("qc_anchor_channels") or []),
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
            self.send_json({"error": f"Not found: {parsed.path}"}, HTTPStatus.NOT_FOUND)
        except BadRequest as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


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
    if project_was_selected:
        try:
            data: AppData | BootstrapAppData = AppData.load(project)
        except FileNotFoundError as exc:
            data = BootstrapAppData(project=project, load_error=str(exc), project_selected=True)
            print(f"Preprocessing inputs are not loaded yet: {exc}", flush=True)
            print("Open the browser and use 新建项目 or 打开项目 to continue.", flush=True)
    else:
        data = BootstrapAppData(project=project, load_error="", project_selected=False)
        print("No project loaded yet. Open the browser and use 新建项目 or 打开项目.", flush=True)
    AnnotationHandler.data = data
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "WARNING: this MVP has no authentication. Prefer --host 127.0.0.1; "
            f"current host {args.host!r} may expose data beyond this machine.",
            flush=True,
        )
    server = ThreadingHTTPServer((args.host, args.port), AnnotationHandler)
    if isinstance(data, AppData):
        print(f"Loaded annotation preprocessing inputs from {project.project_dir}")
        print(f"Annotation DB: {project.annotation_db_path}")
    print(f"Open http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
