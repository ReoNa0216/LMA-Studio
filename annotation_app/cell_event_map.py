"""Strict import and state projection for the single-cell event map.

The source CSV is deliberately treated as an event whitelist with optional
UMAP coordinates, not as an annotation source.  Only explicitly allowed
columns are loaded.  After import, every row is bound to a stable
``ms_event_id`` and all later operations use the canonical five-column table.
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


CELL_EVENT_MAP_SCHEMA_VERSION = 2
CELL_EVENT_MAP_SUPPORTED_SCHEMA_VERSIONS = (1, 2)
CELL_EVENT_MAP_RELATIVE_PATH = "data/interim/lma/cell_event_umap.csv"
CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS = ("scan_start_time",)
CELL_EVENT_MAP_LEGACY_REQUIRED_SOURCE_COLUMNS = ("scan_start_time", "UMAP1", "UMAP2")
CELL_EVENT_MAP_COORDINATE_COLUMNS = ("UMAP1", "UMAP2")
CELL_EVENT_MAP_CANONICAL_COLUMNS = (
    "ms_event_id",
    "scan_id",
    "scan_start_time",
    "UMAP1",
    "UMAP2",
)
# One MS scan in supported acquisitions is about 0.10 s.  A 0.15 s window
# covers roughly one scan of timestamp rounding/selection difference;
# ambiguity is still rejected rather than guessed.
DEFAULT_MATCH_TOLERANCE_SEC = 0.15
# A support/basin relation is only a two-scan timing correction, never a
# license to attach an arbitrary roster time somewhere inside a broad
# prominence basin.  Real HSC2 inputs require at most 0.207 s; 0.25 s leaves
# one bounded margin while keeping this shape-based fallback strictly local.
MAX_PEAK_SHAPE_APEX_OFFSET_SEC = 0.25
MATCH_POLICY = "apex_tolerance_then_unique_near_peak_shape_v3"
PRIMARY_EVENT_STRATEGY = "pc34_primary"
ROSTER_SUPPORTED_EVENT_STRATEGY = "pc34_roster_supported"
EVENT_MAP_EVENT_STRATEGIES = (
    PRIMARY_EVENT_STRATEGY,
    ROSTER_SUPPORTED_EVENT_STRATEGY,
)
PRIMARY_SIGNAL_COLUMN = "pc34_760_max_intensity"
EVENT_MAP_DIAGNOSTIC_FILENAME = "event-map-import-diagnostics.csv"
EVENT_MAP_DIAGNOSTIC_COLUMNS = (
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
)


class CellEventMapError(ValueError):
    """The source map or its project binding is invalid."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic_rows: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_rows = [
            {column: row.get(column, "") for column in EVENT_MAP_DIAGNOSTIC_COLUMNS}
            for row in (diagnostic_rows or [])
        ]

    def diagnostic_payload(self) -> dict[str, Any] | None:
        """Return the safe, serializable report exposed after a failed import."""

        if not self.diagnostic_rows:
            return None
        rows = [dict(row) for row in self.diagnostic_rows]
        status_counts = {
            status: sum(1 for row in rows if row["Status"] == status)
            for status in ("matched", "unmatched", "ambiguous", "conflict")
        }
        report = pd.DataFrame(rows, columns=list(EVENT_MAP_DIAGNOSTIC_COLUMNS))
        csv_text = report.to_csv(
            index=False,
            lineterminator="\n",
            float_format="%.15g",
            na_rep="",
        )
        return {
            "filename": EVENT_MAP_DIAGNOSTIC_FILENAME,
            "columns": list(EVENT_MAP_DIAGNOSTIC_COLUMNS),
            "summary": {
                "total_rows": len(rows),
                "matched_rows": status_counts["matched"],
                "unmatched_rows": status_counts["unmatched"],
                "ambiguous_rows": status_counts["ambiguous"],
                "conflict_rows": status_counts["conflict"],
            },
            "rows": rows,
            "csv_text": csv_text,
        }


