import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from annotation_app.app import (
    AppData,
    AnnotationStore,
    BadRequest,
    ProjectPaths,
    acquisition_layout_hash,
)
from annotation_app.umap_page import UMAP_HTML


def frozen_store(path: Path, layout: dict) -> AnnotationStore:
    store = AnnotationStore(path)
    store.upsert_time_model(
        {
            "time_model_version": "tm-current",
            "status": "frozen",
            "base_model_name": "test",
            "qc_calibration_end_min": 10.5,
            "sample_valve_switch_min": 36.0,
            "annotation_start_min": 40.0,
            "local_delta_seed_window_min": 2.5,
            "ms_local_delta_sec": 0.0,
            "contains_cell_labels": False,
            "max_training_time_min": 42.5,
            "evidence_count": 2,
            "unique_match_count": 2,
            "conflict_count": 0,
            "median_abs_residual_sec": 0.0,
            "p90_abs_residual_sec": 0.0,
            "acquisition_layout_hash": acquisition_layout_hash(layout),
        },
        action="test_frozen",
    )
    return store


def make_app(root: Path, *, with_map: bool) -> AppData:
    layout = {
        "layout_version": 3,
        "lif_channels": [
            {
                "input_id": "g1",
                "channel": "G1",
                "identity_prior": "LK",
                "time_axis": "green_axis",
                "detector": "green",
                "use_for_cell_annotation": True,
            },
            {
                "input_id": "r1",
                "channel": "R1",
                "identity_prior": "",
                "time_axis": "red_axis",
                "detector": "red",
                "use_for_cell_annotation": False,
            },
        ],
        "qc_anchor_channels": ["G1", "R1"],
    }
    event_map = (
        pd.DataFrame(
            [
                {
                    "ms_event_id": "ms-1",
                    "scan_id": "scan-1",
                    "scan_start_time": 41.0,
                    "UMAP1": 1.25,
                    "UMAP2": -2.5,
                }
            ]
        )
        if with_map
        else None
    )
    manifest = {
        "project_id": "project-test",
        "project_schema_version": 1,
        "acquisition_layout": layout,
        "channel_identity_prior": {"G1": "LK", "R1": ""},
        "intermediate_tables": {},
    }
    (root / "lifms_project.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return AppData(
        project=ProjectPaths.from_args(
            project_dir=str(root),
            annotation_db=str(root / "annotation.sqlite"),
        ),
        lif_traces=pd.DataFrame(),
        lif_peaks=pd.DataFrame(
            [
                {
                    "peak_id": "g1-1",
                    "channel": "G1",
                    "time_min": 41.0,
                    "time_sec": 2460.0,
                },
                {
                    "peak_id": "r1-1",
                    "channel": "R1",
                    "time_min": 41.0,
                    "time_sec": 2460.0,
                },
            ]
        ),
        ms_events=pd.DataFrame(
            [
                {
                    "event_id": "ms-1",
                    "scan_id": "scan-1",
                    "time_min": 41.0,
                    "time_sec": 2460.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                },
                {
                    "event_id": "ms-outside",
                    "scan_id": "scan-outside",
                    "time_min": 42.0,
                    "time_sec": 2520.0,
                    "event_strategy": "pc34_primary",
                    "primary_signal_col": "pc34_760_max_intensity",
                },
            ]
        ),
        ms_scan=pd.DataFrame(
            [
                {
                    "scan_id": "scan-1",
                    "scan_start_time_min": 41.0,
                    "pc34_760_mz_at_max_intensity": 760.5854,
                }
            ]
        ),
        alignment={
            "model": "test",
            "axis_shifts_sec": {"green_axis": 0.0, "red_axis": 0.0},
            "green_to_ms_shift_sec": 0.0,
            "red_to_ms_shift_sec": 0.0,
            "qc_groups": {"groups": []},
            "acquisition_layout_hash": acquisition_layout_hash(layout),
        },
        store=frozen_store(root / "annotation.sqlite", layout),
        channel_identity_prior={
            "G1": {"identity_prior": "LK", "identity_prior_source": "test"},
            "R1": {"identity_prior": "", "identity_prior_source": "test"},
        },
        acquisition_layout=layout,
        manifest=manifest,
        cell_event_map=event_map,
        cell_event_map_info=(
            {"sha256": "map-sha", "row_count": 1}
            if with_map
            else None
        ),
    )


