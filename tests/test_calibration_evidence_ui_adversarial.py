from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.app import (
    HTML,
    AppData,
    AnnotationStore,
    annotate_calibration_ms_event_complexes,
    automatic_calibration_ms_evidence,
    build_segmented_calibration_groups,
    qc_triplets_for_range,
)


def _event(
    event_id: str,
    time_sec: float,
    *,
    apex: float,
    prominence: float,
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
        "nearest_event_gap_sec": 1.0,
        "collision_risk_high": False,
        "low_quality_scan_window": False,
    }


def _scan(points: dict[float, float], *, end_sec: float = 125.0) -> pd.DataFrame:
    count = int(round(end_sec * 10.0)) + 1
    times = [index / 10.0 for index in range(count)]
    signal = [0.0] * count
    for time_sec, value in points.items():
        signal[int(round(float(time_sec) * 10.0))] = float(value)
    return pd.DataFrame(
        {
            "scan_start_time_sec": times,
            "scan_start_time_min": [value / 60.0 for value in times],
            "pc34_760_max_intensity": signal,
            "qc_782_max_intensity": [0.0] * count,
        }
    )


def _protocol(start_min: float = 1.5, end_min: float = 2.0) -> dict:
    return {
        "protocol_version": 1,
        "boundaries_confirmed": True,
        "reference_channels": ["G1"],
        "calibration_time_axes": ["green_axis"],
        "segments": [
            {
                "segment_id": "reference",
                "order": 1,
                "population_label": "QC",
                "start_min": float(start_min),
                "end_min": float(end_min),
                "reference_channels": ["G1"],
                "reference_mode": "green_only",
                "time_axes": ["green_axis"],
                "boundaries_confirmed": True,
            }
        ],
    }


def _lif_peak(time_sec: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "peak_id": "g1_anchor",
                "channel": "G1",
                "time_sec": float(time_sec),
                "time_min": float(time_sec) / 60.0,
                "snr": 50.0,
                "peak_tier": "core",
            }
        ]
    )


def _javascript_function(name: str, next_name: str) -> str:
    start = HTML.index(f"function {name}")
    end = HTML.index(f"function {next_name}", start + len(name))
    return HTML[start:end]


