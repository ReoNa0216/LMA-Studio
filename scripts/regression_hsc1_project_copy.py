#!/usr/bin/env python3
"""Create and validate a disposable HSC1 LMA project without mutating sources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annotation_app.app import AppData, ProjectPaths, raw_file_fingerprint


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
