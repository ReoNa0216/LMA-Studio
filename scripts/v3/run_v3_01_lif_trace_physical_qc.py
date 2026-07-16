#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import find_peaks, peak_widths


ROOT = Path.cwd()
STEP = "01_lif_trace_physical_qc"

INPUT_LOCK = ROOT / "results/tables/v3/00_allowed_inputs.csv"
OUT_DATA = ROOT / "data/interim/v3" / STEP
OUT_TABLE = ROOT / "results/tables/v3" / STEP
OUT_FIG = ROOT / "results/figures/v3" / STEP
OUT_QC = ROOT / "results/qc/v3" / STEP
OUT_REPORT = ROOT / "reports/v3/01_lif_trace_physical_qc.md"

BASE_WINDOW_SEC = 60.0
BASE_Q = 0.10
NOISE_POOL_Q = 0.80
PROM_SNR_MIN = 10.0
RAW_MIN_DISTANCE_SEC = 0.02
MERGE_GAP_SEC = 0.12
MIN_WIDTH_SEC = 0.020
MAX_WIDTH_SEC = 1.00
RED_PAIR_MAX_ABS_OFFSET_SEC = 30.0
RED_PAIR_BIN_SEC = 0.25
RED_NEAR_ZERO_HALF_WIDTH_SEC = 0.75
QC_START_MAX_MIN = 10.5
PRE_RUN_MAX_MIN = 40.0
PHASE_BOUNDARIES_MIN = "qc_start:0-10.5 all-QC calibration; pre_run:10.5-40 transition/quiet; cell_run:>=40 acquisition"

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


def configure_project_root(project_dir: str | Path) -> Path:
    global ROOT, INPUT_LOCK, OUT_DATA, OUT_TABLE, OUT_FIG, OUT_QC, OUT_REPORT
    ROOT = Path(project_dir).expanduser().resolve()
    INPUT_LOCK = ROOT / "results/tables/v3/00_allowed_inputs.csv"
    OUT_DATA = ROOT / "data/interim/v3" / STEP
    OUT_TABLE = ROOT / "results/tables/v3" / STEP
    OUT_FIG = ROOT / "results/figures/v3" / STEP
    OUT_QC = ROOT / "results/qc/v3" / STEP
    OUT_REPORT = ROOT / "reports/v3/01_lif_trace_physical_qc.md"
    return ROOT


@dataclass(frozen=True)
class ChannelSpec:
    channel: str
    label: str
    detector: str
    path: Path


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_fingerprint(path: Path, full_hash_limit_bytes: int = 100 * 1024 * 1024) -> dict:
    if not path.exists():
        return {
            "exists": False,
            "size_bytes": np.nan,
            "mtime_iso": "",
            "head_sha256_1mb": "",
            "tail_sha256_1mb": "",
            "full_sha256_if_le_100mb": "",
        }
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
    full_hash = ""
    if size <= full_hash_limit_bytes:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        full_hash = digest.hexdigest()
    return {
        "exists": True,
        "size_bytes": int(size),
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "head_sha256_1mb": sha256_bytes(head),
        "tail_sha256_1mb": sha256_bytes(tail),
        "full_sha256_if_le_100mb": full_hash,
    }


def assert_first_principles_path(path: Path) -> None:
    text = str(path)
    for part in FORBIDDEN_PATH_PARTS:
        if part in text:
            raise ValueError(f"V3-01 forbidden input path detected: {path}")


def resolve_project_input_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_channel_specs() -> list[ChannelSpec]:
    if not INPUT_LOCK.exists():
        raise FileNotFoundError(f"Run V3-00 first; missing {INPUT_LOCK}")
    allowed = pd.read_csv(INPUT_LOCK)
    lif = allowed[allowed["input_class"].eq("raw_lif_trace")].copy()
    if len(lif) != 3:
        raise ValueError(f"Expected 3 raw LIF inputs from V3-00, found {len(lif)}")
    if not lif["allowed_stage"].eq("V3-01~V3-06 main workflow").all():
        bad = lif.loc[~lif["allowed_stage"].eq("V3-01~V3-06 main workflow"), ["input_id", "allowed_stage"]]
        raise ValueError(f"V3-01 received non-main-workflow inputs:\n{bad}")

    specs = []
    for _, row in lif.iterrows():
        path = resolve_project_input_path(str(row["path"]))
        assert_first_principles_path(path)
        current = file_fingerprint(path)
        for col in ["size_bytes", "head_sha256_1mb", "tail_sha256_1mb", "full_sha256_if_le_100mb"]:
            locked = row.get(col, "")
            if pd.isna(locked):
                locked = ""
            current_value = current[col]
            if col == "size_bytes":
                locked_value = int(locked)
                if int(current_value) != locked_value:
                    raise ValueError(f"V3-01 input fingerprint mismatch for {path}: {col}")
            elif str(locked) and str(current_value) != str(locked):
                raise ValueError(f"V3-01 input fingerprint mismatch for {path}: {col}")
        specs.append(
            ChannelSpec(
                channel=str(row["channel"]),
                label=str(row["label"]),
                detector=str(row["detector"]),
                path=path,
            )
        )
    return specs


