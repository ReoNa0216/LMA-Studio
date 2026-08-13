# LMA Studio architecture notes

LMA Studio is a local desktop application for human-reviewed LIF–MS annotation. Raw inputs and generated project data remain outside the application package. Human state, model versions, and audits live in each project's SQLite database.

## Scientific boundaries

- Candidate generation uses only first-principles LIF peak, MS event, and MS scan tables.
- The event source is reduced to the canonical whitelist: project-owned `ms_event_id`/`scan_id`, source `scan_start_time`, and an optional `UMAP1`/`UMAP2` pair.
- Source `Type`, `leiden`, `CellNumber`, h5ad labels, and author/manual outputs never enter matching or model fitting.
- `calibration_protocol` defines front reference segments. `post_qc_strategy` independently defines later QC survey behavior.
- Each LIF channel records detector, physical `time_axis`, scientific identity, and whether it can be used for cell annotation.
- Front `QC anchor` membership and Events-stage `Cell pair` eligibility are independent. The same channel may have both uses; only one accepted semantic relation is allowed for a particular MS event.
- One shift is estimated per physical axis. Multiple same-axis channels pool evidence without creating extra degrees of freedom or requiring simultaneous peaks.

## Calibration protocol

A new project must contain one or more ordered, non-overlapping project-level segments. Each segment has:

- `segment_id` and strict `order`;
- `start_min` / `end_min`;
- one or more `reference_channels`;
- derived `reference_mode`: `green_only`, `red_only`, or `red_green`;
- `boundaries_confirmed`, set explicitly by the user after inspecting raw peak shapes.

The new-project UI can read the selected LIF files and suggest peak-cluster windows. Suggestions are data-derived, never global constants, and always return `boundaries_confirmed=false`. Numeric suggestions may be saved as a project draft so the user can inspect raw tracks before confirming them. A draft cannot compute or review front alignment, estimate/freeze downstream delta, or enter event annotation. Missing evidence, order/overlap conflicts, and near-equal alternatives are surfaced rather than silently resolved. Editing a suggested or confirmed boundary clears its confirmation.

Projects whose peak tables use the retired recognition standard are rejected before project data or SQLite is opened for mutation. Rebuild them from the original LIF/MS/coordinate inputs in a new empty directory. A narrow in-memory adapter remains only for historical calibration semantics in fixtures whose peak tables already satisfy the current recognition standard; it is not a desktop migration path.

## Post-QC policy

`post_qc_strategy.mode` is one of:

- `disabled`: no later QC candidates or manual QC writes;
- `signature`: search for the configured later reference-channel signature;
- `scheduled_windows`: search only declared ordered windows and their channels.

The strategy has its own hash. Changing it preserves prior reviews in SQLite but makes old strategy-bound rows inactive and non-exportable until recomputed.

## Three review stages

1. **Calibration** — review per-segment QC anchors and optionally refit one shift per physical axis from accepted evidence. Confirming the last draft boundary moves the UI to `Aligned` so generated connectors are immediately visible.
2. **MS Δt** — estimate and inspect a generic unlabeled topology-based MS delta. It does not consume cell identity labels or post-QC identity labels.
3. **Events / QC** — after the time model is frozen, review cell candidates and any enabled post-run QC candidates. Weak LIF peaks are selectable only here through `Cell pair`; they never become automatic evidence.

Third-stage writes require the current frozen model and a canonical event-map `ms_event_id`. A single MS event cannot have simultaneous active QC and cell semantics. If G1 and G2 both match the same MS event, candidates are marked `cell_cross_channel_ambiguous`, excluded from batch acceptance, and require explicit channel arbitration.

## Invalidation rules

The frozen time model is bound to the acquisition-layout hash, calibration-protocol hash, front alignment model, annotation start, and local-delta settings.

- Changing front segment boundaries or calibration physics invalidates the applied front alignment and downstream delta/model results.
- Changing annotation start or delta seed settings invalidates dependent delta and third-stage results.
- Changing post-QC strategy invalidates only strategy-bound later QC results; it does not alter the front alignment.
- UI and API require explicit confirmation before clearing a frozen dependency.
- Existing manual annotations and their audits are preserved. Stale rows are retained for provenance but are not silently reused as current results.

## UMAP and Track synchronization

