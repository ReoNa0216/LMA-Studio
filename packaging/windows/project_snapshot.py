#!/usr/bin/env python3
"""Produce a read-only logical integrity snapshot for an LMA Studio project."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_table_hash(
    conn: sqlite3.Connection,
    table: str,
    *,
    order_by: str,
    ignored_columns: frozenset[str] = frozenset(),
) -> str:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    rows: list[dict[str, Any]] = []
    if exists:
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}"):
            item = {key: value for key, value in dict(row).items() if key not in ignored_columns}
            for key, value in list(item.items()):
                if key.endswith("_json") and isinstance(value, str):
                    try:
                        item[key] = json.loads(value)
                    except json.JSONDecodeError:
                        pass
            rows.append(item)
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def project_snapshot(project: Path) -> dict[str, Any]:
    project = project.expanduser().resolve()
    manifest_path = project / "lifms_project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db_path = project / manifest.get("annotation_db", {}).get(
        "path", "annotation_app/annotations/annotation.sqlite"
    )
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        for row in conn.execute("SELECT * FROM annotations ORDER BY annotation_id"):
            item = dict(row)
            for key, value in list(item.items()):
                if key.endswith("_json") and isinstance(value, str):
                    try:
                        item[key] = json.loads(value)
                    except json.JSONDecodeError:
                        pass
            rows.append(item)
        counts = dict(conn.execute("SELECT review_status, COUNT(*) FROM annotations GROUP BY review_status"))
        audit_count = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        audit_sha256 = canonical_table_hash(conn, "audit_events", order_by="audit_id")
        project_config_sha256 = canonical_table_hash(
            conn,
            "project_config",
            order_by="key",
            ignored_columns=frozenset({"updated_at", "app_version"}),
        )
        time_models_sha256 = canonical_table_hash(
            conn, "time_models", order_by="time_model_version"
        )
        time_model_audit_sha256 = canonical_table_hash(
            conn, "time_model_audit_events", order_by="audit_id"
        )
        input_manifest_sha256 = canonical_table_hash(
            conn,
            "input_manifest",
            order_by="input_key",
            # A regression copy is opened at a different absolute path. Identity is
            # carried by the input key, size, and mtime; the stored path is expected
            # to rebind to the copy.
            ignored_columns=frozenset({"relative_path", "recorded_at", "app_version"}),
        )
    finally:
        conn.close()

    annotation_blob = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    parquet_hashes = {}
    for key, entry in manifest["intermediate_tables"].items():
        path = Path(entry["path"])
        if not path.is_absolute():
            path = project / path
        parquet_hashes[key] = sha256_file(path)
    return {
        "annotations_sha256": hashlib.sha256(annotation_blob).hexdigest(),
        "annotation_count": len(rows),
        "counts": counts,
        "audit_count": audit_count,
        "audit_sha256": audit_sha256,
        "project_config_sha256": project_config_sha256,
        "time_models_sha256": time_models_sha256,
        "time_model_audit_sha256": time_model_audit_sha256,
        "input_manifest_sha256": input_manifest_sha256,
        "manifest_sha256": sha256_file(manifest_path),
        "parquets": parquet_hashes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    print(json.dumps(project_snapshot(args.project), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