def _event_map_diagnostic_row(
    *,
    source_line: int,
    source_time: float,
    status: str,
    reason_code: str,
    reason: str,
    match_method: str = "",
    matched_event_id: str = "",
    matched_event_time: float | str = "",
    apex_offset_sec: float | str = "",
    candidate_event_ids: Iterable[str] = (),
    nearest_event_id: str = "",
    nearest_event_time: float | str = "",
    nearest_offset_sec: float | str = "",
) -> dict[str, Any]:
    return {
        "CSVLine": int(source_line),
        "scan_start_time": float(source_time),
        "Status": str(status),
        "ReasonCode": str(reason_code),
        "Reason": str(reason),
        "MatchMethod": str(match_method),
        "MatchedEventID": str(matched_event_id),
        "MatchedEventTimeMin": matched_event_time,
        "ApexOffsetSec": apex_offset_sec,
        "CandidateEventIDs": ";".join(str(value) for value in candidate_event_ids),
        "NearestEventID": str(nearest_event_id),
        "NearestEventTimeMin": nearest_event_time,
        "NearestOffsetSec": nearest_offset_sec,
    }


def _nearest_event_diagnostic(
    events: pd.DataFrame,
    source_time: float,
) -> tuple[str, float | str, float | str]:
    if events.empty:
        return "", "", ""
    event_times = events["time_min"].to_numpy(dtype=float)
    nearest_index = int(np.argmin(np.abs(event_times - float(source_time))))
    event = events.iloc[nearest_index]
    event_time = float(event["time_min"])
    return (
        str(event["event_id"]),
        event_time,
        abs(event_time - float(source_time)) * 60.0,
    )


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
    """Load event times plus an optional, complete UMAP coordinate pair.

    ``UMAP`` is accepted as an explicit alias for ``UMAP1`` only when paired
    with ``UMAP2``.  No cluster, label, or other numeric column is guessed as a
    coordinate.
    """

    path = path.expanduser().resolve()
    if not path.is_file():
        raise CellEventMapError(f"事件坐标 CSV 不存在: {path}")
    header = _csv_header(path)
    scan_count = header.count("scan_start_time")
    if scan_count != 1:
        raise CellEventMapError(
            "事件表必需列必须出现一次: " f"scan_start_time={scan_count}"
        )
    coordinate_counts = {
        column: header.count(column) for column in ("UMAP1", "UMAP", "UMAP2")
    }
    duplicated_coordinates = {
        column: count for column, count in coordinate_counts.items() if count > 1
    }
    if duplicated_coordinates:
        details = ", ".join(
            f"{column}={count}" for column, count in duplicated_coordinates.items()
        )
        raise CellEventMapError(f"UMAP 坐标列不能重复: {details}")

    exact_pair = coordinate_counts["UMAP1"] == 1 and coordinate_counts["UMAP2"] == 1
    alias_pair = coordinate_counts["UMAP"] == 1 and coordinate_counts["UMAP2"] == 1
    no_coordinates = all(count == 0 for count in coordinate_counts.values())
    if exact_pair and coordinate_counts["UMAP"] == 0:
        coordinate_mapping = {"UMAP1": "UMAP1", "UMAP2": "UMAP2"}
    elif alias_pair and coordinate_counts["UMAP1"] == 0:
        coordinate_mapping = {"UMAP1": "UMAP", "UMAP2": "UMAP2"}
    elif no_coordinates:
        coordinate_mapping = {}
    else:
        raise CellEventMapError(
            "UMAP1（或 UMAP）与 UMAP2 必须成对提供；"
            + ", ".join(
                f"{column}={coordinate_counts[column]}"
                for column in ("UMAP1", "UMAP", "UMAP2")
            )
        )

    selected_source_columns = ["scan_start_time", *coordinate_mapping.values()]

    try:
        # Security boundary: never load Type/leiden/CellNumber or any other
        # source column. Header inspection above checks names only.
        frame = pd.read_csv(
            path,
            usecols=selected_source_columns,
            encoding="utf-8-sig",
        )
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, ValueError) as exc:
        raise CellEventMapError(f"无法解析事件坐标 CSV: {exc}") from exc

    if frame.empty:
        raise CellEventMapError("事件坐标 CSV 没有数据行")
    frame = frame.loc[:, selected_source_columns].copy()
    if coordinate_mapping:
        frame = frame.rename(
            columns={source: canonical for canonical, source in coordinate_mapping.items()}
        )
    else:
        frame["UMAP1"] = np.nan
        frame["UMAP2"] = np.nan
    frame = frame.loc[:, ["scan_start_time", "UMAP1", "UMAP2"]]
    numeric_columns = ["scan_start_time", *CELL_EVENT_MAP_COORDINATE_COLUMNS]
    for column in numeric_columns:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if column == "scan_start_time" or coordinate_mapping:
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
    frame.attrs["coordinates_available"] = bool(coordinate_mapping)
    frame.attrs["source_coordinate_columns"] = coordinate_mapping
    return frame


