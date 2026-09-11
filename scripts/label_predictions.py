"""Operator CLI: export allowed evidence / validate and save independent predictions."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from annotation_app.app import AppData, ProjectPaths
from annotation_app.label_predictions import (
    PROMPT, build_evidence, current_review, read_json, render_svg, render_png, save_batch, write_new,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="Use an isolated project copy")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--start", type=float, required=True)
    export.add_argument("--width", type=float, default=.5)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--png", action="store_true", help="Optional Matplotlib development dependency")
    save = sub.add_parser("save")
    save.add_argument("--evidence", type=Path, required=True)
    save.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    app = AppData.load(ProjectPaths.from_args(project_dir=str(args.project)))
    if args.command == "export":
        evidence = build_evidence(app, args.start, args.width)
        args.out.mkdir(parents=True, exist_ok=False)
        write_new(args.out / "evidence.json", evidence)
        (args.out / "waveforms.svg").write_text(render_svg(evidence), encoding="utf-8")
        (args.out / "instructions.txt").write_text(PROMPT, encoding="utf-8")
        if args.png:
            render_png(evidence, args.out / "waveforms.png")
        print(json.dumps({"evidence_id": evidence["evidence_id"], "targets": sum(e["target"] for e in evidence["ms_events"]), "elapsed_seconds": time.monotonic()-started}))
    else:
        path = save_batch(app, read_json(args.evidence), read_json(args.batch))
        path.with_suffix(".html").write_text(current_review(app, path.stem), encoding="utf-8")
        print(json.dumps({"saved": str(path), "review": str(path.with_suffix('.html')), "elapsed_seconds": time.monotonic()-started}))


if __name__ == "__main__":
    main()