def phase_from_time_min(time_min: pd.Series | np.ndarray) -> np.ndarray:
    t = np.asarray(time_min, dtype=float)
    return np.select(
        [t < QC_START_MAX_MIN, (t >= QC_START_MAX_MIN) & (t < PRE_RUN_MAX_MIN), t >= PRE_RUN_MAX_MIN],
        ["qc_start", "pre_run", "cell_run"],
        default="unknown",
    )


def phase_role_from_phase(phase: pd.Series | np.ndarray) -> np.ndarray:
    p = np.asarray(phase, dtype=object)
    return np.select(
        [p == "qc_start", p == "pre_run", p == "cell_run"],
        [
            "all_qc_calibration_only",
            "transition_quiet_not_main_cell_run",
            "main_acquisition_possible_qc_like_events",
        ],
        default="unknown",
    )


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


def read_lif_csv(spec: ChannelSpec) -> pd.DataFrame:
    df = pd.read_csv(spec.path, sep="\t", header=None, names=["time_min", "raw"], encoding="utf-16")
    df["time_min"] = pd.to_numeric(df["time_min"], errors="coerce")
    df["raw"] = pd.to_numeric(df["raw"], errors="coerce")
    df = df.dropna(subset=["time_min", "raw"]).sort_values("time_min").reset_index(drop=True)
    df["time_sec"] = df["time_min"] * 60.0
    df["channel"] = spec.channel
    df["label"] = spec.label
    df["detector"] = spec.detector
    df["phase"] = phase_from_time_min(df["time_min"])
    return df[["channel", "label", "detector", "phase", "time_min", "time_sec", "raw"]]


