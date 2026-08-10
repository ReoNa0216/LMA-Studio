"""Project-owned preprocessing phase semantics.

New projects write ``provenance/project_protocol.json`` before raw
preprocessing starts. Historical v0.4 projects remain readable at their
manifest-declared paths; this module never moves them.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

try:
    from scripts.v3.lif_peak_detection import (
        adaptive_lif_peak_detection,
        require_active_lif_peak_detection,
    )
except ModuleNotFoundError:  # Direct execution from scripts/v3.
    from lif_peak_detection import (  # type: ignore[no-redef]
        adaptive_lif_peak_detection,
        require_active_lif_peak_detection,
    )

try:
    from scripts.v3.project_storage import (
        CANONICAL_PROJECT_PROTOCOL_PATH,
        LEGACY_PROJECT_PROTOCOL_PATH,
        project_protocol_path,
    )
except ModuleNotFoundError:  # Direct execution from scripts/v3.
    from project_storage import (  # type: ignore[no-redef]
        CANONICAL_PROJECT_PROTOCOL_PATH,
        LEGACY_PROJECT_PROTOCOL_PATH,
        project_protocol_path,
    )


# Public compatibility aliases. New callers should use
# ``project_protocol_path(root)`` because an existing project owns its path.
PROTOCOL_RELATIVE_PATH = Path(CANONICAL_PROJECT_PROTOCOL_PATH)
LEGACY_PROTOCOL_RELATIVE_PATH = Path(LEGACY_PROJECT_PROTOCOL_PATH)


def _finite_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _normalize_segment(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("calibration_protocol.segments entries must be objects")
    segment_id = str(raw.get("segment_id") or f"reference_{index}").strip()
    if not segment_id:
        raise ValueError("calibration segment_id cannot be empty")
    start_min = _finite_float(raw.get("start_min"), f"{segment_id}.start_min")
    end_min = _finite_float(raw.get("end_min"), f"{segment_id}.end_min")
    if start_min < 0 or end_min <= start_min:
        raise ValueError(f"{segment_id} must satisfy end_min > start_min >= 0")
    channels = [
        str(channel).strip().upper()
        for channel in raw.get("reference_channels", [])
        if str(channel).strip()
    ]
    if not channels:
        raise ValueError(f"{segment_id} must have reference_channels")
    confirmed = bool(raw.get("boundaries_confirmed", False))
    return {
        "segment_id": segment_id,
        "order": index,
        "population_label": str(raw.get("population_label") or segment_id).strip(),
        "start_min": start_min,
        "end_min": end_min,
        "reference_channels": channels,
        "boundaries_confirmed": confirmed,
    }


def _module_default_policy() -> dict[str, Any]:
    """Non-persistent policy used only while importing preprocessing modules."""
    return {
        "schema_version": 4,
        "source": "unbound_module_default",
        "compatibility_mode": "",
        "segments": [
            {
                "segment_id": "legacy_qc_calibration",
                "order": 1,
                "population_label": "legacy QC reference",
                "start_min": 0.0,
                "end_min": 10.5,
                "reference_channels": ["G2", "R1"],
                "boundaries_confirmed": True,
            }
        ],
        "annotation_start_min": 40.0,
        "post_qc_strategy": {
            "mode": "signature",
            "reference_channels": ["G2", "R1"],
            "compatibility_mode": "v0.3_qc_anchor_channels",
        },
        "lif_peak_detection": adaptive_lif_peak_detection(),
    }


def _with_derived_fields(policy: dict[str, Any]) -> dict[str, Any]:
    boundaries: list[dict[str, Any]] = []
    seen: set[tuple[float, str]] = set()
    for segment in policy["segments"]:
        for edge, kind in ((segment["start_min"], "calibration_start"), (segment["end_min"], "calibration_end")):
            key = (float(edge), kind)
            if key not in seen:
                seen.add(key)
                boundaries.append(
                    {
                        "time_min": float(edge),
                        "kind": kind,
                        "segment_id": segment["segment_id"],
                    }
                )
    annotation_start = float(policy["annotation_start_min"])
    boundaries.append(
        {
            "time_min": annotation_start,
            "kind": "annotation_start",
            "segment_id": "",
        }
    )
    policy["plot_boundaries"] = sorted(
        boundaries,
        key=lambda row: (row["time_min"], row["kind"], row["segment_id"]),
    )
    return policy


def load_project_protocol(
    project_root: str | Path,
    *,
    allow_unbound_module_default: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    path = project_protocol_path(root)
    if not path.exists():
        if allow_unbound_module_default:
            return _with_derived_fields(_module_default_policy())
        raise ValueError(
            "项目缺少预处理设置；请在新的空目录中重新选择原始输入并创建项目"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("项目预处理设置无法读取或内容损坏") from exc
    if not isinstance(raw, dict):
        raise ValueError("项目预处理设置格式无效")
    calibration = raw.get("calibration_protocol")
    if not isinstance(calibration, dict):
        raise ValueError("项目缺少前段参考设置")
    raw_segments = calibration.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("项目至少需要一个前段参考窗口")
    segments = [_normalize_segment(item, index) for index, item in enumerate(raw_segments, start=1)]
    ids = [segment["segment_id"] for segment in segments]
    if len(ids) != len(set(ids)):
        raise ValueError("前段参考窗口存在重复标识")
    previous_end: float | None = None
    for segment in segments:
        if previous_end is not None and segment["start_min"] < previous_end - 1e-9:
            raise ValueError("前段参考窗口必须按时间排序且不能重叠")
        previous_end = float(segment["end_min"])
    annotation = raw.get("annotation_config")
    if not isinstance(annotation, dict):
        raise ValueError("项目缺少事件标注时间设置")
    annotation_start = _finite_float(annotation.get("annotation_start_min"), "annotation_start_min")
    if annotation_start < 0:
        raise ValueError("annotation_start_min must be >= 0")
    if previous_end is not None and annotation_start < previous_end - 1e-9:
        raise ValueError("annotation_start_min cannot precede the final calibration segment")
    post_qc = raw.get("post_qc_strategy")
    if not isinstance(post_qc, dict):
        raise ValueError("项目缺少后段 QC 设置")
    peak_detection_raw = raw.get("lif_peak_detection")
    if peak_detection_raw is None:
        raise ValueError("项目缺少当前峰识别设置；请从原始输入在新目录重建")
    try:
        peak_detection = require_active_lif_peak_detection(peak_detection_raw)
    except ValueError as exc:
        raise ValueError(
            "项目峰识别设置无效或不完整；请在新目录中从原始输入重新创建项目"
        ) from exc
    if peak_detection_raw != peak_detection:
        raise ValueError("项目峰识别设置不完整；请从原始输入在新目录重建")
    policy = {
        "schema_version": int(raw.get("schema_version") or 0),
        "source": str(path),
        "compatibility_mode": "",
        "segments": segments,
        "boundaries_confirmed": all(
            bool(segment["boundaries_confirmed"]) for segment in segments
        ),
        "annotation_start_min": annotation_start,
        "post_qc_strategy": post_qc,
        "lif_peak_detection": peak_detection,
    }
    return _with_derived_fields(policy)


def classify_project_phase(time_min: Any, policy: dict[str, Any]) -> np.ndarray:
    values = np.asarray(time_min, dtype=float)
    conditions = []
    choices = []
    for segment in policy["segments"]:
        conditions.append(
            (values >= float(segment["start_min"]))
            & (values <= float(segment["end_min"]))
        )
        prefix = (
            "calibration"
            if bool(segment.get("boundaries_confirmed"))
            else "calibration_draft"
        )
        choices.append(f"{prefix}:{segment['segment_id']}")
    conditions.append(values >= float(policy["annotation_start_min"]))
    choices.append("annotation_region")
    return np.select(conditions, choices, default="pre_annotation_unassigned")


def phase_role_from_labels(phases: Any) -> np.ndarray:
    values = np.asarray(phases, dtype=object)
    return np.asarray(
        [
            f"calibration_reference_only:{str(value).split(':', 1)[1]}"
            if str(value).startswith("calibration:")
            else f"calibration_reference_draft:{str(value).split(':', 1)[1]}"
            if str(value).startswith("calibration_draft:")
            else "annotation_region"
            if str(value) == "annotation_region"
            else "pre_annotation_unassigned"
            if str(value) == "pre_annotation_unassigned"
            else "unknown"
            for value in values
        ],
        dtype=object,
    )


def phase_boundaries_min(policy: dict[str, Any]) -> str:
    parts = []
    for segment in policy["segments"]:
        population = segment.get("population_label") or segment["segment_id"]
        channels = "+".join(segment.get("reference_channels") or [])
        parts.append(
            f"calibration:{segment['segment_id']}({population}; {channels}):"
            f"{float(segment['start_min']):g}-{float(segment['end_min']):g}"
            f"[{'confirmed' if segment.get('boundaries_confirmed') else 'draft'}]"
        )
    parts.append(f"annotation_region:>={float(policy['annotation_start_min']):g}")
    parts.append("other configured gaps:pre_annotation_unassigned")
    if policy.get("compatibility_mode"):
        parts.append(f"adapter:{policy['compatibility_mode']}")
    return "; ".join(parts)
