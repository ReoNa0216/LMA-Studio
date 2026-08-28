"""Focused contracts for accepted-anchor ranges and concise review feedback."""

import unittest

import pandas as pd

from annotation_app.app import (
    APP_VERSION,
    HTML,
    BadRequest,
    accepted_qc_alignment_refit,
    acquisition_layout_hash,
    apply_qc_alignment_model,
    estimate_shift_alignment,
    manual_qc_alignment_preview_model,
    normalize_acquisition_layout,
)


def _layout():
    return normalize_acquisition_layout(
        {
            "lif_channels": [
                {"input_id": "g1", "channel": "G1", "detector": "green", "time_axis": "green_axis"},
                {"input_id": "g2", "channel": "G2", "detector": "green", "time_axis": "green_axis"},
                {"input_id": "r1", "channel": "R1", "detector": "red", "time_axis": "red_axis"},
                {"input_id": "r2", "channel": "R2", "detector": "red", "time_axis": "red_axis"},
            ],
            "qc_anchor_channels": ["G1", "R1"],
        }
    )


def _evidence(shifts, *, weak_red_indexes=()):
    layout = _layout()
    lif_rows = []
    ms_rows = []
    annotations = []
    for index, (green_shift, red_shift) in enumerate(shifts, start=1):
        ms_time = 120.0 + 60.0 * index
        lif_rows.extend(
            [
                {
                    "peak_id": f"g1_{index}",
                    "channel": "G1",
                    "time_sec": ms_time - green_shift,
                    "time_min": (ms_time - green_shift) / 60.0,
                    "peak_tier": "core",
                },
                {
                    "peak_id": f"r1_{index}",
                    "channel": "R1",
                    "time_sec": ms_time - red_shift,
                    "time_min": (ms_time - red_shift) / 60.0,
                    "peak_tier": "weak" if index in weak_red_indexes else "core",
                },
            ]
        )
        ms_rows.append(
            {
                "event_id": f"ms_{index}",
                "time_sec": ms_time,
                "time_min": ms_time / 60.0,
                "event_strategy": "pc34_primary",
                "primary_signal_col": "pc34_760_max_intensity",
            }
        )
        annotations.append(
            {
                "annotation_id": f"qc_{index}",
                "review_status": "accepted",
                "review_stage": "qc_calibration",
                "candidate_type": "manual_qc_anchor_set",
                "label": "QC",
                "lif_anchor_peak_ids": {"G1": f"g1_{index}", "R1": f"r1_{index}"},
                "ms_event_id": f"ms_{index}",
                "acquisition_layout_hash": acquisition_layout_hash(layout),
            }
        )
    return layout, pd.DataFrame(lif_rows), pd.DataFrame(ms_rows), annotations


class V0413AcceptedEvidenceContractTest(unittest.TestCase):
    def test_candidate_version(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.4.13")

    def test_accepted_refit_uses_valid_shifts_beyond_automatic_search_range(self):
        layout, lif, ms, annotations = _evidence(
            [(49.0, 67.0), (50.0, 68.0), (51.0, 69.0), (50.0, 68.0)]
        )

        preview = accepted_qc_alignment_refit(
            lif,
            ms,
            annotations,
            acquisition_layout=layout,
            qc_calibration_end_min=10.0,
            current_axis_shifts_sec={"green_axis": 0.0, "red_axis": 0.0},
        )

        self.assertAlmostEqual(preview["axis_shifts_sec"]["green_axis"], 50.0)
        self.assertAlmostEqual(preview["axis_shifts_sec"]["red_axis"], 68.0)
        self.assertFalse(
            any(row.get("reason") == "observed_shift_outside_supported_range" for row in preview["conflicts"])
        )

        automatic = estimate_shift_alignment(lif, ms, 10.0, acquisition_layout=layout)
        applied = apply_qc_alignment_model(
            automatic,
            lif,
            ms,
            qc_calibration_end_min=10.0,
            acquisition_layout=layout,
            model=preview,
        )
        self.assertAlmostEqual(applied["channels"]["G1"]["shift_sec"], 50.0)
        self.assertAlmostEqual(applied["channels"]["G2"]["shift_sec"], 50.0)
        self.assertAlmostEqual(applied["channels"]["R1"]["shift_sec"], 68.0)
        self.assertAlmostEqual(applied["channels"]["R2"]["shift_sec"], 68.0)

    def test_manual_preview_allows_large_total_shift_but_still_moves_shared_axes(self):
        layout = _layout()
        current = {
            "axis_shifts_sec": {"green_axis": 50.0, "red_axis": 68.0},
            "green_to_ms_shift_sec": 50.0,
            "red_to_ms_shift_sec": 68.0,
            "qc_alignment_model": {"all_axis_observations": []},
        }

        preview = manual_qc_alignment_preview_model(
            acquisition_layout=layout,
            calibration_protocol=None,
            qc_calibration_end_min=10.0,
            current_alignment=current,
            requested_axis_shifts_sec={"green_axis": 50.5, "red_axis": 71.75},
        )

        self.assertEqual(preview["axis_shifts_sec"], {"green_axis": 50.5, "red_axis": 71.75})

    def test_refit_blocks_when_only_a_minority_of_accepted_axis_relations_are_usable(self):
        layout, lif, ms, annotations = _evidence(
            [(50.0, 68.0)] * 6,
            weak_red_indexes=(3, 4, 5, 6),
        )

        with self.assertRaisesRegex(BadRequest, "红色轴.*2/6.*一致"):
            accepted_qc_alignment_refit(
                lif,
                ms,
                annotations,
                acquisition_layout=layout,
                qc_calibration_end_min=10.0,
            )

    def test_outliers_are_returned_as_collapsed_review_items_not_silent_drops(self):
        layout, lif, ms, annotations = _evidence(
            [(50.0, 68.0), (50.5, 68.5), (49.5, 67.5), (50.0, 75.0)]
        )

        preview = accepted_qc_alignment_refit(
            lif,
            ms,
            annotations,
            acquisition_layout=layout,
            qc_calibration_end_min=10.0,
        )

        self.assertEqual(preview["axes"]["red_axis"]["inlier_count"], 3)
        self.assertEqual(preview["axes"]["red_axis"]["review_count"], 1)
        self.assertEqual(preview["review_count"], 1)
        self.assertEqual(preview["review_items"][0]["reason"], "robust_outlier")
        self.assertAlmostEqual(preview["review_items"][0]["observed_shift_sec"], 75.0)

    def test_ui_uses_dynamic_axis_ranges_and_collapsed_review_details(self):
        panel = HTML.split('id="qcRefitPanel"', 1)[1].split('id="localDeltaPanel"', 1)[0]
        render = HTML.split("function renderQcRefitPanel", 1)[1].split(
            "function persistedAxisShifts", 1
        )[0]
        fine_tune = HTML.split("function renderAxisFineTunePanel", 1)[1].split(
            "async function toggleAxisFineTune", 1
        )[0]

        self.assertIn('id="qcRefitReview"', panel)
        self.assertIn("qcRefitReview", render)
        self.assertIn("需检查", render)
        self.assertIn("采用", render)
        self.assertIn("绿色轴", HTML)
        self.assertNotIn("parts.join(' · ')", render)
        self.assertNotIn("·", HTML)
        self.assertIn("axisFineTuneBounds", fine_tune)
        self.assertNotIn('min="-60" max="60"', fine_tune)


if __name__ == "__main__":
    unittest.main()
