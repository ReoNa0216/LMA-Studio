import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from flame_ms_core.detector import detect_events
from flame_ms_core.export import export_machine_contract
from flame_ms_core.parser import parse_ms_scan_summary
from flame_ms_core.scientific_settings import ProjectScientificSettings
from flame_ms_core.timebase import AnalysisRange
from annotation_app.app import AppData, ProjectPaths, primary_pc34_events, is_manual_cell_ms_event
from annotation_app.ms_core import preview_package_update, lma_event_table
from test_canonical_project_storage import _new_project_request, _write_synthetic_preprocessor_outputs, _tree_snapshot


def make_package(root, raw):
    lines = ["spectrumList (2401 spectra)"]
    for i in range(2401):
        intensity = {600: 1000., 700: 1500., 800: 1200.}.get(i, 0.)
        lines.extend(["spectrum:", f"index: {i}", f"id: scanId={i+1}", "defaultArrayLength: 2",
                      "cvParam: base peak m/z, 760.5851", f"cvParam: base peak intensity, {intensity}",
                      "cvParam: total ion current, 2000000", f"cvParam: scan start time, {24+i/600:.12f}, minute",
                      "cvParam: m/z array", "binary: [2] 760.5851 782.5616",
                      "cvParam: intensity array", f"binary: [2] {intensity} 1"])
    raw.write_text("\n".join(lines), encoding="ascii")
    parsed = parse_ms_scan_summary(raw)
    result = detect_events(parsed.scans, parsed.fingerprint.sha256, AnalysisRange.from_minutes(24, 28))
    reviews = []
    for i, row in enumerate(result.events.to_dict("records")):
        reviews.append({"event_id": f"EV_import_{i}", "auto_event_id": row["auto_event_id"],
                        "original_auto_event_id": row["auto_event_id"], "generation_id": result.generation_id,
                        "original_left_sec": row["left_sec"], "original_right_sec": row["right_sec"],
                        "current_scan_id": row["scan_id"], "current_scan_row_index": row["scan_row_index"],
                        "current_spectrum_index": row["spectrum_index"], "current_apex_time_ns": row["scan_time_ns"],
                        "current_apex_time_sec": row["apex_time_sec"], "current_apex_intensity": row["apex_intensity"],
                        "status": ["accepted", "rejected", "pending"][i], "origin": "automatic", "revision": 1, "snap_offset_sec": 0.})
    export_machine_contract(reviews, result.events, root, source_fingerprint={"sha256": parsed.fingerprint.sha256},
                            detector_version=result.parameters["detector_version"], parameter_hash=result.parameter_hash,
                            generation_id=result.generation_id, analysis_start_ns=int(parsed.scans.scan_time_ns.iloc[0]),
                            analysis_end_ns=int(parsed.scans.scan_time_ns.iloc[-1]),
                            scientific_settings=ProjectScientificSettings().as_dict())
    return parsed, result


