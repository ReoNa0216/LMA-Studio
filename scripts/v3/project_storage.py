"""Project-owned storage layout definitions.

The scientific schema and the on-disk folder layout are deliberately separate.
Existing v0.4 projects keep their manifest-declared paths forever; newly
created projects use a compact, portable layout without pipeline-version
folders.  Nothing in this module migrates an existing project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CANONICAL_STORAGE_LAYOUT_NAME = "portable_project"
CANONICAL_STORAGE_LAYOUT_VERSION = 1

CANONICAL_TABLE_PATHS: dict[str, str] = {
    "lif_traces": "data/lif_traces.parquet",
    "lif_peaks": "data/lif_peaks.parquet",
    "ms_events": "data/ms_events.parquet",
    "ms_scan_summary": "data/ms_scan_summary.parquet",
}
CANONICAL_CELL_EVENT_MAP_PATH = "data/cell_event_map.csv"
CANONICAL_ANNOTATION_DB_PATH = "annotations/annotation.sqlite"
CANONICAL_EXPORTS_DIR = "annotations/exports"
CANONICAL_INPUT_MANIFEST_PATH = "provenance/input_manifest.csv"
CANONICAL_PROJECT_PROTOCOL_PATH = "provenance/project_protocol.json"
CANONICAL_PREPROCESSING_LOG_PATH = "provenance/preprocessing.log"
CANONICAL_PREPROCESSING_REPORT_PATH = "provenance/preprocessing_report.md"
CANONICAL_LIF_DIAGNOSTICS_DIR = "diagnostics/lif"
CANONICAL_MS_DIAGNOSTICS_DIR = "diagnostics/ms"
CANONICAL_PROJECT_README_PATH = "README.md"

LEGACY_PROJECT_PROTOCOL_PATH = "results/tables/v3/00_project_protocol.json"


def canonical_storage_layout_manifest_entry() -> dict[str, Any]:
    """Return small, additive metadata that released v0.4 safely ignores."""

    return {
        "name": CANONICAL_STORAGE_LAYOUT_NAME,
        "layout_version": CANONICAL_STORAGE_LAYOUT_VERSION,
        "portable_relative_paths": True,
    }


def manifest_uses_canonical_storage(manifest: dict[str, Any] | None) -> bool:
    if not isinstance(manifest, dict):
        return False
    raw = manifest.get("storage_layout")
    return bool(
        isinstance(raw, dict)
        and str(raw.get("name") or "") == CANONICAL_STORAGE_LAYOUT_NAME
        and int(raw.get("layout_version") or 0) == CANONICAL_STORAGE_LAYOUT_VERSION
    )


def project_uses_canonical_storage(project_root: str | Path) -> bool:
    """Detect preprocessing layout without creating any files.

    During new-project staging the canonical protocol is written before either
    preprocessor runs.  A legacy protocol therefore remains an unambiguous
    signal to preserve the historical output paths for forensic reruns.
    """

    root = Path(project_root).expanduser().resolve()
    path = project_protocol_path(root)
    if path != root / CANONICAL_PROJECT_PROTOCOL_PATH or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("项目预处理协议无法读取，目录布局不完整") from exc
    raw = payload.get("storage_layout") if isinstance(payload, dict) else None
    try:
        valid = bool(
            isinstance(raw, dict)
            and str(raw.get("name") or "") == CANONICAL_STORAGE_LAYOUT_NAME
            and int(raw.get("layout_version") or 0)
            == CANONICAL_STORAGE_LAYOUT_VERSION
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("项目预处理协议缺少有效的目录布局声明")
    return True


def project_protocol_path(project_root: str | Path) -> Path:
    """Resolve canonical first, then the historical read-compatible path."""

    root = Path(project_root).expanduser().resolve()
    canonical = root / CANONICAL_PROJECT_PROTOCOL_PATH
    legacy = root / LEGACY_PROJECT_PROTOCOL_PATH
    if canonical.is_file() and legacy.is_file():
        raise ValueError(
            "项目同时包含当前与历史预处理协议，无法判断应使用哪套输出目录；"
            "请使用未混合的完整项目副本"
        )
    if canonical.is_file():
        return canonical
    if legacy.is_file():
        return legacy
    # Return the current standard in the error path so user guidance never
    # tells someone to create a historical directory.
    return canonical