def primary_ms_events(ms_events: pd.DataFrame) -> pd.DataFrame:
    required = {"event_id", "event_strategy", "primary_signal_col", "scan_id", "time_min"}
    missing = sorted(required - set(ms_events.columns))
    if missing:
        raise CellEventMapError(f"MS event 表缺少必需列: {', '.join(missing)}")
    selected_columns = ["event_id", "scan_id", "time_min"]
    has_peak_support = {"left_sec", "right_sec"}.issubset(ms_events.columns)
    if has_peak_support:
        selected_columns.extend(["left_sec", "right_sec"])
    has_peak_basin = {"left_base_sec", "right_base_sec"}.issubset(ms_events.columns)
    if has_peak_basin:
        selected_columns.extend(["left_base_sec", "right_base_sec"])
    selected = ms_events[
        ms_events["event_strategy"].astype(str).isin(EVENT_MAP_EVENT_STRATEGIES)
        & ms_events["primary_signal_col"].astype(str).eq(PRIMARY_SIGNAL_COLUMN)
    ][selected_columns].copy()
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
    if has_peak_support:
        selected["left_sec"] = pd.to_numeric(selected["left_sec"], errors="coerce")
        selected["right_sec"] = pd.to_numeric(selected["right_sec"], errors="coerce")
        left_sec = selected["left_sec"].to_numpy(dtype=float, na_value=np.nan)
        right_sec = selected["right_sec"].to_numpy(dtype=float, na_value=np.nan)
        selected["_peak_support_available"] = (
            np.isfinite(left_sec)
            & np.isfinite(right_sec)
            & (left_sec <= right_sec)
        )
    if has_peak_basin:
        selected["left_base_sec"] = pd.to_numeric(
            selected["left_base_sec"], errors="coerce"
        )
        selected["right_base_sec"] = pd.to_numeric(
            selected["right_base_sec"], errors="coerce"
        )
        left_base_sec = selected["left_base_sec"].to_numpy(
            dtype=float, na_value=np.nan
        )
        right_base_sec = selected["right_base_sec"].to_numpy(
            dtype=float, na_value=np.nan
        )
        selected["_peak_basin_available"] = (
            np.isfinite(left_base_sec)
            & np.isfinite(right_base_sec)
            & (left_base_sec <= right_base_sec)
        )
    return selected.sort_values(["time_min", "event_id"], kind="stable").reset_index(drop=True)


