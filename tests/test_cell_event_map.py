import csv
import io
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from annotation_app.cell_event_map import (
    CELL_EVENT_MAP_CANONICAL_COLUMNS,
    CellEventMapError,
    canonical_csv_bytes,
    cell_event_map_manifest_entry,
    import_cell_event_map,
    match_source_to_events,
    project_annotation_state,
    read_canonical_map,
    read_source_coordinates,
    state_revision,
    write_canonical_map,
)
from annotation_app.app import load_project_cell_event_map


def ms_events(rows):
    return pd.DataFrame(
        [
            {
                "event_id": event_id,
                "scan_id": scan_id,
                "time_min": time_min,
                "event_strategy": strategy,
                "primary_signal_col": signal,
            }
            for event_id, scan_id, time_min, strategy, signal in rows
        ]
    )


class CellEventMapImportTest(unittest.TestCase):
    def write_source(self, root: Path, rows, header=None):
        path = root / "source.csv"
        header = header or ["scan_start_time", "UMAP1", "UMAP2", "Type", "leiden", "CellNumber"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def test_source_loader_whitelists_only_three_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_source(
                Path(tmp),
                [[1.0, 2.0, 3.0, "AUTHOR_LABEL", "cluster-x", "cell-1"]],
            )

            frame = read_source_coordinates(path)

        self.assertEqual(frame.columns.tolist(), ["scan_start_time", "UMAP1", "UMAP2"])
        self.assertNotIn("AUTHOR_LABEL", frame.to_csv(index=False))

    def test_source_loader_accepts_excel_style_cell_id_column_before_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_source(
                Path(tmp),
                [["Cell00001", 24.074766666667, 6.312042, -5.778983]],
                ["", "scan_start_time", "UMAP1", "UMAP2"],
            )

            frame = read_source_coordinates(path)

        self.assertEqual(frame.columns.tolist(), ["scan_start_time", "UMAP1", "UMAP2"])
        self.assertEqual(frame.to_dict("records"), [
            {
                "scan_start_time": 24.074766666667,
                "UMAP1": 6.312042,
                "UMAP2": -5.778983,
            }
        ])

    def test_source_loader_accepts_event_times_without_umap_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_source(
                Path(tmp),
                [[24.1, "cell-1"], [24.2, "cell-2"]],
                ["scan_start_time", "CellNumber"],
            )

            frame = read_source_coordinates(path)

        self.assertEqual(frame.columns.tolist(), ["scan_start_time", "UMAP1", "UMAP2"])
        self.assertEqual(frame["scan_start_time"].tolist(), [24.1, 24.2])
        self.assertTrue(frame[["UMAP1", "UMAP2"]].isna().all().all())
        self.assertFalse(frame.attrs["coordinates_available"])
        self.assertEqual(frame.attrs["source_coordinate_columns"], {})

    def test_source_loader_accepts_explicit_umap_alias_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_source(
                Path(tmp),
                [[24.1, -3.0, 4.0, "cluster-1"]],
                ["scan_start_time", "UMAP", "UMAP2", "leiden"],
            )

            frame = read_source_coordinates(path)

        self.assertEqual(frame.columns.tolist(), ["scan_start_time", "UMAP1", "UMAP2"])
        self.assertEqual(frame[["UMAP1", "UMAP2"]].iloc[0].tolist(), [-3.0, 4.0])
        self.assertTrue(frame.attrs["coordinates_available"])
        self.assertEqual(
            frame.attrs["source_coordinate_columns"],
            {"UMAP1": "UMAP", "UMAP2": "UMAP2"},
        )

    def test_source_loader_rejects_partial_coordinate_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_source(
                Path(tmp),
                [[24.1, -3.0]],
                ["scan_start_time", "UMAP1"],
            )

            with self.assertRaisesRegex(CellEventMapError, "UMAP1.*UMAP2.*成对"):
                read_source_coordinates(path)

    def test_header_requires_each_allowed_column_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = self.write_source(root, [[1, 2]], ["scan_start_time", "UMAP1"])
            with self.assertRaisesRegex(CellEventMapError, "UMAP2=0"):
                read_source_coordinates(missing)
            duplicate = self.write_source(
                root,
                [[1, 1, 2, 3]],
                ["scan_start_time", "scan_start_time", "UMAP1", "UMAP2"],
            )
            with self.assertRaisesRegex(CellEventMapError, "scan_start_time=2"):
                read_source_coordinates(duplicate)

    def test_invalid_numeric_and_duplicate_time_report_source_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = self.write_source(root, [[1.0, 2.0, 3.0], [2.0, "bad", 4.0]])
            with self.assertRaisesRegex(CellEventMapError, "CSV 行: 3"):
                read_source_coordinates(invalid)
            duplicate = self.write_source(root, [[1.0, 2.0, 3.0], [1.0, 4.0, 5.0]])
            with self.assertRaisesRegex(CellEventMapError, "CSV 行: 2, 3"):
                read_source_coordinates(duplicate)

    def test_matching_filters_primary_events_and_builds_canonical_rows(self):
        source = pd.DataFrame(
            {
                "scan_start_time": [1.0, 2.0],
                "UMAP1": [4.0, 5.0],
                "UMAP2": [6.0, 7.0],
            }
        )
        events = ms_events(
            [
                ("noise", "n", 1.0, "qc_support", "qc_782_max_intensity"),
                ("ms_2", "scan-2", 2.0 + 1e-9, "pc34_primary", "pc34_760_max_intensity"),
                ("ms_1", "scan-1", 1.0, "pc34_primary", "pc34_760_max_intensity"),
            ]
        )

        canonical = match_source_to_events(source, events)

        self.assertEqual(canonical.columns.tolist(), list(CELL_EVENT_MAP_CANONICAL_COLUMNS))
        self.assertEqual(canonical["ms_event_id"].tolist(), ["ms_1", "ms_2"])
        self.assertEqual(canonical["scan_id"].tolist(), ["scan-1", "scan-2"])

    def test_default_tolerance_accepts_one_scan_apex_offset_without_ambiguity(self):
        source = pd.DataFrame(
            {
                "scan_start_time": [1.0],
                "UMAP1": [4.0],
                "UMAP2": [6.0],
            }
        )
        events = ms_events(
            [
                (
                    "ms_shifted_apex",
                    "scan-apex",
                    1.0 + 0.103 / 60.0,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                )
            ]
        )

        canonical = match_source_to_events(source, events)

        self.assertEqual(canonical["ms_event_id"].tolist(), ["ms_shifted_apex"])

    def test_unique_peak_support_accepts_clp_two_scan_apex_offset(self):
        source_time = 80.269766666667
        apex_time = 80.273216666667
        source = pd.DataFrame(
            {
                "scan_start_time": [source_time],
                "UMAP1": [4.0],
                "UMAP2": [6.0],
            }
        )
        events = ms_events(
            [
                (
                    "ms_clp_shoulder",
                    "4816401",
                    apex_time,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                )
            ]
        )
        events["left_sec"] = [source_time * 60.0 - 0.02]
        events["right_sec"] = [apex_time * 60.0 + 0.10]

        canonical = match_source_to_events(source, events)

        self.assertGreater((apex_time - source_time) * 60.0, 0.15)
        self.assertEqual(canonical["ms_event_id"].tolist(), ["ms_clp_shoulder"])
        self.assertEqual(canonical.attrs["match_diagnostics"]["peak_support_match_count"], 1)
        self.assertEqual(canonical.attrs["match_diagnostics"]["apex_tolerance_match_count"], 0)

    def test_apex_tolerance_match_takes_priority_over_broad_neighbor_support(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                ("ms_exact", "scan-exact", 1.0, "pc34_primary", "pc34_760_max_intensity"),
                ("ms_broad", "scan-broad", 1.02, "pc34_primary", "pc34_760_max_intensity"),
            ]
        )
        events["left_sec"] = [59.98, 59.5]
        events["right_sec"] = [60.02, 60.5]

        canonical = match_source_to_events(source, events)

        self.assertEqual(canonical["ms_event_id"].tolist(), ["ms_exact"])
        self.assertEqual(canonical.attrs["match_diagnostics"]["apex_tolerance_match_count"], 1)
        self.assertEqual(canonical.attrs["match_diagnostics"]["peak_support_match_count"], 0)

    def test_overlapping_peak_support_remains_ambiguous(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                ("ms_left", "scan-left", 1.003, "pc34_primary", "pc34_760_max_intensity"),
                ("ms_right", "scan-right", 1.004, "pc34_primary", "pc34_760_max_intensity"),
            ]
        )
        events["left_sec"] = [59.7, 59.9]
        events["right_sec"] = [60.1, 60.3]

        with self.assertRaisesRegex(CellEventMapError, "多个.*MS event"):
            match_source_to_events(source, events)

    def test_peak_basin_fallback_rejects_a_distant_time_inside_a_broad_basin(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                (
                    "ms_distant_basin",
                    "scan-distant",
                    1.0 + 1.0 / 60.0,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                )
            ]
        )
        events["left_sec"] = [60.8]
        events["right_sec"] = [61.2]
        events["left_base_sec"] = [0.0]
        events["right_base_sec"] = [120.0]

        with self.assertRaises(CellEventMapError):
            match_source_to_events(source, events)

    def test_peak_basin_fallback_accepts_the_observed_two_scan_offset(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                (
                    "ms_near_basin",
                    "scan-near",
                    1.0 + 0.207 / 60.0,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                )
            ]
        )
        events["left_sec"] = [60.19]
        events["right_sec"] = [60.30]
        events["left_base_sec"] = [59.9]
        events["right_base_sec"] = [60.5]

        canonical = match_source_to_events(source, events)

        self.assertEqual(canonical["ms_event_id"].tolist(), ["ms_near_basin"])
        self.assertEqual(
            canonical.attrs["match_diagnostics"]["peak_basin_match_count"],
            1,
        )

    def test_overlapping_near_peak_basins_remain_ambiguous(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                ("ms_left_basin", "scan-left", 1.003, "pc34_primary", "pc34_760_max_intensity"),
                ("ms_right_basin", "scan-right", 1.004, "pc34_primary", "pc34_760_max_intensity"),
            ]
        )
        events["left_sec"] = [60.17, 60.23]
        events["right_sec"] = [60.22, 60.29]
        events["left_base_sec"] = [59.9, 59.9]
        events["right_base_sec"] = [60.4, 60.4]

        with self.assertRaises(CellEventMapError):
            match_source_to_events(source, events)

    def test_legacy_event_table_without_peak_support_keeps_strict_tolerance(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                (
                    "ms_two_scans_away",
                    "scan-apex",
                    1.0 + 0.207 / 60.0,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                )
            ]
        )

        with self.assertRaisesRegex(CellEventMapError, "CSV"):
            match_source_to_events(source, events)

    def test_legacy_schema_one_can_replay_its_uncapped_halfheight_binding(self):
        source = pd.DataFrame(
            {"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]}
        )
        events = ms_events(
            [
                (
                    "ms_legacy_wide",
                    "scan-legacy",
                    1.0 + 0.5 / 60.0,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                )
            ]
        )
        events["left_sec"] = [59.8]
        events["right_sec"] = [60.7]

        with self.assertRaises(CellEventMapError):
            match_source_to_events(source, events)
        replayed = match_source_to_events(
            source,
            events,
            include_peak_basin=False,
            max_peak_shape_apex_offset_sec=None,
        )

        self.assertEqual(replayed["ms_event_id"].tolist(), ["ms_legacy_wide"])
        self.assertEqual(
            replayed.attrs["match_diagnostics"]["peak_support_match_count"],
            1,
        )

    def test_unmatched_ambiguous_and_reused_events_fail_the_whole_import(self):
        base = ms_events(
            [
                ("ms_1", "scan-1", 1.0, "pc34_primary", "pc34_760_max_intensity"),
                ("ms_2", "scan-2", 2.0, "pc34_primary", "pc34_760_max_intensity"),
            ]
        )
        unmatched = pd.DataFrame({"scan_start_time": [3.0], "UMAP1": [0.0], "UMAP2": [0.0]})
        with self.assertRaisesRegex(CellEventMapError, "未匹配 CSV 行 2"):
            match_source_to_events(unmatched, base)

        ambiguous_events = pd.concat(
            [
                base,
                ms_events(
                    [
                        (
                            "ms_1b",
                            "scan-1b",
                            1.0 + 0.005 / 60.0,
                            "pc34_primary",
                            "pc34_760_max_intensity",
                        )
                    ]
                ),
            ],
            ignore_index=True,
        )
        ambiguous = pd.DataFrame({"scan_start_time": [1.0], "UMAP1": [0.0], "UMAP2": [0.0]})
        with self.assertRaisesRegex(CellEventMapError, "多个 MS event"):
            match_source_to_events(ambiguous, ambiguous_events)

        reused = pd.DataFrame(
            {
                "scan_start_time": [1.0 - 0.004 / 60.0, 1.0 + 0.004 / 60.0],
                "UMAP1": [0.0, 1.0],
                "UMAP2": [0.0, 1.0],
            }
        )
        with self.assertRaisesRegex(CellEventMapError, "复用同一 MS event"):
            match_source_to_events(reused, base)

    def test_failed_binding_provides_a_complete_row_diagnostic_csv(self):
        source = pd.DataFrame(
            {
                "scan_start_time": [1.0, 2.0, 4.0],
                "UMAP1": [10.0, 20.0, 30.0],
                "UMAP2": [-10.0, -20.0, -30.0],
            }
        )
        events = ms_events(
            [
                ("ms_exact", "scan-exact", 1.0, "pc34_primary", "pc34_760_max_intensity"),
                ("ms_ambiguous_a", "scan-a", 2.0, "pc34_primary", "pc34_760_max_intensity"),
                (
                    "ms_ambiguous_b",
                    "scan-b",
                    2.0 + 0.01 / 60.0,
                    "pc34_primary",
                    "pc34_760_max_intensity",
                ),
            ]
        )

        with self.assertRaises(CellEventMapError) as raised:
            match_source_to_events(source, events)

        payload = raised.exception.diagnostic_payload()
        self.assertEqual(payload["filename"], "event-map-import-diagnostics.csv")
        self.assertEqual(
            payload["summary"],
            {
                "total_rows": 3,
                "matched_rows": 1,
                "unmatched_rows": 1,
                "ambiguous_rows": 1,
                "conflict_rows": 0,
            },
        )
        report = pd.read_csv(io.StringIO(payload["csv_text"]))
        self.assertEqual(
            report.columns.tolist(),
            [
                "CSVLine",
                "scan_start_time",
                "Status",
                "ReasonCode",
                "Reason",
                "MatchMethod",
                "MatchedEventID",
                "MatchedEventTimeMin",
                "ApexOffsetSec",
                "CandidateEventIDs",
                "NearestEventID",
                "NearestEventTimeMin",
                "NearestOffsetSec",
            ],
        )
        self.assertEqual(report["CSVLine"].tolist(), [2, 3, 4])
        self.assertEqual(
            report["ReasonCode"].tolist(),
            ["matched_apex", "ambiguous_multiple_events", "outside_recognized_event_range"],
        )
        self.assertNotIn("UMAP1", payload["csv_text"])
        self.assertNotIn("UMAP2", payload["csv_text"])
        self.assertNotIn("CellNumber", payload["csv_text"])
        self.assertNotIn("Type", payload["csv_text"])

    def test_reused_event_marks_every_conflicting_row_in_the_diagnostic(self):
        source = pd.DataFrame(
            {
                "scan_start_time": [1.0 - 0.004 / 60.0, 1.0 + 0.004 / 60.0],
                "UMAP1": [0.0, 1.0],
                "UMAP2": [0.0, 1.0],
            }
        )
        events = ms_events(
            [("ms_1", "scan-1", 1.0, "pc34_primary", "pc34_760_max_intensity")]
        )

        with self.assertRaises(CellEventMapError) as raised:
            match_source_to_events(source, events)

        payload = raised.exception.diagnostic_payload()
        report = pd.read_csv(io.StringIO(payload["csv_text"]))
        self.assertEqual(report["Status"].tolist(), ["conflict", "conflict"])
        self.assertEqual(
            report["ReasonCode"].tolist(),
            ["reused_event", "reused_event"],
        )
        self.assertEqual(payload["summary"]["conflict_rows"], 2)

    def test_canonical_write_is_stable_and_strict(self):
        frame = pd.DataFrame(
            [
                {
                    "ms_event_id": "ms_1",
                    "scan_id": "scan-1",
                    "scan_start_time": 1.0,
                    "UMAP1": 2.0,
                    "UMAP2": 3.0,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "map.csv"
            binding = write_canonical_map(frame, path)
            loaded = read_canonical_map(path, expected_sha256=binding["sha256"])

        self.assertEqual(loaded["ms_event_id"].tolist(), ["ms_1"])
        self.assertEqual(binding["size_bytes"], len(canonical_csv_bytes(frame)))

    def test_schema_two_canonical_map_round_trips_without_coordinates(self):
        frame = pd.DataFrame(
            [
                {
                    "ms_event_id": "ms_1",
                    "scan_id": "scan-1",
                    "scan_start_time": 1.0,
                    "UMAP1": float("nan"),
                    "UMAP2": float("nan"),
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "map.csv"
            binding = write_canonical_map(frame, path)
            with self.assertRaisesRegex(CellEventMapError, "UMAP1"):
                read_canonical_map(path, expected_sha256=binding["sha256"])
            loaded = read_canonical_map(
                path,
                expected_sha256=binding["sha256"],
                allow_missing_coordinates=True,
            )

        self.assertTrue(loaded[["UMAP1", "UMAP2"]].isna().all().all())

    def test_full_import_records_coordinate_capability_and_column_mapping(self):
        events = ms_events(
            [("ms_1", "scan-1", 1.0, "pc34_primary", "pc34_760_max_intensity")]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            time_only = self.write_source(
                root,
                [[1.0, "cell-1"]],
                ["scan_start_time", "CellNumber"],
            )
            canonical, metadata = import_cell_event_map(time_only, events)

        self.assertFalse(metadata["coordinates_available"])
        self.assertEqual(metadata["required_source_columns"], ["scan_start_time"])
        self.assertEqual(metadata["source_coordinate_columns"], {})
        self.assertTrue(canonical[["UMAP1", "UMAP2"]].isna().all().all())

    def test_schema_two_time_only_manifest_rebinds_without_mutation(self):
        events = ms_events(
            [("ms_1", "scan-1", 1.0, "pc34_primary", "pc34_760_max_intensity")]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.write_source(
                root,
                [[1.0, "cell-1"]],
                ["scan_start_time", "CellNumber"],
            )
            canonical, metadata = import_cell_event_map(source, events)
            destination = root / "data" / "cell_event_map.csv"
            write_canonical_map(canonical, destination)
            entry = cell_event_map_manifest_entry(
                canonical_path=destination,
                project_dir=root,
                import_metadata=metadata,
            )
            before = destination.read_bytes()

            loaded, loaded_entry = load_project_cell_event_map(
                root,
                {"cell_event_map": entry},
                events,
            )

            self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(entry["schema_version"], 2)
        self.assertFalse(loaded_entry["coordinates_available"])
        self.assertTrue(loaded[["UMAP1", "UMAP2"]].isna().all().all())

    def test_full_import_records_source_identity_without_extra_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_source(
                Path(tmp),
                [[1.0, 2.0, 3.0, "must-not-copy", 9, 101]],
            )
            canonical, metadata = import_cell_event_map(
                path,
                ms_events(
                    [
                        (
                            "ms_1",
                            "scan-1",
                            1.0,
                            "pc34_primary",
                            "pc34_760_max_intensity",
                        )
                    ]
                ),
            )

        self.assertEqual(metadata["row_count"], 1)
        self.assertEqual(metadata["matched_event_count"], 1)
        self.assertEqual(metadata["apex_tolerance_match_count"], 1)
        self.assertEqual(metadata["peak_support_match_count"], 0)
        self.assertAlmostEqual(metadata["max_apex_offset_sec"], 0.0)
        self.assertEqual(
            metadata["match_policy"],
            "apex_tolerance_then_unique_near_peak_shape_v3",
        )
        self.assertEqual(len(metadata["source_sha256"]), 64)
        self.assertNotIn("must-not-copy", canonical_csv_bytes(canonical).decode("utf-8"))


class CellEventMapStateTest(unittest.TestCase):
    def setUp(self):
        self.event_map = pd.DataFrame(
            [
                {"ms_event_id": "a", "scan_id": "1", "scan_start_time": 41.0, "UMAP1": 0.0, "UMAP2": 0.0},
                {"ms_event_id": "b", "scan_id": "2", "scan_start_time": 42.0, "UMAP1": 1.0, "UMAP2": 1.0},
                {"ms_event_id": "c", "scan_id": "3", "scan_start_time": 43.0, "UMAP1": 2.0, "UMAP2": 2.0},
                {"ms_event_id": "d", "scan_id": "4", "scan_start_time": 44.0, "UMAP1": 3.0, "UMAP2": 3.0},
            ]
        )

    def test_state_uses_only_current_accepted_sqlite_semantics(self):
        annotations = [
            {
                "annotation_id": "qc-a",
                "review_status": "accepted",
                "review_stage": "qc_survey",
                "candidate_type": "manual_qc_anchor_partial",
                "time_model_version": "tm-current",
                "ms_event_id": "a",
                "label": "QC",
            },
            {
                "annotation_id": "cell-b",
                "review_status": "accepted",
                "review_stage": "cell_annotation",
                "candidate_type": "manual_cell_pair",
                "time_model_version": "tm-current",
                "ms_event_id": "b",
                "lif_channel": "R2",
                "label": "Day3 cell",
            },
            {
                "annotation_id": "stale-c",
                "review_status": "accepted",
                "review_stage": "cell_annotation",
                "time_model_version": "tm-old",
                "ms_event_id": "c",
                "lif_channel": "G2",
            },
            {
                "annotation_id": "rejected-d",
                "review_status": "rejected",
                "review_stage": "cell_annotation",
                "time_model_version": "tm-current",
                "ms_event_id": "d",
                "lif_channel": "G1",
            },
        ]

        state = project_annotation_state(
            self.event_map,
            annotations,
            active_time_model_version="tm-current",
            annotation_start_min=40.0,
        )

        self.assertEqual(state["counts"], {"cell": 1, "qc": 1, "unknown": 2, "conflict": 0})
        by_id = {point["ms_event_id"]: point for point in state["points"]}
        self.assertEqual(by_id["a"]["classification"], "qc")
        self.assertEqual(by_id["b"]["lif_channel"], "R2")
        self.assertEqual(by_id["c"]["classification"], "unknown")

    def test_conflict_is_explicit_and_revision_changes_after_revoke(self):
        accepted = [
            {
                "annotation_id": "qc-a",
                "review_status": "accepted",
                "review_stage": "qc_survey",
                "time_model_version": "tm",
                "ms_event_id": "a",
            },
            {
                "annotation_id": "cell-a",
                "review_status": "accepted",
                "review_stage": "cell_annotation",
                "time_model_version": "tm",
                "ms_event_id": "a",
                "lif_channel": "G1",
            },
        ]
        conflict = project_annotation_state(
            self.event_map,
            accepted,
            active_time_model_version="tm",
            annotation_start_min=40.0,
        )
        revision_before = state_revision(
            project_id="project",
            map_sha256="map",
            projected_state=conflict,
        )
        accepted[1]["review_status"] = "pending"
        revoked = project_annotation_state(
            self.event_map,
            accepted,
            active_time_model_version="tm",
            annotation_start_min=40.0,
        )
        revision_after = state_revision(
            project_id="project",
            map_sha256="map",
            projected_state=revoked,
        )

        self.assertEqual(conflict["counts"]["conflict"], 1)
        self.assertEqual(revoked["counts"]["qc"], 1)
        self.assertNotEqual(revision_before, revision_after)


if __name__ == "__main__":
    unittest.main()
