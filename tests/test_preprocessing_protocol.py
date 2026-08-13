import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.v3 import run_v3_01_lif_trace_physical_qc as lif_qc
from scripts.v3 import run_v3_02_ms_event_calling as ms_qc
from scripts.v3.project_protocol import (
    classify_project_phase,
    load_project_protocol,
    phase_boundaries_min,
)
from scripts.v3.lif_peak_detection import adaptive_lif_peak_detection


def write_protocol(root: Path, *, boundaries_confirmed: bool = True) -> None:
    path = root / "results/tables/v3/00_project_protocol.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "calibration_protocol": {
                    "mode": "sequential_single_population",
                    "segments": [
                        {
                            "segment_id": "lsk_reference",
                            "population_label": "LSK",
                            "start_min": 1.25,
                            "end_min": 3.5,
                            "reference_channels": ["G1"],
                            "boundaries_confirmed": boundaries_confirmed,
                        },
                        {
                            "segment_id": "linneg_reference",
                            "population_label": "Lin-",
                            "start_min": 5.0,
                            "end_min": 8.75,
                            "reference_channels": ["G2"],
                            "boundaries_confirmed": boundaries_confirmed,
                        },
                    ],
                },
                "post_qc_strategy": {"mode": "disabled"},
                "annotation_config": {"annotation_start_min": 24.0},
                "lif_peak_detection": adaptive_lif_peak_detection(),
            }
        )
        + "\n",
        encoding="utf-8",
    )