class CalibrationEvidencePhysicsTest(unittest.TestCase):
    def test_deep_local_saddle_resolves_two_peaks_even_above_global_background(self):
        """Resolution is a local peak/valley property, not a global-baseline test."""

        events = pd.DataFrame(
            [
                _event("left", 1.0, apex=100.0, prominence=80.0),
                _event("right", 1.2, apex=90.0, prominence=70.0),
            ]
        )
        # The valley (20) remains above the caller height (10), but it is only
        # 20--22% of either apex: these are two locally resolved peaks.
        trace = _scan({1.0: 100.0, 1.1: 20.0, 1.2: 90.0}, end_sec=3.0)

        annotated = annotate_calibration_ms_event_complexes(events, trace)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 2)
        self.assertTrue(annotated["calibration_complex_representative"].all())
        self.assertEqual(
            annotated["calibration_evidence_role"].tolist(),
            ["representative", "representative"],
        )

    def test_shallow_tail_oscillation_remains_one_unresolved_complex(self):
        events = pd.DataFrame(
            [
                _event("main", 1.0, apex=100.0, prominence=30.0),
                _event("tail", 1.2, apex=90.0, prominence=20.0),
            ]
        )
        trace = _scan({1.0: 100.0, 1.1: 78.0, 1.2: 90.0}, end_sec=3.0)

        annotated = annotate_calibration_ms_event_complexes(events, trace)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 1)
        self.assertEqual(int(annotated["calibration_complex_size"].max()), 2)
        self.assertEqual(
            int(annotated["calibration_complex_representative"].sum()), 1
        )

    def test_deep_saddle_breaks_a_chain_instead_of_transitively_merging_it(self):
        events = pd.DataFrame(
            [
                _event("main", 1.0, apex=100.0, prominence=25.0),
                _event("shoulder", 1.2, apex=90.0, prominence=18.0),
                _event("resolved", 1.4, apex=85.0, prominence=70.0),
            ]
        )
        trace = _scan(
            {1.0: 100.0, 1.1: 80.0, 1.2: 90.0, 1.3: 18.0, 1.4: 85.0},
            end_sec=3.0,
        )

        annotated = annotate_calibration_ms_event_complexes(events, trace)

        self.assertEqual(annotated["calibration_complex_id"].nunique(), 2)
        self.assertEqual(
            annotated["calibration_complex_size"].astype(int).tolist(), [2, 2, 1]
        )
        self.assertEqual(
            annotated["calibration_evidence_role"].tolist(),
            ["representative", "secondary_local_maximum", "representative"],
        )

    def test_near_background_event_is_review_only_and_cannot_steal_calibration(self):
        events = pd.DataFrame(
            [
                # The clear peak is one second from the aligned LIF apex.
                _event("clear", 99.0, apex=100.0, prominence=88.0),
                # The marginal extremum is exactly time-aligned, but barely
                # clears the caller's own height/prominence floors.
                _event("marginal", 100.0, apex=12.0, prominence=2.2),
            ]
        )
        trace = _scan(
            {
                98.9: 8.0,
                99.0: 100.0,
                99.1: 7.0,
                99.9: 10.0,
                100.0: 12.0,
                100.1: 10.0,
            }
        )

        annotated = annotate_calibration_ms_event_complexes(events, trace)
        evidence = automatic_calibration_ms_evidence(annotated)
        by_id = annotated.set_index("event_id")

        for column in (
            "calibration_auto_eligible",
            "calibration_evidence_strength",
            "calibration_review_reason",
        ):
            self.assertIn(column, annotated.columns)
        self.assertTrue(bool(by_id.loc["clear", "calibration_auto_eligible"]))
        self.assertFalse(bool(by_id.loc["marginal", "calibration_auto_eligible"]))
        self.assertGreater(
            float(by_id.loc["clear", "calibration_evidence_strength"]),
            float(by_id.loc["marginal", "calibration_evidence_strength"]),
        )
        self.assertTrue(str(by_id.loc["marginal", "calibration_review_reason"]))
        self.assertEqual(evidence["event_id"].tolist(), ["clear"])

        groups = build_segmented_calibration_groups(
            _lif_peak(100.0),
            annotated,
            calibration_protocol=_protocol(),
            channel_time_axes={"G1": "green_axis"},
            axis_shifts_sec={"green_axis": 0.0},
        )["groups"]
        self.assertEqual([row["ms_event_id"] for row in groups], ["clear"])

    def test_calibration_grade_is_scale_invariant_not_an_absolute_intensity_rule(self):
        def classify(scale: float) -> pd.Series:
            event = pd.DataFrame(
                [
                    _event(
                        "same_shape",
                        1.0,
                        apex=100.0 * scale,
                        prominence=80.0 * scale,
                        calling_height=10.0 * scale,
                        calling_prominence=2.0 * scale,
                    )
                ]
            )
            trace = _scan(
                {
                    0.9: 8.0 * scale,
                    1.0: 100.0 * scale,
                    1.1: 7.0 * scale,
                },
                end_sec=3.0,
            )
            return annotate_calibration_ms_event_complexes(event, trace).iloc[0]

        ordinary = classify(1.0)
        tiny_units = classify(0.001)

        self.assertTrue(bool(ordinary["calibration_auto_eligible"]))
        self.assertTrue(bool(tiny_units["calibration_auto_eligible"]))
        self.assertAlmostEqual(
            float(ordinary["calibration_evidence_strength"]),
            float(tiny_units["calibration_evidence_strength"]),
            places=9,
        )

    def test_two_trustworthy_nearby_events_remain_explicitly_ambiguous(self):
        events = pd.DataFrame(
            [
                _event("left", 99.0, apex=100.0, prominence=85.0),
                _event("right", 101.0, apex=98.0, prominence=83.0),
            ]
        )
        trace = _scan({98.9: 5.0, 99.0: 100.0, 99.1: 5.0, 100.9: 5.0, 101.0: 98.0, 101.1: 5.0})
        annotated = annotate_calibration_ms_event_complexes(events, trace)

        groups = build_segmented_calibration_groups(
            _lif_peak(100.0),
            annotated,
            calibration_protocol=_protocol(),
            channel_time_axes={"G1": "green_axis"},
            axis_shifts_sec={"green_axis": 0.0},
        )["groups"]

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertTrue(bool(group.get("component_ambiguous")))
        alternatives = {
            str(value) for value in group.get("alternative_ms_event_ids", [])
        }
        self.assertTrue({"left", "right"}.issubset(alternatives | {str(group["ms_event_id"])}))