def _candidate_indices_for_source_time(
    events: pd.DataFrame,
    source_time_min: float,
    *,
    tolerance_sec: float,
    include_peak_basin: bool = True,
    max_peak_shape_apex_offset_sec: float | None = MAX_PEAK_SHAPE_APEX_OFFSET_SEC,
) -> tuple[list[int], str]:
    """Return event-map candidates using the same policy as final binding."""

    event_times = events["time_min"].to_numpy(dtype=float)
    tolerance_min = float(tolerance_sec) / 60.0
    left = int(
        np.searchsorted(event_times, source_time_min - tolerance_min, side="left")
    )
    right = int(
        np.searchsorted(event_times, source_time_min + tolerance_min, side="right")
    )
    candidate_indices = [
        index
        for index in range(left, right)
        if abs(float(event_times[index]) - source_time_min) * 60.0
        <= float(tolerance_sec) + 1e-12
    ]
    if candidate_indices:
        return candidate_indices, "apex_tolerance"

    support_columns = {
        "left_sec",
        "right_sec",
        "_peak_support_available",
    }
    if not support_columns.issubset(events.columns):
        return [], "apex_tolerance"
    source_sec = source_time_min * 60.0
    apex_offset_sec = np.abs(event_times * 60.0 - source_sec)
    if max_peak_shape_apex_offset_sec is None:
        shape_offset_ok = np.ones(len(events), dtype=bool)
    else:
        maximum_offset = float(max_peak_shape_apex_offset_sec)
        if not math.isfinite(maximum_offset) or maximum_offset <= 0:
            raise CellEventMapError("峰形匹配的峰顶距离上限必须是正有限秒数")
        shape_offset_ok = apex_offset_sec <= maximum_offset + 1e-12
    support_available = events["_peak_support_available"].to_numpy(dtype=bool)
    support_left_sec = events["left_sec"].to_numpy(dtype=float)
    support_right_sec = events["right_sec"].to_numpy(dtype=float)
    candidate_indices = np.flatnonzero(
        support_available
        & shape_offset_ok
        & (support_left_sec <= source_sec + 1e-12)
        & (source_sec <= support_right_sec + 1e-12)
    ).tolist()
    if candidate_indices:
        return candidate_indices, "peak_support"

    if not include_peak_basin:
        return [], "peak_support"
    basin_columns = {
        "left_base_sec",
        "right_base_sec",
        "_peak_basin_available",
    }
    if not basin_columns.issubset(events.columns):
        return [], "peak_support"
    basin_available = events["_peak_basin_available"].to_numpy(dtype=bool)
    basin_left_sec = events["left_base_sec"].to_numpy(dtype=float)
    basin_right_sec = events["right_base_sec"].to_numpy(dtype=float)
    candidate_indices = np.flatnonzero(
        basin_available
        & shape_offset_ok
        & (basin_left_sec <= source_sec + 1e-12)
        & (source_sec <= basin_right_sec + 1e-12)
    ).tolist()
    return candidate_indices, "peak_basin"


def source_event_candidates(
    source_coordinates: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    tolerance_sec: float = DEFAULT_MATCH_TOLERANCE_SEC,
    include_peak_basin: bool = True,
    max_peak_shape_apex_offset_sec: float | None = MAX_PEAK_SHAPE_APEX_OFFSET_SEC,
) -> list[dict[str, Any]]:
    """Inspect candidate relations without accepting or guessing a match.

    This is used by project creation to identify only the roster rows that
    lack a conservative core event.  The final import still performs the
    complete one-to-one and ambiguity validation.
    """

    if not math.isfinite(float(tolerance_sec)) or float(tolerance_sec) <= 0:
        raise CellEventMapError("match tolerance 必须是正有限秒数")
    if "scan_start_time" not in source_coordinates.columns:
        raise CellEventMapError("坐标表缺少必需列: scan_start_time")
    events = primary_ms_events(ms_events)
    relations: list[dict[str, Any]] = []
    for frame_index, source_row in source_coordinates.reset_index(drop=True).iterrows():
        source_time = float(source_row["scan_start_time"])
        indices, method = _candidate_indices_for_source_time(
            events,
            source_time,
            tolerance_sec=float(tolerance_sec),
            include_peak_basin=bool(include_peak_basin),
            max_peak_shape_apex_offset_sec=max_peak_shape_apex_offset_sec,
        )
        relations.append(
            {
                "source_line": int(frame_index) + 2,
                "scan_start_time": source_time,
                "candidate_event_ids": events.iloc[indices]["event_id"].astype(str).tolist(),
                "match_method": method,
            }
        )
    return relations


