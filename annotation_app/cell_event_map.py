"""Strict import and state projection for the single-cell UMAP event map.

The source CSV is deliberately treated as a coordinate/whitelist input, not as
an annotation source.  Only the three explicitly allowed columns are loaded.
After import, every row is bound to a stable ``ms_event_id`` and all later
operations use the canonical five-column table.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


CELL_EVENT_MAP_SCHEMA_VERSION = 1
CELL_EVENT_MAP_RELATIVE_PATH = "data/interim/lma/cell_event_umap.csv"
CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS = ("scan_start_time", "UMAP1", "UMAP2")
CELL_EVENT_MAP_CANONICAL_COLUMNS = (
    "ms_event_id",
    "scan_id",
    "scan_start_time",
    "UMAP1",
    "UMAP2",
)
# One MS scan in supported acquisitions is about 0.10 s.  A 0.15 s window
# binds a coordinate selected on the shoulder to its unique called apex while
# remaining below half the 0.30 s minimum event separation; ambiguity is still
# rejected rather than guessed.
DEFAULT_MATCH_TOLERANCE_SEC = 0.15
PRIMARY_EVENT_STRATEGY = "pc34_primary"
PRIMARY_SIGNAL_COLUMN = "pc34_760_max_intensity"


class CellEventMapError(ValueError):
    """The source map or its project binding is invalid."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return next(csv.reader(handle))
    except StopIteration as exc:
        raise CellEventMapError("事件坐标 CSV 为空，缺少 header") from exc
    except UnicodeDecodeError as exc:
        raise CellEventMapError("事件坐标 CSV 必须使用 UTF-8 编码") from exc
    except OSError as exc:
        raise CellEventMapError(f"无法读取事件坐标 CSV: {exc}") from exc


def read_source_coordinates(path: Path) -> pd.DataFrame:
    """Load only the three allowed source columns and validate every value."""

    path = path.expanduser().resolve()
    if not path.is_file():
        raise CellEventMapError(f"事件坐标 CSV 不存在: {path}")
    header = _csv_header(path)
    bad_counts = {
        column: header.count(column)
        for column in CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS
        if header.count(column) != 1
    }
    if bad_counts:
        details = ", ".join(f"{column}={count}" for column, count in bad_counts.items())
        raise CellEventMapError(f"事件坐标 CSV 必需列必须各出现一次: {details}")

    try:
        # Security boundary: never load Type/leiden/CellNumber or any other
        # source column.  Header inspection above checks names only.
        frame = pd.read_csv(
            path,
            usecols=list(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS),
            encoding="utf-8-sig",
        )
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
        raise CellEventMapError(f"无法解析事件坐标 CSV: {exc}") from exc

    if frame.empty:
        raise CellEventMapError("事件坐标 CSV 没有数据行")
    frame = frame.loc[:, list(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS)].copy()
    for column in CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        finite = np.isfinite(numeric.to_numpy(dtype=float, na_value=np.nan))
        if not finite.all():
            source_rows = (np.flatnonzero(~finite) + 2).tolist()
            preview = ", ".join(str(row) for row in source_rows[:10])
            raise CellEventMapError(f"{column} 含非数值/NaN/Inf，CSV 行: {preview}")
        frame[column] = numeric.astype(float)

    duplicate_mask = frame["scan_start_time"].duplicated(keep=False)
    if duplicate_mask.any():
        source_rows = (np.flatnonzero(duplicate_mask.to_numpy()) + 2).tolist()
        preview = ", ".join(str(row) for row in source_rows[:10])
        raise CellEventMapError(f"scan_start_time 不能重复，CSV 行: {preview}")
    return frame