class ProjectDrivenPreprocessingPhaseTest(unittest.TestCase):
    def test_unconfirmed_project_windows_remain_explicit_drafts_during_preprocessing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            write_protocol(root, boundaries_confirmed=False)

            policy = load_project_protocol(root)
            phases = classify_project_phase(
                np.asarray([2.0, 6.0, 24.0]),
                policy,
            ).tolist()

            self.assertFalse(policy["boundaries_confirmed"])
            self.assertEqual(
                phases,
                [
                    "calibration_draft:lsk_reference",
                    "calibration_draft:linneg_reference",
                    "annotation_region",
                ],
            )
            self.assertIn("draft", phase_boundaries_min(policy))

    def test_custom_segments_and_annotation_start_drive_phase_labels(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            write_protocol(root)
            policy = load_project_protocol(root)

            phases = classify_project_phase(
                np.asarray([0.5, 2.0, 4.0, 6.0, 20.0, 24.0, 30.0]),
                policy,
            ).tolist()

            self.assertEqual(
                phases,
                [
                    "pre_annotation_unassigned",
                    "calibration:lsk_reference",
                    "pre_annotation_unassigned",
                    "calibration:linneg_reference",
                    "pre_annotation_unassigned",
                    "annotation_region",
                    "annotation_region",
                ],
            )
            description = phase_boundaries_min(policy)
            self.assertIn("LSK", description)
            self.assertIn("1.25-3.5", description)
            self.assertIn("annotation_region:>=24", description)
            self.assertNotIn("10.5", description)
            self.assertNotIn(">=40", description)

    def test_lif_and_ms_preprocessing_use_the_same_project_policy(self):
        lif_root = lif_qc.ROOT
        ms_root = ms_qc.ROOT
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                root = Path(tmp)
                write_protocol(root)
                lif_qc.configure_project_root(root)
                ms_qc.configure_project_root(root)

                times = pd.Series([2.0, 4.0, 6.0, 24.0])
                self.assertEqual(
                    lif_qc.phase_from_time_min(times).tolist(),
                    [
                        "calibration:lsk_reference",
                        "pre_annotation_unassigned",
                        "calibration:linneg_reference",
                        "annotation_region",
                    ],
                )

                events = pd.DataFrame(
                    [
                        {
                            "event_id": f"ms_{index}",
                            "time_min": time_min,
                            "time_sec": time_min * 60,
                            "qc_782_apex": 1.0,
                            "low_quality_scan_window": False,
                            "pc34_760_ppm_error_at_apex": 0.1,
                            "qc_782_ppm_error_at_apex": 0.1,
                        }
                        for index, time_min in enumerate([2.0, 6.0, 24.0], start=1)
                    ]
                )
                audit = ms_qc.build_pc34_support_audit(events, pd.DataFrame())
                roles = set(audit.loc[audit["start_min"].ne("all"), "segment_role"])
                self.assertIn("calibration_reference_only:lsk_reference", roles)
                self.assertIn("annotation_region", roles)
        finally:
            lif_qc.configure_project_root(
                lif_root,
                allow_unbound_module_default=True,
            )
            ms_qc.configure_project_root(
                ms_root,
                allow_unbound_module_default=True,
            )

    def test_plot_boundaries_come_from_project_not_fixed_minutes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            write_protocol(root)
            policy = load_project_protocol(root)
            self.assertEqual(
                [item["time_min"] for item in policy["plot_boundaries"]],
                [1.25, 3.5, 5.0, 8.75, 24.0],
            )

    def test_empty_primary_event_result_retains_schema_and_builds_qc(self):
        signal_col = "pc34_760_max_intensity"
        scan = pd.DataFrame(
            {
                signal_col: [0.0, 0.0, 0.0],
                "scan_start_time_sec": [0.0, 1.0, 2.0],
            }
        )
        params = {
            "signal_col": signal_col,
            "scan_step_sec": 1.0,
            "peak_height": 1.0,
            "peak_prominence": 1.0,
            "min_distance_sec": 1.0,
            "quiet_start_min": 0.0,
            "quiet_end_min": 1.0,
        }

        events = ms_qc.build_event_table(
            scan,
            np.asarray([], dtype=int),
            params,
            "pc34_primary",
        )
        comparison = ms_qc.build_strategy_comparison(events, events)
        qc = ms_qc.build_event_qc(events, comparison, params)

        for column in (
            "event_id",
            "time_min",
            "collision_risk_high",
            "broad_peak_width_gt_1p5_sec",
            "low_quality_scan_window",
        ):
            self.assertIn(column, events.columns)
        self.assertEqual(len(events), 0)
        self.assertEqual(
            qc.loc[qc["metric"].eq("pc34_event_count"), "value"].iloc[0],
            0,
        )

    def test_pc34_threshold_falls_back_when_quiet_estimate_exceeds_data(self):
        signal_col = "pc34_760_max_intensity"
        time_sec = np.arange(600, dtype=float) * 0.1
        signal = np.zeros(600, dtype=float)
        peak_indices = np.asarray([100, 300, 500])
        signal[peak_indices] = [1000.0, 2000.0, 3000.0]
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                signal_col: signal,
            }
        )
        localmax = pd.DataFrame(
            {
                "scan_row_index": peak_indices,
                "time_min": time_sec[peak_indices] / 60.0,
                "height": signal[peak_indices],
            }
        )
        bins = pd.DataFrame(
            [
                {
                    "start_min": 0.0,
                    "end_min": 1.0,
                    "scan_count": 600,
                    "localmax_p99": 2980.0,
                    "scan_p99": 2500.0,
                    "positive_scan_fraction": 0.005,
                }
            ]
        )

        params, _quiet = ms_qc.estimate_parameters(
            scan,
            signal_col,
            bins,
            localmax,
            0.1,
        )

        self.assertLess(params["peak_height"], float(signal.max()))
        self.assertLess(params["peak_prominence"], float(signal.max()))
        self.assertEqual(params["threshold_fallback_reason"], "quiet_threshold_exceeded_signal_range")
        self.assertGreaterEqual(params["preliminary_peak_count"], 3)
        self.assertAlmostEqual(params["min_distance_sec"], 0.2, places=9)

    def test_pc34_robust_background_quantile_rejects_noise_without_fallback(self):
        signal_col = "pc34_760_max_intensity"
        time_sec = np.arange(1200, dtype=float) * 0.1
        signal = np.zeros(1200, dtype=float)
        noise_indices = np.arange(10, 900, 20)
        signal[noise_indices] = 20.0
        true_peak_indices = np.asarray([950, 1050, 1150])
        signal[true_peak_indices] = [1000.0, 2000.0, 3000.0]
        all_peak_indices = np.concatenate([noise_indices, true_peak_indices])
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                signal_col: signal,
            }
        )
        localmax = pd.DataFrame(
            {
                "scan_row_index": all_peak_indices,
                "time_min": time_sec[all_peak_indices] / 60.0,
                "height": signal[all_peak_indices],
            }
        )
        bins = pd.DataFrame(
            [
                {
                    "start_min": 0.0,
                    "end_min": 2.0,
                    "scan_count": 1200,
                    "localmax_p99": 2980.0,
                    "scan_p99": 2500.0,
                    "positive_scan_fraction": 0.04,
                }
            ]
        )

        params, _quiet = ms_qc.estimate_parameters(
            scan,
            signal_col,
            bins,
            localmax,
            0.1,
        )

        self.assertEqual(params["threshold_fallback_reason"], "")
        self.assertAlmostEqual(params["peak_height"], 80.0, places=9)
        self.assertAlmostEqual(params["peak_prominence"], 16.0, places=9)
        self.assertEqual(params["preliminary_peak_count"], 3)
        self.assertAlmostEqual(params["min_distance_sec"], 0.2, places=9)

    def test_pc34_positive_background_uses_robust_tail_without_losing_low_events(self):
        rng = np.random.default_rng(20260812)
        signal_col = "pc34_760_max_intensity"
        dt_sec = 0.1
        signal = np.clip(rng.normal(100.0, 100.0, 12_000), 0.0, 500.0)
        true_peak_indices = np.arange(500, 11_500, 1000)
        signal[true_peak_indices] = 1000.0
        time_sec = np.arange(len(signal), dtype=float) * dt_sec
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                signal_col: signal,
            }
        )
        bins, localmax = ms_qc.build_bin_summary(scan, signal_col, dt_sec)

        params, _background = ms_qc.estimate_parameters(
            scan,
            signal_col,
            bins,
            localmax,
            dt_sec,
        )
        called = ms_qc.call_peak_indices(
            scan,
            signal_col,
            params["peak_height"],
            params["peak_prominence"],
            params["min_distance_sec"],
            dt_sec,
        )

        recalled = sum(np.any(np.abs(called - index) <= 1) for index in true_peak_indices)
        extras = sum(not np.any(np.abs(true_peak_indices - index) <= 1) for index in called)
        self.assertEqual(recalled, len(true_peak_indices))
        self.assertEqual(extras, 0)
        self.assertLess(params["peak_height"], 1000.0)
        self.assertEqual(
            params["peak_height_model"],
            "positive_background_tail_cap",
        )
        self.assertLess(params["quiet_zero_fraction"], 0.5)
        self.assertGreater(params["peak_height_body_candidate"], 1000.0)
        self.assertLess(params["peak_height_positive_noise_candidate"], 1000.0)

    def test_pc34_zero_inflated_variable_noise_uses_localmax_fence_without_losing_events(self):
        """Sparse cells must not contaminate the zero-background height estimate.

        A zero-inflated trace can still contain a broad, variable population of
        electronic local maxima.  Multiplying its upper quartile by four puts
        the height threshold above the real 1,000-count impulses even though
        every noise maximum remains below 500.  The detector must estimate an
        upper fence from the local-maximum body and keep prominence as an
        independent gate; no expected event count or coordinate table is used.
        """

        rng = np.random.default_rng(20260814)
        signal_col = "pc34_760_max_intensity"
        dt_sec = 0.1
        scan_count = 24_000
        signal = np.zeros(scan_count, dtype=float)
        noise_indices = np.arange(10, scan_count - 10, 20)
        signal[noise_indices] = rng.uniform(100.0, 500.0, len(noise_indices))
        true_peak_indices = np.arange(1_000, scan_count - 1_000, 2_000)
        signal[true_peak_indices] = 1_000.0
        time_sec = np.arange(scan_count, dtype=float) * dt_sec
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                signal_col: signal,
            }
        )
        bins, localmax = ms_qc.build_bin_summary(scan, signal_col, dt_sec)

        params, _background = ms_qc.estimate_parameters(
            scan,
            signal_col,
            bins,
            localmax,
            dt_sec,
        )
        called = ms_qc.call_peak_indices(
            scan,
            signal_col,
            params["event_roster_support_height"],
            params["event_roster_support_prominence"],
            params["min_distance_sec"],
            dt_sec,
        )

        recalled = sum(
            np.any(np.abs(called - index) <= 1) for index in true_peak_indices
        )
        extras = sum(
            not np.any(np.abs(true_peak_indices - index) <= 1) for index in called
        )
        self.assertEqual(recalled, len(true_peak_indices))
        self.assertEqual(extras, 0)
        self.assertGreater(params["peak_height"], 1_000.0)
        self.assertLess(params["event_roster_support_height"], 1_000.0)
        self.assertGreater(params["event_roster_support_height"], 500.0)
        self.assertEqual(
            params["peak_height_model"],
            "background_body_multiplier",
        )
        self.assertEqual(
            params["event_roster_support_model"],
            "zero_inflated_height_and_shoulder_gate",
        )
        self.assertGreaterEqual(params["quiet_zero_fraction"], 0.5)
        self.assertEqual(params["zero_inflated_min_zero_fraction"], 0.5)

    def test_zero_inflated_height_significance_keeps_a_resolved_shoulder_event(self):
        """A high, scan-resolved shoulder is an event even on a prior tail.

        Absolute height already has to clear the robust ~4-sigma local-maximum
        fence.  Requiring the same upper-quartile prominence again would
        reject a second cell pulse merely because it lands on the descending
        tail of the first one.
        """

        rng = np.random.default_rng(76020260814)
        signal_col = "pc34_760_max_intensity"
        dt_sec = 0.1
        scan_count = 24_000
        signal = np.zeros(scan_count, dtype=float)
        noise_indices = np.arange(10, scan_count - 10, 20)
        signal[noise_indices] = rng.uniform(80.0, 420.0, len(noise_indices))
        # Two independently resolved maxima, four scans / 0.4 sec apart.  The
        # second clears the noise height fence but has only 180 counts of
        # prominence because it is superimposed on the first event's tail.
        signal[1000:1007] = [5_000.0, 2_500.0, 1_100.0, 920.0, 1_100.0, 980.0, 700.0]
        time_sec = np.arange(scan_count, dtype=float) * dt_sec
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": time_sec,
                "scan_start_time_min": time_sec / 60.0,
                signal_col: signal,
            }
        )
        bins, localmax = ms_qc.build_bin_summary(scan, signal_col, dt_sec)

        params, _background = ms_qc.estimate_parameters(
            scan,
            signal_col,
            bins,
            localmax,
            dt_sec,
        )
        called = ms_qc.call_peak_indices(
            scan,
            signal_col,
            params["event_roster_support_height"],
            params["event_roster_support_prominence"],
            params["min_distance_sec"],
            dt_sec,
        )

        self.assertIn(1000, called.tolist())
        self.assertIn(1004, called.tolist())
        self.assertEqual(
            params["event_roster_support_model"],
            "zero_inflated_height_and_shoulder_gate",
        )


if __name__ == "__main__":
    unittest.main()