class MachineImportTest(unittest.TestCase):
    def prepare(self, root):
        request = _new_project_request(root)
        request.pop("cell_event_map_path")
        request["ms_event_package_path"] = root / "package"
        make_package(request["ms_event_package_path"], request["ms_path"])
        return request

    @staticmethod
    def lif_only(script, project_dir):
        if script != "run_v3_01_lif_trace_physical_qc.py":
            raise AssertionError("machine import must not call MS detector")
        return _write_synthetic_preprocessor_outputs(script, project_dir)

    def test_import_preserves_all_statuses_and_exports_only_accepted_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            with patch("annotation_app.app.run_preprocessing_script", side_effect=self.lif_only), patch("annotation_app.app.reconcile_event_roster_supported_ms_events", side_effect=AssertionError("no roster recaller")):
                app = AppData.create_project_from_raw_inputs(**request)
            self.assertEqual(app.ms_events.event_id.tolist(), ["EV_import_0", "EV_import_1", "EV_import_2"])
            self.assertEqual(primary_pc34_events(app.ms_events).event_id.tolist(), ["EV_import_0"])
            self.assertFalse(is_manual_cell_ms_event(app.ms_events.iloc[1]))
            self.assertFalse(is_manual_cell_ms_event(app.ms_events.iloc[2]))
            self.assertEqual(app.cell_event_map.ms_event_id.tolist(), ["EV_import_0"])
            before = _tree_snapshot(app.project.project_dir)
            reopened = AppData.load(ProjectPaths.from_args(project_dir=str(app.project.project_dir)))
            self.assertEqual(_tree_snapshot(app.project.project_dir), before)
            preview = preview_package_update(app.project.project_dir, app.manifest, request["ms_event_package_path"])
            self.assertFalse(preview["requires_new_project"])
            self.assertEqual(_tree_snapshot(app.project.project_dir), before)
            exported = reopened.export_accepted_annotations_csv()["csv_text"]
            self.assertIn("EV_import_0", exported)
            self.assertNotIn("EV_import_1", exported)

    def test_wrong_raw_and_failed_publication_leave_no_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            with request["ms_path"].open("a") as handle:
                handle.write("\nchanged header\n")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                AppData.create_project_from_raw_inputs(**request)
            self.assertFalse(request["project_dir"].exists())
            self.assertFalse(list(root.glob(".*.lma-building-*")))

    def test_manual_addition_preserves_identity_without_inventing_automatic_support(self):
        from flame_ms_core.exchange import read_event_package
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            package = read_event_package(request["ms_event_package_path"])
            scans = parse_ms_scan_summary(request["ms_path"]).scans
            added = dict(package.events.iloc[0])
            added.update(event_id="EV_manual_added", auto_event_id=None,
                         original_auto_event_id=None, original_left_sec=None,
                         original_right_sec=None, origin="manual_added", revision=1)
            raw = scans.iloc[900]
            for target, source in {"current_scan_id": "scan_id", "current_scan_row_index": "scan_row_index",
                                   "current_spectrum_index": "spectrum_index", "current_apex_time_ns": "scan_time_ns",
                                   "current_apex_time_sec": "scan_start_time_sec",
                                   "current_apex_intensity": "primary_marker_max_intensity"}.items():
                added[target] = raw[source]
            updated = root / "manual-package"
            manifest = package.manifest
            export_machine_contract([*package.events.to_dict("records"), added], package.events, updated,
                                    source_fingerprint=manifest["source_fingerprint"],
                                    detector_version=manifest["detector"]["version"],
                                    parameter_hash=manifest["detector"]["parameter_hash"],
                                    generation_id=manifest["detector"]["generation_id"],
                                    analysis_start_ns=manifest["analysis_range"]["start_ns"],
                                    analysis_end_ns=manifest["analysis_range"]["end_ns"],
                                    scientific_settings=manifest["scientific_settings"])
            request["ms_event_package_path"] = updated
            with patch("annotation_app.app.run_preprocessing_script", side_effect=self.lif_only):
                app = AppData.create_project_from_raw_inputs(**request)
            event = app.ms_events.set_index("event_id").loc["EV_manual_added"]
            self.assertEqual(str(event.scan_id), str(raw.scan_id))
            self.assertEqual(event.left_sec, event.time_sec)
            self.assertEqual(event.right_sec, event.time_sec)
            self.assertTrue(pd.isna(event.auto_event_id))
            self.assertTrue(pd.isna(event.peak_width_sec))
            self.assertIn("EV_manual_added", app.cell_event_map.ms_event_id.tolist())
            self.assertIn("EV_manual_added", app.export_accepted_annotations_csv()["csv_text"])

    def test_original_physical_scan_is_checked_before_current_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            from flame_ms_core.exchange import read_event_package
            package = read_event_package(request["ms_event_package_path"])
            parsed = parse_ms_scan_summary(request["ms_path"])
            events = package.events.copy()
            events.loc[0, "scan_row_index"] = 99998
            with self.assertRaises(ValueError):
                lma_event_table(events, parsed.scans, imported=True)

    def test_missing_saved_database_fails_without_creating_an_empty_database(self):
        with tempfile.TemporaryDirectory() as temp:
            request = self.prepare(Path(temp))
            with patch("annotation_app.app.run_preprocessing_script", side_effect=self.lif_only):
                app = AppData.create_project_from_raw_inputs(**request)
            app.project.annotation_db_path.unlink()
            before = _tree_snapshot(app.project.project_dir)
            with self.assertRaisesRegex(Exception, "标注数据库缺失"):
                AppData.load(app.project)
            self.assertEqual(_tree_snapshot(app.project.project_dir), before)


if __name__ == "__main__":
    unittest.main()