def primary_ms_events(ms_events: pd.DataFrame) -> pd.DataFrame:
    required = {"event_id", "event_strategy", "primary_signal_col", "scan_id", "time_min"}
    missing = sorted(required - set(ms_events.columns))
    if missing:
        raise CellEventMapError(f"MS event 表缺少必需列: {', '.join(missing)}")
    selected = ms_events[
        ms_events["event_strategy"].astype(str).eq(PRIMARY_EVENT_STRATEGY)
        & ms_events["primary_signal_col"].astype(str).eq(PRIMARY_SIGNAL_COLUMN)
    ][["event_id", "scan_id", "time_min"]].copy()
    if selected.empty:
        raise CellEventMapError("MS event 表没有 pc34_primary / pc34_760_max_intensity event")
    selected["event_id"] = selected["event_id"].astype(str)
    if selected["event_id"].duplicated().any():
        ids = selected.loc[selected["event_id"].duplicated(keep=False), "event_id"].tolist()
        raise CellEventMapError(f"MS event_id 不能重复: {', '.join(ids[:5])}")
    selected["time_min"] = pd.to_numeric(selected["time_min"], errors="coerce")
    finite = np.isfinite(selected["time_min"].to_numpy(dtype=float, na_value=np.nan))
    if not finite.all():
        ids = selected.loc[~finite, "event_id"].astype(str).tolist()
        raise CellEventMapError(f"MS event time_min 含 NaN/Inf: {', '.join(ids[:5])}")
    if selected["scan_id"].isna().any():
        ids = selected.loc[selected["scan_id"].isna(), "event_id"].astype(str).tolist()
        raise CellEventMapError(f"MS event scan_id 缺失: {', '.join(ids[:5])}")
    return selected.sort_values(["time_min", "event_id"], kind="stable").reset_index(drop=True)


def match_source_to_events(
    source_coordinates: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    tolerance_sec: float = DEFAULT_MATCH_TOLERANCE_SEC,
) -> pd.DataFrame:
    """Bind every source row to exactly one distinct primary MS760 event."""

    if not math.isfinite(float(tolerance_sec)) or float(tolerance_sec) <= 0:
        raise CellEventMapError("match tolerance 必须是正有限秒数")
    missing_source = sorted(set(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS) - set(source_coordinates.columns))
    if missing_source:
        raise CellEventMapError(f"坐标表缺少必需列: {', '.join(missing_source)}")
    events = primary_ms_events(ms_events)
    event_times = events["time_min"].to_numpy(dtype=float)
    tolerance_min = float(tolerance_sec) / 60.0
    matches: list[dict[str, Any]] = []
    unmatched_rows: list[int] = []
    ambiguous_rows: list[tuple[int, list[str]]] = []

    for frame_index, source_row in source_coordinates.reset_index(drop=True).iterrows():
        source_line = int(frame_index) + 2
        source_time = float(source_row["scan_start_time"])
        left = int(np.searchsorted(event_times, source_time - tolerance_min, side="left"))
        right = int(np.searchsorted(event_times, source_time + tolerance_min, side="right"))
        candidate_indices = [
            index
            for index in range(left, right)
            if abs(float(event_times[index]) - source_time) * 60.0 <= float(tolerance_sec) + 1e-12
        ]
        if not candidate_indices:
            unmatched_rows.append(source_line)
            continue
        if len(candidate_indices) > 1:
            ambiguous_rows.append(
                (source_line, events.iloc[candidate_indices]["event_id"].astype(str).tolist())
            )
            continue
        event = events.iloc[candidate_indices[0]]
        matches.append(
            {
                "source_line": source_line,
                "ms_event_id": str(event["event_id"]),
                "scan_id": event["scan_id"],
                "scan_start_time": source_time,
                "UMAP1": float(source_row["UMAP1"]),
                "UMAP2": float(source_row["UMAP2"]),
            }
        )

    failures: list[str] = []
    if unmatched_rows:
        if len(events) < len(source_coordinates):
            failures.append(
                f"MS760 event 仅识别到 {len(events)} 个，但事件坐标 CSV 有 "
                f"{len(source_coordinates)} 行；请检查 MS 峰识别结果，不要删除坐标 CSV 行"
            )
        failures.append("未匹配 CSV 行 " + ", ".join(str(row) for row in unmatched_rows[:10]))
    if ambiguous_rows:
        preview = "; ".join(
            f"{line} -> {','.join(event_ids)}" for line, event_ids in ambiguous_rows[:10]
        )
        failures.append("容差内存在多个 MS event: " + preview)
    if failures:
        raise CellEventMapError("；".join(failures))

    matched = pd.DataFrame(matches)
    reused = matched[matched["ms_event_id"].duplicated(keep=False)]
    if not reused.empty:
        details = []
        for event_id, rows in reused.groupby("ms_event_id", sort=True):
            details.append(
                f"{event_id} <- CSV 行 {','.join(str(int(value)) for value in rows['source_line'])}"
            )
        raise CellEventMapError("两个 CSV 行不能复用同一 MS event: " + "; ".join(details[:10]))
    if len(matched) != len(source_coordinates):
        raise CellEventMapError("事件坐标 CSV 未能整体一对一匹配")
    canonical = matched.loc[:, ["ms_event_id", "scan_id", "scan_start_time", "UMAP1", "UMAP2"]]
    return canonical.sort_values(["scan_start_time", "ms_event_id"], kind="stable").reset_index(drop=True)


