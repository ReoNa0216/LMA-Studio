#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import find_peaks, peak_prominences, peak_widths

try:
    from scripts.v3.project_protocol import (
        classify_project_phase,
        load_project_protocol,
        phase_boundaries_min,
        phase_role_from_labels,
    )
except ModuleNotFoundError:  # Direct execution from scripts/v3.
    from project_protocol import (  # type: ignore[no-redef]
        classify_project_phase,
        load_project_protocol,
        phase_boundaries_min,
        phase_role_from_labels,
    )


ROOT = Path.cwd()
STEP = "02_ms_event_calling"

INPUT_LOCK = ROOT / "results/tables/v3/00_allowed_inputs.csv"
OUT_DATA = ROOT / "data/interim/v3" / STEP
OUT_TABLE = ROOT / "results/tables/v3" / STEP
OUT_FIG = ROOT / "results/figures/v3" / STEP
OUT_QC = ROOT / "results/qc/v3" / STEP
OUT_REPORT = ROOT / "reports/v3/02_ms_event_calling.md"

TOLERANCE_PPM = 10.0
PSEUDOCOUNT = 1.0
BIN_SIZE_MIN = 2.0
COLLISION_GAP_SEC = 0.60
BROAD_PEAK_WIDTH_SEC = 1.50
LOW_TIC_THRESHOLD = 1e6
LOW_ARRAY_LENGTH_THRESHOLD = 6000
LOW_ARRAY_LENGTH_SEVERE = 1000
TIC_SUPPORT_TOL_SEC = 0.75
PC34_FALLBACK_HEIGHT_FRACTION = 0.10
PC34_FALLBACK_PROMINENCE_FRACTION = 0.10
PROJECT_PHASE_POLICY = load_project_protocol(
    ROOT,
    allow_unbound_module_default=True,
)

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


def configure_project_root(
    project_dir: str | Path,
    *,
    allow_unbound_module_default: bool = False,
) -> Path:
    global ROOT, INPUT_LOCK, OUT_DATA, OUT_TABLE, OUT_FIG, OUT_QC, OUT_REPORT, PROJECT_PHASE_POLICY
    ROOT = Path(project_dir).expanduser().resolve()
    INPUT_LOCK = ROOT / "results/tables/v3/00_allowed_inputs.csv"
    OUT_DATA = ROOT / "data/interim/v3" / STEP
    OUT_TABLE = ROOT / "results/tables/v3" / STEP
    OUT_FIG = ROOT / "results/figures/v3" / STEP
    OUT_QC = ROOT / "results/qc/v3" / STEP
    OUT_REPORT = ROOT / "reports/v3/02_ms_event_calling.md"
    PROJECT_PHASE_POLICY = load_project_protocol(
        ROOT,
        allow_unbound_module_default=allow_unbound_module_default,
    )
    return ROOT

RE_INDEX = re.compile(r"^\s*index:\s*(\d+)")
RE_SCAN_ID = re.compile(r"id:\s*scanId=(\d+)")
RE_ARRAY_LENGTH = re.compile(r"defaultArrayLength:\s*(\d+)")
RE_BASE_PEAK_MZ = re.compile(r"base peak m/z,\s*([0-9.eE+-]+)")
RE_BASE_PEAK_INTENSITY = re.compile(r"base peak intensity,\s*([0-9.eE+-]+)")
RE_TIC = re.compile(r"total ion current,\s*([0-9.eE+-]+)")
RE_LOWEST_MZ = re.compile(r"lowest observed m/z,\s*([0-9.eE+-]+)")
RE_HIGHEST_MZ = re.compile(r"highest observed m/z,\s*([0-9.eE+-]+)")
RE_SCAN_START_TIME = re.compile(r"scan start time,\s*([0-9.eE+-]+),\s*minute")
RE_SPECTRUM_LIST = re.compile(r"spectrumList \((\d+) spectra\)")


@dataclass(frozen=True)
class MarkerSpec:
    prefix: str
    mz: float
    label: str
    role: str


MARKERS = [
    MarkerSpec("pc34_760", 760.5851, "PC34 / 760.5851", "primary MS event trace"),
    MarkerSpec("qc_782", 782.5616, "782.5616", "QC marker support"),
]


@dataclass(frozen=True)
class StrategySpec:
    strategy: str
    signal_col: str
    height: float
    prominence: float
    min_distance_sec: float
    role: str


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_fingerprint(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "size_bytes": np.nan, "head_sha256_1mb": "", "tail_sha256_1mb": ""}
    stat = path.stat()
    size = stat.st_size
    block = 1024 * 1024
    with path.open("rb") as fh:
        head = fh.read(min(block, size))
        if size > block:
            fh.seek(max(0, size - block))
            tail = fh.read(block)
        else:
            tail = head
    return {
        "exists": True,
        "size_bytes": int(size),
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "head_sha256_1mb": sha256_bytes(head),
        "tail_sha256_1mb": sha256_bytes(tail),
    }


def assert_first_principles_path(path: Path) -> None:
    text = str(path)
    for part in FORBIDDEN_PATH_PARTS:
        if part in text:
            raise ValueError(f"V3-02 forbidden input path detected: {path}")


def resolve_project_input_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def project_display_path(path: Path) -> str:
    try:
        return str(path.absolute().relative_to(ROOT.absolute()))
    except ValueError:
        return str(path.absolute())


def load_ms_path() -> Path:
    if not INPUT_LOCK.exists():
        raise FileNotFoundError(f"Run V3-00 first; missing {INPUT_LOCK}")
    allowed = pd.read_csv(INPUT_LOCK)
    ms = allowed[allowed["input_class"].eq("raw_ms_spectra")].copy()
    if len(ms) != 1:
        raise ValueError(f"Expected exactly 1 raw MS input from V3-00, found {len(ms)}")
    row = ms.iloc[0]
    if str(row["allowed_stage"]) != "V3-01~V3-06 main workflow":
        raise ValueError(f"V3-02 received non-main-workflow MS input: {row['allowed_stage']}")
    path = resolve_project_input_path(str(row["path"]))
    assert_first_principles_path(path)
    current = file_fingerprint(path)
    for col in ["size_bytes", "head_sha256_1mb", "tail_sha256_1mb"]:
        locked = row.get(col, "")
        if pd.isna(locked):
            locked = ""
        current_value = current[col]
        if col == "size_bytes":
            if int(current_value) != int(locked):
                raise ValueError(f"V3-02 input fingerprint mismatch for {path}: {col}")
        elif str(locked) and str(current_value) != str(locked):
            raise ValueError(f"V3-02 input fingerprint mismatch for {path}: {col}")
    return path