def match_source_to_events(
    source_coordinates: pd.DataFrame,
    ms_events: pd.DataFrame,
    *,
    tolerance_sec: float = DEFAULT_MATCH_TOLERANCE_SEC,
    include_peak_basin: bool = True,
    max_peak_shape_apex_offset_sec: float | None = MAX_PEAK_SHAPE_APEX_OFFSET_SEC,
) -> pd.DataFrame:
    """Bind every source row to exactly one distinct primary MS760 event."""

    if not math.isfinite(float(tolerance_sec)) or float(tolerance_sec) <= 0:
        raise CellEventMapError("match tolerance 必须是正有限秒数")
    missing_source = sorted(set(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS) - set(source_coordinates.columns))
    if missing_source:
        raise CellEventMapError(f"坐标表缺少必需列: {', '.join(missing_source)}")
    source_coordinates = source_coordinates.copy()
    has_umap1 = "UMAP1" in source_coordinates.columns
    has_umap2 = "UMAP2" in source_coordinates.columns
    if has_umap1 != has_umap2:
        raise CellEventMapError("UMAP1 与 UMAP2 必须成对提供")
    if not has_umap1:
        source_coordinates["UMAP1"] = np.nan
        source_coordinates["UMAP2"] = np.nan
    coordinate_values = source_coordinates[["UMAP1", "UMAP2"]].apply(
        pd.to_numeric,
        errors="coerce",
    )
    coordinate_finite = np.isfinite(coordinate_values.to_numpy(dtype=float))
    coordinates_available = bool(coordinate_finite.all())
    coordinates_missing = bool((~coordinate_finite).all())
    if not coordinates_available and not coordinates_missing:
        raise CellEventMapError("UMAP1 与 UMAP2 必须为完整有限坐标，或整表同时留空")
    source_coordinates[["UMAP1", "UMAP2"]] = coordinate_values
    try:
        events = primary_ms_events(ms_events)
    except CellEventMapError as exc:
        required_event_columns = {
            "event_id",
            "event_strategy",
            "primary_signal_col",
            "scan_id",
            "time_min",
        }
        if required_event_columns.issubset(ms_events.columns):
            diagnostics = [
                _event_map_diagnostic_row(
                    source_line=int(index) + 2,
                    source_time=float(row["scan_start_time"]),
                    status="unmatched",
                    reason_code="no_recognized_ms760_events",
                    reason="没有识别到可用于事件名单绑定的 MS760 峰。",
                )
                for index, row in source_coordinates.reset_index(drop=True).iterrows()
            ]
            raise CellEventMapError(str(exc), diagnostic_rows=diagnostics) from exc
        raise
    matches: list[dict[str, Any]] = []
    unmatched_rows: list[int] = []
    ambiguous_rows: list[tuple[int, list[str]]] = []
    diagnostics_by_line: dict[int, dict[str, Any]] = {}
    apex_tolerance_match_count = 0
    peak_support_match_count = 0
    peak_basin_match_count = 0
    apex_offsets_sec: list[float] = []

    for frame_index, source_row in source_coordinates.reset_index(drop=True).iterrows():
        source_line = int(frame_index) + 2
        source_time = float(source_row["scan_start_time"])
        candidate_indices, match_method = _candidate_indices_for_source_time(
            events,
            source_time,
            tolerance_sec=float(tolerance_sec),
            include_peak_basin=bool(include_peak_basin),
            max_peak_shape_apex_offset_sec=max_peak_shape_apex_offset_sec,
        )
        nearest_event_id, nearest_event_time, nearest_offset_sec = (
            _nearest_event_diagnostic(events, source_time)
        )
        if not candidate_indices:
            unmatched_rows.append(source_line)
            reason_code = "no_eligible_event"
            reason = "附近没有满足时间与峰形约束的唯一 MS760 峰。"
            diagnostic_method = match_method
            diagnostic_candidate_ids: list[str] = []
            if max_peak_shape_apex_offset_sec is not None:
                uncapped_indices, uncapped_method = _candidate_indices_for_source_time(
                    events,
                    source_time,
                    tolerance_sec=float(tolerance_sec),
                    include_peak_basin=bool(include_peak_basin),
                    max_peak_shape_apex_offset_sec=None,
                )
                if uncapped_indices:
                    reason_code = "peak_shape_too_far"
                    reason = (
                        "该时间落在较宽峰形范围内，但与峰顶距离超过安全上限，"
                        "因此未自动绑定。"
                    )
                    diagnostic_method = uncapped_method
                    diagnostic_candidate_ids = (
                        events.iloc[uncapped_indices]["event_id"].astype(str).tolist()
                    )
            tolerance_min = float(tolerance_sec) / 60.0
            if (
                source_time < float(events["time_min"].min()) - tolerance_min
                or source_time > float(events["time_min"].max()) + tolerance_min
            ):
                reason_code = "outside_recognized_event_range"
                reason = "该时间超出当前已识别 MS760 事件的时间范围。"
            diagnostics_by_line[source_line] = _event_map_diagnostic_row(
                source_line=source_line,
                source_time=source_time,
                status="unmatched",
                reason_code=reason_code,
                reason=reason,
                match_method=diagnostic_method,
                candidate_event_ids=diagnostic_candidate_ids,
                nearest_event_id=nearest_event_id,
                nearest_event_time=nearest_event_time,
                nearest_offset_sec=nearest_offset_sec,
            )
            continue
        if len(candidate_indices) > 1:
            candidate_ids = (
                events.iloc[candidate_indices]["event_id"].astype(str).tolist()
            )
            ambiguous_rows.append(
                (source_line, candidate_ids)
            )
            diagnostics_by_line[source_line] = _event_map_diagnostic_row(
                source_line=source_line,
                source_time=source_time,
                status="ambiguous",
                reason_code="ambiguous_multiple_events",
                reason="同一 CSV 行附近存在多个合格 MS760 峰，软件不会猜测。",
                match_method=match_method,
                candidate_event_ids=candidate_ids,
                nearest_event_id=nearest_event_id,
                nearest_event_time=nearest_event_time,
                nearest_offset_sec=nearest_offset_sec,
            )
            continue
        event = events.iloc[candidate_indices[0]]
        apex_offset_sec = abs(float(event["time_min"]) - source_time) * 60.0
        apex_offsets_sec.append(apex_offset_sec)
        if match_method == "peak_basin":
            peak_basin_match_count += 1
            matched_reason_code = "matched_peak_basin"
            matched_reason = "通过唯一且邻近的峰底范围匹配。"
        elif match_method == "peak_support":
            peak_support_match_count += 1
            matched_reason_code = "matched_peak_support"
            matched_reason = "通过唯一且邻近的半峰高范围匹配。"
        else:
            apex_tolerance_match_count += 1
            matched_reason_code = "matched_apex"
            matched_reason = "峰顶时间在允许误差内唯一匹配。"
        diagnostics_by_line[source_line] = _event_map_diagnostic_row(
            source_line=source_line,
            source_time=source_time,
            status="matched",
            reason_code=matched_reason_code,
            reason=matched_reason,
            match_method=match_method,
            matched_event_id=str(event["event_id"]),
            matched_event_time=float(event["time_min"]),
            apex_offset_sec=apex_offset_sec,
            candidate_event_ids=[str(event["event_id"])],
            nearest_event_id=nearest_event_id,
            nearest_event_time=nearest_event_time,
            nearest_offset_sec=nearest_offset_sec,
        )
        matches.append(
            {
                "source_line": source_line,
                "ms_event_id": str(event["event_id"]),
                "scan_id": event["scan_id"],
                "scan_start_time": source_time,
                "UMAP1": (
                    float(source_row["UMAP1"]) if coordinates_available else np.nan
                ),
                "UMAP2": (
                    float(source_row["UMAP2"]) if coordinates_available else np.nan
                ),
            }
        )

    matched = pd.DataFrame(
        matches,
        columns=[
            "source_line",
            "ms_event_id",
            "scan_id",
            "scan_start_time",
            "UMAP1",
            "UMAP2",
        ],
    )
    reused = matched[matched["ms_event_id"].duplicated(keep=False)]
    reused_details: list[str] = []
    if not reused.empty:
        for event_id, rows in reused.groupby("ms_event_id", sort=True):
            source_lines = [int(value) for value in rows["source_line"]]
            reused_details.append(
                f"{event_id} <- CSV 行 {','.join(str(value) for value in source_lines)}"
            )
            for source_line in source_lines:
                diagnostic = diagnostics_by_line[source_line]
                diagnostic["Status"] = "conflict"
                diagnostic["ReasonCode"] = "reused_event"
                diagnostic["Reason"] = (
                    "多个 CSV 行指向同一 MS760 事件；一对一绑定要求禁止复用。"
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
        failures.append("存在多个 MS event 可匹配: " + preview)
    if reused_details:
        failures.append("两个 CSV 行不能复用同一 MS event: " + "; ".join(reused_details[:10]))
    if failures:
        raise CellEventMapError(
            "；".join(failures),
            diagnostic_rows=[
                diagnostics_by_line[line] for line in sorted(diagnostics_by_line)
            ],
        )
    if len(matched) != len(source_coordinates):
        raise CellEventMapError(
            "事件坐标 CSV 未能整体一对一匹配",
            diagnostic_rows=[
                diagnostics_by_line[line] for line in sorted(diagnostics_by_line)
            ],
        )
    canonical = matched.loc[:, ["ms_event_id", "scan_id", "scan_start_time", "UMAP1", "UMAP2"]]
    canonical = canonical.sort_values(["scan_start_time", "ms_event_id"], kind="stable").reset_index(drop=True)
    canonical.attrs["match_diagnostics"] = {
        "match_policy": MATCH_POLICY,
        "apex_tolerance_match_count": int(apex_tolerance_match_count),
        "peak_support_match_count": int(peak_support_match_count),
        "peak_basin_match_count": int(peak_basin_match_count),
        "max_apex_offset_sec": float(max(apex_offsets_sec, default=0.0)),
        "coordinates_available": coordinates_available,
    }
    return canonical


def import_cell_event_map(
    source_path: Path,
    ms_events: pd.DataFrame,
    *,
    tolerance_sec: float = DEFAULT_MATCH_TOLERANCE_SEC,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source_path = source_path.expanduser().resolve()
    coordinates = read_source_coordinates(source_path)
    coordinates_available = bool(coordinates.attrs.get("coordinates_available", False))
    source_coordinate_columns = dict(
        coordinates.attrs.get("source_coordinate_columns") or {}
    )
    canonical = match_source_to_events(coordinates, ms_events, tolerance_sec=tolerance_sec)
    diagnostics = canonical.attrs.get("match_diagnostics", {})
    return canonical, {
        "schema_version": CELL_EVENT_MAP_SCHEMA_VERSION,
        "source_name": source_path.name,
        "source_sha256": sha256_file(source_path),
        "row_count": int(len(canonical)),
        "matched_event_count": int(canonical["ms_event_id"].nunique()),
        "time_unit": "min",
        "match_tolerance_sec": float(tolerance_sec),
        "match_policy": str(diagnostics.get("match_policy") or MATCH_POLICY),
        "apex_tolerance_match_count": int(
            diagnostics.get("apex_tolerance_match_count", len(canonical))
        ),
        "peak_support_match_count": int(diagnostics.get("peak_support_match_count", 0)),
        "peak_basin_match_count": int(diagnostics.get("peak_basin_match_count", 0)),
        "max_apex_offset_sec": float(diagnostics.get("max_apex_offset_sec", 0.0)),
        "required_source_columns": list(CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS),
        "coordinates_available": coordinates_available,
        "source_coordinate_columns": source_coordinate_columns,
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
        na_rep="",
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


def read_canonical_map(
    path: Path,
    *,
    expected_sha256: str | None = None,
    allow_missing_coordinates: bool = False,
) -> pd.DataFrame:
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
    frame["scan_start_time"] = pd.to_numeric(frame["scan_start_time"], errors="coerce")
    if not np.isfinite(
        frame["scan_start_time"].to_numpy(dtype=float, na_value=np.nan)
    ).all():
        raise CellEventMapError("canonical event map 的 scan_start_time 含 NaN/Inf")
    coordinates = frame.loc[:, list(CELL_EVENT_MAP_COORDINATE_COLUMNS)].apply(
        pd.to_numeric,
        errors="coerce",
    )
    coordinate_finite = np.isfinite(coordinates.to_numpy(dtype=float))
    coordinates_available = bool(coordinate_finite.all())
    coordinates_missing = bool((~coordinate_finite).all())
    if not coordinates_available:
        if not allow_missing_coordinates or not coordinates_missing:
            raise CellEventMapError(
                "canonical event map 的 UMAP1/UMAP2 含 NaN/Inf 或坐标不完整"
            )
    frame.loc[:, list(CELL_EVENT_MAP_COORDINATE_COLUMNS)] = coordinates
    result = frame.loc[:, list(CELL_EVENT_MAP_CANONICAL_COLUMNS)].copy()
    result.attrs["coordinates_available"] = coordinates_available
    return result


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
        "schema_version": int(
            import_metadata.get("schema_version", CELL_EVENT_MAP_SCHEMA_VERSION)
        ),
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
        "match_policy": str(import_metadata.get("match_policy") or MATCH_POLICY),
        "apex_tolerance_match_count": int(
            import_metadata.get(
                "apex_tolerance_match_count",
                import_metadata.get("row_count", 0),
            )
        ),
        "peak_support_match_count": int(import_metadata.get("peak_support_match_count", 0)),
        "peak_basin_match_count": int(import_metadata.get("peak_basin_match_count", 0)),
        "max_apex_offset_sec": float(import_metadata.get("max_apex_offset_sec", 0.0)),
        "required_source_columns": list(
            import_metadata.get(
                "required_source_columns",
                CELL_EVENT_MAP_REQUIRED_SOURCE_COLUMNS,
            )
        ),
        "coordinates_available": bool(
            import_metadata.get("coordinates_available", False)
        ),
        "source_coordinate_columns": dict(
            import_metadata.get("source_coordinate_columns") or {}
        ),
    }
    if not entry["source_sha256"]:
        raise CellEventMapError("event map import metadata 缺少 source_sha256")
    if entry["row_count"] <= 0 or entry["matched_event_count"] != entry["row_count"]:
        raise CellEventMapError("event map manifest 需要完整的一对一匹配计数")
    for key in ("core_event_count", "roster_supported_event_count"):
        if key in import_metadata:
            entry[key] = int(import_metadata[key])
    support_model = str(import_metadata.get("event_roster_support_model") or "")
    if support_model:
        entry["event_roster_support_model"] = support_model
    review_model = str(import_metadata.get("event_roster_review_model") or "")
    if review_model:
        entry["event_roster_review_model"] = review_model
    for key in (
        "event_roster_support_height",
        "event_roster_support_prominence",
        "event_roster_review_height",
        "event_roster_review_prominence",
    ):
        value = import_metadata.get(key)
        if value is not None and math.isfinite(float(value)):
            entry[key] = float(value)
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
        umap1 = float(source["UMAP1"])
        umap2 = float(source["UMAP2"])
        coordinates_available = math.isfinite(umap1) and math.isfinite(umap2)
        points.append(
            {
                "ms_event_id": event_id,
                "scan_id": scan_id,
                "scan_start_time": float(source["scan_start_time"]),
                "UMAP1": umap1 if coordinates_available else None,
                "UMAP2": umap2 if coordinates_available else None,
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
        "coordinates_available": bool(
            points
            and all(
                point["UMAP1"] is not None and point["UMAP2"] is not None
                for point in points
            )
        ),
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