def add_baseline_and_noise(trace: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    out = trace.copy()
    time_sec = out["time_sec"].to_numpy(float)
    raw = out["raw"].to_numpy(float)
    dt_sec = float(np.median(np.diff(time_sec)))
    base_window_points = max(11, int(round(BASE_WINDOW_SEC / dt_sec)))
    if base_window_points % 2 == 0:
        base_window_points += 1
    baseline = (
        pd.Series(raw)
        .rolling(window=base_window_points, center=True, min_periods=max(5, base_window_points // 10))
        .quantile(BASE_Q)
        .bfill()
        .ffill()
        .to_numpy(float)
    )
    signal = raw - baseline
    noise_pool = signal[signal <= np.quantile(signal, NOISE_POOL_Q)]
    noise_center = float(np.median(noise_pool))
    noise = float(1.4826 * np.median(np.abs(noise_pool - noise_center)))
    if not np.isfinite(noise) or noise <= 0:
        noise = float(np.std(noise_pool))
    if not np.isfinite(noise) or noise <= 0:
        noise = 1e-12

    out["baseline"] = baseline
    out["signal"] = signal
    out["signal_pos"] = np.maximum(signal, 0.0)
    out["snr_trace"] = out["signal_pos"] / noise
    meta = {
        "channel": out["channel"].iloc[0],
        "label": out["label"].iloc[0],
        "detector": out["detector"].iloc[0],
        "row_count": int(len(out)),
        "time_min_min": float(out["time_min"].min()),
        "time_min_max": float(out["time_min"].max()),
        "dt_sec": dt_sec,
        "base_window_sec": BASE_WINDOW_SEC,
        "base_window_points": int(base_window_points),
        "base_quantile": BASE_Q,
        "noise_pool_quantile": NOISE_POOL_Q,
        "noise": noise,
        "prom_snr_min": PROM_SNR_MIN,
        "prom_threshold": PROM_SNR_MIN * noise,
        "raw_min_distance_sec": RAW_MIN_DISTANCE_SEC,
        "merge_gap_sec": MERGE_GAP_SEC,
        "min_width_sec": MIN_WIDTH_SEC,
        "max_width_sec": MAX_WIDTH_SEC,
        "phase_boundaries_min": PHASE_BOUNDARIES_MIN,
        "red_pair_max_abs_offset_sec": RED_PAIR_MAX_ABS_OFFSET_SEC,
        "red_pair_bin_sec": RED_PAIR_BIN_SEC,
        "red_near_zero_half_width_sec": RED_NEAR_ZERO_HALF_WIDTH_SEC,
    }
    return out, meta


def call_raw_peaks(trace: pd.DataFrame, meta: dict) -> pd.DataFrame:
    signal = trace["signal"].to_numpy(float)
    time_sec = trace["time_sec"].to_numpy(float)
    raw = trace["raw"].to_numpy(float)
    dt_sec = float(meta["dt_sec"])
    distance_points = max(1, int(round(RAW_MIN_DISTANCE_SEC / dt_sec)))
    width_points = (MIN_WIDTH_SEC / dt_sec, MAX_WIDTH_SEC / dt_sec)
    peaks, props = find_peaks(
        signal,
        prominence=float(meta["prom_threshold"]),
        distance=distance_points,
        width=width_points,
    )
    if len(peaks) == 0:
        return pd.DataFrame()

    width_result = peak_widths(signal, peaks, rel_height=0.5)
    width_samples = width_result[0]
    left_ips = width_result[2]
    right_ips = width_result[3]
    prominences = props["prominences"]
    left_bases = props["left_bases"]
    right_bases = props["right_bases"]

    rows = []
    channel = trace["channel"].iloc[0]
    for i, peak_idx in enumerate(peaks, start=1):
        left_i = max(0, int(np.floor(left_ips[i - 1])))
        right_i = min(len(trace) - 1, int(np.ceil(right_ips[i - 1])))
        local_time = time_sec[left_i : right_i + 1]
        local_signal = np.maximum(signal[left_i : right_i + 1], 0.0)
        area = float(np.trapezoid(local_signal, local_time)) if len(local_time) >= 2 else 0.0
        rows.append(
            {
                "peak_id": f"{channel}_raw_{i:06d}",
                "peak_stage": "raw",
                "parent_raw_peak_ids": f"{channel}_raw_{i:06d}",
                "raw_peak_count_merged": 1,
                "channel": channel,
                "label": trace["label"].iloc[0],
                "detector": trace["detector"].iloc[0],
                "phase": trace["phase"].iloc[peak_idx],
                "peak_index": int(peak_idx),
                "time_min": float(trace["time_min"].iloc[peak_idx]),
                "time_sec": float(time_sec[peak_idx]),
                "raw": float(raw[peak_idx]),
                "baseline": float(trace["baseline"].iloc[peak_idx]),
                "height": float(signal[peak_idx]),
                "prominence": float(prominences[i - 1]),
                "snr": float(prominences[i - 1] / meta["noise"]),
                "width_sec": float(width_samples[i - 1] * dt_sec),
                "area": area,
                "left_sec": float(np.interp(left_ips[i - 1], np.arange(len(time_sec)), time_sec)),
                "right_sec": float(np.interp(right_ips[i - 1], np.arange(len(time_sec)), time_sec)),
                "left_base_sec": float(time_sec[left_bases[i - 1]]),
                "right_base_sec": float(time_sec[right_bases[i - 1]]),
                "noise": float(meta["noise"]),
                "prom_threshold": float(meta["prom_threshold"]),
            }
        )
    peaks_df = pd.DataFrame(rows)
    peaks_df["prev_gap_sec"] = peaks_df["time_sec"].diff()
    peaks_df["next_gap_sec"] = peaks_df["time_sec"].shift(-1) - peaks_df["time_sec"]
    peaks_df["nearest_gap_sec"] = peaks_df[["prev_gap_sec", "next_gap_sec"]].min(axis=1)
    return peaks_df


def merge_close_raw_peaks(raw_peaks: pd.DataFrame) -> pd.DataFrame:
    if raw_peaks.empty:
        return raw_peaks.copy()
    rows = []
    ordered = raw_peaks.sort_values("time_sec").reset_index(drop=True)
    groups: list[list[int]] = [[0]]
    for idx in range(1, len(ordered)):
        if float(ordered.loc[idx, "time_sec"] - ordered.loc[idx - 1, "time_sec"]) <= MERGE_GAP_SEC:
            groups[-1].append(idx)
        else:
            groups.append([idx])

    channel = str(ordered["channel"].iloc[0])
    for group_index, indices in enumerate(groups, start=1):
        sub = ordered.iloc[indices].copy()
        best = sub.sort_values(["prominence", "height"], ascending=False).iloc[0].copy()
        left_sec = float(sub["left_sec"].min())
        right_sec = float(sub["right_sec"].max())
        duration = max(0.0, right_sec - left_sec)
        row = best.to_dict()
        row.update(
            {
                "peak_id": f"{channel}_merged_{group_index:06d}",
                "peak_stage": "merged",
                "parent_raw_peak_ids": ";".join(sub["peak_id"].astype(str).tolist()),
                "raw_peak_count_merged": int(len(sub)),
                "time_sec": float(np.average(sub["time_sec"], weights=sub["prominence"].clip(lower=1e-12))),
                "time_min": float(np.average(sub["time_min"], weights=sub["prominence"].clip(lower=1e-12))),
                "height": float(sub["height"].max()),
                "prominence": float(sub["prominence"].sum()),
                "snr": float(sub["prominence"].sum() / sub["noise"].median()),
                "width_sec": duration,
                "area": float(sub["area"].sum()),
                "left_sec": left_sec,
                "right_sec": right_sec,
                "left_base_sec": float(sub["left_base_sec"].min()),
                "right_base_sec": float(sub["right_base_sec"].max()),
            }
        )
        rows.append(row)

    merged = pd.DataFrame(rows).sort_values("time_sec").reset_index(drop=True)
    merged["prev_gap_sec"] = merged["time_sec"].diff()
    merged["next_gap_sec"] = merged["time_sec"].shift(-1) - merged["time_sec"]
    merged["nearest_gap_sec"] = merged[["prev_gap_sec", "next_gap_sec"]].min(axis=1)
    median_width = float(merged["width_sec"].median()) if len(merged) else np.nan
    close_gap_threshold = max(3.0 * median_width, 0.15) if np.isfinite(median_width) else 0.15
    merged["close_gap_threshold_sec"] = close_gap_threshold
    merged["close_peak_risk"] = merged["nearest_gap_sec"] < close_gap_threshold
    merged["merge_risk"] = merged["raw_peak_count_merged"] > 1
    return merged


def build_trace_phase_summary(traces: pd.DataFrame) -> pd.DataFrame:
    out = (
        traces.groupby(["channel", "label", "detector", "phase"], as_index=False)
        .agg(
            row_count=("raw", "size"),
            time_min_min=("time_min", "min"),
            time_min_max=("time_min", "max"),
            raw_median=("raw", "median"),
            raw_p95=("raw", lambda s: float(np.quantile(s, 0.95))),
            signal_p95=("signal", lambda s: float(np.quantile(s, 0.95))),
            snr_trace_p95=("snr_trace", lambda s: float(np.quantile(s, 0.95))),
        )
        .sort_values(["channel", "phase"])
    )
    out["phase_role"] = phase_role_from_phase(out["phase"])
    return out


def build_peak_summary(peaks: pd.DataFrame, trace_meta: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    merged = peaks[peaks["peak_stage"].eq("merged")].copy()
    raw_counts = (
        peaks[peaks["peak_stage"].eq("raw")]
        .groupby("channel", as_index=False)
        .agg(raw_peak_count=("peak_id", "size"))
    )
    by_channel = (
        merged.groupby(["channel", "label", "detector"], as_index=False)
        .agg(
            merged_peak_count=("peak_id", "size"),
            time_min_min=("time_min", "min"),
            time_min_max=("time_min", "max"),
            height_median=("height", "median"),
            prominence_median=("prominence", "median"),
            snr_median=("snr", "median"),
            width_sec_median=("width_sec", "median"),
            nearest_gap_sec_median=("nearest_gap_sec", "median"),
            close_peak_risk_count=("close_peak_risk", "sum"),
            merge_risk_count=("merge_risk", "sum"),
        )
        .merge(
            raw_counts,
            on="channel",
            how="left",
        )
        .merge(
            trace_meta[
                [
                    "channel",
                    "row_count",
                    "dt_sec",
                    "noise",
                    "prom_threshold",
                    "base_window_sec",
                    "base_quantile",
                    "noise_pool_quantile",
                    "prom_snr_min",
                    "raw_min_distance_sec",
                    "merge_gap_sec",
                    "phase_boundaries_min",
                    "red_pair_max_abs_offset_sec",
                    "red_pair_bin_sec",
                    "red_near_zero_half_width_sec",
                ]
            ],
            on="channel",
            how="left",
        )
    )
    by_channel["raw_to_merged_ratio"] = by_channel["raw_peak_count"] / by_channel["merged_peak_count"]
    by_phase = (
        merged.groupby(["channel", "label", "detector", "phase"], as_index=False)
        .agg(
            merged_peak_count=("peak_id", "size"),
            height_median=("height", "median"),
            prominence_median=("prominence", "median"),
            snr_median=("snr", "median"),
            width_sec_median=("width_sec", "median"),
            close_peak_risk_count=("close_peak_risk", "sum"),
            merge_risk_count=("merge_risk", "sum"),
        )
        .sort_values(["channel", "phase"])
    )
    by_phase["phase_role"] = phase_role_from_phase(by_phase["phase"])
    return by_channel, by_phase


def all_pair_offsets(left: pd.DataFrame, right: pd.DataFrame, max_abs_sec: float) -> np.ndarray:
    offsets = []
    right_times = right["time_sec"].to_numpy(float)
    for left_time in left["time_sec"].to_numpy(float):
        close = right_times[(right_times >= left_time - max_abs_sec) & (right_times <= left_time + max_abs_sec)]
        offsets.extend((close - left_time).tolist())
    return np.asarray(offsets, dtype=float)


def count_offsets_in_window(offsets: np.ndarray, center_sec: float, half_width_sec: float) -> int:
    if len(offsets) == 0:
        return 0
    return int((np.abs(offsets - center_sec) <= half_width_sec).sum())


def median_sideband_window_count(offsets: np.ndarray, center_sec: float, half_width_sec: float) -> float:
    if len(offsets) == 0:
        return np.nan
    step = 2.0 * half_width_sec
    centers = np.arange(
        -RED_PAIR_MAX_ABS_OFFSET_SEC + half_width_sec,
        RED_PAIR_MAX_ABS_OFFSET_SEC - half_width_sec + step / 2.0,
        step,
    )
    centers = centers[np.abs(centers - center_sec) > max(3.0, step)]
    counts = [count_offsets_in_window(offsets, float(center), half_width_sec) for center in centers]
    return float(np.median(counts)) if counts else np.nan


def red_detector_audit(merged_peaks: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    offset_rows = []
    if "detector" in merged_peaks.columns:
        red = merged_peaks[merged_peaks["detector"].astype(str).str.lower().eq("red")]
    else:
        red = merged_peaks[merged_peaks["channel"].astype(str).str.upper().str.startswith("R")]
    red_channels = list(dict.fromkeys(red["channel"].astype(str).tolist()))
    channel_pairs = [
        (red_channels[left_index], red_channels[right_index])
        for left_index in range(len(red_channels))
        for right_index in range(left_index + 1, len(red_channels))
    ]
    if not channel_pairs:
        only_channel = red_channels[0] if red_channels else ""
        for phase in ["qc_start", "pre_run", "cell_run"]:
            left = red[red["phase"].eq(phase)] if only_channel else red.iloc[0:0]
            rows.append(
                {
                    "phase": phase,
                    "left_channel": only_channel,
                    "right_channel": "",
                    "left_peak_count": int(len(left)),
                    "right_peak_count": 0,
                    "r1_peak_count": int(len(left)) if only_channel == "R1" else 0,
                    "r2_peak_count": int(len(left)) if only_channel == "R2" else 0,
                    "all_pair_offset_count_pm30sec": 0,
                    "offset_mode_sec": np.nan,
                    "right_minus_left_offset_mode_sec": np.nan,
                    "r2_minus_r1_offset_mode_sec": np.nan,
                    "offset_mode_bin_count": 0,
                    "near_zero_pair_count_pm0p75sec": 0,
                    "sideband_median_pair_count_pm0p75sec": np.nan,
                    "near_zero_over_sideband": np.nan,
                    "median_abs_offset_sec_all_pairs": np.nan,
                    "audit_status": "not_applicable_fewer_than_two_red_channels",
                    "interpretation": "A same-detector offset audit requires at least two red channels.",
                }
            )
        return pd.DataFrame(rows), pd.DataFrame(
            columns=["phase", "left_channel", "right_channel", "right_minus_left_sec", "r2_minus_r1_sec"]
        )

    for left_channel, right_channel in channel_pairs:
        left_all = red[red["channel"].eq(left_channel)].sort_values("time_sec")
        right_all = red[red["channel"].eq(right_channel)].sort_values("time_sec")
        for phase in ["qc_start", "pre_run", "cell_run"]:
            left = left_all[left_all["phase"].eq(phase)].reset_index(drop=True)
            right = right_all[right_all["phase"].eq(phase)].reset_index(drop=True)
            offsets = all_pair_offsets(left, right, RED_PAIR_MAX_ABS_OFFSET_SEC)
            for offset in offsets:
                if (left_channel, right_channel) == ("R1", "R2"):
                    r2_minus_r1 = float(offset)
                elif (left_channel, right_channel) == ("R2", "R1"):
                    r2_minus_r1 = -float(offset)
                else:
                    r2_minus_r1 = np.nan
                offset_rows.append(
                    {
                        "phase": phase,
                        "left_channel": left_channel,
                        "right_channel": right_channel,
                        "right_minus_left_sec": float(offset),
                        "r2_minus_r1_sec": r2_minus_r1,
                    }
                )
            if len(offsets):
                bins = np.arange(
                    -RED_PAIR_MAX_ABS_OFFSET_SEC,
                    RED_PAIR_MAX_ABS_OFFSET_SEC + RED_PAIR_BIN_SEC,
                    RED_PAIR_BIN_SEC,
                )
                hist, edges = np.histogram(offsets, bins=bins)
                best = int(np.argmax(hist))
                mode_offset = float((edges[best] + edges[best + 1]) / 2.0)
                mode_count = int(hist[best])
                near_zero = count_offsets_in_window(offsets, 0.0, RED_NEAR_ZERO_HALF_WIDTH_SEC)
                sideband = median_sideband_window_count(offsets, 0.0, RED_NEAR_ZERO_HALF_WIDTH_SEC)
                near_zero_over_sideband = float(near_zero / sideband) if np.isfinite(sideband) and sideband > 0 else np.nan
                median_abs_nearest = float(np.median(np.abs(offsets)))
            else:
                mode_offset = np.nan
                mode_count = 0
                near_zero = 0
                sideband = np.nan
                near_zero_over_sideband = np.nan
                median_abs_nearest = np.nan
            if (left_channel, right_channel) == ("R2", "R1"):
                compatibility_mode_offset = -mode_offset
            else:
                compatibility_mode_offset = mode_offset
            r2_minus_r1_mode_offset = (
                compatibility_mode_offset
                if {left_channel, right_channel} == {"R1", "R2"}
                else np.nan
            )
            rows.append(
                {
                    "phase": phase,
                    "left_channel": left_channel,
                    "right_channel": right_channel,
                    "left_peak_count": int(len(left)),
                    "right_peak_count": int(len(right)),
                    "r1_peak_count": int(len(left)) if left_channel == "R1" else int(len(right)) if right_channel == "R1" else 0,
                    "r2_peak_count": int(len(left)) if left_channel == "R2" else int(len(right)) if right_channel == "R2" else 0,
                    "all_pair_offset_count_pm30sec": int(len(offsets)),
                    "offset_mode_sec": compatibility_mode_offset,
                    "right_minus_left_offset_mode_sec": mode_offset,
                    "r2_minus_r1_offset_mode_sec": r2_minus_r1_mode_offset,
                    "offset_mode_bin_count": mode_count,
                    "near_zero_pair_count_pm0p75sec": near_zero,
                    "sideband_median_pair_count_pm0p75sec": sideband,
                    "near_zero_over_sideband": near_zero_over_sideband,
                    "median_abs_offset_sec_all_pairs": median_abs_nearest,
                    "audit_status": "pair_audited",
                    "interpretation": "All-pair offsets are density-sensitive and are not annotation evidence.",
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(offset_rows)


def plot_trace_overview(traces: pd.DataFrame, peaks: pd.DataFrame) -> None:
    merged = peaks[peaks["peak_stage"].eq("merged")]
    fig, axes = plt.subplots(3, 1, figsize=(10, 6.2), sharex=True)
    colors = {"G2": "#2a9d55", "R2": "#c2410c", "R1": "#b91c1c"}
    for ax, (channel, sub) in zip(axes, traces.groupby("channel", sort=False)):
        plot_sub = sub.iloc[:: max(1, len(sub) // 6000)].copy()
        ax.plot(plot_sub["time_min"], plot_sub["signal"], lw=0.55, color=colors.get(channel, "0.25"), label=channel)
        pk = merged[merged["channel"].eq(channel)]
        ax.scatter(pk["time_min"], pk["height"], s=5, color="black", alpha=0.45, linewidths=0)
        ax.axvline(10, color="0.7", lw=0.7, ls="--")
        ax.axvline(40, color="0.7", lw=0.7, ls="--")
        ax.set_ylabel(f"{channel}\nsignal")
    axes[-1].set_xlabel("time (min)")
    fig.suptitle("V3-01 LIF baseline-corrected traces and merged peaks", y=0.995)
    save_png(fig, OUT_FIG / "v3_01_lif_trace_peak_overview.png")


def plot_peak_qc(peaks: pd.DataFrame, red_offsets: pd.DataFrame) -> None:
    merged = peaks[peaks["peak_stage"].eq("merged")].copy()
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.0))
    for channel, sub in merged.groupby("channel"):
        axes[0].hist(sub["width_sec"], bins=40, histtype="step", lw=1.2, label=channel)
        axes[1].hist(np.clip(sub["snr"], 0, 100), bins=40, histtype="step", lw=1.2, label=channel)
    axes[0].set_xlabel("width (sec)")
    axes[0].set_ylabel("peak count")
    axes[1].set_xlabel("SNR, clipped at 100")
    axes[1].set_ylabel("peak count")
    if len(red_offsets):
        offset_col = "right_minus_left_sec" if "right_minus_left_sec" in red_offsets.columns else "r2_minus_r1_sec"
        axes[2].hist(red_offsets[offset_col].dropna(), bins=np.arange(-30, 30.25, 0.5), color="#a855f7", alpha=0.8)
    axes[2].axvline(0, color="black", lw=0.8)
    axes[2].set_xlabel("same-detector channel offset (sec)")
    axes[2].set_ylabel("pair count")
    axes[0].legend()
    fig.suptitle("V3-01 compact LIF QC distributions", y=1.03)
    save_png(fig, OUT_FIG / "v3_01_lif_peak_qc_distributions.png")


def write_report(
    trace_meta: pd.DataFrame,
    trace_phase_summary: pd.DataFrame,
    peak_summary: pd.DataFrame,
    peak_phase_summary: pd.DataFrame,
    red_audit: pd.DataFrame,
) -> None:
    lines = [
        "# V3-01 LIF trace physical QC and peak calling",
        "",
        "## 结论",
        "",
        "- 本步骤只读取 V3-00 的 `00_allowed_inputs.csv` 输入锁和其中允许的三个 raw LIF CSV；没有读取作者 CSV、h5ad、人工补峰或任何 V2 输出。",
        "- 读取 raw LIF 前会校验 V3-00 记录的大小、首尾 1MB SHA256 和 full SHA256，避免锁定后文件变化或路径替换。",
        "- `qc_start` 明确定义为 0-10.5 min 全 QC 校准段，只能用于 QC/time-calibration 审计；不作为普通 CAR-T 细胞段解释。",
        "- 每个 channel 独立估计 baseline 和 noise，再用物理峰宽、prominence/SNR 和近邻风险生成 raw peak 与 merged peak。",
        "- R1/R2 共用 red detector 的问题在本步骤只做时间轴/串扰候选审计，不把它转成标签判断。",
        "- QC 图只保留两张：全程信号+峰概览、峰宽/SNR/红通道 offset 分布，便于人工快速审查。",
        "",
        "## 参数",
        "",
        md_table(
            trace_meta[
                [
                    "channel",
                    "label",
                    "detector",
                    "row_count",
                    "time_min_min",
                    "time_min_max",
                    "dt_sec",
                    "noise",
                    "prom_threshold",
                    "base_window_sec",
                    "base_quantile",
                    "noise_pool_quantile",
                    "prom_snr_min",
                    "raw_min_distance_sec",
                    "merge_gap_sec",
                    "min_width_sec",
                    "max_width_sec",
                    "phase_boundaries_min",
                    "red_pair_max_abs_offset_sec",
                    "red_pair_bin_sec",
                    "red_near_zero_half_width_sec",
                ]
            ]
        ),
        "",
        "## merged peak 按 channel 汇总",
        "",
        md_table(peak_summary),
        "",
        "## merged peak 按 phase 汇总",
        "",
        "说明：`phase_role=all_qc_calibration_only` 的 0-10.5 min 峰全部按 QC 校准证据处理；`cell_run` 中若出现 QC-like pattern，也只能作为后续 QC anchor 候选，不能反推标签。",
        "",
        md_table(peak_phase_summary),
        "",
        "## trace phase 背景统计",
        "",
        md_table(trace_phase_summary),
        "",
        "## R1/R2 red detector 审计",
        "",
        "说明：R1/R2 使用同一个 red detector，但代表不同样本通道。这里统计 ±30 sec 内所有 R2-R1 pair offset 的模式、近 0 sec 计数和 sideband 背景；all-pair offset 会受峰密度影响，因此这里只作为串扰或同步结构风险提示，不作为标签证据。",
        "",
        md_table(red_audit),
        "",
        "## 输出文件",
        "",
        "- `data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet`",
        "- `data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet`",
        "- `results/tables/v3/01_lif_trace_physical_qc/v3_01_lif_peak_summary.csv`",
        "- `results/tables/v3/01_lif_trace_physical_qc/v3_01_lif_peak_phase_summary.csv`",
        "- `results/tables/v3/01_lif_trace_physical_qc/v3_01_red_detector_audit.csv`",
        "- `results/qc/v3/01_lif_trace_physical_qc/v3_01_red_detector_pair_offsets.csv`",
        "- `results/figures/v3/01_lif_trace_physical_qc/v3_01_lif_trace_peak_overview.png`",
        "- `results/figures/v3/01_lif_trace_physical_qc/v3_01_lif_peak_qc_distributions.png`",
        "",
        "## 下一步 gate",
        "",
        "- 如果峰数、峰宽、SNR 和 close/merge risk 可解释，则进入 V3-02 MS event calling。",
        "- 如果 V3-03 发现 QC composite anchor 不足，应回到本步骤检查 G2/R1 peak calling，但仍不能用作者 CSV 或 h5ad 调参。",
    ]
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(project_dir: str | Path | None = None) -> None:
    if project_dir is not None:
        configure_project_root(project_dir)
    apply_plot_style()
    OUT_DATA.mkdir(parents=True, exist_ok=True)
    OUT_TABLE.mkdir(parents=True, exist_ok=True)
    OUT_FIG.mkdir(parents=True, exist_ok=True)
    OUT_QC.mkdir(parents=True, exist_ok=True)

    traces = []
    peak_tables = []
    meta_rows = []
    for spec in load_channel_specs():
        trace = read_lif_csv(spec)
        trace, meta = add_baseline_and_noise(trace)
        raw_peaks = call_raw_peaks(trace, meta)
        merged_peaks = merge_close_raw_peaks(raw_peaks)
        peaks = pd.concat([raw_peaks, merged_peaks], ignore_index=True, sort=False)
        traces.append(trace)
        peak_tables.append(peaks)
        meta_rows.append(meta)

    trace_all = pd.concat(traces, ignore_index=True)
    peaks_all = pd.concat(peak_tables, ignore_index=True, sort=False)
    trace_meta = pd.DataFrame(meta_rows)
    merged = peaks_all[peaks_all["peak_stage"].eq("merged")].copy()
    trace_phase_summary = build_trace_phase_summary(trace_all)
    peak_summary, peak_phase_summary = build_peak_summary(peaks_all, trace_meta)
    red_audit, red_offsets = red_detector_audit(merged)

    trace_all.to_parquet(OUT_DATA / "v3_01_lif_traces.parquet", index=False)
    peaks_all.to_parquet(OUT_DATA / "v3_01_lif_peaks.parquet", index=False)
    trace_meta.to_csv(OUT_TABLE / "v3_01_lif_trace_meta.csv", index=False)
    trace_phase_summary.to_csv(OUT_TABLE / "v3_01_lif_trace_phase_summary.csv", index=False)
    peak_summary.to_csv(OUT_TABLE / "v3_01_lif_peak_summary.csv", index=False)
    peak_phase_summary.to_csv(OUT_TABLE / "v3_01_lif_peak_phase_summary.csv", index=False)
    red_audit.to_csv(OUT_TABLE / "v3_01_red_detector_audit.csv", index=False)
    red_offsets.to_csv(OUT_QC / "v3_01_red_detector_pair_offsets.csv", index=False)

    plot_trace_overview(trace_all, peaks_all)
    plot_peak_qc(peaks_all, red_offsets)
    write_report(trace_meta, trace_phase_summary, peak_summary, peak_phase_summary, red_audit)

    print(f"Wrote {OUT_DATA / 'v3_01_lif_traces.parquet'}")
    print(f"Wrote {OUT_DATA / 'v3_01_lif_peaks.parquet'}")
    print(f"Wrote {OUT_TABLE / 'v3_01_lif_peak_summary.csv'}")
    print(f"Wrote {OUT_REPORT}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LIF trace physical QC and peak calling.")
    parser.add_argument("--project-dir", default=None, help="项目根目录；默认使用当前工作目录。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(project_dir=args.project_dir)


if __name__ == "__main__":
    main()
