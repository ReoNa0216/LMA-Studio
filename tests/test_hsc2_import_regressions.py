import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from annotation_app.app import (
    estimate_axis_shift,
    estimate_channel_shift,
    local_delta_evidence_pairs,
    ms760_review_trace,
    multi_anchor_groups_for_range,
    primary_pc34_events,
    reconcile_event_roster_supported_ms_events,
)
from annotation_app.cell_event_map import CellEventMapError
from scripts.v3 import run_v3_02_ms_event_calling as ms_qc


def synthetic_zero_inflated_scan() -> pd.DataFrame:
    rng = np.random.default_rng(20260814)
    scan_count = 24_000
    dt_sec = 0.1
    signal = np.zeros(scan_count, dtype=float)
    noise_indices = np.arange(10, scan_count - 10, 20)
    signal[noise_indices] = rng.uniform(80.0, 420.0, len(noise_indices))
    # Core event plus a roster-supported shallow shoulder four scans later.
    signal[1000:1007] = [5_000.0, 2_500.0, 1_100.0, 920.0, 1_100.0, 980.0, 700.0]
    time_sec = np.arange(scan_count, dtype=float) * dt_sec
    frame = pd.DataFrame(
        {
            "scan_row_index": np.arange(scan_count, dtype=int),
            "spectrum_index": np.arange(scan_count, dtype=int),
            "scan_id": np.arange(1_000_000, 1_000_000 + scan_count, dtype=int),
            "scan_start_time_sec": time_sec,
            "scan_start_time_min": time_sec / 60.0,
            "pc34_760_max_intensity": signal,
            "qc_782_max_intensity": np.zeros(scan_count),
            "pc34_760_ppm_error_at_max_intensity": np.zeros(scan_count),
            "pc34_760_mz_at_max_intensity": np.full(scan_count, 760.5851),
            "pc34_760_roster_support_ppm_error_at_max_intensity": np.zeros(
                scan_count
            ),
            "pc34_760_roster_support_mz_at_max_intensity": np.full(
                scan_count, 760.5851
            ),
            "qc_782_ppm_error_at_max_intensity": np.zeros(scan_count),
            "tic": np.full(scan_count, 2_000_000.0),
            "ratio_760_782_max_pseudo1": signal + 1.0,
            "array_length": np.full(scan_count, 8_000, dtype=int),
            "base_peak_mz": np.full(scan_count, 760.5851),
        }
    )
    frame["pc34_760_roster_support_max_intensity"] = frame[
        "pc34_760_max_intensity"
    ]
    return frame


