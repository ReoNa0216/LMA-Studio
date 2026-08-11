from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from annotation_app.app import HTML, suggest_calibration_segment_windows
from annotation_app.cell_event_map import CellEventMapError, match_source_to_events
from scripts.v3 import run_v3_02_ms_event_calling as ms_qc


def peak(channel: str, minute: float, *, snr: float = 70.0) -> dict:
    return {
        "channel": channel,
        "time_min": float(minute),
        "time_sec": float(minute) * 60.0,
        "snr": float(snr),
        "peak_tier": "core",
        "peak_stage": "merged",
    }


class MppImportRegressionTest(unittest.TestCase):
    def test_revised_pc34_threshold_retains_prior_event_apices_on_project_copies(self):
        """The more sensitive caller must not discard established PC34 events."""

        workspace = Path(__file__).resolve().parents[2]
        project_roots = [
            workspace / name
            for name in ("CART_Exp1-3", "CART_Exp2-1", "Young_HSC3", "Lin-_LSK")
            if (workspace / name / "lifms_project.json").is_file()
        ]
        if not project_roots:
            self.skipTest("project compatibility fixtures are not available")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            copy_root = Path(tmp)
            for source_root in project_roots:
                with self.subTest(project=source_root.name):
                    manifest = json.loads(
                        (source_root / "lifms_project.json").read_text(encoding="utf-8")
                    )
                    relative_paths = {
                        key: Path(manifest["intermediate_tables"][key]["path"])
                        for key in ("ms_events", "ms_scan_summary")
                    }
                    source_paths = {
                        key: source_root / relative
                        for key, relative in relative_paths.items()
                    }
                    source_hashes = {
                        key: hashlib.sha256(path.read_bytes()).hexdigest()
                        for key, path in source_paths.items()
                    }
                    copied_root = copy_root / source_root.name
                    copied_paths = {}
                    for key, source_path in source_paths.items():
                        destination = copied_root / relative_paths[key]
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_path, destination)
                        copied_paths[key] = destination

                    old_events = pd.read_parquet(copied_paths["ms_events"])
                    scan = pd.read_parquet(copied_paths["ms_scan_summary"])
                    scan = scan.sort_values("scan_start_time_sec").reset_index(drop=True)
                    steps = np.diff(scan["scan_start_time_sec"].to_numpy(float))
                    dt_sec = float(np.median(steps[steps > 0]))
                    bins, localmax = ms_qc.build_bin_summary(
                        scan,
                        "pc34_760_max_intensity",
                        dt_sec,
                    )
                    params, _background = ms_qc.estimate_parameters(
                        scan,
                        "pc34_760_max_intensity",
                        bins,
                        localmax,
                        dt_sec,
                    )
                    called_indices = ms_qc.call_peak_indices(
                        scan,
                        "pc34_760_max_intensity",
                        params["peak_height"],
                        params["peak_prominence"],
                        params["min_distance_sec"],
                        dt_sec,
                    )
                    called_times = np.sort(
                        scan.iloc[called_indices]["scan_start_time_sec"].to_numpy(float)
                    )
                    old_times = np.sort(old_events["time_sec"].to_numpy(float))
                    positions = np.searchsorted(called_times, old_times)
                    nearest = np.full(len(old_times), np.inf, dtype=float)
                    right = positions < len(called_times)
                    nearest[right] = np.minimum(
                        nearest[right],
                        np.abs(called_times[positions[right]] - old_times[right]),
                    )
                    left = positions > 0
                    nearest[left] = np.minimum(
                        nearest[left],
                        np.abs(called_times[positions[left] - 1] - old_times[left]),
                    )

                    self.assertGreaterEqual(len(called_times), len(old_times))
                    self.assertLessEqual(float(nearest.max()), dt_sec + 1e-9)
                    self.assertEqual(
                        {
                            key: hashlib.sha256(path.read_bytes()).hexdigest()
                            for key, path in source_paths.items()
                        },
                        source_hashes,
                    )

    def test_window_suggestion_refuses_a_minor_tail_cluster_when_main_order_is_reversed(self):
        # Lin-/G2 is the early dominant population and MPP/G1 the later one.
        # The declared order is deliberately reversed.  A small late G2 tail
        # must not be promoted into a plausible-looking second reference.
        peaks = pd.DataFrame(
            [peak("G2", 2.6 + index * 0.25) for index in range(20)]
            + [peak("G1", 10.8 + index * 0.30, snr=80.0) for index in range(20)]
            + [peak("G2", 21.3 + index * 0.35) for index in range(4)]
        )
        segments = [
            {
                "segment_id": "mpp",
                "order": 1,
                "reference_channels": ["G1"],
                "population_label": "MPP",
            },
            {
                "segment_id": "lin",
                "order": 2,
                "reference_channels": ["G2"],
                "population_label": "Lin-",
            },
        ]

        result = suggest_calibration_segment_windows(
            peaks,
            segments,
            annotation_start_min=24.0,
        )

        self.assertFalse(result["can_apply_suggestions"])
        self.assertEqual(
            [row["status"] for row in result["segments"]],
            ["order_conflict", "order_conflict"],
        )
        self.assertEqual(result["recommended_segment_order"], ["lin", "mpp"])
        self.assertTrue(any("主峰簇顺序" in warning for warning in result["warnings"]))
        self.assertTrue(any("G2" in warning and "G1" in warning for warning in result["warnings"]))

    def test_ms_threshold_is_not_captured_by_one_extreme_outlier(self):
        rng = np.random.default_rng(20260811)
        scan_count = 12_000
        dt_sec = 0.1
        time_sec = np.arange(scan_count, dtype=float) * dt_sec
        signal = rng.uniform(0.0, 250.0, scan_count)
        injected = np.arange(100, scan_count - 100, 50)
        signal[injected] = rng.uniform(3_000.0, 12_000.0, len(injected))
        signal[injected[len(injected) // 2]] = 55_000.0
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                "pc34_760_max_intensity": signal,
            }
        )

        bins, localmax = ms_qc.build_bin_summary(
            scan,
            "pc34_760_max_intensity",
            dt_sec,
        )
        params, _background = ms_qc.estimate_parameters(
            scan,
            "pc34_760_max_intensity",
            bins,
            localmax,
            dt_sec,
        )
        called = ms_qc.call_peak_indices(
            scan,
            "pc34_760_max_intensity",
            params["peak_height"],
            params["peak_prominence"],
            params["min_distance_sec"],
            dt_sec,
        )
        recalled = sum(np.any(np.abs(called - index) <= 1) for index in injected)
        extras = sum(not np.any(np.abs(injected - index) <= 1) for index in called)

        self.assertGreaterEqual(recalled / len(injected), 0.95)
        self.assertLessEqual(extras, 2)
        self.assertLess(params["peak_height"], 0.25 * float(signal.max()))
        self.assertAlmostEqual(params["min_distance_sec"], 2.0 * dt_sec, places=9)

    def test_event_free_positive_noise_does_not_trigger_range_fallback(self):
        rng = np.random.default_rng(760)
        dt_sec = 0.1
        signal = rng.uniform(0.0, 250.0, 12_000)
        time_sec = np.arange(len(signal), dtype=float) * dt_sec
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                "pc34_760_max_intensity": signal,
            }
        )
        bins, localmax = ms_qc.build_bin_summary(
            scan,
            "pc34_760_max_intensity",
            dt_sec,
        )
        params, _background = ms_qc.estimate_parameters(
            scan,
            "pc34_760_max_intensity",
            bins,
            localmax,
            dt_sec,
        )
        called = ms_qc.call_peak_indices(
            scan,
            "pc34_760_max_intensity",
            params["peak_height"],
            params["peak_prominence"],
            params["min_distance_sec"],
            dt_sec,
        )

        self.assertEqual(params["threshold_fallback_reason"], "")
        self.assertGreater(params["peak_height"], float(signal.max()))
        self.assertEqual(len(called), 0)

    def test_pc34_extraction_includes_observed_minus_10p68_ppm_event(self):
        target_mz = 760.5851
        observed_mz = target_mz * (1.0 - 10.68e-6)
        payload = "\n".join(
            [
                "spectrumList (1 spectra)",
                "spectrum:",
                "  index: 0",
                "  id: scanId=1501139",
                "  defaultArrayLength: 3",
                "  cvParam: total ion current, 5461770, number of detector counts",
                "  cvParam: scan start time, 25.01885, minute",
                "  cvParam: m/z array, m/z",
                f"  binary: [3] 100.0 {observed_mz:.12f} 800.0",
                "  cvParam: intensity array, number of detector counts",
                "  binary: [3] 0.0 3666.78222656 0.0",
                "",
            ]
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            source = Path(tmp) / "one-scan.txt"
            source.write_text(payload, encoding="ascii")
            scan, _summary = ms_qc.parse_ms_scan_summary(source)

        self.assertEqual(len(scan), 1)
        self.assertEqual(int(scan.iloc[0]["pc34_760_n_mz"]), 1)
        self.assertAlmostEqual(
            float(scan.iloc[0]["pc34_760_max_intensity"]),
            3666.78222656,
            places=6,
        )

    def test_12ppm_window_keeps_prior_in_tolerance_ions_and_rejects_outside_ions(self):
        target_mz = 760.5851
        prior_in_window_mz = target_mz * (1.0 + 9.9e-6)
        outside_mz = target_mz * (1.0 + 12.1e-6)
        payload = "\n".join(
            [
                "spectrumList (1 spectra)",
                "spectrum:",
                "  index: 0",
                "  id: scanId=1",
                "  defaultArrayLength: 4",
                "  cvParam: total ion current, 1000, number of detector counts",
                "  cvParam: scan start time, 1.0, minute",
                "  cvParam: m/z array, m/z",
                f"  binary: [4] 100.0 {prior_in_window_mz:.12f} {outside_mz:.12f} 800.0",
                "  cvParam: intensity array, number of detector counts",
                "  binary: [4] 0.0 1234.0 9999.0 0.0",
                "",
            ]
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            source = Path(tmp) / "mass-window.txt"
            source.write_text(payload, encoding="ascii")
            scan, summary = ms_qc.parse_ms_scan_summary(source)

        self.assertEqual(summary["tolerance_ppm"], 12.0)
        self.assertEqual(int(scan.iloc[0]["pc34_760_n_mz"]), 1)
        self.assertAlmostEqual(
            float(scan.iloc[0]["pc34_760_max_intensity"]),
            1234.0,
            places=6,
        )
        self.assertAlmostEqual(
            float(scan.iloc[0]["pc34_760_ppm_error_at_max_intensity"]),
            9.9,
            places=3,
        )

    def test_event_map_error_explains_detector_under_call_instead_of_blaming_csv(self):
        source = pd.DataFrame(
            {
                "scan_start_time": [24.0 + index * 0.1 for index in range(10)],
                "UMAP1": np.arange(10, dtype=float),
                "UMAP2": -np.arange(10, dtype=float),
            }
        )
        events = pd.DataFrame(
            [
                {
                    "event_id": "only_event",
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                    "scan_id": "scan-1",
                    "time_min": 24.0,
                }
            ]
        )

        with self.assertRaisesRegex(CellEventMapError, "仅识别到 1 个.*10 行"):
            match_source_to_events(source, events)

    def test_calibration_suggestion_action_stays_on_one_line(self):
        self.assertRegex(
            HTML,
            r"\.import-section-title\s*>\s*\.row-actions\s*button\s*\{[^}]*white-space:\s*nowrap",
        )
        self.assertRegex(
            HTML,
            r"\.import-section-title\s*\{[^}]*flex-wrap:\s*wrap",
        )


if __name__ == "__main__":
    unittest.main()
