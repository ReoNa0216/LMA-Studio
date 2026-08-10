# Linux → Windows Current-Standard Project Transfer

Use this checklist only for a project already created with the current adaptive two-tier peak standard. A project made with the retired peak recognizer cannot be opened or upgraded in place; rebuild it from the original LIF/MS/coordinate inputs in a new empty directory.

## First principle

`annotation.sqlite` is meaningful only with the exact manifest, intermediate tables, canonical event map, and model inputs that produced its peak/event IDs. Transfer the complete project directory, never SQLite alone.

Recommended:

```text
complete Linux project -> archive -> extract on Windows -> open project directory
```

Unsafe:

```text
annotation.sqlite -> attach to separately regenerated tables
```

## Required project assets

The project root must contain:

- `lifms_project.json`
- every intermediate parquet file named in `lifms_project.json.intermediate_tables`
- the SQLite file named in `lifms_project.json.annotation_db.path`
- `results/tables/v3/00_project_protocol.json`
- `data/interim/lma/cell_event_umap.csv`

The canonical event map is a project asset with exactly:

```text
ms_event_id,scan_id,scan_start_time,UMAP1,UMAP2
```

It is not the original source coordinate CSV. The source CSV may have extra columns, but only `scan_start_time`, `UMAP1`, and `UMAP2` are imported.

Keep these with the project when present:

- `results/tables/v3/00_allowed_inputs.csv`
- `results/tables/v3/00_imported_raw_inputs.csv`
- `reports/`
- `annotation_app/annotations/exports/`
- `raw_inputs/` when the project used copy-into-project mode

External raw inputs do not need to be copied merely to browse a complete project, but they are required for exact reprocessing. Their recorded fingerprints must still describe the original files.

## Confirm the current standard before transfer

Inspect `lifms_project.json` without editing it. A current project records:

- project schema 3;
- acquisition layout 4;
- the current `lif_peak_detection` policy and its matching scientific hash;
- `calibration_protocol` and `post_qc_strategy`;
- fingerprints for every intermediate table;
- the canonical event-map binding.

Do not manually add or rewrite these fields to make an old project appear current. The peak-table metadata and hashes are checked against the manifest, so a cosmetic manifest edit is invalid and risks losing provenance.

## Linux preflight

From the project root:

```bash
set -euo pipefail
test -f lifms_project.json
test -f results/tables/v3/00_project_protocol.json
test -f data/interim/lma/cell_event_umap.csv
test -f annotation_app/annotations/annotation.sqlite
sqlite3 annotation_app/annotations/annotation.sqlite ".tables"
sqlite3 annotation_app/annotations/annotation.sqlite \
  "select review_status, count(*) from annotations group by review_status;"
```

Then verify that all parquet paths listed under `intermediate_tables` exist. Do not assume a hard-coded parquet directory if the manifest names another valid project-relative path.

## Record hashes before archiving

From the project root:

```bash
find . -type f -print0 | sort -z | xargs -0 sha256sum > project_transfer_sha256.txt
```

Keep `project_transfer_sha256.txt` outside the project tree while generating it, or exclude the file itself from the command. Store the completed hash list beside the archive.

Archive from the parent directory:

```bash
tar -czf MyProject_lma_current.tar.gz MyProject/
```

## Windows destination and verification

Extract to a normal project directory, for example:

```text
D:\LMAProjects\MyProject\
```

Keep the directory structure intact. Verify transferred hashes before opening. Then launch `dist\LMAStudio\LMAStudio.exe`, choose `打开项目`, and select the project root.

Expected behavior:

- a valid current-standard project opens without rewriting its manifest merely because it was opened;
- paths and fingerprints are validated before annotation writes;
- Track and UMAP use the transferred canonical event map;
- existing accepted/rejected/manual rows remain in the transferred SQLite database;
- a retired or malformed peak-table binding is refused before project writes, with instructions to rebuild in a new empty directory.

## Safety rules

- Never attach a transferred SQLite database to newly generated parquet tables unless every bound input/table digest is proven identical.
- Never “repair” a rejected project by hand-editing version or hash fields.
- If raw paths changed but content is identical, preserve the complete original project and first test any path repair on a full copy.
- Do not use imported identity labels or author CSVs as calibration evidence.
- Treat `export_runs` as provenance, not as new input.
- Before experimenting, make an untouched full-directory backup.

For Windows candidate behavior, use `README_CANDIDATE_v0.4.0-rc4.md` and `docs/HSC1_v0.4.0-rc4_UAT.md`.
