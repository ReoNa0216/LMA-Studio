from __future__ import annotations

import unittest

import pandas as pd

from annotation_app.app import (
    HTML,
    annotate_calibration_ms_event_complexes,
    automatic_calibration_ms_evidence,
    build_segmented_calibration_groups,
    is_manual_cell_ms_event,
    primary_pc34_events,
)


def event(
    event_id: str,
    time_sec: float,
    apex: float,
    prominence: float,
    *,
    calling_height: float = 10.0,
    calling_prominence: float = 2.0,
) -> dict:
    return {
        "event_id": event_id,
        "time_sec": float(time_sec),
        "time_min": float(time_sec) / 60.0,
        "event_strategy": "pc34_primary",
        "primary_signal_col": "pc34_760_max_intensity",
        "pc34_760_apex": float(apex),
        "qc_782_apex": 0.0,
        "peak_prominence": float(prominence),
        "calling_height": float(calling_height),
        "calling_prominence": float(calling_prominence),
        "nearest_event_gap_sec": 0.3,
        "collision_risk_high": True,
        "low_quality_scan_window": False,
    }


def scan(values: list[float], *, step_sec: float = 0.1) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "scan_start_time_sec": [index * step_sec for index in range(len(values))],
            "scan_start_time_min": [index * step_sec / 60.0 for index in range(len(values))],
            "pc34_760_max_intensity": values,
            "qc_782_max_intensity": [0.0] * len(values),
        }
    )


def lif_peak(channel: str, peak_id: str, time_sec: float) -> dict:
    return {
        "peak_id": peak_id,
        "channel": channel,
        "time_sec": float(time_sec),
        "time_min": float(time_sec) / 60.0,
        "snr": 50.0,
        "peak_tier": "core",
    }


def red_green_protocol(*, start_min: float = 0.0, end_min: float = 10.0) -> dict:
    return {
        "protocol_version": 1,
        "boundaries_confirmed": True,
        "reference_channels": ["G1", "R1"],
        "calibration_time_axes": ["green_axis", "red_axis"],
        "segments": [
            {
                "segment_id": "qc_reference",
                "order": 1,
                "population_label": "QC",
                "start_min": float(start_min),
                "end_min": float(end_min),
                "reference_channels": ["G1", "R1"],
                "reference_mode": "red_green",
                "time_axes": ["green_axis", "red_axis"],
                "boundaries_confirmed": True,
            }
        ],
    }