The event source CSV must contain `scan_start_time`. `UMAP1` and `UMAP2` are optional, but must appear together; any number of unrelated columns may remain in the file. Source `CellNumber`, `batch`, `Type`, `leiden`, and other fields are ignored. Each new source time first requires a unique event apex within the strict tolerance. Only when no apex qualifies may it use one unique local peak-shape relation whose apex is no more than 0.25 seconds away; overlapping evidence remains ambiguous and is rejected. Historical schema-1 projects replay their original half-height binding semantics. Project configuration can import replacement UMAP coordinates only when the CSV resolves to exactly the same canonical MS-event population as the current map. The application writes a normalized project-internal copy and stores only content hashes, never a machine-specific external path. Switching coordinates changes UMAP and exported coordinates only; annotations, peaks, and the frozen time model remain unchanged, and a failed validation atomically preserves the current map. The UMAP window displays only canonical event-map points. Colors are projected from current accepted SQLite semantics, and the QC legend is omitted when there is no active current QC event. Track-to-UMAP and UMAP-to-Track messages are bound to both project ID and map SHA, and navigation uses canonical `ms_event_id`.

## MS event-calling compatibility

The automatic/core MS760 caller remains on the established ±12 ppm trace and keeps its existing background model. A distinct roster-support lane is evaluated only when an independently supplied event-list row has no core event. That lane may inspect PC34 evidence out to ±15 ppm, but it can create a review event only on a zero-inflated trace with a measurable local-maximum distribution, robust `Q3 + 2.35×IQR` upper fence, sufficient prominence, and the established minimum peak distance. The fence is a conservative robust bound for this gated signal regime, not a universal “4σ” identity rule. Coordinate-row count, UMAP coordinates, and source identity labels never participate in threshold selection.

Roster-supported events are whitelist-bound and manual-Cell-only. They are excluded from automatic calibration, shift/delta estimation, post-run QC, automatic Cell candidates, and model refits. Opening an existing formal v0.4 project never reruns either caller: its manifest-bound MS event and scan tables remain active, so existing annotations and candidates do not change merely because the software was upgraded. Rebuilding from raw input uses the current caller and may add manual-review evidence, while established core apices must remain unchanged.

## Compact CSV contract

The main event-roster CSV intentionally contains only 16 columns:

```text
CellNumber,scan_Id,scan_start_time,TIC,PC(34:1)_mz,PC(34:1)_intensity,
UMAP1,UMAP2,Type,annotation_kind,review_stage,LIF_channel,LIF_peak_id,
MS_event_id,residual_sec,annotation_id
```

Every canonical event-map event appears exactly once. `Type` is generated from the current accepted project relation: channel identity for cell rows, `QC` for active post-QC rows, and `unknown` when the event has not yet been annotated. It is never copied from the source coordinate CSV. Unknown rows keep annotation-specific fields blank. Detailed protocol/model hashes, ambiguity alternatives, payloads, and audits remain in SQLite and `export_runs` rather than bloating the user CSV.

## Project storage and preprocessing

New projects use a compact, manifest-bound layout:

```text
<project>/
├─ lifms_project.json
├─ data/          # four runtime parquet tables and cell_event_map.csv
├─ annotations/   # annotation.sqlite and exports/
├─ provenance/    # input manifest, project protocol, log, and report
└─ diagnostics/   # optional LIF/MS review artifacts
```

`provenance/project_protocol.json` is written before preprocessing. Both LIF and MS preprocessing read the same project policy for reference segments, the pre-annotation gap, and the annotation region; reports and plots do not assign fixed 0–10.5 / 10.5–40 / >=40 meanings. Runtime tables, the canonical event map, and SQLite are resolved only from project-relative paths declared in `lifms_project.json` and are content-bound to that manifest.

Existing v0.4.0 projects keep their historical manifest paths and are opened without directory migration. New projects do not recreate `results/`, `reports/`, `annotation_app/`, or pipeline-version subdirectories. External-reference projects are portable for browsing, annotation, and export, but require the original raw inputs for exact preprocessing reruns; copy mode additionally keeps those inputs under `raw_inputs/`.

## Development and packaging

Run tests:

```powershell
python -m unittest discover -s tests
```

Build Windows:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
```

The Windows and macOS PyInstaller specs bundle the detector, project-protocol, and project-storage helpers together with both LIF and MS preprocessing entry points. Existing-project regressions must operate on temporary copies and compare protected SQLite, manifest, and parquet hashes before/after. Manual Actions runs may publish an explicitly requested prerelease candidate; formal publication remains tag-triggered after both desktop release gates pass.

On macOS, the main Cocoa/WebKit document receives a process-random bridge capability URL. Only that exact native document receives the narrow CSP allowance required by pywebview 6.2.1 to install its JavaScript-to-Python bridge; ordinary browser pages and API responses keep the strict policy. The UMAP action waits for `pywebviewready`, passes the exact same-server `/umap` URL to the exposed `open_umap` method, and Python validates the origin and path before creating or restoring a second native WebView. It never falls back to Chrome or another external browser.
