#!/usr/bin/env python3
"""Open current-standard historical projects only through disposable copies."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annotation_app.app import AppData, ProjectPaths, read_project_manifest


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


def file_tree_snapshot(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        stat = path.stat()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "sha256": digest.hexdigest(),
            }
        )
    return rows


def manifest_created_by(manifest: dict[str, Any]) -> str:
    return str(manifest.get("created_by_app_version") or "")


def require_current_standard(project_dir: Path) -> dict[str, Any]:
    manifest = read_project_manifest(project_dir)
    detector = manifest.get("lif_peak_detection") or {}
    if int(manifest.get("project_schema_version", 0)) != 3:
        raise AssertionError(f"{project_dir.name}: expected project schema 3")
    layout = manifest.get("acquisition_layout") or {}
    if int(layout.get("layout_version", 0)) != 4:
        raise AssertionError(f"{project_dir.name}: expected acquisition layout 4")
    if int(detector.get("detector_version", 0)) != 2:
        raise AssertionError(f"{project_dir.name}: expected active peak detector 2")
    return manifest


def validate_copy(source: Path, copy: Path) -> dict[str, Any]:
    manifest = require_current_standard(copy)
    app = AppData.load(ProjectPaths.from_args(project_dir=str(copy)))
    meta = app.meta()
    raw_window = app.window(
        float(meta["time_min_min"]),
        min(float(meta["default_window_min"]), 1.0),
        time_mode="raw",
    )
    config = app.project_config()
    event_start = float(config["annotation_start_min"])
    aligned_window = app.window(event_start, 1.0, time_mode="aligned")

    relationships = [
        app.project_saved_relation(row)
        for row in app.store.records()
        if app.annotation_review_stage(row) in {"qc_survey", "cell_annotation"}
    ]
    relation_identities = {
        (
            str(row.get("annotation_id") or ""),
            str(row.get("lif_peak_id") or ""),
            str(row.get("ms_event_id") or ""),
            str(row.get("review_status") or ""),
        )
        for row in relationships
    }
    if len(relation_identities) != len(relationships):
        raise AssertionError(f"{source.name}: duplicate projected relationship identity")

    umap_points = 0
    if app.cell_event_map is not None:
        umap = app.projected_cell_event_map_state()
        umap_points = len(umap["points"])
        if umap_points != len(app.cell_event_map):
            raise AssertionError(f"{source.name}: UMAP projection lost event identities")

    copy_before_preview = file_tree_snapshot(copy)
    previewed = False
    frozen = app.frozen_time_model()
    if frozen:
        current_shifts = dict(app.alignment.get("axis_shifts_sec") or {})
        preview_shifts = {
            axis: float(shift) + 0.25 for axis, shift in current_shifts.items()
        }
        app.timeline_adjustment_preview(
            preview_shifts,
            ms_local_delta_sec=float(frozen.get("ms_local_delta_sec", 0.0) or 0.0),
        )
        previewed = True
    if file_tree_snapshot(copy) != copy_before_preview:
        raise AssertionError(f"{source.name}: timeline preview wrote project files")

    exported = app.export_accepted_annotations_csv()
    export_frame = pd.read_csv(io.StringIO(exported["csv_text"]))
    if list(export_frame.columns) != EXPECTED_EXPORT_COLUMNS:
        raise AssertionError(f"{source.name}: export schema changed")

    reopened = AppData.load(ProjectPaths.from_args(project_dir=str(copy)))
    if reopened.project_identity() != app.project_identity():
        raise AssertionError(f"{source.name}: project identity changed after reopen")

    return {
        "project": source.name,
        "created_by": manifest_created_by(manifest),
        "lif_trace_rows": int(meta["lif_trace_rows"]),
        "lif_peak_rows": int(meta["lif_peak_rows"]),
        "ms_event_rows": int(meta["ms_event_rows"]),
        "raw_window_lif_points": int(
            raw_window["counts"]["lif_trace_points_returned"]
        ),
        "aligned_window_lif_points": int(
            aligned_window["counts"]["lif_trace_points_returned"]
        ),
        "relationship_count": len(relationships),
        "umap_point_count": umap_points,
        "timeline_previewed": previewed,
        "export_row_count": len(export_frame),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dirs", nargs="+", type=Path)
    args = parser.parse_args()

    sources = [path.expanduser().resolve() for path in args.project_dirs]
    for source in sources:
        if not (source / "lifms_project.json").is_file():
            raise FileNotFoundError(f"Project manifest is missing: {source}")
        require_current_standard(source)

    source_snapshots = {str(source): file_tree_snapshot(source) for source in sources}
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="LMAStudioProjectRegression_v050_") as tmp:
        copy_root = Path(tmp)
        for index, source in enumerate(sources, start=1):
            copy = copy_root / f"{index:02d}_{source.name}"
            shutil.copytree(source, copy)
            results.append(validate_copy(source, copy))

    changed_sources = [
        str(source)
        for source in sources
        if file_tree_snapshot(source) != source_snapshots[str(source)]
    ]
    if changed_sources:
        raise AssertionError(
            "Original projects changed during copy regression: "
            + ", ".join(changed_sources)
        )

    print(
        json.dumps(
            {
                "project_count": len(results),
                "source_projects_unchanged": True,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