def import_cell_event_map(
    source_path: Path,
    ms_events: pd.DataFrame,
    *,
    tolerance_sec: float = DEFAULT_MATCH_TOLERANCE_SEC,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source_path = source_path.expanduser().resolve()
    coordinates = read_source_coordinates(source_path)
    canonical = match_source_to_events(coordinates, ms_events, tolerance_sec=tolerance_sec)
    return canonical, {
        "schema_version": CELL_EVENT_MAP_SCHEMA_VERSION,
        "source_name": source_path.name,
        "source_sha256": sha256_file(source_path),
        "row_count": int(len(canonical)),
        "matched_event_count": int(canonical["ms_event_id"].nunique()),
        "time_unit": "min",
        "match_tolerance_sec": float(tolerance_sec),
        "required_source_columns": list(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS),
    }


def canonical_csv_bytes(frame: pd.DataFrame) -> bytes:
    missing = sorted(set(CELL_EVENT_MAP_CANONICAL_COLUMNS) - set(frame.columns))
    if missing:
        raise CellEventMapError(f"canonical event map 缺少列: {', '.join(missing)}")
    canonical = frame.loc[:, list(CELL_EVENT_MAP_CANONICAL_COLUMNS)].copy()
    text = canonical.to_csv(
        index=False,
        lineterminator="\n",
        float_format="%.15g",
    )
    return text.encode("utf-8")


def write_canonical_map(frame: pd.DataFrame, destination: Path) -> dict[str, Any]:
    """Atomically write a canonical map and return its immutable binding."""

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_csv_bytes(frame)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": destination,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def read_canonical_map(path: Path, *, expected_sha256: str | None = None) -> pd.DataFrame:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise CellEventMapError(f"canonical event map 不存在: {path}")
    if expected_sha256 and sha256_file(path) != str(expected_sha256):
        raise CellEventMapError("canonical event map SHA256 与项目 manifest 不一致")
    header = _csv_header(path)
    if header != list(CELL_EVENT_MAP_CANONICAL_COLUMNS):
        raise CellEventMapError(
            "canonical event map 列必须严格为 " + ", ".join(CELL_EVENT_MAP_CANONICAL_COLUMNS)
        )
    try:
        frame = pd.read_csv(path, usecols=list(CELL_EVENT_MAP_CANONICAL_COLUMNS), encoding="utf-8")
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
        raise CellEventMapError(f"无法读取 canonical event map: {exc}") from exc
    if frame.empty:
        raise CellEventMapError("canonical event map 为空")
    if frame["ms_event_id"].isna().any() or frame["ms_event_id"].astype(str).duplicated().any():
        raise CellEventMapError("canonical event map 的 ms_event_id 必须非空且唯一")
    if frame["scan_id"].isna().any() or frame["scan_id"].astype(str).str.strip().eq("").any():
        raise CellEventMapError("canonical event map 的 scan_id 必须非空")
    for column in ("scan_start_time", "UMAP1", "UMAP2"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if not np.isfinite(frame[column].to_numpy(dtype=float, na_value=np.nan)).all():
            raise CellEventMapError(f"canonical event map 的 {column} 含 NaN/Inf")
    return frame.loc[:, list(CELL_EVENT_MAP_CANONICAL_COLUMNS)].copy()


def cell_event_map_manifest_entry(
    *,
    canonical_path: Path,
    project_dir: Path,
    import_metadata: dict[str, Any],
) -> dict[str, Any]:
    canonical_path = canonical_path.expanduser().resolve()
    project_dir = project_dir.expanduser().resolve()
    try:
        relative = canonical_path.relative_to(project_dir).as_posix()
    except ValueError as exc:
        raise CellEventMapError("canonical event map 必须复制到项目目录内") from exc
    entry = {
        "schema_version": CELL_EVENT_MAP_SCHEMA_VERSION,
        "path": relative,
        "sha256": sha256_file(canonical_path),
        "source_name": str(import_metadata.get("source_name") or ""),
        "source_sha256": str(import_metadata.get("source_sha256") or ""),
        "row_count": int(import_metadata.get("row_count", 0)),
        "matched_event_count": int(import_metadata.get("matched_event_count", 0)),
        "time_unit": "min",
        "match_tolerance_sec": float(
            import_metadata.get("match_tolerance_sec", DEFAULT_MATCH_TOLERANCE_SEC)
        ),
        "required_source_columns": list(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS),
    }
    if not entry["source_sha256"]:
        raise CellEventMapError("event map import metadata 缺少 source_sha256")
    if entry["row_count"] <= 0 or entry["matched_event_count"] != entry["row_count"]:
        raise CellEventMapError("event map manifest 需要完整的一对一匹配计数")
    return entry


def _annotation_stage(row: dict[str, Any], annotation_start_min: float) -> str:
    explicit = str(row.get("review_stage") or "")
    if explicit in {"qc_calibration", "qc_survey", "cell_annotation"}:
        return explicit
    candidate_type = str(row.get("candidate_type") or "")
    if candidate_type.startswith("cell") or candidate_type == "manual_cell_pair":
        return "cell_annotation"
    if candidate_type == "qc_survey_post_10p5" or candidate_type.startswith("manual_qc"):
        try:
            return (
                "qc_survey"
                if float(row.get("ms_time_min")) >= float(annotation_start_min)
                else "qc_calibration"
            )
        except (TypeError, ValueError):
            return "qc_calibration"
    return "qc_calibration"


def project_annotation_state(
    event_map: pd.DataFrame,
    annotations: Iterable[dict[str, Any]],
    *,
    active_time_model_version: str | None,
    annotation_start_min: float,
) -> dict[str, Any]:
    """Derive UMAP classifications solely from current SQLite-style rows."""

    event_ids = event_map["ms_event_id"].astype(str).tolist()
    event_id_set = set(event_ids)
    relations: dict[str, list[dict[str, str]]] = {event_id: [] for event_id in event_ids}
    active_version = str(active_time_model_version or "")
    for row in annotations:
        if str(row.get("review_status") or "") != "accepted":
            continue
        event_id = str(row.get("ms_event_id") or "")
        if event_id not in event_id_set:
            continue
        stage = _annotation_stage(row, annotation_start_min)
        if stage not in {"qc_survey", "cell_annotation"}:
            continue
        if not active_version or str(row.get("time_model_version") or "") != active_version:
            continue
        relation = {
            "annotation_id": str(row.get("annotation_id") or ""),
            "kind": "qc" if stage == "qc_survey" else "cell",
            "lif_channel": "" if stage == "qc_survey" else str(row.get("lif_channel") or ""),
            "label": str(row.get("label") or ""),
        }
        relations[event_id].append(relation)

    points: list[dict[str, Any]] = []
    counts = {"cell": 0, "qc": 0, "unknown": 0, "conflict": 0}
    map_rows = event_map.set_index(event_map["ms_event_id"].astype(str), drop=False)
    for event_id in event_ids:
        accepted = relations[event_id]
        if not accepted:
            classification = "unknown"
            lif_channel = ""
            label = ""
        elif len(accepted) == 1:
            classification = accepted[0]["kind"]
            lif_channel = accepted[0]["lif_channel"]
            label = accepted[0]["label"]
        else:
            classification = "conflict"
            lif_channel = ""
            label = ""
        counts[classification] += 1
        source = map_rows.loc[event_id]
        scan_id = source["scan_id"]
        if isinstance(scan_id, np.generic):
            scan_id = scan_id.item()
        points.append(
            {
                "ms_event_id": event_id,
                "scan_id": scan_id,
                "scan_start_time": float(source["scan_start_time"]),
                "UMAP1": float(source["UMAP1"]),
                "UMAP2": float(source["UMAP2"]),
                "classification": classification,
                "lif_channel": lif_channel,
                "label": label,
                "accepted_relations": accepted,
            }
        )
    return {
        "points": points,
        "counts": counts,
        "active_time_model_version": active_version,
    }


def state_revision(
    *,
    project_id: str,
    map_sha256: str,
    projected_state: dict[str, Any],
) -> str:
    compact_points = [
        {
            "ms_event_id": str(point.get("ms_event_id") or ""),
            "classification": str(point.get("classification") or "unknown"),
            "lif_channel": str(point.get("lif_channel") or ""),
            "relations": [
                {
                    "annotation_id": str(relation.get("annotation_id") or ""),
                    "kind": str(relation.get("kind") or ""),
                    "lif_channel": str(relation.get("lif_channel") or ""),
                }
                for relation in point.get("accepted_relations", [])
            ],
        }
        for point in projected_state.get("points", [])
    ]
    payload = {
        "project_id": str(project_id),
        "map_sha256": str(map_sha256),
        "active_time_model_version": str(
            projected_state.get("active_time_model_version") or ""
        ),
        "points": compact_points,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
