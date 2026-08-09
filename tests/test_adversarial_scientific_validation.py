"""Independent scientific regressions without author-count targets.

The injected peak schedule is the only ground truth in the detector test.  In
particular, none of these assertions uses a MassHunter/971/event count.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

from annotation_app.app import (
    acquisition_layout_from_manifest,
    calibration_protocol_from_manifest,
    post_qc_strategy_from_manifest,
    read_project_manifest,
)
from scripts.v3 import run_v3_01_lif_trace_physical_qc as lif_qc
from scripts.v3.lif_peak_detection import adaptive_lif_peak_detection


SAMPLE_INTERVAL_SEC = 0.01
TRACE_DURATION_SEC = 180.0
MATCH_TOLERANCE_SEC = 0.08
INJECTED_PEAKS = (
    # center_sec, amplitude/noise-SD, FWHM_sec
    (15.123, 11.5, 0.10),
    (45.456, 12.5, 0.14),
    (75.789, 14.0, 0.08),
    (110.321, 18.0, 0.18),
    (145.654, 25.0, 0.12),
)
FOUR_TO_EIGHT_SIGMA_PEAKS = (
    (15.123, 4.0, 0.10),
    (45.456, 5.0, 0.14),
    (75.789, 6.0, 0.08),
    (110.321, 7.0, 0.18),
    (145.654, 8.0, 0.12),
)
CORE_TEMPLATE_SEED_PEAKS = (
    # Independent high-confidence pulses let v2 learn this channel's pulse
    # morphology; they are not used as a target count or weak-peak truth.
    (27.222, 15.0, 0.12),
    (90.222, 16.0, 0.10),
    (132.333, 18.0, 0.15),
)
ADAPTIVE_VALIDATION_PEAKS = FOUR_TO_EIGHT_SIGMA_PEAKS + CORE_TEMPLATE_SEED_PEAKS


def synthetic_lif_trace(
    seed: int,
    *,
    channel: str,
    detector: str,
    injections: tuple[tuple[float, float, float], ...] = (),
) -> pd.DataFrame:
    """Create drifted, colored detector noise with optional known peaks."""

    rng = np.random.default_rng(seed)
    time_sec = np.arange(0.0, TRACE_DURATION_SEC, SAMPLE_INTERVAL_SEC)
    innovations = rng.normal(0.0, 1.0, len(time_sec))
    colored = np.empty_like(innovations)
    colored[0] = innovations[0]
    ar_coefficient = 0.35
    innovation_scale = np.sqrt(1.0 - ar_coefficient**2)
    for index in range(1, len(colored)):
        colored[index] = (
            ar_coefficient * colored[index - 1]
            + innovation_scale * innovations[index]
        )
    raw = (
        100.0
        + 0.006 * time_sec
        + 0.6 * np.sin(2.0 * np.pi * time_sec / 70.0)
        + colored
    )
    for center_sec, amplitude_snr, fwhm_sec in injections:
        sigma_sec = fwhm_sec / 2.354820045
        raw += amplitude_snr * np.exp(
            -0.5 * ((time_sec - center_sec) / sigma_sec) ** 2
        )
    return pd.DataFrame(
        {
            "channel": channel,
            "label": channel,
            "detector": detector,
            "phase": "synthetic_validation",
            "time_min": time_sec / 60.0,
            "time_sec": time_sec,
            "raw": raw,
        }
    )


def detected_peak_times(trace: pd.DataFrame) -> np.ndarray:
    detector = adaptive_lif_peak_detection()
    corrected, metadata = lif_qc.add_baseline_and_noise(
        trace,
        detection_config=detector,
    )
    raw_peaks = lif_qc.call_raw_peaks(
        corrected,
        metadata,
        detection_config=detector,
    )
    merged = lif_qc.merge_close_raw_peaks(raw_peaks)
    if merged.empty:
        return np.asarray([], dtype=float)
    return merged["time_sec"].to_numpy(float)


def match_injected_peaks(
    detected: np.ndarray,
    truth: tuple[tuple[float, float, float], ...],
) -> tuple[list[float], set[int], list[bool]]:
    available = set(range(len(detected)))
    errors: list[float] = []
    matched_truth: list[bool] = []
    for center_sec, _amplitude_snr, _fwhm_sec in truth:
        candidates = [
            (abs(float(detected[index]) - center_sec), index)
            for index in available
            if abs(float(detected[index]) - center_sec) <= MATCH_TOLERANCE_SEC
        ]
        if not candidates:
            matched_truth.append(False)
            continue
        error_sec, detected_index = min(candidates)
        available.remove(detected_index)
        errors.append(error_sec)
        matched_truth.append(True)
    return errors, available, matched_truth


class SyntheticWeakPeakAndNoiseValidationTest(unittest.TestCase):
    def test_g2_weak_peak_recall_red_noise_fdr_and_timing_error(self):
        replicate_count = 20
        noise_calls = 0
        true_positives = 0
        false_positives = 0
        weak_true_positives = 0
        timing_errors: list[float] = []

        # R1/R2 are negative controls: colored detector noise with no truth peaks.
        for seed in range(replicate_count):
            channel = "R1" if seed % 2 == 0 else "R2"
            noise_calls += len(
                detected_peak_times(
                    synthetic_lif_trace(
                        seed,
                        channel=channel,
                        detector="red",
                    )
                )
            )

        # G2 receives a fixed schedule including two near-threshold weak peaks.
        for seed in range(replicate_count):
            detected = detected_peak_times(
                synthetic_lif_trace(
                    100 + seed,
                    channel="G2",
                    detector="green",
                    injections=INJECTED_PEAKS,
                )
            )
            errors, unmatched_detected, matched_truth = match_injected_peaks(
                detected, INJECTED_PEAKS
            )
            timing_errors.extend(errors)
            true_positives += sum(matched_truth)
            weak_true_positives += sum(matched_truth[:2])
            false_positives += len(unmatched_detected)

        truth_count = replicate_count * len(INJECTED_PEAKS)
        weak_truth_count = replicate_count * 2
        recall = true_positives / truth_count
        weak_recall = weak_true_positives / weak_truth_count
        fdr = false_positives / max(1, true_positives + false_positives)
        noise_minutes = replicate_count * TRACE_DURATION_SEC / 60.0
        noise_call_density_per_min = noise_calls / noise_minutes
        p95_error_sec = (
            float(np.quantile(timing_errors, 0.95)) if timing_errors else float("inf")
        )
        metrics = {
            "recall": recall,
            "weak_recall": weak_recall,
            "injected_fdr": fdr,
            "red_noise_calls_per_min": noise_call_density_per_min,
            "p95_time_error_sec": p95_error_sec,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "noise_calls": noise_calls,
        }
        detail = json.dumps(metrics, sort_keys=True)

        self.assertGreaterEqual(recall, 0.90, detail)
        self.assertGreaterEqual(weak_recall, 0.80, detail)
        self.assertLessEqual(fdr, 0.05, detail)
        self.assertLessEqual(noise_call_density_per_min, 0.10, detail)
        self.assertLessEqual(p95_error_sec, 0.05, detail)

    def test_strict_four_to_eight_sigma_recall_fdr_and_timing_gate(self):
        """Validate v2 against injected truth, never an experimental row count."""

        replicate_count = 20
        true_positives = 0
        false_positives = 0
        four_to_six_sigma_true_positives = 0
        timing_errors: list[float] = []
        for seed in range(replicate_count):
            detected = detected_peak_times(
                synthetic_lif_trace(
                    500 + seed,
                    channel="G2",
                    detector="green",
                    injections=ADAPTIVE_VALIDATION_PEAKS,
                )
            )
            errors, unmatched_detected, matched_truth = match_injected_peaks(
                detected, ADAPTIVE_VALIDATION_PEAKS
            )
            timing_errors.extend(errors)
            true_positives += sum(matched_truth)
            four_to_six_sigma_true_positives += sum(matched_truth[:3])
            false_positives += len(unmatched_detected)

        truth_count = replicate_count * len(ADAPTIVE_VALIDATION_PEAKS)
        four_to_six_truth_count = replicate_count * 3
        recall = true_positives / truth_count
        four_to_six_recall = (
            four_to_six_sigma_true_positives / four_to_six_truth_count
        )
        fdr = false_positives / max(1, true_positives + false_positives)
        p95_error_sec = (
            float(np.quantile(timing_errors, 0.95)) if timing_errors else float("inf")
        )
        metrics = {
            "four_to_eight_sigma_recall": recall,
            "four_to_six_sigma_recall": four_to_six_recall,
            "injected_fdr": fdr,
            "p95_time_error_sec": p95_error_sec,
            "true_positives": true_positives,
            "false_positives": false_positives,
        }
        detail = json.dumps(metrics, sort_keys=True)

        # These targets are defined against injected truth, never an observed
        # experimental event count. Legacy V3 fails this same weak-peak gate.
        self.assertGreaterEqual(recall, 0.90, detail)
        self.assertGreaterEqual(four_to_six_recall, 0.80, detail)
        self.assertLessEqual(fdr, 0.05, detail)
        self.assertLessEqual(p95_error_sec, 0.05, detail)


class RealProjectManifestReadOnlyCompatibilityTest(unittest.TestCase):
    PROJECT_NAMES = ("CART_Exp1-3", "CART_Exp2-1", "Young_HSC3")

    def test_cart_and_young_hsc_legacy_adapters_are_read_only(self):
        repository = Path(__file__).resolve().parents[1]
        projects_root = Path(
            os.environ.get("LMA_EXISTING_PROJECTS_ROOT", repository.parent)
        ).expanduser().resolve()
        available = [
            projects_root / name
            for name in self.PROJECT_NAMES
            if (projects_root / name / "lifms_project.json").is_file()
        ]
        if not available:
            self.skipTest("No external CART/Young_HSC project manifests are available")

        for project_dir in available:
            with self.subTest(project=project_dir.name):
                manifest_path = project_dir / "lifms_project.json"
                before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                with tempfile.TemporaryDirectory(
                    prefix=f"lma_{project_dir.name}_compat_"
                ) as temporary_root:
                    project_copy = Path(temporary_root) / project_dir.name
                    project_copy.mkdir()
                    shutil.copy2(
                        manifest_path,
                        project_copy / "lifms_project.json",
                    )
                    manifest = read_project_manifest(project_copy)
                    layout = acquisition_layout_from_manifest(manifest)
                    expected_channels = list(layout["qc_anchor_channels"])
                    protocol = calibration_protocol_from_manifest(manifest, {})
                    strategy = post_qc_strategy_from_manifest(manifest, {})
                after = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

                self.assertEqual(before, after)
                self.assertEqual(
                    protocol.get("compatibility_mode"),
                    "v0.3_qc_anchor_channels",
                )
                self.assertEqual(
                    protocol["segments"][0]["reference_channels"],
                    expected_channels,
                )
                self.assertEqual(strategy["mode"], "signature")
                self.assertEqual(
                    strategy.get("compatibility_mode"),
                    "v0.3_qc_anchor_channels",
                )
                self.assertEqual(strategy["reference_channels"], expected_channels)


if __name__ == "__main__":
    unittest.main()