def core_events(scan: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    dt_sec = 0.1
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
    indices = ms_qc.call_peak_indices(
        scan,
        "pc34_760_max_intensity",
        params["peak_height"],
        params["peak_prominence"],
        params["min_distance_sec"],
        dt_sec,
    )
    return ms_qc.build_event_table(scan, indices, params, "pc34_primary"), params


class Hsc2EventRosterRegressionTest(unittest.TestCase):
    def write_roster(self, root: Path, scan: pd.DataFrame, index: int) -> Path:
        path = root / "events.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["scan_start_time", "UMAP1", "UMAP2", "Type"])
            writer.writerow(
                [
                    float(scan.iloc[index]["scan_start_time_min"]),
                    1.0,
                    -1.0,
                    "must-not-enter-event-evidence",
                ]
            )
        return path

    def test_roster_support_adds_only_the_significant_resolved_shoulder(self):
        scan = synthetic_zero_inflated_scan()
        events, params = core_events(scan)
        self.assertNotIn(
            int(scan.iloc[1004]["scan_id"]),
            events["scan_id"].astype(int).tolist(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write_roster(Path(tmp), scan, 1004)
            reconciled, canonical, metadata, audit = (
                reconcile_event_roster_supported_ms_events(source, events, scan)
            )

        added = reconciled[reconciled["event_tier"].eq("roster_supported")]
        self.assertEqual(len(added), 1)
        self.assertEqual(int(added.iloc[0]["scan_id"]), int(scan.iloc[1004]["scan_id"]))
        self.assertEqual(canonical["ms_event_id"].tolist(), added["event_id"].tolist())
        self.assertEqual(metadata["roster_supported_event_count"], 1)
        self.assertEqual(len(audit), 1)
        self.assertEqual(len(primary_pc34_events(reconciled)), len(events))
        self.assertEqual(added["event_strategy"].tolist(), ["pc34_roster_supported"])
        self.assertGreater(params["peak_height"], 1_100.0)
        self.assertLess(params["event_roster_support_height"], 1_100.0)
        self.assertNotIn("must-not-enter-event-evidence", reconciled.to_csv(index=False))

    def test_roster_scan_on_a_unique_core_peak_flank_reuses_that_core_event(self):
        scan = synthetic_zero_inflated_scan()
        events, _params = core_events(scan)
        core_event = events.loc[
            events["scan_id"].astype(int).eq(int(scan.iloc[1000]["scan_id"]))
        ].iloc[0]
        self.assertGreater(
            abs(float(core_event["time_sec"]) - float(scan.iloc[1002]["scan_start_time_sec"])),
            0.15,
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write_roster(Path(tmp), scan, 1002)
            reconciled, canonical, metadata, audit = (
                reconcile_event_roster_supported_ms_events(source, events, scan)
            )

        self.assertEqual(metadata["roster_supported_event_count"], 0)
        self.assertTrue(audit.empty)
        self.assertEqual(canonical["ms_event_id"].tolist(), [core_event["event_id"]])
        self.assertEqual(len(reconciled), len(events))

    def test_roster_support_refuses_a_subthreshold_noise_maximum(self):
        scan = synthetic_zero_inflated_scan()
        events, _params = core_events(scan)
        noise_index = 10
        self.assertLess(float(scan.iloc[noise_index]["pc34_760_max_intensity"]), 420.0)
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write_roster(Path(tmp), scan, noise_index)
            with self.assertRaisesRegex(CellEventMapError, "未匹配 CSV 行 2") as raised:
                reconcile_event_roster_supported_ms_events(source, events, scan)

        diagnostic = raised.exception.diagnostic_payload()
        self.assertEqual(
            diagnostic["rows"][0]["ReasonCode"],
            "no_eligible_event_after_roster_support",
        )
        self.assertIn("补充检查", diagnostic["rows"][0]["Reason"])

    def test_roster_review_accepts_an_upper_decile_resolved_peak_below_strong_fence(self):
        scan = synthetic_zero_inflated_scan()
        target_index = 1500
        scan.loc[target_index, "pc34_760_max_intensity"] = 450.0
        scan.loc[target_index, "pc34_760_roster_support_max_intensity"] = 450.0
        events, params = core_events(scan)
        self.assertGreater(params["event_roster_support_height"], 450.0)
        self.assertNotIn(
            int(scan.iloc[target_index]["scan_id"]),
            events["scan_id"].astype(int).tolist(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = self.write_roster(Path(tmp), scan, target_index)
            reconciled, canonical, metadata, audit = (
                reconcile_event_roster_supported_ms_events(source, events, scan)
            )

        added = reconciled[reconciled["event_tier"].eq("roster_supported")]
        self.assertEqual(len(added), 1)
        self.assertEqual(int(added.iloc[0]["scan_id"]), int(scan.iloc[target_index]["scan_id"]))
        self.assertEqual(
            added["selection_reason"].tolist(),
            ["event_roster_time_plus_upper_decile_resolved_peak"],
        )
        self.assertLess(metadata["event_roster_review_height"], 450.0)
        self.assertGreater(
            metadata["event_roster_review_height"],
            float(scan.iloc[10]["pc34_760_max_intensity"]),
        )
        self.assertEqual(canonical["ms_event_id"].tolist(), added["event_id"].tolist())
        self.assertEqual(audit["selection_reason"].tolist(), added["selection_reason"].tolist())

    def test_roster_support_can_rescue_a_real_peak_just_outside_core_mass_window(self):
        scan = synthetic_zero_inflated_scan()
        target_index = 2000
        scan.loc[target_index, "pc34_760_max_intensity"] = 0.0
        scan.loc[target_index, "pc34_760_roster_support_max_intensity"] = 1_100.0
        scan.loc[target_index, "qc_782_max_intensity"] = 99.0
        scan.loc[
            target_index,
            "pc34_760_roster_support_ppm_error_at_max_intensity",
        ] = -12.3
        scan.loc[
            target_index,
            "pc34_760_roster_support_mz_at_max_intensity",
        ] = 760.575748358
        events, _params = core_events(scan)
        self.assertNotIn(
            int(scan.iloc[target_index]["scan_id"]),
            events["scan_id"].astype(int).tolist(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = self.write_roster(Path(tmp), scan, target_index)
            reconciled, canonical, metadata, audit = (
                reconcile_event_roster_supported_ms_events(source, events, scan)
            )

        added = reconciled[reconciled["event_tier"].eq("roster_supported")]
        self.assertEqual(len(added), 1)
        self.assertEqual(int(added.iloc[0]["scan_id"]), int(scan.iloc[target_index]["scan_id"]))
        self.assertEqual(
            added["primary_signal_col"].tolist(),
            ["pc34_760_max_intensity"],
        )
        self.assertAlmostEqual(float(added.iloc[0]["pc34_760_apex"]), 1_100.0)
        self.assertAlmostEqual(
            float(added.iloc[0]["ratio_760_782_max_pseudo1"]),
            11.01,
        )
        self.assertAlmostEqual(
            float(added.iloc[0]["pc34_760_ppm_error_at_apex"]),
            -12.3,
        )
        self.assertAlmostEqual(
            float(added.iloc[0]["pc34_760_mz_at_apex"]),
            760.575748358,
        )
        self.assertEqual(canonical["ms_event_id"].tolist(), added["event_id"].tolist())
        self.assertEqual(metadata["roster_supported_event_count"], 1)
        self.assertEqual(metadata["event_roster_support_tolerance_ppm"], 15.0)
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            audit["roster_support_signal_col"].tolist(),
            ["pc34_760_roster_support_max_intensity"],
        )
        self.assertEqual(audit["roster_support_tolerance_ppm"].tolist(), [15.0])
        self.assertEqual(len(primary_pc34_events(reconciled)), len(events))

    def test_core_mass_evidence_wins_when_extended_lane_also_has_an_apex(self):
        scan = synthetic_zero_inflated_scan()
        target_index = 3000
        scan.loc[target_index, "pc34_760_max_intensity"] = 1_100.0
        scan.loc[target_index, "pc34_760_roster_support_max_intensity"] = 1_700.0
        scan.loc[
            target_index,
            "pc34_760_roster_support_ppm_error_at_max_intensity",
        ] = -12.3
        events, _params = core_events(scan)
        self.assertNotIn(
            int(scan.iloc[target_index]["scan_id"]),
            events["scan_id"].astype(int).tolist(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = self.write_roster(Path(tmp), scan, target_index)
            reconciled, canonical, metadata, audit = (
                reconcile_event_roster_supported_ms_events(source, events, scan)
            )

        added = reconciled[reconciled["event_tier"].eq("roster_supported")]
        self.assertEqual(len(added), 1)
        self.assertEqual(canonical["ms_event_id"].tolist(), added["event_id"].tolist())
        self.assertEqual(metadata["roster_supported_event_count"], 1)
        self.assertEqual(
            added["roster_support_signal_col"].tolist(),
            ["pc34_760_max_intensity"],
        )
        self.assertAlmostEqual(float(added.iloc[0]["pc34_760_apex"]), 1_100.0)
        self.assertEqual(len(audit), 1)

    def test_roster_supported_event_never_changes_shift_estimation(self):
        lif = pd.DataFrame(
            [
                {"peak_id": "g1", "channel": "G1", "time_min": 10.0 / 60.0, "time_sec": 10.0, "snr": 30.0},
                {"peak_id": "g2", "channel": "G2", "time_min": 10.0 / 60.0, "time_sec": 10.0, "snr": 30.0},
            ]
        )
        events = pd.DataFrame(
            [
                {
                    "event_id": "core",
                    "time_min": 12.0 / 60.0,
                    "time_sec": 12.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                },
                {
                    "event_id": "manual-only",
                    "time_min": 10.0 / 60.0,
                    "time_sec": 10.0,
                    "event_strategy": "pc34_roster_supported",
                    "primary_signal_col": "pc34_760_max_intensity",
                },
            ]
        )

        channel = estimate_channel_shift(lif, events, "G1", 1.0)
        axis = estimate_axis_shift(
            lif,
            events,
            time_axis="green_axis",
            channels=["G1", "G2"],
            qc_calibration_end_min=1.0,
        )

        self.assertAlmostEqual(channel["shift_sec"], 2.0)
        self.assertAlmostEqual(axis["shift_sec"], 2.0)
        self.assertEqual(
            {row["ms_event_id"] for row in channel["shift_estimation_matches"]},
            {"core"},
        )
        self.assertEqual(
            {row["ms_event_id"] for row in axis["shift_estimation_matches"]},
            {"core"},
        )

    def test_extended_mass_lane_is_spliced_only_into_the_display_trace(self):
        scan = pd.DataFrame(
            {
                "scan_start_time_sec": [10.0, 10.1, 10.2],
                "plot_time_min": np.asarray([10.0, 10.1, 10.2]) / 60.0,
                "pc34_760_max_intensity": [0.0, 0.0, 0.0],
                "pc34_760_roster_support_max_intensity": [0.0, 1_700.0, 0.0],
            }
        )
        events = pd.DataFrame(
            [
                {
                    "event_id": "edge",
                    "event_strategy": "pc34_roster_supported",
                    "roster_support_signal_col": "pc34_760_roster_support_max_intensity",
                    "time_sec": 10.1,
                    "left_sec": 10.05,
                    "right_sec": 10.15,
                }
            ]
        )

        displayed = ms760_review_trace(scan, events)
        legacy = ms760_review_trace(scan.drop(columns=["pc34_760_roster_support_max_intensity"]), events)

        self.assertEqual(displayed["pc34_760_display_intensity"].tolist(), [0.0, 1_700.0, 0.0])
        self.assertEqual(scan["pc34_760_max_intensity"].tolist(), [0.0, 0.0, 0.0])
        self.assertEqual(legacy["pc34_760_display_intensity"].tolist(), [0.0, 0.0, 0.0])

    def test_roster_supported_event_is_excluded_from_delta_and_post_qc(self):
        lif = pd.DataFrame(
            [
                {
                    "peak_id": "g1",
                    "channel": "G1",
                    "time_min": 24.0,
                    "time_sec": 1440.0,
                    "snr": 30.0,
                    "nearest_gap_sec": 10.0,
                    "close_peak_risk": False,
                    "merge_risk": False,
                }
            ]
        )
        events = pd.DataFrame(
            [
                {
                    "event_id": "support",
                    "time_min": 24.0,
                    "time_sec": 1440.0,
                    "pc34_760_apex": 2_000.0,
                    "nearest_event_gap_sec": 10.0,
                    "collision_risk_high": False,
                    "low_quality_scan_window": False,
                    "event_strategy": "pc34_roster_supported",
                    "primary_signal_col": "pc34_760_max_intensity",
                },
                {
                    "event_id": "core",
                    "time_min": 1442.0 / 60.0,
                    "time_sec": 1442.0,
                    "pc34_760_apex": 2_000.0,
                    "nearest_event_gap_sec": 10.0,
                    "collision_risk_high": False,
                    "low_quality_scan_window": False,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                },
            ]
        )

        delta = local_delta_evidence_pairs(
            lif,
            events,
            annotation_start_min=24.0,
            seed_window_min=1.0,
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            ms_delta_sec=0.0,
            channel_shifts_sec={"G1": 0.0},
        )
        post_qc = multi_anchor_groups_for_range(
            lif,
            events,
            anchor_channels=["G1"],
            channel_time_axes={"G1": "green_axis"},
            axis_shifts_sec={"green_axis": 0.0},
            context_start_min=24.0,
            context_end_min=25.0,
            minimum_raw_time_min=24.0,
            ms_shift_sec=0.0,
            tolerance_sec=3.0,
        )

        self.assertNotIn("support", {row["ms_event_id"] for row in delta["evidence"]})
        self.assertNotIn("support", {row["ms_event_id"] for row in post_qc})


if __name__ == "__main__":
    unittest.main()
