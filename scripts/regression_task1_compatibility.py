#!/usr/bin/env python3
"""Compare saved projects through old/new code in separate disposable copies.

Usage: python scripts/regression_task1_compatibility.py --baseline-code OLD_CODE
       --output NEW_DIRECTORY PROJECT [PROJECT ...]

Use a Python environment with both checkouts' dependencies installed. The output
contains private project copies and scientific snapshots; do not commit it.
Original projects are never passed to AppData.load. No raw reanalysis occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def tree_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result[path.relative_to(root).as_posix()] = {
            "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()
        }
    return result


def changed_paths(before: dict, after: dict) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def persistent_load_changes(before: dict, after: dict) -> list[str]:
    # SQLite may remove an empty WAL and its SHM when the last reader closes.
    # Exclude only removal of these pre-existing empty sidecars, never new or
    # changed sidecars, a nonempty WAL, or changes to the database itself.
    removable = set()
    for name, entry in before.items():
        if name.endswith("-wal") and entry["size_bytes"] == 0 and name not in after:
            removable.update((name, name[:-4] + "-shm"))
    return [name for name in changed_paths(before, after)
            if not (name in removable and name not in after)]


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, np.ndarray):
        return normalize(value.tolist())
    if isinstance(value, np.generic):
        return normalize(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": str(value)}
    return value


def first_difference(old: Any, new: Any, path: str = "") -> dict | None:
    if type(old) is not type(new):
        return {"path": path, "old_type": type(old).__name__, "new_type": type(new).__name__}
    if isinstance(old, dict):
        if old.keys() != new.keys():
            return {"path": path, "old_only": sorted(old.keys() - new.keys()),
                    "new_only": sorted(new.keys() - old.keys())}
        for key in old:
            difference = first_difference(old[key], new[key], f"{path}/{key}")
            if difference:
                return difference
    elif isinstance(old, list):
        if len(old) != len(new):
            return {"path": path, "old_length": len(old), "new_length": len(new)}
        for index, (left, right) in enumerate(zip(old, new)):
            difference = first_difference(left, right, f"{path}/{index}")
            if difference:
                return difference
    elif old != new:
        return {"path": path, "old": str(old)[:350], "new": str(new)[:350]}
    return None


def physical_pairs(science: dict) -> list[dict]:
    # Keep duplicate rows and their order: a dictionary keyed by ID alone could
    # hide duplicate identities or a same-name replacement of a physical peak.
    ms = science["ms_events"]
    lif = science["lif_peaks"]
    ms_ids = ms.event_id.astype(str)
    lif_ids = lif.peak_id.astype(str)
    result = []
    for annotation in science["annotations"]:
        references = {}

        def walk(value: Any, path: str = "") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if (key.endswith("_peak_id") or key == "peak_id"
                            or path.endswith("/lif_anchor_peak_ids")) and isinstance(item, str) and item:
                        references[f"{path}/{key}"] = item
                    elif key in {"lif_anchor_peak_ids", "lif_anchors"}:
                        walk(item, f"{path}/{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}/{index}")

        walk(annotation)
        event_id = str(annotation.get("ms_event_id") or "")
        result.append({
            "annotation_id": annotation["annotation_id"], "ms_event_id": event_id,
            "ms_physical_rows": ms[ms_ids.eq(event_id)].to_dict("records"),
            "lif_physical_rows": {
                role: lif[lif_ids.eq(peak_id)].to_dict("records")
                for role, peak_id in references.items()
            },
            "review_status": annotation.get("review_status"), "label": annotation.get("label"),
        })
    return result


def worker(code: Path, project: Path, output: Path) -> None:
    # Each subprocess imports only the requested Studio checkout. Dependencies
    # come from the selected Python environment, not another Studio's internals.
    sys.path.insert(0, str(code))
    from annotation_app.app import AppData, ProjectPaths

    before = tree_snapshot(project)
    app = AppData.load(ProjectPaths.from_args(project_dir=str(project)))
    after = tree_snapshot(project)
    annotations = app.store.records()
    science = {
        "ms_events": app.ms_events, "lif_peaks": app.lif_peaks,
        "cell_event_map": app.cell_event_map, "annotations": annotations,
        "pair_interpretations": [
            app.project_saved_relation(row) for row in annotations
            if app.annotation_review_stage(row) in {"qc_survey", "cell_annotation"}
        ],
        "active_time_model": app.store.active_time_model(),
        "qc_alignment_model": app.store.qc_alignment_model(),
        "project_config": app.store.project_config(), "computed_alignment": app.alignment,
        "csv": app.export_accepted_annotations_csv()["csv_text"],
    }
    science["resolved_saved_pair_evidence"] = physical_pairs(science)
    with output.with_suffix(".pickle").open("wb") as handle:
        pickle.dump(science, handle, protocol=5)
    details = {
        "events": len(app.ms_events), "annotations": len(annotations),
        "pair_interpretations": len(science["pair_interpretations"]),
        "load_changed_paths": changed_paths(before, after),
        "load_persistent_changed_paths": persistent_load_changes(before, after),
    }
    output.with_suffix(".json").write_text(json.dumps(details, indent=2), encoding="utf-8")


def compare(old_path: Path, new_path: Path) -> dict:
    # These pickles are generated exclusively by the two child processes in the
    # newly created output directory, never supplied by a project or user file.
    with old_path.open("rb") as handle:
        old = pickle.load(handle)
    with new_path.open("rb") as handle:
        new = pickle.load(handle)
    checks = {}
    for key in old:
        try:
            if isinstance(old[key], pd.DataFrame):
                pd.testing.assert_frame_equal(old[key], new[key], check_exact=True, check_like=False)
            else:
                difference = first_difference(normalize(old[key]), normalize(new[key]))
                if difference:
                    raise AssertionError(json.dumps(difference, ensure_ascii=False))
            checks[key] = {"exact": True}
            if isinstance(old[key], pd.DataFrame):
                checks[key].update(rows=len(old[key]), columns=list(old[key].columns))
            elif isinstance(old[key], list):
                checks[key]["rows"] = len(old[key])
        except Exception as exc:
            checks[key] = {"exact": False, "difference": str(exc)[:3000]}
    return checks


def code_evidence(root: Path) -> dict:
    git = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=False)
    return {"path": str(root), "git_head": git.stdout.strip() if git.returncode == 0 else None,
            "app_sha256": hashlib.sha256((root / "annotation_app/app.py").read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-code", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("projects", nargs="+", type=Path)
    args = parser.parse_args()
    baseline = args.baseline_code.expanduser().resolve()
    output = args.output.expanduser().resolve()
    sources = [p.expanduser().resolve() for p in args.projects]
    if not (baseline / "annotation_app/app.py").is_file():
        parser.error("--baseline-code must contain annotation_app/app.py")
    if output.exists():
        parser.error("--output must be a new, nonexistent directory")
    if len(set(sources)) != len(sources):
        parser.error("duplicate project paths are not allowed")
    for source in sources:
        if output.is_relative_to(source):
            parser.error("--output must be outside every original project")
        if not (source / "lifms_project.json").is_file():
            parser.error(f"project manifest missing: {source}")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "baseline_code": code_evidence(baseline), "current_code": code_evidence(ROOT),
        "python": sys.executable,
        "scope": {
            "schema": 3, "project_count": len(sources),
            "comparison": "all saved event/peak/map fields, dtypes and order; labels, physical pair bindings, models, alignment, scientific CSV",
            "not_executed": ["raw reanalysis", "GUI", "packaging", "unprovided submission projects"],
            "private_output": "project copies and scientific snapshots; keep out of source control",
        },
        "projects": [],
    }
    for index, source in enumerate(sources, start=1):
        row = {"project": str(source), "passed": False}
        report["projects"].append(row)
        before = None
        try:
            before = tree_snapshot(source)
            manifest = json.loads((source / "lifms_project.json").read_text(encoding="utf-8"))
            row["created_by"] = manifest.get("created_by_app_version")
            row["schema"] = manifest.get("project_schema_version")
            if row["schema"] != 3:
                raise ValueError("outside this regression's current-standard schema 3 scope")
            case = output / f"{index:02d}_{source.name}"
            case.mkdir()
            for name, code in (("old", baseline), ("new", ROOT)):
                copied = case / f"{name}-project"
                shutil.copytree(source, copied)
                with (case / f"{name}-worker.log").open("w", encoding="utf-8") as log:
                    completed = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "--_worker", str(code),
                         str(copied), str(case / name)], stdout=log, stderr=subprocess.STDOUT,
                        check=False,
                    )
                if completed.returncode:
                    raise RuntimeError(f"{name} worker failed ({completed.returncode}); see {name}-worker.log")
                row[name] = json.loads((case / f"{name}.json").read_text(encoding="utf-8"))
            row["checks"] = compare(case / "old.pickle", case / "new.pickle")
            row["passed"] = (all(check["exact"] for check in row["checks"].values())
                             and not row["new"]["load_persistent_changed_paths"])
        except Exception as exc:
            row["error"] = repr(exc)
        finally:
            if before is not None:
                try:
                    changes = changed_paths(before, tree_snapshot(source))
                    row["original_unchanged"] = not changes
                    row["original_changed_paths"] = changes
                    if changes:
                        row["passed"] = False
                except Exception as exc:
                    row.update(passed=False, original_unchanged=False, source_check_error=repr(exc))
        report["passed"] = all(item["passed"] for item in report["projects"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: row[key] for key in ("project", "passed", "error") if key in row}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--_worker":
        worker(*(Path(value).resolve() for value in sys.argv[2:]))
    else:
        raise SystemExit(main())
