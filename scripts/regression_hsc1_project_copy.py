#!/usr/bin/env python3
"""Create and validate a disposable HSC1 LMA project without mutating sources."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annotation_app.app import (
    AppData,
    ProjectPaths,
    cell_candidate_id,
    manual_cell_annotation_id,
    raw_file_fingerprint,
)


EXPECTED_EXPORT_COLUMNS = [
    "CellNumber",
    "scan_Id",
    "scan_start_time",
    "TIC",
    "PC(34:1)_mz",
    "PC(34:1)_intensity",
    "UMAP1",
    "UMAP2",
    "Type",
    "annotation_kind",
    "review_stage",
    "LIF_channel",
    "LIF_peak_id",
    "MS_event_id",
    "residual_sec",
    "annotation_id",
]


def tree_snapshot(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": int(path.stat().st_size),
            "mtime_ns": int(path.stat().st_mtime_ns),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def source_fingerprints(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    return {
        key: raw_file_fingerprint(path)
        for key, path in paths.items()
    }


def require_safe_target(source_root: Path, target: Path) -> None:
    source_root = source_root.resolve()
    target = target.resolve()
    if target == source_root or source_root in target.parents or target in source_root.parents:
        raise ValueError("测试项目必须位于 HSC1_data 之外的独立目录")
    if target.exists():
        raise ValueError("测试项目目录必须不存在；脚本不会覆盖或删除现有目录")


def write_lsk_coordinate_whitelist(source: Path, destination: Path) -> int:
    """Prepare a dataset-specific coordinate file; never carry author labels."""
    frame = pd.read_csv(
        source,
        usecols=["scan_start_time", "UMAP1", "UMAP2", "batch"],
    )
    selected = frame.loc[
        frame["batch"].astype(str).eq("Lin-LSK"),
        ["scan_start_time", "UMAP1", "UMAP2"],
    ].copy()
    if selected.empty:
        raise ValueError("combined HSC coordinate CSV has no Lin-LSK rows")
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected.to_csv(destination, index=False, lineterminator="\n")
    return int(len(selected))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hsc-data-dir", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.hsc_data_dir.expanduser().resolve()
    project_dir = args.project_dir.expanduser().resolve()
    require_safe_target(source_root, project_dir)
    paths = {
        "g1": source_root / "Lin-_LSK" / "G1.CSV",
        "g2": source_root / "Lin-_LSK" / "G2.CSV",
        "ms": source_root / "Lin-_LSK.txt",
        "event_map": source_root
        / "HSC-Lin-LSK-MPP-CLP-LK-20260809-After-Batch-Correction.csv",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("HSC1 测试输入缺失: " + ", ".join(missing))

    tree_before = tree_snapshot(source_root)
    fingerprints_before = source_fingerprints(paths)
    coordinate_whitelist = (
        project_dir.parent / f".{project_dir.name}.Lin-LSK.coordinates-only.csv"
    )
    if coordinate_whitelist.exists():
        raise ValueError("临时坐标白名单路径已存在；脚本不会覆盖")
    coordinate_row_count = write_lsk_coordinate_whitelist(
        paths["event_map"],
        coordinate_whitelist,
    )
    print(
        f"Prepared {coordinate_row_count} Lin-LSK coordinate-only rows outside HSC1_data.",
        flush=True,
    )
    print("HSC1 source snapshot captured; creating isolated project copy...", flush=True)
    try:
        app = AppData.create_project_from_raw_inputs(
            project_dir=project_dir,
            ms_path=paths["ms"],
            raw_input_mode="external_reference",
            lif_inputs=[
                {
                    "key": "lif_g1",
                    "path": paths["g1"],
                    "channel": "G1",
                    "identity_prior": "LSK",
                    "detector": "green",
                    "time_axis": "green_axis",
                    "use_for_cell_annotation": True,
                },
                {
                    "key": "lif_g2",
                    "path": paths["g2"],
                    "channel": "G2",
                    "identity_prior": "Lin-",
                    "detector": "green",
                    "time_axis": "green_axis",
                    "use_for_cell_annotation": True,
                },
            ],
            calibration_protocol={
                "protocol_version": 1,
                "segments": [
                    {
                        "segment_id": "lsk_reference",
                        "order": 1,
                        "start_min": 1.913,
                        "end_min": 8.288,
                        "reference_channels": ["G1"],
                        "population_label": "LSK",
                        "boundaries_confirmed": True,
                    },
                    {
                        "segment_id": "lin_reference",
                        "order": 2,
                        "start_min": 13.089,
                        "end_min": 19.954,
                        "reference_channels": ["G2"],
                        "population_label": "Lin-",
                        "boundaries_confirmed": True,
                    },
                ],
            },
            post_qc_strategy={"mode": "disabled"},
            annotation_start_min=24.0,
            local_delta_seed_window_min=2.5,
            cell_event_map_path=coordinate_whitelist,
        )
        reopened = AppData.load(
            ProjectPaths.from_args(
                project_dir=str(project_dir),
                annotation_db=str(
                    project_dir / "annotation_app" / "annotations" / "annotation.sqlite"
                ),
            )
        )
        config = reopened.project_config()
        if config["annotation_start_min"] != 24.0:
            raise AssertionError("HSC1 annotation_start_min was not preserved")
        if config["post_qc_strategy"]["mode"] != "disabled":
            raise AssertionError("HSC1 post-QC default was not disabled")
        if config["calibration_protocol"]["reference_channels"] != ["G1", "G2"]:
            raise AssertionError("HSC1 sequential reference channels were not preserved")
        if set(reopened.alignment.get("axis_shifts_sec") or {}) != {"green_axis"}:
            raise AssertionError("HSC1 must expose exactly one shared green_axis shift")
        exported = reopened.export_accepted_annotations_csv()
        header = exported["csv_text"].splitlines()[0].split(",")
        if header != EXPECTED_EXPORT_COLUMNS:
            raise AssertionError(f"Unexpected compact CSV header: {header}")
        frozen = reopened.freeze_local_delta_model()
        if frozen["status"] != "frozen":
            raise AssertionError("HSC1 disposable project did not freeze its local time model")
        config = reopened.project_config()
        candidates = reopened.build_cell_candidates(
            float(config["annotation_start_min"]),
            float(reopened.meta()["time_min_max"]),
            "aligned",
        )
        unique_candidates: dict[str, dict[str, Any]] = {}
        for candidate in sorted(
            candidates,
            key=lambda row: (
                float(row.get("abs_residual_sec", 9999.0)),
                str(row.get("lif_channel") or ""),
            ),
        ):
            unique_candidates.setdefault(str(candidate["ms_event_id"]), candidate)
        selected = list(unique_candidates.values())[:120]
        if len(selected) < 12:
            raise AssertionError(
                f"HSC1 real-data copy produced too few cell candidates: {len(selected)}"
            )
        if len(selected) < 120:
            selected_event_ids = {str(row["ms_event_id"]) for row in selected}
            allowed_event_ids = reopened.cell_event_map_event_ids() or set()
            cell_channels = set(reopened.cell_annotation_channels())
            manual_peaks = reopened.lif_peaks[
                reopened.lif_peaks["channel"].astype(str).isin(cell_channels)
            ].copy()
            manual_peaks["projected_time_sec"] = manual_peaks.apply(
                lambda row: float(row["time_sec"])
                + reopened.channel_shift_sec(str(row["channel"]), "aligned"),
                axis=1,
            )
            for _, event in reopened.ms_events.sort_values("time_sec").iterrows():
                event_id = str(event["event_id"])
                if event_id in selected_event_ids or event_id not in allowed_event_ids:
                    continue
                if float(event["time_min"]) < float(config["annotation_start_min"]):
                    continue
                event_plot_sec = float(event["time_sec"]) + reopened.ms_shift_sec_at(
                    float(event["time_min"]),
                    "aligned",
                    require_frozen=True,
                )
                nearest_index = (
                    manual_peaks["projected_time_sec"] - event_plot_sec
                ).abs().idxmin()
                peak = manual_peaks.loc[nearest_index]
                relation = {
                    "lif_channel": str(peak["channel"]),
                    "lif_peak_id": str(peak["peak_id"]),
                    "ms_event_id": event_id,
                    "candidate_type": "manual_cell_pair",
                    "_regression_manual_only": True,
                }
                try:
                    reopened.payload_from_cell_ids(
                        relation["lif_channel"],
                        relation["lif_peak_id"],
                        relation["ms_event_id"],
                        enforce_acceptance_conflicts=False,
                    )
                except ValueError:
                    continue
                selected.append(relation)
                selected_event_ids.add(event_id)
                if len(selected) >= 120:
                    break
        if len(selected) < 100:
            raise AssertionError(
                f"HSC1 long-session copy produced too few durable relations: {len(selected)}"
            )
        statuses = ("accepted", "pending", "rejected")
        for index, candidate in enumerate(selected):
            source = (
                "manual_created"
                if candidate.get("_regression_manual_only") or index % 4 == 0
                else "auto_candidate"
            )
            status = statuses[index % len(statuses)]
            payload = reopened.payload_from_cell_ids(
                str(candidate["lif_channel"]),
                str(candidate["lif_peak_id"]),
                str(candidate["ms_event_id"]),
                enforce_acceptance_conflicts=False,
            )
            payload.update(
                {
                    "review_stage": "cell_annotation",
                    "candidate_type": (
                        "manual_cell_pair"
                        if source == "manual_created"
                        else str(candidate.get("candidate_type") or "cell_high_confidence")
                    ),
                }
            )
            annotation_id = (
                manual_cell_annotation_id(
                    str(candidate["lif_channel"]),
                    str(candidate["lif_peak_id"]),
                    str(candidate["ms_event_id"]),
                )
                if source == "manual_created"
                else cell_candidate_id(candidate)
            )
            reopened.store.upsert_review(
                annotation_id=annotation_id,
                source=source,
                review_status=status,
                payload=payload,
                action="hsc1_long_session_regression_review",
            )
        decisions_before = {
            str(row["annotation_id"]): {
                "review_status": str(row["review_status"]),
                "source": str(row["source"]),
                "lif_peak_id": str(row.get("lif_peak_id") or ""),
                "ms_event_id": str(row.get("ms_event_id") or ""),
                "time_model_version": str(row.get("time_model_version") or ""),
            }
            for row in reopened.store.records()
        }
        umap_before = reopened.projected_cell_event_map_state()
        current_shifts = dict(reopened.alignment.get("axis_shifts_sec") or {})
        adjusted_shifts = {
            axis: float(shift) + 2.0 for axis, shift in current_shifts.items()
        }
        timeline_preview = reopened.timeline_adjustment_preview(
            adjusted_shifts,
            ms_local_delta_sec=float(frozen.get("ms_local_delta_sec", 0.0) or 0.0),
        )
        applied = reopened.apply_timeline_adjustment(
            adjusted_shifts,
            ms_local_delta_sec=float(frozen.get("ms_local_delta_sec", 0.0) or 0.0),
            expected_preview_hash=str(timeline_preview["preview_hash"]),
        )
        decisions_after = {
            str(row["annotation_id"]): {
                "review_status": str(row["review_status"]),
                "source": str(row["source"]),
                "lif_peak_id": str(row.get("lif_peak_id") or ""),
                "ms_event_id": str(row.get("ms_event_id") or ""),
                "time_model_version": str(row.get("time_model_version") or ""),
            }
            for row in reopened.store.records()
        }
        if decisions_after != decisions_before:
            raise AssertionError("Timeline adjustment mutated reviewed raw-ID decisions")
        projected_relations = [
            reopened.project_saved_relation(row)
            for row in reopened.store.records()
            if reopened.annotation_review_stage(row) in {"qc_survey", "cell_annotation"}
        ]
        needs_review_count = sum(
            1 for row in projected_relations if bool(row.get("needs_review"))
        )
        if needs_review_count < 1:
            raise AssertionError("Adjusted HSC1 relations were not flagged for review")
        umap_after = reopened.projected_cell_event_map_state()
        before_classification = {
            str(point["ms_event_id"]): (
                str(point["classification"]),
                str(point.get("lif_channel") or ""),
            )
            for point in umap_before["points"]
        }
        after_classification = {
            str(point["ms_event_id"]): (
                str(point["classification"]),
                str(point.get("lif_channel") or ""),
            )
            for point in umap_after["points"]
        }
        if after_classification != before_classification:
            raise AssertionError("Timeline adjustment changed HSC1 UMAP identities")
        if umap_after["revision"] == umap_before["revision"]:
            raise AssertionError("Timeline adjustment did not advance the UMAP state revision")
        reviewed_export = reopened.export_accepted_annotations_csv()
        exported_frame = pd.read_csv(io.StringIO(reviewed_export["csv_text"]))
        expected_accepted_ids = {
            annotation_id
            for annotation_id, row in decisions_before.items()
            if row["review_status"] == "accepted"
        }
        actual_accepted_ids = set(
            exported_frame["annotation_id"].dropna().astype(str)
        )
        if not expected_accepted_ids.issubset(actual_accepted_ids):
            raise AssertionError("Timeline adjustment dropped accepted HSC1 CSV identities")
        print(
            json.dumps(
                {
                    "project_dir": str(project_dir),
                    "project_schema_version": int(reopened.manifest["project_schema_version"]),
                    "layout_version": int(reopened.acquisition_layout["layout_version"]),
                    "lif_trace_rows": int(len(reopened.lif_traces)),
                    "lif_peak_rows": int(len(reopened.lif_peaks)),
                    "ms_event_rows": int(len(reopened.ms_events)),
                    "event_map_rows": int(
                        0
                        if reopened.cell_event_map is None
                        else len(reopened.cell_event_map)
                    ),
                    "calibration_segments": config["calibration_protocol"]["segments"],
                    "annotation_start_min": config["annotation_start_min"],
                    "post_qc_mode": config["post_qc_strategy"]["mode"],
                    "axis_shifts_sec": reopened.alignment.get("axis_shifts_sec"),
                    "empty_export_header": header,
                    "long_session_relation_count": len(decisions_before),
                    "long_session_status_counts": {
                        status: sum(
                            1
                            for row in decisions_before.values()
                            if row["review_status"] == status
                        )
                        for status in statuses
                    },
                    "timeline_revision": applied["time_model"]["time_model_version"],
                    "needs_review_count": needs_review_count,
                    "accepted_csv_identity_count": len(expected_accepted_ids),
                    "umap_identity_preserved": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        del app
    finally:
        if coordinate_whitelist.exists():
            coordinate_whitelist.unlink()
        tree_after = tree_snapshot(source_root)
        fingerprints_after = source_fingerprints(paths)
        if tree_after != tree_before or fingerprints_after != fingerprints_before:
            raise AssertionError("HSC1_data changed during isolated project regression")
        print("HSC1 source snapshot unchanged.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
