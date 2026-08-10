import unittest

import pandas as pd

from scripts.v3.run_v3_02_ms_event_calling import portable_diagnostic_frame
from scripts.v3.run_v3_01_lif_trace_physical_qc import has_red_pair_diagnostics


class CanonicalDiagnosticContractTest(unittest.TestCase):
    def test_red_pair_diagnostics_exist_only_when_two_red_channels_can_be_compared(self):
        cases = [
            ([{"channel": "G1", "detector": "green"}], False),
            ([{"channel": "R1", "detector": "red"}], False),
            (
                [
                    {"channel": "G1", "detector": "green"},
                    {"channel": "R1", "detector": "red"},
                    {"channel": "R2", "detector": "red"},
                ],
                True,
            ),
        ]
        for rows, expected in cases:
            with self.subTest(rows=rows):
                self.assertEqual(
                    has_red_pair_diagnostics(pd.DataFrame(rows)),
                    expected,
                )

    def test_ms_diagnostics_use_background_terms_without_mutating_science_frames(self):
        original = pd.DataFrame(
            [
                {
                    "quiet_score": 0.1,
                    "selected_as_quiet_platform": True,
                    "metric": "quiet_start_min",
                    "reason": "quiet_threshold_exceeded_signal_range",
                    "role": "main V3-02 MS event caller",
                }
            ]
        )
        before = original.copy(deep=True)

        portable = portable_diagnostic_frame(original)

        pd.testing.assert_frame_equal(original, before)
        self.assertIn("background_score", portable.columns)
        self.assertIn("selected_for_background_estimation", portable.columns)
        rendered = "\n".join(
            [
                *map(str, portable.columns),
                *portable.astype(str).to_numpy().ravel().tolist(),
            ]
        ).lower()
        for retired_term in ("quiet_", "quiet platform", "v3-", "v2"):
            self.assertNotIn(retired_term, rendered)
        self.assertIn("background_start_min", rendered)
        self.assertIn("background_threshold_exceeded_signal_range", rendered)
        self.assertIn("primary ms event caller", rendered)


if __name__ == "__main__":
    unittest.main()
