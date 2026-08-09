import unittest

import numpy as np
import pandas as pd

from annotation_app.app import (
    build_qc_alignment_groups,
    greedy_time_matches,
    multi_anchor_groups_for_range,
    qc_triplets_for_range,
)
from tests.test_calibration_protocol import lif_peak, ms_event


HSC1_LIF_MIN = [2.795083, 2.801950]
HSC1_MS_MIN = [2.807000, 2.823167]


class MonotoneQcMatchingRegressionTest(unittest.TestCase):
    def test_long_exact_tie_does_not_recurse_through_the_full_match_path(self):
        # Real projects can contain well over Python's default recursion limit
        # of peaks.  The first n-1 relations below are unique; the last LIF
        # peak has two scientifically exact ties.  Tie arbitration must remain
        # iterative (or otherwise bounded), rather than recursively rebuilding
        # the entire predecessor path.
        count = 1200
        lif_times = np.arange(count, dtype=float) * 10.0
        ms_times = np.r_[
            np.arange(count - 1, dtype=float) * 10.0,
            lif_times[-1] - 1.0,
            lif_times[-1] + 1.0,
        ]

        matches = greedy_time_matches(
            lif_times,
            ms_times,
            shift_sec=0.0,
            tolerance_sec=1.1,
        )

        self.assertEqual(len(matches), count)
        self.assertEqual(matches[-1][:2], (count - 1, count - 1))

    def test_hsc1_two_peak_match_is_order_preserving(self):
        matches = greedy_time_matches(
            np.asarray(HSC1_LIF_MIN, dtype=float) * 60.0,
            np.asarray(HSC1_MS_MIN, dtype=float) * 60.0,
            shift_sec=0.0,
            tolerance_sec=2.5,
        )

        self.assertEqual(
            [(lif_index, ms_index) for lif_index, ms_index, _residual in matches],
            [(0, 0), (1, 1)],
        )
        self.assertTrue(
            all(
                matches[index][1] < matches[index + 1][1]
                for index in range(len(matches) - 1)
            )
        )

    def test_monotone_match_maximizes_cardinality_before_local_residual(self):
        # A nearest-edge greedy matcher consumes ms[0] with lif[1] and leaves
        # lif[0] unmatched.  The safe monotone solution keeps both relations.
        matches = greedy_time_matches(
            np.asarray([0.0, 1.0], dtype=float),
            np.asarray([0.9, 2.0], dtype=float),
            shift_sec=0.0,
            tolerance_sec=1.1,
        )

        self.assertEqual(
            [(lif_index, ms_index) for lif_index, ms_index, _residual in matches],
            [(0, 0), (1, 1)],
        )

    def test_hsc1_multichannel_groups_are_monotone_and_remain_ambiguous(self):
        lif_rows = []
        for channel in ["G1", "G2", "R1"]:
            lif_rows.extend(
                [
                    lif_peak(
                        channel,
                        f"{channel.lower()}_early",
                        HSC1_LIF_MIN[0] * 60.0,
                    ),
                    lif_peak(
                        channel,
                        f"{channel.lower()}_late",
                        HSC1_LIF_MIN[1] * 60.0,
                    ),
                ]
            )
        ms = pd.DataFrame(
            [
                ms_event("ms_early", HSC1_MS_MIN[0] * 60.0),
                ms_event("ms_late", HSC1_MS_MIN[1] * 60.0),
            ]
        )

        groups = multi_anchor_groups_for_range(
            pd.DataFrame(lif_rows),
            ms,
            anchor_channels=["G1", "G2", "R1"],
            channel_time_axes={
                "G1": "green_axis",
                "G2": "green_axis",
                "R1": "red_axis",
            },
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
            context_start_min=2.7,
            context_end_min=2.9,
            minimum_raw_time_min=0.0,
            ms_shift_sec=0.0,
            tolerance_sec=2.5,
        )

        self.assertEqual([group["ms_event_id"] for group in groups], ["ms_early", "ms_late"])
        self.assertEqual(
            groups[0]["lif_anchor_peak_ids"],
            {"G1": "g1_early", "G2": "g2_early", "R1": "r1_early"},
        )
        self.assertEqual(
            groups[1]["lif_anchor_peak_ids"],
            {"G1": "g1_late", "G2": "g2_late", "R1": "r1_late"},
        )
        self.assertTrue(all(group["complete_anchor_set"] for group in groups))
        self.assertTrue(all(group["axis_coherent"] for group in groups))
        self.assertTrue(
            all(group["conflict_count"] > 0 for group in groups),
            "Both monotone pairings have nearby alternative evidence and must remain manual-review conflicts",
        )

    def test_dense_legacy_g2_r1_groups_remain_monotone_but_are_marked_ambiguous(self):
        lif_rows = []
        for channel in ["G2", "R1"]:
            lif_rows.extend(
                [
                    lif_peak(channel, f"{channel.lower()}-early", HSC1_LIF_MIN[0] * 60.0),
                    lif_peak(channel, f"{channel.lower()}-late", HSC1_LIF_MIN[1] * 60.0),
                ]
            )
        ms = pd.DataFrame(
            [
                ms_event("ms-early", HSC1_MS_MIN[0] * 60.0),
                ms_event("ms-late", HSC1_MS_MIN[1] * 60.0),
            ]
        )

        result = build_qc_alignment_groups(
            pd.DataFrame(lif_rows),
            ms,
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            qc_calibration_end_min=3.0,
        )

        self.assertEqual(
            [group["ms_event_id"] for group in result["groups"]],
            ["ms-early", "ms-late"],
        )
        self.assertTrue(
            all(group["conflict_count"] > 0 for group in result["groups"]),
            "A dense legacy component has alternative pair-to-MS relations and must not be batch-auto-accepted",
        )

    def test_dense_legacy_post_qc_groups_are_also_marked_ambiguous(self):
        lif_rows = []
        for channel in ["G2", "R1"]:
            lif_rows.extend(
                [
                    lif_peak(channel, f"post-{channel.lower()}-early", HSC1_LIF_MIN[0] * 60.0),
                    lif_peak(channel, f"post-{channel.lower()}-late", HSC1_LIF_MIN[1] * 60.0),
                ]
            )
        ms = pd.DataFrame(
            [
                ms_event("post-ms-early", HSC1_MS_MIN[0] * 60.0),
                ms_event("post-ms-late", HSC1_MS_MIN[1] * 60.0),
            ]
        )

        groups = qc_triplets_for_range(
            pd.DataFrame(lif_rows),
            ms,
            context_start_min=2.7,
            context_end_min=2.9,
            qc_calibration_end_min=2.0,
            green_shift_sec=0.0,
            red_shift_sec=0.0,
            ms_shift_sec=0.0,
            pair_offset_sec=0.0,
            tolerance_sec=2.5,
        )

        self.assertEqual(
            [group["ms_event_id"] for group in groups],
            ["post-ms-early", "post-ms-late"],
        )
        self.assertTrue(
            all(group["conflict_count"] > 0 for group in groups),
            "The v0.3 post-QC caller must retain monotone matches but surface nearby alternatives for manual review",
        )

    def test_unambiguous_legacy_g2_r1_pairing_remains_unchanged(self):
        lif = pd.DataFrame(
            [
                lif_peak("G2", "g2_early", 60.0),
                lif_peak("R1", "r1_early", 60.0),
                lif_peak("G2", "g2_late", 120.0),
                lif_peak("R1", "r1_late", 120.0),
            ]
        )
        ms = pd.DataFrame(
            [ms_event("ms_early", 65.0), ms_event("ms_late", 125.0)]
        )

        result = build_qc_alignment_groups(
            lif,
            ms,
            green_shift_sec=5.0,
            red_shift_sec=5.0,
            qc_calibration_end_min=3.0,
        )

        self.assertEqual(
            [
                (
                    group["g2_peak_id"],
                    group["r1_peak_id"],
                    group["ms_event_id"],
                )
                for group in result["groups"]
            ],
            [
                ("g2_early", "r1_early", "ms_early"),
                ("g2_late", "r1_late", "ms_late"),
            ],
        )
        self.assertTrue(
            all(group["conflict_count"] == 0 for group in result["groups"])
        )


if __name__ == "__main__":
    unittest.main()
