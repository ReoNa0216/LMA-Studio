# LMA Studio architecture notes

LMA Studio is a local desktop application for human-reviewed LIF–MS annotation. Raw inputs and generated project data remain outside the application package. Human state, model versions, and audits live in each project's SQLite database.

## Scientific boundaries

- Candidate generation uses only first-principles LIF peak, MS event, and MS scan tables.
- The event-coordinate source is reduced to the canonical whitelist: `ms_event_id`, `scan_id`, `scan_start_time`, `UMAP1`, and `UMAP2`.
- Source `Type`, `leiden`, `CellNumber`, h5ad labels, and author/manual outputs never enter matching or model fitting.
- `calibration_protocol` defines front reference segments. `post_qc_strategy` independently defines later QC survey behavior.
- Each LIF channel records detector, physical `time_axis`, scientific identity, and whether it can be used for cell annotation.
- One shift is estimated per physical axis. Multiple same-axis channels pool evidence without creating extra degrees of freedom or requiring simultaneous peaks.

## Calibration protocol

A new project must contain one or more ordered, non-overlapping project-level segments. Each segment has:

- `segment_id` and strict `order`;
- `start_min` / `end_min`;
- one or more `reference_channels`;
- derived `reference_mode`: `green_only`, `red_only`, or `red_green`;
- `boundaries_confirmed=true`, set explicitly by the user.

The new-project UI can read the selected LIF files and suggest peak-cluster windows. Suggestions are data-derived, never global constants, and always return `boundaries_confirmed=false`. Missing evidence, order/overlap conflicts, and near-equal alternatives are surfaced rather than silently resolved. Editing a suggested or confirmed boundary clears its confirmation.

Legacy v0.3 G2+R1 projects are interpreted through an in-memory compatibility adapter based on their existing `qc_anchor_channels` and configured QC end. Opening a legacy project does not rewrite its manifest.

## Post-QC policy

`post_qc_strategy.mode` is one of:

- `disabled`: no later QC candidates or manual QC writes;
- `signature`: search for the configured later reference-channel signature;
- `scheduled_windows`: search only declared ordered windows and their channels.

The strategy has its own hash. Changing it preserves prior reviews in SQLite but makes old strategy-bound rows inactive and non-exportable until recomputed.

## Three review stages

1. **前段参考校准** — review per-segment anchors and optionally refit one shift per physical axis from accepted evidence.
2. **无标签后段 delta** — estimate and inspect a generic unlabeled topology-based MS delta. It does not consume cell identity labels or post-QC identity labels.
3. **事件标注 / QC 巡检** — after the time model is frozen, review cell candidates and any enabled post-QC candidates.

Third-stage writes require the current frozen model and a canonical event-map `ms_event_id`. A single MS event cannot have simultaneous active QC and cell semantics. If G1 and G2 both match the same MS event, candidates are marked `cell_cross_channel_ambiguous`, excluded from batch acceptance, and require explicit channel arbitration.

## Invalidation rules

The frozen time model is bound to the acquisition-layout hash, calibration-protocol hash, front alignment model, annotation start, and local-delta settings.

- Changing front segment boundaries or calibration physics invalidates the applied front alignment and downstream delta/model results.
- Changing annotation start or delta seed settings invalidates dependent delta and third-stage results.
- Changing post-QC strategy invalidates only strategy-bound later QC results; it does not alter the front alignment.
- UI and API require explicit confirmation before clearing a frozen dependency.
- Existing manual annotations and their audits are preserved. Stale rows are retained for provenance but are not silently reused as current results.

## UMAP and Track synchronization

The UMAP window displays only canonical event-map points. Colors are projected from current accepted SQLite semantics. Track-to-UMAP and UMAP-to-Track messages are bound to both project ID and map SHA, and navigation uses canonical `ms_event_id`.

## Compact CSV contract

The main accepted-annotation CSV intentionally contains only 16 columns:

```text
CellNumber,scan_Id,scan_start_time,TIC,PC(34:1)_mz,PC(34:1)_intensity,
UMAP1,UMAP2,Type,annotation_kind,review_stage,LIF_channel,LIF_peak_id,
MS_event_id,residual_sec,annotation_id
```

`Type` is generated from the accepted project relation: channel identity for cell rows and `QC` for QC rows. It is never copied from the source coordinate CSV. Detailed protocol/model hashes, ambiguity alternatives, payloads, and audits remain in SQLite and `export_runs` rather than bloating the user CSV.

## Project-driven preprocessing

New projects write `results/tables/v3/00_project_protocol.json`. Both LIF and MS preprocessing use the same policy to label reference segments, the pre-annotation gap, and the annotation region. Reports and plots do not assign fixed 0–10.5 / 10.5–40 / >=40 meanings. A fixed-boundary fallback exists only for opening historical projects that lack this file.

## Development and packaging

Run tests:

```powershell
python -m unittest discover -s tests
```

Build Windows:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
```

The Windows and macOS PyInstaller specs must bundle all three preprocessing modules, including `scripts/v3/project_protocol.py`. Existing-project regressions must operate on temporary copies and compare protected SQLite, manifest, and parquet hashes before/after. Manual macOS Actions runs create candidate artifacts only; no formal Release is published before Windows user acceptance.
