#!/usr/bin/env python3
"""Read-only LIF evidence audit for HSC1-style G1/G2/R1/R2 inputs.

This report deliberately has no expected experimental peak/cell count.  It
describes SNR, time concentration, and negative-control call density; injected
truth performance is covered by ``test_adversarial_scientific_validation``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "LMAStudioHscValidationMpl")
)
REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

import numpy as np

from scripts.v3 import run_v3_01_lif_trace_physical_qc as lif_qc
from scripts.v3.lif_peak_detection import (
    adaptive_lif_peak_detection,
    legacy_lif_peak_detection,
    lif_peak_detection_hash,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_channel(
    path: Path,
    channel: str,
    *,
    detector_version: int,
) -> dict[str, object]:
    detector = "green" if channel.upper().startswith("G") else "red"
    detection_config = (
        adaptive_lif_peak_detection()
        if detector_version == 2
        else legacy_lif_peak_detection()
    )
    spec = lif_qc.ChannelSpec(channel, channel, detector, path)
    trace = lif_qc.read_lif_csv(spec)
    corrected, metadata = lif_qc.add_baseline_and_noise(
        trace,
        detection_config=detection_config,
    )
    raw_peaks = lif_qc.call_raw_peaks(
        corrected,
        metadata,
        detection_config=detection_config,
    )
    merged = lif_qc.merge_close_raw_peaks(raw_peaks)
    duration_min = float(trace["time_sec"].max() - trace["time_sec"].min()) / 60.0
    snr = merged["snr"].to_numpy(float) if not merged.empty else np.asarray([])
    minute_indices = (
        np.floor(merged["time_min"].to_numpy(float)).astype(int)
        if not merged.empty
        else np.asarray([], dtype=int)
    )
    minute_counts = (
        np.bincount(minute_indices) if len(minute_indices) else np.asarray([], dtype=int)
    )
    top_minutes = []
    if len(minute_counts):
        top_minutes = [
            {"minute": int(index), "peak_count": int(minute_counts[index])}
            for index in np.argsort(minute_counts)[-10:][::-1]
            if minute_counts[index] > 0
        ]
    near_threshold_count = int(((snr >= 10.0) & (snr < 15.0)).sum())
    tier_counts = (
        {
            str(tier): int(count)
            for tier, count in merged["peak_tier"].value_counts().items()
        }
        if not merged.empty and "peak_tier" in merged
        else {"core": int(len(merged))}
    )
    core_count = int(tier_counts.get("core", 0))
    weak_model_ready = (
        detector_version == 2
        and core_count
        >= int(detection_config["weak"]["min_core_template_peaks"])
        and core_count / duration_min
        >= float(detection_config["weak"]["min_core_rate_per_min"])
    )
    return {
        "path": str(path),
        "channel": channel,
        "detector": detector,
        "row_count": int(len(trace)),
        "duration_min": duration_min,
        "dt_sec": float(metadata["dt_sec"]),
        "estimated_noise": float(metadata["noise"]),
        "detector_version": detector_version,
        "detector_config_hash": lif_peak_detection_hash(detection_config),
        "merged_peak_count": int(len(merged)),
        "peak_tier_counts": tier_counts,
        "weak_model_ready": weak_model_ready,
        "call_density_per_min": float(len(merged) / duration_min),
        "near_threshold_10_to_15_count": near_threshold_count,
        "near_threshold_fraction": float(near_threshold_count / len(merged))
        if len(merged)
        else 0.0,
        "snr_q10_q50_q90": [
            float(np.quantile(snr, quantile)) for quantile in (0.1, 0.5, 0.9)
        ]
        if len(snr)
        else [],
        "nonempty_peak_minutes": int((minute_counts > 0).sum())
        if len(minute_counts)
        else 0,
        "max_peak_count_in_one_minute": int(minute_counts.max())
        if len(minute_counts)
        else 0,
        "top_minutes": top_minutes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "lif_directory",
        type=Path,
        help="Directory containing G1.CSV, G2.CSV, R1.CSV, and R2.CSV",
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        default=["G1", "G2", "R1", "R2"],
    )
    parser.add_argument(
        "--detector-version",
        type=int,
        choices=(1, 2),
        default=2,
        help="Detector semantics to audit in memory (default: adaptive v2)",
    )
    args = parser.parse_args()
    root = args.lif_directory.expanduser().resolve()
    paths = {channel: root / f"{channel}.CSV" for channel in args.channels}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        parser.error("Missing LIF inputs: " + ", ".join(missing))

    before = {channel: sha256_file(path) for channel, path in paths.items()}
    metrics = {
        channel: audit_channel(
            path,
            channel,
            detector_version=args.detector_version,
        )
        for channel, path in paths.items()
    }
    after = {channel: sha256_file(path) for channel, path in paths.items()}
    result = {
        "input_directory": str(root),
        "input_sha256": before,
        "files_unchanged": before == after,
        "count_target_used": False,
        "detector_version": args.detector_version,
        "metrics": metrics,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["files_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