def apply_plot_style() -> None:
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = ["Arial", "DejaVu Sans", "Liberation Sans"]
    plt.rcParams["svg.fonttype"] = "none"
    mpl.rcParams.update(
        {
            "pdf.fonttype": 42,
            "font.size": 8,
            "axes.spines.right": False,
            "axes.spines.top": False,
            "axes.linewidth": 0.8,
            "axes.labelsize": 8,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "legend.frameon": False,
            "legend.fontsize": 7,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def save_png(fig: mpl.figure.Figure, path: Path, dpi: int = 300) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def md_table(df: pd.DataFrame, float_digits: int = 4, max_rows: int | None = None) -> str:
    if max_rows is not None:
        df = df.head(max_rows)
    if df.empty:
        return "_无记录_"
    formatted = df.copy()
    for col in formatted.columns:
        if pd.api.types.is_float_dtype(formatted[col]):
            formatted[col] = formatted[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.{float_digits}f}")
        else:
            formatted[col] = formatted[col].map(lambda x: "" if pd.isna(x) else str(x))
    header = "| " + " | ".join(formatted.columns.astype(str)) + " |"
    sep = "| " + " | ".join(["---"] * len(formatted.columns)) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in formatted.astype(str).to_numpy()]
    return "\n".join([header, sep, *rows])


def fmt_size(n_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def array_from_binary_line(line: str) -> np.ndarray:
    payload = line.split("]", 1)[1]
    return np.fromstring(payload, sep=" ", dtype=np.float64)


def init_marker_fields(current: dict) -> None:
    for marker in MARKERS:
        prefix = marker.prefix
        current[f"{prefix}_n_mz"] = 0
        current[f"{prefix}_closest_mz"] = np.nan
        current[f"{prefix}_closest_ppm_error"] = np.nan
        current[f"{prefix}_max_intensity"] = 0.0
        current[f"{prefix}_sum_intensity"] = 0.0
        current[f"{prefix}_mz_at_max_intensity"] = np.nan
        current[f"{prefix}_ppm_error_at_max_intensity"] = np.nan


def parse_ms_scan_summary(path: Path, progress_every: int = 10000) -> tuple[pd.DataFrame, dict]:
    started = time.time()
    rows: list[dict] = []
    metadata_spectrum_count = None
    historical_label_seen = False
    correct_label_seen = "Day0-G2_Day3-R2_Day9-R1" in path.name

    bounds = {}
    for marker in MARKERS:
        tol_mz = marker.mz * TOLERANCE_PPM * 1e-6
        bounds[marker.prefix] = (marker.mz, marker.mz - tol_mz, marker.mz + tol_mz)

    in_spectrum = False
    current: dict = {}
    array_mode: str | None = None
    target_indices: dict[str, np.ndarray] = {}
    target_mz_values: dict[str, np.ndarray] = {}
    bytes_seen = 0

    with path.open("r", encoding="ascii", errors="replace", newline="") as fh:
        for line in fh:
            bytes_seen += len(line.encode("ascii", errors="replace"))
            stripped = line.strip()

            if "Day0-G2_Day1-R2_Day3-R1" in stripped:
                historical_label_seen = True
            if "Day0-G2_Day3-R2_Day9-R1" in stripped:
                correct_label_seen = True

            if not in_spectrum:
                match = RE_SPECTRUM_LIST.search(stripped)
                if match:
                    metadata_spectrum_count = int(match.group(1))
                if stripped == "spectrum:":
                    in_spectrum = True
                    current = {}
                    init_marker_fields(current)
                    array_mode = None
                    target_indices = {}
                    target_mz_values = {}
                continue

            matched_metadata = False
            for regex, key, caster in [
                (RE_INDEX, "spectrum_index", int),
                (RE_SCAN_ID, "scan_id", int),
                (RE_ARRAY_LENGTH, "array_length", int),
                (RE_BASE_PEAK_MZ, "base_peak_mz", float),
                (RE_BASE_PEAK_INTENSITY, "base_peak_intensity", float),
                (RE_TIC, "tic", float),
                (RE_LOWEST_MZ, "lowest_observed_mz", float),
                (RE_HIGHEST_MZ, "highest_observed_mz", float),
            ]:
                match = regex.search(stripped)
                if match:
                    current[key] = caster(match.group(1))
                    matched_metadata = True
                    break
            if matched_metadata:
                continue

            match = RE_SCAN_START_TIME.search(stripped)
            if match:
                current["scan_start_time_min"] = float(match.group(1))
                current["scan_start_time_sec"] = current["scan_start_time_min"] * 60.0
                continue

            if "cvParam: m/z array" in stripped:
                array_mode = "mz"
                continue

            if "cvParam: intensity array" in stripped:
                array_mode = "intensity"
                continue

            if "binary:" in stripped and array_mode == "mz":
                mz_array = array_from_binary_line(stripped)
                current["mz_array_length_parsed"] = int(len(mz_array))
                for marker in MARKERS:
                    target_mz, lower_mz, upper_mz = bounds[marker.prefix]
                    left = int(np.searchsorted(mz_array, lower_mz, side="left"))
                    right = int(np.searchsorted(mz_array, upper_mz, side="right"))
                    indices = np.arange(left, right, dtype=int)
                    mz_values = mz_array[left:right]
                    target_indices[marker.prefix] = indices
                    target_mz_values[marker.prefix] = mz_values
                    current[f"{marker.prefix}_n_mz"] = int(len(mz_values))
                    if len(mz_values) > 0:
                        closest_pos = int(np.argmin(np.abs(mz_values - target_mz)))
                        closest_mz = float(mz_values[closest_pos])
                        current[f"{marker.prefix}_closest_mz"] = closest_mz
                        current[f"{marker.prefix}_closest_ppm_error"] = (closest_mz - target_mz) / target_mz * 1e6
                array_mode = None
                continue

            if "binary:" in stripped and array_mode == "intensity":
                any_marker_hit = any(len(v) > 0 for v in target_indices.values())
                if any_marker_hit:
                    intensity_array = array_from_binary_line(stripped)
                    current["intensity_array_length_parsed_if_marker_hit"] = int(len(intensity_array))
                    for marker in MARKERS:
                        indices = target_indices.get(marker.prefix, np.asarray([], dtype=int))
                        mz_values = target_mz_values.get(marker.prefix, np.asarray([], dtype=float))
                        if len(indices) == 0:
                            continue
                        selected = intensity_array[indices]
                        max_pos = int(np.argmax(selected))
                        mz_at_max = float(mz_values[max_pos])
                        current[f"{marker.prefix}_max_intensity"] = float(selected[max_pos])
                        current[f"{marker.prefix}_sum_intensity"] = float(selected.sum())
                        current[f"{marker.prefix}_mz_at_max_intensity"] = mz_at_max
                        current[f"{marker.prefix}_ppm_error_at_max_intensity"] = (
                            mz_at_max - marker.mz
                        ) / marker.mz * 1e6
                else:
                    current["intensity_array_length_parsed_if_marker_hit"] = np.nan

                rows.append(current.copy())
                if len(rows) % progress_every == 0:
                    print(
                        f"Parsed {len(rows)} spectra, read {fmt_size(bytes_seen)}, "
                        f"elapsed {time.time() - started:.1f} sec",
                        flush=True,
                    )
                in_spectrum = False
                current = {}
                array_mode = None
                target_indices = {}
                target_mz_values = {}

    parse_summary = {
        "path": project_display_path(path),
        "size_bytes": path.stat().st_size,
        "size_human": fmt_size(path.stat().st_size),
        "metadata_spectrum_count": metadata_spectrum_count,
        "parsed_spectrum_count": len(rows),
        "historical_label_seen_in_ms_header": historical_label_seen,
        "correct_label_seen_in_file_or_path": correct_label_seen,
        "tolerance_ppm": TOLERANCE_PPM,
        "elapsed_sec": time.time() - started,
    }
    for marker in MARKERS:
        tol_mz = marker.mz * TOLERANCE_PPM * 1e-6
        parse_summary[f"{marker.prefix}_target_mz"] = marker.mz
        parse_summary[f"{marker.prefix}_lower_mz"] = marker.mz - tol_mz
        parse_summary[f"{marker.prefix}_upper_mz"] = marker.mz + tol_mz

    return pd.DataFrame(rows), parse_summary


def add_derived_columns(scan: pd.DataFrame) -> pd.DataFrame:
    out = scan.copy().sort_values("scan_start_time_sec").reset_index(drop=True)
    out["scan_row_index"] = np.arange(len(out), dtype=int)
    out["scan_step_sec"] = out["scan_start_time_sec"].diff()
    out["has_pc34_760"] = out["pc34_760_n_mz"] > 0
    out["has_qc_782"] = out["qc_782_n_mz"] > 0
    out["has_both_markers"] = out["has_pc34_760"] & out["has_qc_782"]
    out["ratio_760_782_max_pseudo1"] = (out["pc34_760_max_intensity"] + PSEUDOCOUNT) / (
        out["qc_782_max_intensity"] + PSEUDOCOUNT
    )
    out["ratio_760_782_sum_pseudo1"] = (out["pc34_760_sum_intensity"] + PSEUDOCOUNT) / (
        out["qc_782_sum_intensity"] + PSEUDOCOUNT
    )
    out["log10_tic"] = np.log10(out["tic"].clip(lower=0) + 1.0)
    out["log10_pc34_760_max"] = np.log10(out["pc34_760_max_intensity"] + 1.0)
    out["log10_qc_782_max"] = np.log10(out["qc_782_max_intensity"] + 1.0)
    return out


def strict_contiguous_runs(df: pd.DataFrame, start_col: str = "start_min", end_col: str = "end_min") -> list[pd.DataFrame]:
    if df.empty:
        return []
    runs: list[pd.DataFrame] = []
    current = []
    prev_end = None
    for _, row in df.sort_values(start_col).iterrows():
        if prev_end is None or abs(float(row[start_col]) - float(prev_end)) <= 1e-6:
            current.append(row)
        else:
            runs.append(pd.DataFrame(current))
            current = [row]
        prev_end = float(row[end_col])
    if current:
        runs.append(pd.DataFrame(current))
    return runs


def build_bin_summary(scan: pd.DataFrame, signal_col: str, dt_sec: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    y = scan[signal_col].to_numpy(float)
    t_min = scan["scan_start_time_min"].to_numpy(float)
    localmax_distance_points = max(1, int(round(0.30 / dt_sec)))
    localmax_idx, _ = find_peaks(y, height=0, distance=localmax_distance_points)
    localmax = pd.DataFrame(
        {
            "scan_row_index": localmax_idx,
            "time_min": t_min[localmax_idx],
            "height": y[localmax_idx],
        }
    )
    bins = np.arange(0, np.ceil(scan["scan_start_time_min"].max() / BIN_SIZE_MIN) * BIN_SIZE_MIN + BIN_SIZE_MIN, BIN_SIZE_MIN)
    rows = []
    for start, end in zip(bins[:-1], bins[1:]):
        sub = scan[(scan["scan_start_time_min"] >= start) & (scan["scan_start_time_min"] < end)]
        peaks = localmax[(localmax["time_min"] >= start) & (localmax["time_min"] < end)]
        if sub.empty:
            continue
        rows.append(
            {
                "signal_col": signal_col,
                "start_min": float(start),
                "end_min": float(end),
                "scan_count": int(len(sub)),
                "positive_scan_fraction": float((sub[signal_col] > 0).mean()),
                "scan_p95": float(sub[signal_col].quantile(0.95)),
                "scan_p99": float(sub[signal_col].quantile(0.99)),
                "scan_max": float(sub[signal_col].max()),
                "localmax_count": int(len(peaks)),
                "localmax_p95": float(peaks["height"].quantile(0.95)) if len(peaks) else np.nan,
                "localmax_p99": float(peaks["height"].quantile(0.99)) if len(peaks) else np.nan,
                "localmax_max": float(peaks["height"].max()) if len(peaks) else np.nan,
            }
        )
    return pd.DataFrame(rows), localmax


def select_quiet_platform(bin_summary: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    valid = bin_summary[bin_summary["scan_count"] >= 200].copy()
    for col in ["localmax_p99", "scan_p99", "positive_scan_fraction"]:
        valid[f"{col}_rank"] = valid[col].rank(pct=True)
    valid["quiet_score"] = valid[["localmax_p99_rank", "scan_p99_rank", "positive_scan_fraction_rank"]].mean(axis=1)
    candidates = valid[valid["quiet_score"] <= valid["quiet_score"].quantile(0.35)].copy()
    runs = strict_contiguous_runs(candidates)
    if runs:
        runs = sorted(
            runs,
            key=lambda x: (float(x["end_min"].max() - x["start_min"].min()), -float(x["quiet_score"].mean())),
            reverse=True,
        )
        selected = runs[0].copy()
        method = "longest_contiguous_low_signal_platform"
    else:
        selected = valid.nsmallest(max(3, int(np.ceil(len(valid) * 0.20))), "quiet_score").copy()
        method = "fallback_lowest_signal_bins"
    selected = selected.sort_values("start_min").reset_index(drop=True)
    selected["selected_as_quiet_platform"] = True
    return selected, method


def estimate_parameters(scan: pd.DataFrame, signal_col: str, bin_summary: pd.DataFrame, localmax: pd.DataFrame, dt_sec: float) -> tuple[dict, pd.DataFrame]:
    quiet_bins, quiet_method = select_quiet_platform(bin_summary)
    quiet_scan_parts = []
    quiet_peak_parts = []
    for _, row in quiet_bins.iterrows():
        quiet_scan_parts.append(
            scan[(scan["scan_start_time_min"] >= row["start_min"]) & (scan["scan_start_time_min"] < row["end_min"])][signal_col]
        )
        quiet_peak_parts.append(
            localmax[(localmax["time_min"] >= row["start_min"]) & (localmax["time_min"] < row["end_min"])]["height"]
        )
    quiet_scans = pd.concat(quiet_scan_parts, ignore_index=True)
    quiet_peaks = pd.concat(quiet_peak_parts, ignore_index=True)
    quiet_scan_p99 = float(quiet_scans.quantile(0.99))
    quiet_localmax_p99 = float(quiet_peaks.quantile(0.99))
    quiet_mad_sigma = float(1.4826 * np.median(np.abs(quiet_scans - quiet_scans.median())))

    if signal_col == "pc34_760_max_intensity":
        height = float(max(6.0 * quiet_scan_p99, 4.0 * quiet_localmax_p99))
        prominence = float(max(0.8 * quiet_localmax_p99, 3.0 * quiet_mad_sigma))
    else:
        quiet_median = float(quiet_scans.median())
        localmax_excess_p99 = max(0.0, quiet_localmax_p99 - quiet_median)
        height = float(max(quiet_scan_p99 + 3.0 * quiet_mad_sigma, quiet_localmax_p99))
        prominence = float(max(0.25 * localmax_excess_p99, 3.0 * quiet_mad_sigma, 0.02))

    y = scan[signal_col].to_numpy(float)
    t = scan["scan_start_time_sec"].to_numpy(float)
    threshold_fallback_reason = ""
    signal_max = float(np.nanmax(y)) if len(y) else 0.0
    if (
        signal_col == "pc34_760_max_intensity"
        and signal_max > 0
        and (not np.isfinite(height) or height >= signal_max)
    ):
        # A contaminated "quiet" run can put its estimated threshold above
        # the entire trace.  Falling back to the median positive local maximum
        # is unsafe for sparse MS traces: the median is then the electronic
        # noise floor and turns thousands of scan-level fluctuations into
        # events.  Keep this fallback project-adaptive by scaling it to the
        # observed trace range; no author event count or event-map label is
        # consulted here.
        height = float(
            max(
                np.nextafter(0.0, 1.0),
                PC34_FALLBACK_HEIGHT_FRACTION * signal_max,
            )
        )
        prominence = float(
            max(
                np.nextafter(0.0, 1.0),
                min(prominence, PC34_FALLBACK_PROMINENCE_FRACTION * height),
            )
        )
        threshold_fallback_reason = "quiet_threshold_exceeded_signal_range"
    prelim_distance_points = 2 if threshold_fallback_reason else 3
    prelim_idx, _ = find_peaks(
        y,
        height=height,
        prominence=prominence,
        distance=prelim_distance_points,
    )
    if len(prelim_idx) >= 3:
        gap_q10 = float(np.quantile(np.diff(t[prelim_idx]), 0.10))
        min_distance_sec = (
            float(2.0 * dt_sec)
            if threshold_fallback_reason
            else float(np.clip(gap_q10, 6.0 * dt_sec, 15.0 * dt_sec))
        )
    else:
        gap_q10 = np.nan
        min_distance_sec = float(
            (2.0 if threshold_fallback_reason else 6.0) * dt_sec
        )

    params = {
        "signal_col": signal_col,
        "quiet_selection_method": quiet_method,
        "quiet_start_min": float(quiet_bins["start_min"].min()),
        "quiet_end_min": float(quiet_bins["end_min"].max()),
        "quiet_bin_count": int(len(quiet_bins)),
        "quiet_scan_p99": quiet_scan_p99,
        "quiet_localmax_p99": quiet_localmax_p99,
        "quiet_median": float(quiet_scans.median()),
        "quiet_mad_sigma": quiet_mad_sigma,
        "peak_height": height,
        "peak_prominence": prominence,
        "threshold_fallback_reason": threshold_fallback_reason,
        "signal_max": signal_max,
        "preliminary_peak_count": int(len(prelim_idx)),
        "preliminary_gap_q10_sec": gap_q10,
        "min_distance_sec": min_distance_sec,
        "scan_step_sec": dt_sec,
        "bin_size_min": BIN_SIZE_MIN,
    }
    return params, quiet_bins


def call_peak_indices(scan: pd.DataFrame, signal_col: str, height: float, prominence: float, min_distance_sec: float, dt_sec: float) -> np.ndarray:
    y = scan[signal_col].to_numpy(float)
    distance_points = max(1, int(round(min_distance_sec / dt_sec)))
    peaks, _ = find_peaks(y, height=height, prominence=prominence, distance=distance_points)
    return peaks


def build_event_table(scan: pd.DataFrame, peaks: np.ndarray, params: dict, strategy: str) -> pd.DataFrame:
    signal_col = str(params["signal_col"])
    y = scan[signal_col].to_numpy(float)
    t = scan["scan_start_time_sec"].to_numpy(float)
    dt_sec = float(params["scan_step_sec"])
    if len(peaks):
        prominences = peak_prominences(y, peaks)[0]
        width_result = peak_widths(y, peaks, rel_height=0.5)
        width_points = width_result[0]
        left_ips = width_result[2]
        right_ips = width_result[3]
    else:
        prominences = width_points = left_ips = right_ips = np.asarray([])

    rows = []
    for i, peak_idx in enumerate(peaks, start=1):
        left_i = max(0, int(np.floor(left_ips[i - 1])))
        right_i = min(len(scan) - 1, int(np.ceil(right_ips[i - 1])))
        window = scan.iloc[left_i : right_i + 1]
        apex = scan.iloc[int(peak_idx)]
        rows.append(
            {
                "event_id": f"MS_{strategy}_{i:06d}",
                "event_strategy": strategy,
                "primary_signal_col": signal_col,
                "scan_row_index": int(apex["scan_row_index"]),
                "spectrum_index": int(apex["spectrum_index"]),
                "scan_id": int(apex["scan_id"]),
                "time_min": float(apex["scan_start_time_min"]),
                "time_sec": float(apex["scan_start_time_sec"]),
                "apex_intensity": float(apex[signal_col]),
                "peak_prominence": float(prominences[i - 1]),
                "peak_width_sec": float(width_points[i - 1] * dt_sec),
                "left_sec": float(np.interp(left_ips[i - 1], np.arange(len(t)), t)),
                "right_sec": float(np.interp(right_ips[i - 1], np.arange(len(t)), t)),
                "window_scan_count": int(len(window)),
                "pc34_760_apex": float(apex["pc34_760_max_intensity"]),
                "qc_782_apex": float(apex["qc_782_max_intensity"]),
                "pc34_760_ppm_error_at_apex": float(apex["pc34_760_ppm_error_at_max_intensity"]),
                "qc_782_ppm_error_at_apex": float(apex["qc_782_ppm_error_at_max_intensity"]),
                "tic_apex": float(apex["tic"]),
                "ratio_760_782_max_pseudo1": float(apex["ratio_760_782_max_pseudo1"]),
                "array_length_apex": int(apex["array_length"]),
                "base_peak_mz_apex": float(apex["base_peak_mz"]),
                "low_array_length_lt_6000_window": bool((window["array_length"] < LOW_ARRAY_LENGTH_THRESHOLD).any()),
                "low_array_length_lt_1000_window": bool((window["array_length"] < LOW_ARRAY_LENGTH_SEVERE).any()),
                "low_tic_lt_1e6_window": bool((window["tic"] < LOW_TIC_THRESHOLD).any()),
                "calling_height": float(params["peak_height"]),
                "calling_prominence": float(params["peak_prominence"]),
                "calling_min_distance_sec": float(params["min_distance_sec"]),
            }
        )
    events = pd.DataFrame(rows)
    if events.empty:
        # A no-event result is valid evidence and must retain the table
        # contract so QC/reporting can explain it instead of crashing on a
        # missing column.  Project creation may then surface a focused data
        # suitability error at the appropriate boundary.
        return pd.DataFrame(
            columns=[
                "event_id",
                "event_strategy",
                "primary_signal_col",
                "scan_row_index",
                "spectrum_index",
                "scan_id",
                "time_min",
                "time_sec",
                "apex_intensity",
                "peak_prominence",
                "peak_width_sec",
                "left_sec",
                "right_sec",
                "window_scan_count",
                "pc34_760_apex",
                "qc_782_apex",
                "pc34_760_ppm_error_at_apex",
                "qc_782_ppm_error_at_apex",
                "tic_apex",
                "ratio_760_782_max_pseudo1",
                "array_length_apex",
                "base_peak_mz_apex",
                "low_array_length_lt_6000_window",
                "low_array_length_lt_1000_window",
                "low_tic_lt_1e6_window",
                "calling_height",
                "calling_prominence",
                "calling_min_distance_sec",
                "prev_event_gap_sec",
                "next_event_gap_sec",
                "nearest_event_gap_sec",
                "collision_risk_high",
                "broad_peak_width_gt_1p5_sec",
                "low_quality_scan_window",
            ]
        )
    events["prev_event_gap_sec"] = events["time_sec"].diff()
    events["next_event_gap_sec"] = events["time_sec"].shift(-1) - events["time_sec"]
    events["nearest_event_gap_sec"] = events[["prev_event_gap_sec", "next_event_gap_sec"]].min(axis=1)
    events["collision_risk_high"] = events["nearest_event_gap_sec"] < COLLISION_GAP_SEC
    events["broad_peak_width_gt_1p5_sec"] = events["peak_width_sec"] > BROAD_PEAK_WIDTH_SEC
    events["low_quality_scan_window"] = (
        events["low_array_length_lt_6000_window"]
        | events["low_array_length_lt_1000_window"]
        | events["low_tic_lt_1e6_window"]
        | events["broad_peak_width_gt_1p5_sec"]
    )
    return events


def nearest_support_count(primary: pd.DataFrame, support: pd.DataFrame, tol_sec: float) -> tuple[int, float]:
    if primary.empty or support.empty:
        return 0, np.nan
    support_t = support["time_sec"].to_numpy(float)
    deltas = []
    for t in primary["time_sec"].to_numpy(float):
        idx = int(np.argmin(np.abs(support_t - t)))
        deltas.append(float(support_t[idx] - t))
    abs_delta = np.abs(np.asarray(deltas))
    supported = abs_delta[abs_delta <= tol_sec]
    return int(len(supported)), float(np.median(supported)) if len(supported) else np.nan


def build_strategy_comparison(pc34_events: pd.DataFrame, tic_events: pd.DataFrame) -> pd.DataFrame:
    pc34_with_tic, pc34_tic_median = nearest_support_count(pc34_events, tic_events, TIC_SUPPORT_TOL_SEC)
    tic_with_pc34, tic_pc34_median = nearest_support_count(tic_events, pc34_events, TIC_SUPPORT_TOL_SEC)
    return pd.DataFrame(
        [
            {
                "strategy": "PC34-only",
                "event_count": int(len(pc34_events)),
                "support_count_within_0p75sec": "",
                "support_fraction": "",
                "median_abs_support_delta_sec": "",
                "role": "main V3-02 MS event caller",
            },
            {
                "strategy": "TIC-only",
                "event_count": int(len(tic_events)),
                "support_count_within_0p75sec": tic_with_pc34,
                "support_fraction": float(tic_with_pc34 / len(tic_events)) if len(tic_events) else np.nan,
                "median_abs_support_delta_sec": tic_pc34_median,
                "role": "sensitivity; TIC can include non-PC34 fluctuations",
            },
            {
                "strategy": "PC34+TIC support",
                "event_count": pc34_with_tic,
                "support_count_within_0p75sec": pc34_with_tic,
                "support_fraction": float(pc34_with_tic / len(pc34_events)) if len(pc34_events) else np.nan,
                "median_abs_support_delta_sec": pc34_tic_median,
                "role": "PC34 primary events with nearby TIC support",
            },
        ]
    )


def build_pc34_support_audit(pc34_events: pd.DataFrame, tic_events: pd.DataFrame) -> pd.DataFrame:
    events = pc34_events.copy()
    if events.empty:
        return pd.DataFrame()
    tic_times = tic_events["time_sec"].to_numpy(float) if len(tic_events) else np.asarray([])
    if len(tic_times):
        nearest = []
        for t in events["time_sec"].to_numpy(float):
            nearest.append(float(np.min(np.abs(tic_times - t))))
        events["nearest_tic_delta_sec"] = nearest
        events["tic_support_within_0p75sec"] = events["nearest_tic_delta_sec"].le(TIC_SUPPORT_TOL_SEC)
    else:
        events["nearest_tic_delta_sec"] = np.nan
        events["tic_support_within_0p75sec"] = False

    positive_782 = events.loc[events["qc_782_apex"] > 0, "qc_782_apex"]
    q75_782 = float(positive_782.quantile(0.75)) if len(positive_782) else np.inf
    events["qc782_positive"] = events["qc_782_apex"].gt(0)
    events["qc782_high_q75"] = events["qc_782_apex"].ge(q75_782)
    events["time_bin_10min"] = (np.floor(events["time_min"] / 10.0) * 10.0).astype(float)
    events["project_phase"] = classify_project_phase(
        events["time_min"], PROJECT_PHASE_POLICY
    )
    events["segment_role"] = phase_role_from_labels(events["project_phase"])

    rows = []
    for (start, segment_role), sub in events.groupby(
        ["time_bin_10min", "segment_role"], sort=True
    ):
        rows.append(
            {
                "start_min": float(start),
                "end_min": float(start + 10.0),
                "segment_role": str(segment_role),
                "project_phases": ";".join(sorted(set(sub["project_phase"].astype(str)))),
                "pc34_event_count": int(len(sub)),
                "tic_supported_count": int(sub["tic_support_within_0p75sec"].sum()),
                "tic_supported_fraction": float(sub["tic_support_within_0p75sec"].mean()),
                "qc782_positive_count": int(sub["qc782_positive"].sum()),
                "qc782_high_q75_count": int(sub["qc782_high_q75"].sum()),
                "low_quality_scan_window_count": int(sub["low_quality_scan_window"].sum()),
                "pc34_ppm_error_abs_median": float(sub["pc34_760_ppm_error_at_apex"].abs().median()),
                "qc782_ppm_error_abs_median_positive": float(sub.loc[sub["qc782_positive"], "qc_782_ppm_error_at_apex"].abs().median())
                if sub["qc782_positive"].any()
                else np.nan,
            }
        )
    summary = pd.DataFrame(rows)
    totals = pd.DataFrame(
        [
            {
                "start_min": "all",
                "end_min": "all",
                "segment_role": "all_segments",
                "project_phases": ";".join(sorted(set(events["project_phase"].astype(str)))),
                "pc34_event_count": int(len(events)),
                "tic_supported_count": int(events["tic_support_within_0p75sec"].sum()),
                "tic_supported_fraction": float(events["tic_support_within_0p75sec"].mean()),
                "qc782_positive_count": int(events["qc782_positive"].sum()),
                "qc782_high_q75_count": int(events["qc782_high_q75"].sum()),
                "low_quality_scan_window_count": int(events["low_quality_scan_window"].sum()),
                "pc34_ppm_error_abs_median": float(events["pc34_760_ppm_error_at_apex"].abs().median()),
                "qc782_ppm_error_abs_median_positive": float(events.loc[events["qc782_positive"], "qc_782_ppm_error_at_apex"].abs().median()),
            }
        ]
    )
    return pd.concat([totals, summary], ignore_index=True)


def summarize_scan(scan: pd.DataFrame, parse_summary: dict) -> pd.DataFrame:
    rows = [
        {"metric": "metadata_spectrum_count", "value": parse_summary["metadata_spectrum_count"]},
        {"metric": "parsed_spectrum_count", "value": parse_summary["parsed_spectrum_count"]},
        {"metric": "parsed_equals_metadata", "value": parse_summary["parsed_spectrum_count"] == parse_summary["metadata_spectrum_count"]},
        {"metric": "elapsed_sec", "value": parse_summary["elapsed_sec"]},
        {"metric": "time_min_min", "value": float(scan["scan_start_time_min"].min())},
        {"metric": "time_min_max", "value": float(scan["scan_start_time_min"].max())},
        {"metric": "scan_step_sec_median", "value": float(scan["scan_step_sec"].median())},
        {"metric": "scan_step_sec_min", "value": float(scan["scan_step_sec"].min())},
        {"metric": "scan_step_sec_max", "value": float(scan["scan_step_sec"].max())},
        {"metric": "tic_median", "value": float(scan["tic"].median())},
        {"metric": "tic_min", "value": float(scan["tic"].min())},
        {"metric": "tic_max", "value": float(scan["tic"].max())},
        {"metric": "array_length_median", "value": float(scan["array_length"].median())},
        {"metric": "array_length_min", "value": int(scan["array_length"].min())},
        {"metric": "array_length_max", "value": int(scan["array_length"].max())},
        {"metric": "array_length_lt_6000_scans", "value": int((scan["array_length"] < 6000).sum())},
        {"metric": "tic_lt_1e6_scans", "value": int((scan["tic"] < LOW_TIC_THRESHOLD).sum())},
    ]
    for marker in MARKERS:
        prefix = marker.prefix
        hits = scan[scan[f"{prefix}_n_mz"] > 0]
        rows.extend(
            [
                {"metric": f"{prefix}_target_mz", "value": marker.mz},
                {"metric": f"{prefix}_hit_scans", "value": int(len(hits))},
                {"metric": f"{prefix}_hit_rate", "value": float(len(hits) / len(scan)) if len(scan) else np.nan},
                {"metric": f"{prefix}_max_intensity_median_all_scans", "value": float(scan[f"{prefix}_max_intensity"].median())},
                {"metric": f"{prefix}_max_intensity_median_hit_scans", "value": float(hits[f"{prefix}_max_intensity"].median()) if len(hits) else np.nan},
                {"metric": f"{prefix}_max_intensity_max", "value": float(scan[f"{prefix}_max_intensity"].max())},
                {"metric": f"{prefix}_ppm_error_at_max_median", "value": float(hits[f"{prefix}_ppm_error_at_max_intensity"].median()) if len(hits) else np.nan},
            ]
        )
    return pd.DataFrame(rows)


def build_event_qc(events: pd.DataFrame, strategy_comparison: pd.DataFrame, params: dict) -> pd.DataFrame:
    rows = [
        {"metric": "main_strategy", "value": "PC34-only"},
        {"metric": "pc34_event_count", "value": int(len(events))},
        {"metric": "collision_risk_high_events", "value": int(events["collision_risk_high"].sum())},
        {"metric": "broad_peak_width_gt_1p5_sec_events", "value": int(events["broad_peak_width_gt_1p5_sec"].sum())},
        {"metric": "low_array_length_lt_6000_window_events", "value": int(events["low_array_length_lt_6000_window"].sum())},
        {"metric": "low_tic_lt_1e6_window_events", "value": int(events["low_tic_lt_1e6_window"].sum())},
        {"metric": "pc34_peak_height", "value": float(params["peak_height"])},
        {"metric": "pc34_peak_prominence", "value": float(params["peak_prominence"])},
        {"metric": "pc34_min_distance_sec", "value": float(params["min_distance_sec"])},
        {"metric": "quiet_start_min", "value": float(params["quiet_start_min"])},
        {"metric": "quiet_end_min", "value": float(params["quiet_end_min"])},
    ]
    for _, row in strategy_comparison.iterrows():
        rows.append({"metric": f"{row['strategy']}_event_count", "value": row["event_count"]})
        if row["support_fraction"] != "":
            rows.append({"metric": f"{row['strategy']}_support_fraction", "value": row["support_fraction"]})
    return pd.DataFrame(rows)


def draw_project_phase_boundaries(ax: mpl.axes.Axes) -> None:
    for boundary in PROJECT_PHASE_POLICY["plot_boundaries"]:
        is_annotation = boundary["kind"] == "annotation_start"
        ax.axvline(
            float(boundary["time_min"]),
            color="#374151" if is_annotation else "0.72",
            lw=0.9 if is_annotation else 0.6,
            ls="--" if is_annotation else ":",
        )


def plot_ms_overview(scan: pd.DataFrame, events: pd.DataFrame, quiet_bins: pd.DataFrame, pc34_params: dict) -> None:
    binned = scan.copy()
    binned["time_bin_sec"] = np.floor(binned["scan_start_time_sec"]).astype(int)
    binned = binned.groupby("time_bin_sec", as_index=False).agg(
        time_min=("scan_start_time_min", "mean"),
        pc34=("pc34_760_max_intensity", "max"),
        qc782=("qc_782_max_intensity", "max"),
        tic=("tic", "max"),
    )
    fig, axes = plt.subplots(3, 1, figsize=(10, 5.8), sharex=True)
    axes[0].plot(binned["time_min"], np.log10(binned["pc34"] + 1), lw=0.65, color="#1f8a70")
    high_782_threshold = events.loc[events["qc_782_apex"].gt(0), "qc_782_apex"].quantile(0.75)
    high_782 = events["qc_782_apex"].ge(high_782_threshold)
    axes[0].scatter(events.loc[~high_782, "time_min"], np.log10(events.loc[~high_782, "pc34_760_apex"] + 1), s=5, color="0.25", alpha=0.35, label="PC34")
    axes[0].scatter(events.loc[high_782, "time_min"], np.log10(events.loc[high_782, "pc34_760_apex"] + 1), s=9, color="#7f4fb3", alpha=0.65, label="PC34 + high 782")
    axes[0].axhline(np.log10(float(pc34_params["peak_height"]) + 1), color="black", lw=0.8, ls="--", label="PC34 threshold")
    axes[0].legend(loc="upper right", ncol=3)
    axes[0].set_ylabel("log10 PC34")
    axes[1].plot(binned["time_min"], np.log10(binned["tic"] + 1), lw=0.65, color="0.25")
    axes[1].set_ylabel("log10 TIC")
    axes[2].plot(binned["time_min"], np.log10(binned["qc782"] + 1), lw=0.65, color="#7f4fb3")
    axes[2].set_ylabel("log10 782")
    axes[2].set_xlabel("time (min)")
    for ax in axes:
        draw_project_phase_boundaries(ax)
        for _, row in quiet_bins.iterrows():
            ax.axvspan(row["start_min"], row["end_min"], color="#d9f99d", alpha=0.18, lw=0)
    fig.suptitle("V3-02 MS traces, PC34 events, and quiet platform", y=0.995)
    save_png(fig, OUT_FIG / "v3_02_ms_trace_event_overview.png")


def plot_event_qc(events: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.0))
    axes[0].hist(events["peak_width_sec"], bins=40, color="#1f8a70", alpha=0.8)
    axes[0].axvline(BROAD_PEAK_WIDTH_SEC, color="black", lw=0.8, ls="--")
    axes[0].set_xlabel("PC34 width (sec)")
    axes[0].set_ylabel("event count")
    axes[1].hist(np.log10(events["pc34_760_apex"] + 1), bins=40, color="#2563eb", alpha=0.8)
    axes[1].set_xlabel("log10 PC34 apex")
    axes[1].set_ylabel("event count")
    axes[2].hist(events["nearest_event_gap_sec"].dropna(), bins=40, color="#c2410c", alpha=0.8)
    axes[2].axvline(COLLISION_GAP_SEC, color="black", lw=0.8, ls="--")
    axes[2].set_xlabel("nearest event gap (sec)")
    axes[2].set_ylabel("event count")
    fig.suptitle("V3-02 compact MS event QC", y=1.03)
    save_png(fig, OUT_FIG / "v3_02_ms_event_qc_distributions.png")


def write_report(
    scan_summary: pd.DataFrame,
    params_table: pd.DataFrame,
    quiet_bins: pd.DataFrame,
    strategy_comparison: pd.DataFrame,
    support_audit: pd.DataFrame,
    event_qc: pd.DataFrame,
) -> None:
    lines = [
        "# V3-02 MS trace physical QC and event calling",
        "",
        "## 结论",
        "",
        "- 本步骤只读取 V3-00 锁定的 raw MS txt；没有读取作者 CSV、h5ad、人工补峰、LIF peak 或任何 V2 输出。",
        "- PC34/760.5851 和 782.5616 是 V3-00 中声明的预指定 marker 先验；它们不是作者标签，也不来自 h5ad/UMAP/downstream feature。",
        f"- 阶段语义来自项目协议：`{phase_boundaries_min(PROJECT_PHASE_POLICY)}`；前段参考窗口、未分配间隙和 annotation region 被分别报告。",
        "- scan summary 从原始 MS 文本流式解析，提取 PC34/760、782/QC marker、TIC、array length、scan time 和 m/z 命中误差。",
        "- 主 event caller 使用 PC34/760 extracted trace；TIC-only 和 PC34+TIC 只作为 MS-only 对照，不参与身份标注。",
        "- 阈值来自 MS 自身 quiet platform 的背景和峰状尖刺上沿，不使用作者 event list 调参。",
        "",
        "## scan summary",
        "",
        md_table(scan_summary, max_rows=40),
        "",
        "## event calling 参数",
        "",
        md_table(params_table),
        "",
        "## quiet platform bins",
        "",
        md_table(quiet_bins[["start_min", "end_min", "positive_scan_fraction", "scan_p99", "localmax_p99", "quiet_score"]], max_rows=20),
        "",
        "## 策略对照",
        "",
        md_table(strategy_comparison),
        "",
        "## PC34 支持审计",
        "",
        "说明：PC34-only 是主 event caller；本表只帮助人工审查哪些项目配置时段缺少 TIC/782 支持或存在低质量窗口，不用于作者标签拟合。`calibration_reference_only:*`、`pre_annotation_unassigned` 与 `annotation_region` 均由项目协议生成。",
        "",
        md_table(support_audit, max_rows=20),
        "",
        "## 主 event QC",
        "",
        md_table(event_qc),
        "",
        "## 输出文件",
        "",
        "- `data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet`",
        "- `data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet`",
        "- `results/tables/v3/02_ms_event_calling/v3_02_event_calling_qc.csv`",
        "- `results/tables/v3/02_ms_event_calling/v3_02_strategy_comparison.csv`",
        "- `results/tables/v3/02_ms_event_calling/v3_02_pc34_support_audit.csv`",
        "- `results/figures/v3/02_ms_event_calling/v3_02_ms_trace_event_overview.png`",
        "- `results/figures/v3/02_ms_event_calling/v3_02_ms_event_qc_distributions.png`",
        "",
        "## 下一步 gate",
        "",
        "- 如果 PC34 event calling 的 quiet platform、峰宽、collision/low-quality 风险可解释，则进入 V3-03 QC-only time model。",
        "- 如果 V3-03 发现 QC anchor 与 MS event 不连续，应先回到本步骤检查 PC34/782/TIC 的 MS-only evidence，而不是读取作者标签。",
    ]
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(project_dir: str | Path | None = None) -> None:
    # Module imports use an in-memory default so parsers remain importable.
    # Every actual run, including the no-argument CLI, must bind a real project
    # protocol before creating any output directory.
    configure_project_root(Path.cwd() if project_dir is None else project_dir)
    apply_plot_style()
    OUT_DATA.mkdir(parents=True, exist_ok=True)
    OUT_TABLE.mkdir(parents=True, exist_ok=True)
    OUT_FIG.mkdir(parents=True, exist_ok=True)
    OUT_QC.mkdir(parents=True, exist_ok=True)

    ms_path = load_ms_path()
    scan, parse_summary = parse_ms_scan_summary(ms_path)
    scan = add_derived_columns(scan)
    dt_sec = float(scan["scan_step_sec"].median())

    pc34_bin_summary, pc34_localmax = build_bin_summary(scan, "pc34_760_max_intensity", dt_sec)
    pc34_params, quiet_bins = estimate_parameters(scan, "pc34_760_max_intensity", pc34_bin_summary, pc34_localmax, dt_sec)
    pc34_peaks = call_peak_indices(
        scan,
        "pc34_760_max_intensity",
        float(pc34_params["peak_height"]),
        float(pc34_params["peak_prominence"]),
        float(pc34_params["min_distance_sec"]),
        dt_sec,
    )
    pc34_events = build_event_table(scan, pc34_peaks, pc34_params, "pc34_primary")

    tic_bin_summary, tic_localmax = build_bin_summary(scan, "log10_tic", dt_sec)
    tic_params, tic_quiet_bins = estimate_parameters(scan, "log10_tic", tic_bin_summary, tic_localmax, dt_sec)
    tic_peaks = call_peak_indices(
        scan,
        "log10_tic",
        float(tic_params["peak_height"]),
        float(tic_params["peak_prominence"]),
        float(tic_params["min_distance_sec"]),
        dt_sec,
    )
    tic_events = build_event_table(scan, tic_peaks, tic_params, "tic_only")

    scan_summary = summarize_scan(scan, parse_summary)
    params_table = pd.DataFrame([pc34_params, tic_params])
    strategy_comparison = build_strategy_comparison(pc34_events, tic_events)
    support_audit = build_pc34_support_audit(pc34_events, tic_events)
    event_qc = build_event_qc(pc34_events, strategy_comparison, pc34_params)

    scan.to_parquet(OUT_DATA / "v3_02_ms_scan_summary.parquet", index=False)
    pc34_events.to_parquet(OUT_DATA / "v3_02_ms_events.parquet", index=False)
    tic_events.to_parquet(OUT_QC / "v3_02_tic_only_events.parquet", index=False)
    scan_summary.to_csv(OUT_TABLE / "v3_02_scan_summary_qc.csv", index=False)
    params_table.to_csv(OUT_TABLE / "v3_02_event_calling_parameters.csv", index=False)
    quiet_bins.to_csv(OUT_TABLE / "v3_02_pc34_quiet_platform_bins.csv", index=False)
    pc34_bin_summary.to_csv(OUT_QC / "v3_02_pc34_bin_summary.csv", index=False)
    tic_quiet_bins.to_csv(OUT_QC / "v3_02_tic_quiet_platform_bins.csv", index=False)
    tic_bin_summary.to_csv(OUT_QC / "v3_02_tic_bin_summary.csv", index=False)
    strategy_comparison.to_csv(OUT_TABLE / "v3_02_strategy_comparison.csv", index=False)
    support_audit.to_csv(OUT_TABLE / "v3_02_pc34_support_audit.csv", index=False)
    event_qc.to_csv(OUT_TABLE / "v3_02_event_calling_qc.csv", index=False)

    plot_ms_overview(scan, pc34_events, quiet_bins, pc34_params)
    plot_event_qc(pc34_events)
    write_report(scan_summary, params_table, quiet_bins, strategy_comparison, support_audit, event_qc)

    print(f"Wrote {OUT_DATA / 'v3_02_ms_scan_summary.parquet'}")
    print(f"Wrote {OUT_DATA / 'v3_02_ms_events.parquet'}")
    print(f"Wrote {OUT_TABLE / 'v3_02_event_calling_qc.csv'}")
    print(f"Wrote {OUT_REPORT}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MS event calling from raw spectra text export.")
    parser.add_argument("--project-dir", default=None, help="项目根目录；默认使用当前工作目录。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(project_dir=args.project_dir)


if __name__ == "__main__":
    main()
