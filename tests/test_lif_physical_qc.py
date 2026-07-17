import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from scripts.v3 import run_v3_01_lif_trace_physical_qc as lif_qc
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


class VariableChannelInputTest(unittest.TestCase):
    def make_lock(self, root: Path, channels: list[str]) -> None:
        rows = []
        for index, channel in enumerate(channels):
            path = root / f"{channel}.csv"
            path.write_bytes(f"trace-{channel}-{index}".encode("ascii"))
            fingerprint = lif_qc.file_fingerprint(path)
            rows.append(
                {
                    "input_id": f"lif_{index + 1}_raw",
                    "path": str(path),
                    "input_class": "raw_lif_trace",
                    "channel": channel,
                    "label": channel,
                    "detector": "green" if channel.startswith("G") else "red",
                    "allowed_stage": "V3-01~V3-06 main workflow",
                    **fingerprint,
                }
            )
        lock = root / "results/tables/v3/00_allowed_inputs.csv"
        lock.parent.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(lock, index=False)

    def test_input_lock_accepts_two_and_four_lif_channels(self):
        original_root = lif_qc.ROOT
        try:
            for channels in (["G1", "R1"], ["G1", "G2", "R1", "R2"]):
                with self.subTest(channels=channels), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.make_lock(root, channels)
                    lif_qc.configure_project_root(root)
                    specs = lif_qc.load_channel_specs()
                    self.assertEqual([spec.channel for spec in specs], channels)
        finally:
            lif_qc.configure_project_root(original_root)

    def test_overview_plot_allocates_one_axis_per_channel(self):
        traces = pd.DataFrame(
            [
                {"channel": channel, "time_min": time, "signal": time + index}
                for index, channel in enumerate(["G1", "G2", "R1", "R2"])
                for time in [0.0, 1.0]
            ]
        )
        peaks = pd.DataFrame(
            [
                {"channel": channel, "time_min": 0.5, "height": 1.0, "peak_stage": "merged"}
                for channel in ["G1", "G2", "R1", "R2"]
            ]
        )
        captured = []

        with mock.patch.object(lif_qc, "save_png", side_effect=lambda fig, _path: captured.append(fig)):
            lif_qc.plot_trace_overview(traces, peaks)

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0].axes), 4)
        lif_qc.plt.close(captured[0])


if __name__ == "__main__":
    unittest.main()