class CalibrationMsEventComplexTest(unittest.TestCase):
    def test_tail_oscillations_without_background_recovery_cast_one_vote(self):
        values = [0.0] * 26
        values[10] = 100.0
        values[11] = 42.0
        values[12] = 45.0
        values[13] = 70.0
        values[14] = 32.0
        values[15] = 60.0
        events = pd.DataFrame(
            [
                event("main", 1.0, 100.0, 95.0),
                event("tail_1", 1.3, 70.0, 28.0),
                event("tail_2", 1.5, 60.0, 18.0),
            ]
        )
        before = events.copy(deep=True)

        annotated = annotate_calibration_ms_event_complexes(events, scan(values))
        evidence = automatic_calibration_ms_evidence(annotated)

        pd.testing.assert_frame_equal(events, before)
        self.assertEqual(annotated["calibration_complex_id"].nunique(), 1)
        self.assertEqual(annotated["calibration_complex_size"].tolist(), [3, 3, 3])
        self.assertEqual(evidence["event_id"].tolist(), ["main"])
        self.assertEqual(
            annotated["calibration_evidence_role"].tolist(),
            ["representative", "secondary_local_maximum", "secondary_local_maximum"],
        )

    def test_close_peaks_that_return_to_background_remain_independent(self):
        values = [0.0] * 24
        values[10] = 100.0
        values[11] = 5.0
        values[12] = 4.0
        values[13] = 90.0
        events = pd.DataFrame(
            [
                event("left", 1.0, 100.0, 95.0),
                event("right", 1.3, 90.0, 85.0),
            ]
        )

        annotated = annotate_calibration_ms_event_complexes(events, scan(values))
        evidence = automatic_calibration_ms_evidence(annotated)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 2)
        self.assertEqual(evidence["event_id"].tolist(), ["left", "right"])
        self.assertTrue(annotated["calibration_complex_representative"].all())

    def test_deep_local_saddle_splits_peaks_even_without_global_background_recovery(self):
        """A deep saddle is resolution evidence even when the tail stays elevated.

        The event caller's absolute height band is not a chromatographic
        resolution criterion.  Both maxima have large, independently measured
        excursions above that band and the intervening saddle removes most of
        the smaller peak's net height, so they must cast two calibration votes.
        """

        values = [0.0] * 30
        values[10] = 100.0
        values[11:13] = [20.0, 20.0]  # Deep saddle, still above height=10.
        values[13] = 90.0
        events = pd.DataFrame(
            [
                event("left", 1.0, 100.0, 90.0),
                event("right", 1.3, 90.0, 80.0),
            ]
        )

        annotated = annotate_calibration_ms_event_complexes(events, scan(values))
        evidence = automatic_calibration_ms_evidence(annotated)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 2)
        self.assertEqual(evidence["event_id"].tolist(), ["left", "right"])

    def test_shallow_tail_saddle_remains_one_unresolved_complex(self):
        """A tiny ripple on an elevated tail is not a second independent event."""

        values = [0.0] * 30
        values[10] = 100.0
        values[11:13] = [65.0, 65.0]
        values[13] = 70.0
        events = pd.DataFrame(
            [
                event("main", 1.0, 100.0, 90.0),
                event("tail_ripple", 1.3, 70.0, 5.0),
            ]
        )

        annotated = annotate_calibration_ms_event_complexes(events, scan(values))
        evidence = automatic_calibration_ms_evidence(annotated)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 1)
        self.assertEqual(evidence["event_id"].tolist(), ["main"])

    def test_pairwise_deep_saddle_break_prevents_single_link_chain_merging(self):
        """A shallow A-B shoulder must not transitively swallow resolved C."""

        values = [0.0] * 45
        values[20] = 100.0
        values[21:26] = [75.0] * 5
        values[26] = 80.0
        values[27:31] = [20.0] * 4
        values[31] = 90.0
        events = pd.DataFrame(
            [
                event("a", 1.00, 100.0, 90.0),
                event("b", 1.30, 80.0, 5.0),
                event("c", 1.55, 90.0, 70.0),
            ]
        )

        annotated = annotate_calibration_ms_event_complexes(
            events,
            scan(values, step_sec=0.05),
        )
        evidence = automatic_calibration_ms_evidence(annotated)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 2)
        self.assertEqual(evidence["event_id"].tolist(), ["a", "c"])

    def test_calibration_grade_uses_caller_normalized_strength_without_deleting_core_rows(self):
        """Detection and calibration eligibility are distinct scientific claims."""

        events = pd.DataFrame(
            [
                event(
                    "near_background_core",
                    1.0,
                    13.5,
                    10.0,
                    calling_height=10.0,
                    calling_prominence=2.0,
                ),
                event(
                    "clear_core",
                    2.0,
                    100.0,
                    90.0,
                    calling_height=10.0,
                    calling_prominence=2.0,
                ),
            ]
        )
        before = events.copy(deep=True)

        annotated = annotate_calibration_ms_event_complexes(events, pd.DataFrame())
        automatic = automatic_calibration_ms_evidence(annotated)

        pd.testing.assert_frame_equal(events, before)
        self.assertEqual(len(annotated), 2)
        self.assertEqual(primary_pc34_events(annotated)["event_id"].tolist(), [
            "near_background_core",
            "clear_core",
        ])
        self.assertTrue(is_manual_cell_ms_event(annotated.iloc[0]))
        self.assertEqual(automatic["event_id"].tolist(), ["clear_core"])
        self.assertAlmostEqual(
            float(annotated.iloc[0]["calibration_evidence_strength"]),
            1.75,
            places=9,
        )
        self.assertFalse(bool(annotated.iloc[0]["calibration_auto_eligible"]))
        self.assertTrue(bool(annotated.iloc[1]["calibration_auto_eligible"]))

    def test_ctr_near_background_core_cannot_steal_anchor_from_clear_peak(self):
        """Regression for Ctr 3.9528 versus the time-nearest 3.9700 core call."""

        events = pd.DataFrame(
            [
                event(
                    "ctr_clear_3p9528",
                    237.170,
                    60822.816406,
                    60822.816406,
                    calling_height=1222.133301,
                    calling_prominence=244.426660,
                ),
                event(
                    "ctr_background_3p9614",
                    237.686,
                    1477.120850,
                    382.010132,
                    calling_height=1222.133301,
                    calling_prominence=244.426660,
                ),
                event(
                    "ctr_background_3p9700",
                    238.202,
                    1596.511719,
                    1293.733307,
                    calling_height=1222.133301,
                    calling_prominence=244.426660,
                ),
            ]
        )
        annotated = annotate_calibration_ms_event_complexes(events, pd.DataFrame())
        lif_peaks = pd.DataFrame(
            [
                lif_peak("G1", "g1_ctr", 238.128),
                lif_peak("R1", "r1_ctr", 238.248),
            ]
        )

        result = build_segmented_calibration_groups(
            lif_peaks,
            annotated,
            calibration_protocol=red_green_protocol(),
            channel_time_axes={"G1": "green_axis", "R1": "red_axis"},
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
        )

        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["ms_event_id"], "ctr_clear_3p9528")
        self.assertNotIn(
            "ctr_background_3p9700",
            {group["ms_event_id"] for group in result["groups"]},
        )
        self.assertEqual(
            primary_pc34_events(annotated)["event_id"].tolist(),
            [
                "ctr_clear_3p9528",
                "ctr_background_3p9614",
                "ctr_background_3p9700",
            ],
        )

    def test_two_equal_grade_ms_peaks_are_reported_as_ambiguous(self):
        events = pd.DataFrame(
            [
                event("strong_left", 99.0, 100.0, 90.0),
                event("strong_right", 101.0, 100.0, 90.0),
            ]
        )
        annotated = annotate_calibration_ms_event_complexes(events, pd.DataFrame())
        lif_peaks = pd.DataFrame(
            [
                lif_peak("G1", "g1_equal", 100.0),
                lif_peak("R1", "r1_equal", 100.0),
            ]
        )

        result = build_segmented_calibration_groups(
            lif_peaks,
            annotated,
            calibration_protocol=red_green_protocol(),
            channel_time_axes={"G1": "green_axis", "R1": "red_axis"},
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
        )

        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        selected = str(group["ms_event_id"])
        alternative = "strong_right" if selected == "strong_left" else "strong_left"
        self.assertGreater(int(group["conflict_count"]), 0)
        self.assertIn(alternative, group["alternative_ms_event_ids"])
        self.assertIn("ambiguous", str(group["selection_reason"]))

    def test_equal_grade_ms_alternatives_cannot_split_axes_and_erase_anchor_set(self):
        """Per-channel nearest choices must not hide a joint ambiguity.

        Both trusted MS peaks are within tolerance of both coherent LIF axes.
        G1 happens to be nearer the left event and R1 nearer the right event.
        Matching each channel greedily first creates two incomplete groups and
        currently drops both.  The scientific evidence is not absent: it is
        one complete LIF anchor set with two equally credible MS alternatives,
        which must remain visible as an explicit manual-review ambiguity.
        """

        events = pd.DataFrame(
            [
                event("strong_left", 99.0, 100.0, 90.0),
                event("strong_right", 101.0, 100.0, 90.0),
            ]
        )
        annotated = annotate_calibration_ms_event_complexes(events, pd.DataFrame())
        lif_peaks = pd.DataFrame(
            [
                lif_peak("G1", "g1_left_leaning", 99.4),
                lif_peak("R1", "r1_right_leaning", 100.6),
            ]
        )

        result = build_segmented_calibration_groups(
            lif_peaks,
            annotated,
            calibration_protocol=red_green_protocol(),
            channel_time_axes={"G1": "green_axis", "R1": "red_axis"},
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
        )

        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(
            set(group["lif_anchor_peak_ids"].values()),
            {"g1_left_leaning", "r1_right_leaning"},
        )
        self.assertTrue(bool(group["component_ambiguous"]))
        self.assertEqual(
            {str(group["ms_event_id"]), *map(str, group["alternative_ms_event_ids"])},
            {"strong_left", "strong_right"},
        )
        self.assertGreater(int(group["conflict_count"]), 0)
        self.assertIn("ambiguous", str(group["selection_reason"]))

    def test_joint_axis_rescue_does_not_hide_same_channel_lif_competitors(self):
        """Choosing one high-SNR LIF member must retain the competing anchor."""

        events = pd.DataFrame([event("strong_ms", 100.0, 100.0, 90.0)])
        annotated = annotate_calibration_ms_event_complexes(events, pd.DataFrame())
        rows = [
            lif_peak("G1", "g1_left", 99.8),
            lif_peak("G1", "g1_right", 100.2),
            lif_peak("R1", "r1", 100.0),
        ]
        rows[0]["snr"] = 100.0
        rows[1]["snr"] = 90.0
        lif_peaks = pd.DataFrame(rows)

        result = build_segmented_calibration_groups(
            lif_peaks,
            annotated,
            calibration_protocol=red_green_protocol(),
            channel_time_axes={"G1": "green_axis", "R1": "red_axis"},
            axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
        )

        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["ms_event_id"], "strong_ms")
        self.assertGreater(int(group["conflict_count"]), 0)

    def test_segmented_front_calibration_never_matches_secondary_local_maxima(self):
        values = [0.0] * 1210
        values[1000] = 100.0
        values[1001] = 45.0
        values[1002] = 65.0
        values[1003] = 42.0
        values[1004] = 55.0
        values[1100] = 90.0
        events = pd.DataFrame(
            [
                event("main", 100.0, 100.0, 95.0),
                event("tail_1", 100.2, 65.0, 20.0),
                event("tail_2", 100.4, 55.0, 12.0),
                {**event("independent", 110.0, 90.0, 85.0), "collision_risk_high": False},
            ]
        )
        annotated = annotate_calibration_ms_event_complexes(events, scan(values))
        lif_peaks = pd.DataFrame(
            [
                {
                    "peak_id": "g1_main",
                    "channel": "G1",
                    "time_sec": 100.0,
                    "time_min": 100.0 / 60.0,
                    "snr": 50.0,
                    "peak_tier": "core",
                },
                {
                    "peak_id": "g1_independent",
                    "channel": "G1",
                    "time_sec": 110.0,
                    "time_min": 110.0 / 60.0,
                    "snr": 50.0,
                    "peak_tier": "core",
                },
            ]
        )
        protocol = {
            "protocol_version": 1,
            "boundaries_confirmed": True,
            "reference_channels": ["G1"],
            "calibration_time_axes": ["green_axis"],
            "segments": [
                {
                    "segment_id": "reference",
                    "order": 1,
                    "population_label": "QC",
                    "start_min": 1.5,
                    "end_min": 2.0,
                    "reference_channels": ["G1"],
                    "reference_mode": "green_only",
                    "time_axes": ["green_axis"],
                    "boundaries_confirmed": True,
                }
            ],
        }

        result = build_segmented_calibration_groups(
            lif_peaks,
            annotated,
            calibration_protocol=protocol,
            channel_time_axes={"G1": "green_axis"},
            axis_shifts_sec={"green_axis": 0.0},
        )

        self.assertEqual(
            [row["ms_event_id"] for row in result["groups"]],
            ["main", "independent"],
        )
        self.assertNotIn("tail_1", {row["ms_event_id"] for row in result["groups"]})
        self.assertNotIn("tail_2", {row["ms_event_id"] for row in result["groups"]})

    def test_ms782_trace_does_not_claim_pc34_events_as_qc_peaks(self):
        self.assertIn("function msEventMarkerPolicy", HTML)
        start = HTML.index("function msEventMarkerPolicy")
        end = HTML.index("function ", start + len("function msEventMarkerPolicy"))
        body = HTML[start:end]
        self.assertRegex(
            body,
            r"trace\s*===\s*['\"]qc_782_linear['\"][\s\S]*visible:\s*false",
        )
        self.assertIn("calibration_complex_representative", body)
        self.assertIn("secondary_local_maximum", body)


if __name__ == "__main__":
    unittest.main()
