import unittest

import pandas as pd

from scripts.v3.run_v3_01_lif_trace_physical_qc import red_detector_audit


class RedDetectorAuditTest(unittest.TestCase):
    def test_single_red_channel_is_explicitly_not_applicable(self):
        peaks = pd.DataFrame(
            [
                {"peak_id": "r1_1", "channel": "R1", "detector": "red", "phase": "qc_start", "time_sec": 60.0},
                {"peak_id": "g1_1", "channel": "G1", "detector": "green", "phase": "qc_start", "time_sec": 60.0},
            ]
        )

        audit, offsets = red_detector_audit(peaks)

        self.assertTrue(offsets.empty)
        self.assertEqual(set(audit["audit_status"]), {"not_applicable_fewer_than_two_red_channels"})
        self.assertEqual(audit.loc[audit["phase"].eq("qc_start"), "left_peak_count"].iloc[0], 1)

    def test_two_red_channels_are_audited_without_fixed_channel_names(self):
        peaks = pd.DataFrame(
            [
                {"peak_id": "r3_1", "channel": "R3", "detector": "red", "phase": "qc_start", "time_sec": 60.0},
                {"peak_id": "r4_1", "channel": "R4", "detector": "red", "phase": "qc_start", "time_sec": 60.25},
            ]
        )

        audit, offsets = red_detector_audit(peaks)

        self.assertEqual(offsets["right_minus_left_sec"].tolist(), [0.25])
        first = audit[audit["phase"].eq("qc_start")].iloc[0]
        self.assertEqual((first["left_channel"], first["right_channel"]), ("R3", "R4"))
        self.assertEqual(first["audit_status"], "pair_audited")

    def test_r1_r2_compatibility_fields_do_not_depend_on_input_order(self):
        peaks = pd.DataFrame(
            [
                {"peak_id": "r2_1", "channel": "R2", "detector": "red", "phase": "qc_start", "time_sec": 60.25},
                {"peak_id": "r1_1", "channel": "R1", "detector": "red", "phase": "qc_start", "time_sec": 60.0},
            ]
        )

        audit, offsets = red_detector_audit(peaks)

        self.assertEqual(offsets["right_minus_left_sec"].tolist(), [-0.25])
        self.assertEqual(offsets["r2_minus_r1_sec"].tolist(), [0.25])
        first = audit[audit["phase"].eq("qc_start")].iloc[0]
        self.assertEqual(first["r1_peak_count"], 1)
        self.assertEqual(first["r2_peak_count"], 1)
        self.assertAlmostEqual(first["offset_mode_sec"], first["r2_minus_r1_offset_mode_sec"])
        self.assertAlmostEqual(first["offset_mode_sec"], -first["right_minus_left_offset_mode_sec"])


if __name__ == "__main__":
    unittest.main()