class CalibrationEvidenceUiContractTest(unittest.TestCase):
    def _marker_policies(self) -> dict:
        if shutil.which("node") is None:
            self.skipTest("Node.js is needed for the embedded-JS behavior contract")
        function = _javascript_function("msEventMarkerPolicy", "draw")
        script = f"""
const SIGNAL_COLORS = {{ ms760: '#2878b5', ms782: '#2a9d76' }};
const state = {{ stage: 'qc_calibration' }};
{function}
const candidates = new Set(['candidate']);
const base = {{
  event_id: 'eligible',
  calibration_evidence_role: 'representative',
  calibration_auto_eligible: true,
  calibration_evidence_strength: 12,
  calibration_complex_representative: true,
  calibration_complex_size: 1,
  collision_risk_high: false,
  low_quality_scan_window: false
}};
const output = {{}};
output.candidate = msEventMarkerPolicy({{...base, event_id: 'candidate'}}, 'pc34_760_linear', candidates);
output.eligible = msEventMarkerPolicy(base, 'pc34_760_linear', candidates);
output.eligibleCollision = msEventMarkerPolicy({{...base, collision_risk_high: true}}, 'pc34_760_linear', candidates);
output.secondary = msEventMarkerPolicy({{...base, event_id: 'secondary', calibration_evidence_role: 'secondary_local_maximum', calibration_auto_eligible: false}}, 'pc34_760_linear', candidates);
output.review = msEventMarkerPolicy({{...base, event_id: 'review', calibration_auto_eligible: false, calibration_review_reason: 'near_background'}}, 'pc34_760_linear', candidates);
output.qc782 = msEventMarkerPolicy(base, 'qc_782_linear', candidates);
state.stage = 'event_annotation';
output.eventsStage = msEventMarkerPolicy({{...base, event_id: 'review', calibration_auto_eligible: false, calibration_review_reason: 'near_background'}}, 'pc34_760_linear', candidates);
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)

    def test_calibration_marker_colors_encode_use_role_not_collision_risk(self):
        policies = self._marker_policies()

        self.assertEqual(policies["candidate"].get("role"), "candidate")
        self.assertEqual(policies["eligible"].get("role"), "eligible_unmatched")
        self.assertEqual(policies["secondary"].get("role"), "secondary")
        self.assertEqual(policies["review"].get("role"), "review_only")
        self.assertNotEqual(
            policies["candidate"].get("stroke"), policies["eligible"].get("stroke")
        )
        # Collision remains audit metadata; it must not recolor an otherwise
        # identical eligible calibration event.
        for visual_key in (
            "role",
            "visible",
            "labelEligible",
            "radius",
            "fill",
            "stroke",
            "strokeWidth",
            "opacity",
        ):
            self.assertEqual(
                policies["eligible"].get(visual_key),
                policies["eligibleCollision"].get(visual_key),
                visual_key,
            )

    def test_secondary_and_review_only_markers_are_visible_but_not_label_noise(self):
        policies = self._marker_policies()

        for role in ("secondary", "review"):
            policy = policies[role]
            self.assertTrue(policy.get("visible"), role)
            self.assertFalse(policy.get("labelEligible"), role)
            self.assertGreaterEqual(float(policy.get("radius", 0)), 3.4, role)
            self.assertGreaterEqual(float(policy.get("opacity", 0)), 0.65, role)
        self.assertFalse(policies["qc782"].get("visible"))

    def test_events_stage_does_not_inherit_front_calibration_suppression(self):
        policies = self._marker_policies()

        self.assertTrue(policies["eventsStage"].get("visible"))
        self.assertNotIn(
            policies["eventsStage"].get("role"), {"review_only", "secondary"}
        )

    def test_manual_ms_selection_remains_available_for_visible_review_points(self):
        draw = _javascript_function("draw", "trackShiftSec")
        self.assertIn("const markerPolicy = msEventMarkerPolicy", draw)
        self.assertIn("c.addEventListener('click', () => selectManualPeak('MS760', e))", draw)
        self.assertNotRegex(
            draw,
            r"(?:review_only|secondary).*pointer-events[^\n]*(?:none|false)",
        )

    def test_ambiguous_calibration_evidence_is_not_drawn_as_one_decisive_line(self):
        body = _javascript_function("drawAlignmentGroups", "drawPostQcCandidates")
        self.assertRegex(
            body,
            r"component_ambiguous|alternative_ms_event_ids",
            "The Calibration renderer must branch on trustworthy alternatives.",
        )
        self.assertRegex(
            body,
            r"ambig|歧义|multiple|alternative",
            "Ambiguity must have an explicit visual path, not an ordinary connector.",
        )

    def test_ambiguous_candidate_card_explains_the_ms_alternatives(self):
        body = _javascript_function("renderCandidateList", "confirmQcEvidenceInvalidation")
        self.assertIn("component_ambiguous", body)
        self.assertIn("alternative_ms_event_times_min", body)
        self.assertRegex(
            body,
            r"多个[^`'\"\n]*MS|MS[^`'\"\n]*歧义|ambiguous[^`'\"\n]*MS",
            "With the connector suppressed, the candidate card must say why and show the alternative MS evidence.",
        )

        backend = inspect.getsource(AppData._review_auto_candidate_from_request_snapshot)
        self.assertIn("component_ambiguous", backend)
        self.assertRegex(backend, r"raise\s+BadRequest")


class CalibrationEvidenceCompatibilityGateTest(unittest.TestCase):
    def test_runtime_classification_does_not_mutate_event_rows_or_annotations(self):
        events = pd.DataFrame([_event("event", 1.0, apex=100.0, prominence=80.0)])
        before = events.copy(deep=True)
        with tempfile.TemporaryDirectory() as tmp:
            store = AnnotationStore(Path(tmp) / "annotation.sqlite")
            store.upsert_review(
                annotation_id="manual-existing",
                source="manual_created",
                review_status="accepted",
                payload={
                    "annotation_id": "manual-existing",
                    "candidate_type": "manual_cell_pair",
                    "review_stage": "cell_annotation",
                    "lif_peak_id": "g1-existing",
                    "ms_event_id": "event",
                    "time_model_version": "frozen-v040",
                },
                action="test_seed",
            )
            stored_before = store.records()

            annotate_calibration_ms_event_complexes(
                events, _scan({1.0: 100.0}, end_sec=3.0)
            )

            pd.testing.assert_frame_equal(events, before)
            self.assertEqual(AnnotationStore(store.db_path).records(), stored_before)

    def test_post_run_qc_matcher_does_not_apply_front_calibration_grade(self):
        source = inspect.getsource(qc_triplets_for_range)
        self.assertNotIn("automatic_calibration_ms_evidence", source)
        self.assertNotIn("calibration_auto_eligible", source)
        self.assertNotIn("calibration_evidence_strength", source)


if __name__ == "__main__":
    unittest.main()
