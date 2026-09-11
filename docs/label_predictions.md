# Independent label prediction prototype

The operator exports actual window waveforms and stable IDs from an isolated LMA
project copy. Codex judges the image at runtime and returns a batch; the operator
validates it against the current saved project and writes a separate prediction
run. This is a development prototype, not a released labeling method or an
accuracy benchmark.

## Run the small loop

Use a complete, consistent project copy with an already frozen time model. The
normal LMA window response includes human annotations and automatic suggestions;
do not send that response to the predictor.

```powershell
python scripts/label_predictions.py --project C:/scratch/LMA-copy export --start 40 --width 0.5 --out C:/scratch/predictor/window1 --png
python scripts/label_predictions.py --project C:/scratch/LMA-copy save --evidence C:/scratch/predictor/window1/evidence.json --batch C:/scratch/batch.json
```

`--png` uses optional Matplotlib in the development environment. The evidence
JSON, SVG and instructions need only the existing desktop dependencies. The SVG
and PNG show sampled measured waveforms, not generated imagery. Fixed ID prefixes
are shortened only in graph labels; the complete IDs remain in JSON and results.

The batch shape is:

```json
{
  "run_id": "visual-run-1",
  "evidence_id": "copy the exported evidence_id",
  "method": {
    "route": "codex_visual",
    "model": "record actual model or explicitly mark unavailable revision",
    "prompt_version": "label-correct-visual-v1",
    "started_at": "2026-09-11T13:00:00+00:00",
    "elapsed_seconds": 30.0
  },
  "predictions": [{
    "ms_event_id": "copy a target MS ID",
    "event_version": "copy its exported version",
    "label": null,
    "lif_candidate_ids": [],
    "status": "uncertain",
    "reason": "Insufficient evidence"
  }]
}
```

Return exactly one row for every target in the window. A certain label requires
one or more valid same-class LIF candidates. Multiple MS hypotheses may cite the
same candidate because evidence support does not create one-to-one pairs. Unknown
IDs, missing rows, wrong versions, conflicting overlaps and extra review fields
are rejected. Replaying an identical batch is idempotent. A failed atomic publish
preserves any previously complete result; a leftover `.partial` requires operator
inspection rather than silent recovery.

Predictions are stored beside the bound annotation database in
`label_predictions/<run_id>.json`, with the complete allowed evidence and method
record. There is no AnnotationStore write, manual-cell-pair call or accepted-label
export change. An HTML report is emitted beside the JSON. While running this
source checkout, `/predictions?run_id=<run_id>` displays the run and rechecks its
input binding. Dashed lines mean unreviewed support, never accepted pairs.

## Identity and isolation boundaries

The binding includes saved table fingerprints, the event roster, acquisition
layout and frozen time-model/alignment state. Upstream event revisions are retained
when present. Older saved sequential IDs use an explicit `legacy-table` version
bound to their saved table; they are not fabricated v2 revisions. Changed evidence
or projection is rejected rather than rebound.

The exported fields allow only relevant LIF channels, their configured class
mapping, candidate times and IDs, MS760 trace and target/context MS events. They
exclude barcode traces, TIC/full spectrum, UMAP, human decisions, other methods'
relations and source filenames. The prototype is an **operator CLI**, not a
security sandbox. For a real blinded comparison, run the predictor with access
only to the exported directory and its output destination, with no project/API,
prior conversation or scoring access. That restricted execution has not yet been
validated. Do not describe a normal unrestricted development session as an
independently blinded evaluation.

## Windows evidence on 2026-09-11

The source is `Lin-_MPP_LMA`; all operations used
`../task2-validation/LMA-MPP-copy`. At 40–40.5 aligned minutes, Codex read the actual
PNG and supplied 8 rows (2 labels, 6 uncertain). No reference labels were scored.
The run preserves the session's self-reported model identity and wall time;
inference-only timing and exact serving revision are unavailable. Export/save
times are recorded separately; this is not a speed comparison.

The real regression rejects nine malformed/stale/conflicting cases and verifies
idempotence, reopen and unchanged authoritative project files. SQLite logical
contents match between original and copy; transient WAL/SHM sidecars are excluded
from file equivalence and were removed on opening the copy. `real_regression.json`,
the original hash list, images and suite logs live in `../task2-validation/` and
are not product source. Final automated suite: 491 tests, 489 pass, 2 skip.

Still outstanding: restricted predictor execution; multi-window runtime judgments
and tuning; independent human and physical-method runs; reliable new barcode
scoring; product navigation/import UX, packaging and native UAT. No release or
performance claim is made by this prototype.