class UmapAppStateTest(unittest.TestCase):
    def test_umap_page_exposes_responsive_axes_and_plain_language_controls(self):
        self.assertIn(">显示全部点</button>", UMAP_HTML)
        self.assertIn("不会修改任何标注", UMAP_HTML)
        self.assertIn("滚轮缩放 · 拖动平移 · 单击定位事件", UMAP_HTML)
        self.assertIn("function drawAxes()", UMAP_HTML)
        self.assertIn("ctx.fillText('UMAP1'", UMAP_HTML)
        self.assertIn("ctx.fillText('UMAP2'", UMAP_HTML)
        self.assertIn("points.length && (sizeChanged || !fitted)", UMAP_HTML)
        self.assertIn("`${points.length.toLocaleString()} 个事件点`", UMAP_HTML)
        self.assertNotIn("slice(0, 12)", UMAP_HTML)
        self.assertIn("MS760 时间：", UMAP_HTML)
        self.assertNotIn("<div class=\"muted\">event:", UMAP_HTML)
        self.assertNotIn("<div class=\"muted\">scan:", UMAP_HTML)

    def test_umap_points_keep_ms760_time_without_search_only_mz_payload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)

            state = app.projected_cell_event_map_state()

            self.assertAlmostEqual(state["points"][0]["scan_start_time"], 41.0)
            self.assertNotIn("mz", state["points"][0])

    def test_umap_page_can_find_ms760_time_with_tolerance_and_draw_red_outlines(self):
        self.assertIn('id="timeQuery"', UMAP_HTML)
        self.assertIn('id="timeTolerance"', UMAP_HTML)
        self.assertIn('value="0.001"', UMAP_HTML)
        self.assertIn('MS760 time (min)', UMAP_HTML)
        self.assertIn('function findTimePoints()', UMAP_HTML)
        self.assertIn('function pointMs760Time(point)', UMAP_HTML)
        self.assertIn(
            "Math.abs(pointMs760Time(point) - target) <= tolerance",
            UMAP_HTML,
        )
        self.assertIn("String(timeQuery.value).trim()", UMAP_HTML)
        self.assertIn("ctx.strokeStyle = '#d92d20'", UMAP_HTML)
        self.assertIn("matchedTimeEventIds.has(String(point.ms_event_id))", UMAP_HTML)
        self.assertIn('id="clearTime"', UMAP_HTML)
        self.assertNotIn('id="mzQuery"', UMAP_HTML)
        self.assertNotIn('PC(34:1) m/z', UMAP_HTML)

    def test_umap_legend_omits_qc_when_no_current_event_is_qc(self):
        body = re.search(
            r"function renderLegend\(data\) \{(?P<body>.*?)\n    \}",
            UMAP_HTML,
            re.S,
        )
        self.assertIsNotNone(body)
        self.assertRegex(
            body.group("body"),
            r"if\s*\(Number\(counts\.qc\s*\|\|\s*0\)\s*>\s*0\)",
        )

    def test_backend_whitelist_and_cross_classification_conflict(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)
            with self.assertRaisesRegex(BadRequest, "白名单"):
                app.require_third_stage_event_in_map("ms-outside")

            qc_payload = {
                "review_stage": "qc_survey",
                "candidate_type": "manual_qc_anchor_partial",
                "ms_event_id": "ms-1",
                "ms_time_min": 41.0,
                "time_model_version": "tm-current",
                "label": "QC",
            }
            app.store.upsert_review(
                annotation_id="qc-1",
                source="manual_created",
                review_status="accepted",
                payload=qc_payload,
                action="test_accept",
            )
            before = app.projected_cell_event_map_state()
            self.assertEqual(before["counts"], {"cell": 0, "qc": 1, "unknown": 0, "conflict": 0})

            with self.assertRaisesRegex(BadRequest, "只能有一个"):
                app.ensure_third_stage_acceptance_allowed(
                    {
                        "review_stage": "cell_annotation",
                        "candidate_type": "manual_cell_pair",
                        "ms_event_id": "ms-1",
                        "time_model_version": "tm-current",
                    },
                    annotation_id="cell-1",
                )

            app.store.upsert_review(
                annotation_id="qc-1",
                source="manual_created",
                review_status="rejected",
                payload=qc_payload,
                action="test_revoke",
            )
            after = app.projected_cell_event_map_state()
            self.assertEqual(after["counts"]["unknown"], 1)
            self.assertNotEqual(before["revision"], after["revision"])

    def test_disabled_post_qc_does_not_project_historical_qc_on_umap(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)
            app.store.update_project_config(
                {"post_qc_strategy": {"mode": "disabled"}}
            )
            app.store.upsert_review(
                annotation_id="historical-qc",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "qc_survey",
                    "candidate_type": "manual_qc_anchor_partial",
                    "ms_event_id": "ms-1",
                    "ms_time_min": 41.0,
                    "time_model_version": "tm-current",
                    "label": "QC",
                },
                action="test_historical_qc_after_disable",
            )

            state = app.projected_cell_event_map_state()

            self.assertEqual(
                state["counts"],
                {"cell": 0, "qc": 0, "unknown": 1, "conflict": 0},
            )
            self.assertEqual(state["points"][0]["classification"], "unknown")

    def test_main_csv_keeps_unannotated_event_map_rows_as_unknown(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)

            exported = app.export_accepted_annotations_csv()
            frame = pd.read_csv(io.StringIO(exported["csv_text"]))

            self.assertEqual(exported["row_count"], 1)
            self.assertEqual(frame["Type"].tolist(), ["unknown"])
            self.assertEqual(frame["CellNumber"].tolist(), ["Cell00001"])
            self.assertEqual(frame["MS_event_id"].tolist(), ["ms-1"])
            self.assertTrue(frame["LIF_channel"].isna().all())
            self.assertTrue(frame["annotation_id"].isna().all())

    def test_third_stage_acceptance_requires_current_frozen_time_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)
            payload = {
                "review_stage": "cell_annotation",
                "candidate_type": "manual_cell_pair",
                "ms_event_id": "ms-1",
                "time_model_version": "tm-stale",
            }
            with self.assertRaisesRegex(BadRequest, "当前 frozen time model"):
                app.ensure_third_stage_acceptance_allowed(
                    payload,
                    annotation_id="cell-stale",
                )

            active = app.store.active_time_model()
            app.store.upsert_time_model(
                {**active, "status": "draft"},
                action="test_unfreeze",
            )
            with self.assertRaisesRegex(BadRequest, "冻结 delta"):
                app.ensure_third_stage_acceptance_allowed(
                    {**payload, "time_model_version": "tm-current"},
                    annotation_id="cell-without-frozen-model",
                )

    def test_attach_and_replacement_preserve_annotations_and_time_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            app = make_app(root, with_map=False)
            payload = {
                "review_stage": "cell_annotation",
                "candidate_type": "manual_cell_pair",
                "ms_event_id": "ms-1",
                "ms_time_min": 41.0,
                "time_model_version": "tm-current",
                "lif_channel": "G1",
            }
            app.store.upsert_review(
                annotation_id="cell-1",
                source="manual_created",
                review_status="accepted",
                payload=payload,
                action="test_accept",
            )
            source = root / "source.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["scan_start_time", "UMAP1", "UMAP2", "Type", "leiden"])
                writer.writerow([41.0, 3.0, 4.0, "AUTHOR_LABEL", "cluster-7"])

            records_before = app.store.records()
            model_before = app.store.active_time_model()
            attached = app.attach_cell_event_map(source)

            self.assertEqual(attached.store.records(), records_before)
            self.assertEqual(attached.store.active_time_model(), model_before)
            self.assertIsNotNone(attached.cell_event_map)
            self.assertEqual(len(attached.cell_event_map), 1)
            canonical_text = (
                root / "data/interim/lma/cell_event_umap.csv"
            ).read_text(encoding="utf-8")
            self.assertNotIn("Type", canonical_text)
            self.assertNotIn("AUTHOR_LABEL", canonical_text)
            manifest = json.loads((root / "lifms_project.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["cell_event_map"]["row_count"], 1)

            replacement_source = root / "replacement.csv"
            with replacement_source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["scan_start_time", "UMAP1", "UMAP2", "batch"])
                writer.writerow([41.0, -8.5, 9.25, "after-correction"])

            replaced = attached.attach_cell_event_map(replacement_source)

            self.assertEqual(replaced.store.records(), records_before)
            self.assertEqual(replaced.store.active_time_model(), model_before)
            self.assertEqual(float(replaced.cell_event_map.iloc[0]["UMAP1"]), -8.5)
            self.assertEqual(float(replaced.cell_event_map.iloc[0]["UMAP2"]), 9.25)
            replaced_manifest = json.loads(
                (root / "lifms_project.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                replaced_manifest["cell_event_map"]["sha256"],
                manifest["cell_event_map"]["sha256"],
            )
            self.assertEqual(
                replaced_manifest["cell_event_map_history"][-1]["sha256"],
                manifest["cell_event_map"]["sha256"],
            )
            self.assertEqual(
                replaced_manifest["cell_event_map"]["source_name"],
                replacement_source.name,
            )
            self.assertNotIn(
                "path",
                replaced_manifest["cell_event_map_history"][-1],
            )
            self.assertNotIn(str(replacement_source), json.dumps(replaced_manifest))

    def test_invalid_replacement_leaves_current_map_and_manifest_untouched(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            app = make_app(root, with_map=False)
            app.store.upsert_review(
                annotation_id="accepted-cell",
                source="manual_created",
                review_status="accepted",
                payload={
                    "review_stage": "cell_annotation",
                    "candidate_type": "manual_cell_pair",
                    "ms_event_id": "ms-1",
                    "ms_time_min": 41.0,
                    "time_model_version": "tm-current",
                    "lif_channel": "G1",
                },
                action="test_accept_before_map_switch",
            )
            destination = root / "data/interim/lma/cell_event_umap.csv"
            manifest_before = (root / "lifms_project.json").read_bytes()

            invalid_source = root / "invalid.csv"
            invalid_source.write_text(
                "scan_start_time,UMAP1,UMAP2\n42.0,3.0,4.0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BadRequest, "缺少已有 accepted"):
                app.attach_cell_event_map(invalid_source)

            self.assertFalse(destination.exists())
            self.assertEqual((root / "lifms_project.json").read_bytes(), manifest_before)

    def test_manifest_write_failure_rolls_back_replacement_bytes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            app = make_app(root, with_map=False)
            first_source = root / "first.csv"
            first_source.write_text(
                "scan_start_time,UMAP1,UMAP2\n41.0,1.0,2.0\n",
                encoding="utf-8",
            )
            attached = app.attach_cell_event_map(first_source)
            destination = root / "data/interim/lma/cell_event_umap.csv"
            map_before = destination.read_bytes()
            manifest_before = (root / "lifms_project.json").read_bytes()
            replacement_source = root / "replacement.csv"
            replacement_source.write_text(
                "scan_start_time,UMAP1,UMAP2\n41.0,8.0,9.0\n",
                encoding="utf-8",
            )

            with mock.patch(
                "annotation_app.app.write_existing_project_manifest",
                side_effect=OSError("simulated manifest failure"),
            ):
                with self.assertRaisesRegex(BadRequest, "失败"):
                    attached.attach_cell_event_map(replacement_source)

            self.assertEqual(destination.read_bytes(), map_before)
            self.assertEqual((root / "lifms_project.json").read_bytes(), manifest_before)

    def test_replacement_requires_the_same_ms_event_population(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            app = make_app(root, with_map=False)
            first_source = root / "first.csv"
            first_source.write_text(
                "scan_start_time,UMAP1,UMAP2\n"
                "41.0,1.0,2.0\n"
                "42.0,3.0,4.0\n",
                encoding="utf-8",
            )
            attached = app.attach_cell_event_map(first_source)
            destination = root / "data/interim/lma/cell_event_umap.csv"
            map_before = destination.read_bytes()
            manifest_before = (root / "lifms_project.json").read_bytes()

            incomplete_source = root / "incomplete.csv"
            incomplete_source.write_text(
                "scan_start_time,UMAP1,UMAP2\n41.0,8.0,9.0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BadRequest, "相同的一批 MS event"):
                attached.attach_cell_event_map(incomplete_source)

            self.assertEqual(destination.read_bytes(), map_before)
            self.assertEqual((root / "lifms_project.json").read_bytes(), manifest_before)

    def test_export_adds_coordinates_only_for_third_stage(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)
            base = {
                "annotation_id": "cell-1",
                "source": "manual_created",
                "review_status": "accepted",
                "candidate_type": "manual_cell_pair",
                "ms_event_id": "ms-1",
                "lif_channel": "G1",
                "lif_peak_id": "g1-1",
            }

            cell = app.export_row(
                base,
                stage="cell_annotation",
                export_id="export",
                exported_at="now",
            )
            early_qc = app.export_row(
                {**base, "candidate_type": "manual_qc_triplet"},
                stage="qc_calibration",
                export_id="export",
                exported_at="now",
            )

        self.assertEqual((cell["UMAP1"], cell["UMAP2"]), (1.25, -2.5))
        self.assertEqual(cell["Type"], "LK")
        self.assertNotEqual(cell["Type"], "AUTHOR_LABEL")
        self.assertEqual(cell["cell_event_map_sha256"], "map-sha")
        self.assertIsNone(early_qc["UMAP1"])
        self.assertIn("Type", app.export_columns())

    def test_export_type_never_falls_back_to_payload_or_source_author_label(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = make_app(Path(tmp), with_map=True)
            app.channel_identity_prior["G1"] = {
                "identity_prior": "",
                "identity_prior_source": "test",
            }
            app.acquisition_layout["lif_channels"][0]["identity_prior"] = ""
            row = app.export_row(
                {
                    "annotation_id": "cell-author-label",
                    "source": "manual_created",
                    "review_status": "accepted",
                    "candidate_type": "manual_cell_pair",
                    "ms_event_id": "ms-1",
                    "lif_channel": "G1",
                    "lif_peak_id": "g1-1",
                    "label": "AUTHOR_LABEL",
                },
                stage="cell_annotation",
                export_id="export",
                exported_at="now",
            )

        self.assertEqual(row["Type"], "cell")
        self.assertNotEqual(row["Type"], "AUTHOR_LABEL")


if __name__ == "__main__":
    unittest.main()
