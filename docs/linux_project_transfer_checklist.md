# Linux LIF-MS Annotation Project Transfer Checklist

This checklist is for moving an already annotated Linux project to the Windows
packaged LIF-MS annotation app without redrawing annotations.

The first-principles rule is:

> Annotation SQLite is valid only for the exact raw inputs, preprocessing outputs,
> peak/event IDs, channel identity map, and time model that produced it.

Do not treat `annotation.sqlite` as a standalone project. Move it together with
the matching project data.

## Recommended Decision

Use the Linux project as a complete project package.

Good:

```text
Linux project directory -> archive -> Windows project directory -> open project
```

Avoid:

```text
Only copy annotation.sqlite -> attach it to a newly generated Windows project
```

The second path can silently attach annotations to the wrong LIF peak or MS
event if IDs differ.

## Required Files

From the Linux project root, these files must exist:

```text
data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet
data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet
data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet
data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet
annotation_app/annotations/annotation.sqlite
```

These four parquet files define the LIF peaks and MS events that the SQLite
annotations point to.

## Strongly Recommended Files

These should also be moved with the project:

```text
lifms_project.json
results/tables/v3/00_allowed_inputs.csv
results/tables/v3/00_imported_raw_inputs.csv
reports/import_project.md
reports/import_preprocess.log
annotation_app/annotations/exports/
```

`raw_inputs/` is optional. The Windows app now supports two raw input modes:

- `external_reference`: raw files stay outside the project and
  `lifms_project.json` records absolute source paths plus fingerprints. This is
  the recommended mode when the MS raw text is very large.
- `copy_into_project`: raw files are copied under `raw_inputs/` for a
  self-contained archival project.

In both modes, project opening depends on the four parquet files and
`annotation.sqlite`. Missing external raw files should not prevent browsing
existing annotations, but it will prevent exact reprocessing from raw inputs.

## Quick Linux Check

Run this from the Linux project root:

```bash
set -e

test -f data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet
test -f data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet
test -f data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet
test -f data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet
test -f annotation_app/annotations/annotation.sqlite

test -f results/tables/v3/00_imported_raw_inputs.csv || true
test -f lifms_project.json || true

echo "Required project files are present."
```

If the required parquet or SQLite checks fail, do not move only the SQLite.
First identify what is missing.

## SQLite Table Check

Run:

```bash
sqlite3 annotation_app/annotations/annotation.sqlite ".tables"
```

Expected core tables:

```text
annotations
audit_events
input_manifest
project_config
export_runs
```

Depending on the version and workflow, there may also be time-model related
tables. That is expected.

Then run:

```bash
sqlite3 annotation_app/annotations/annotation.sqlite \
  "select review_status, count(*) from annotations group by review_status;"
```

This gives a quick summary of accepted, pending, and rejected records.

## Optional Hash Manifest

For a stronger transfer record, generate hashes before archiving:

```bash
paths="data/interim annotation_app/annotations"
test -d raw_inputs && paths="$paths raw_inputs"
find $paths \
  -type f \
  \( -name '*.parquet' -o -name '*.sqlite' -o -name '*.csv' -o -name '*.txt' \) \
  -print0 | sort -z | xargs -0 sha256sum > project_transfer_sha256.txt
```

Move `project_transfer_sha256.txt` with the project. On Windows, hashes can be
verified later if needed.

## Archive The Project

From the parent directory of the project:

```bash
tar -czf Batch03_lifms_project.tar.gz Batch03/
```

Replace `Batch03/` with the actual project directory name.

Do not archive only `annotation_app/annotations/annotation.sqlite`.

## Windows Destination

Extract to a normal project location, for example:

```text
D:\LIFMSProjects\Batch03\
```

The expected structure on Windows should be:

```text
D:\LIFMSProjects\Batch03\
  data\interim\v3\01_lif_trace_physical_qc\v3_01_lif_traces.parquet
  data\interim\v3\01_lif_trace_physical_qc\v3_01_lif_peaks.parquet
  data\interim\v3\02_ms_event_calling\v3_02_ms_events.parquet
  data\interim\v3\02_ms_event_calling\v3_02_ms_scan_summary.parquet
  annotation_app\annotations\annotation.sqlite
  lifms_project.json
  results\tables\v3\
  reports\
```

`raw_inputs\` may also exist if the project uses `copy_into_project`.

## Current Windows App Opening Method

Use the app's "打开项目" button and select the project directory.

The command-line fallback is:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1 `
  -ProjectDir "D:\LIFMSProjects\Batch03"
```

Both methods expect:

```text
D:\LIFMSProjects\Batch03\annotation_app\annotations\annotation.sqlite
```

and the four parquet files at the standard paths above.

## `lifms_project.json`

The professional project format includes a manifest file:

```text
lifms_project.json
```

Recommended fields:

```json
{
  "project_id": "uuid",
  "dataset_id": "stable dataset uuid or hash",
  "project_schema_version": 1,
  "created_by_app_version": "annotation_app_mvp1_review_sqlite",
  "raw_inputs": {
    "lif_g2": {
      "path": "D:\\RawData\\lif_g2.csv",
      "path_mode": "external_reference",
      "size_bytes": 0,
      "original_source_path": "/server/path/to/source"
    },
    "lif_r1": {
      "path": "D:\\RawData\\lif_r1.csv",
      "path_mode": "external_reference",
      "size_bytes": 0,
      "original_source_path": "/server/path/to/source"
    },
    "lif_r2": {
      "path": "D:\\RawData\\lif_r2.csv",
      "path_mode": "external_reference",
      "size_bytes": 0,
      "original_source_path": "/server/path/to/source"
    },
    "ms": {
      "path": "D:\\RawData\\ms.txt",
      "path_mode": "external_reference",
      "size_bytes": 0,
      "original_source_path": "/server/path/to/source"
    }
  },
  "intermediate_tables": {
    "lif_traces": {
      "path": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_traces.parquet",
      "sha256": "..."
    },
    "lif_peaks": {
      "path": "data/interim/v3/01_lif_trace_physical_qc/v3_01_lif_peaks.parquet",
      "sha256": "..."
    },
    "ms_events": {
      "path": "data/interim/v3/02_ms_event_calling/v3_02_ms_events.parquet",
      "sha256": "..."
    },
    "ms_scan_summary": {
      "path": "data/interim/v3/02_ms_event_calling/v3_02_ms_scan_summary.parquet",
      "sha256": "..."
    }
  },
  "annotation_db": {
    "path": "annotation_app/annotations/annotation.sqlite",
    "schema_version": 1
  },
  "channel_identity_prior": {
    "G2": "Day0",
    "R1": "Day9",
    "R2": "Day3"
  }
}
```

This file is the authority for project metadata. It records full SHA256 hashes
for the four intermediate parquet files and a `project_table_binding` digest.
The same binding is stored in SQLite `project_config`.

When `lifms_project.json` is present, the app validates recorded
intermediate-table fingerprints before it writes anything to
`annotation.sqlite`. If a transferred Linux project does not yet have
`lifms_project.json`, the Windows app treats the first successful open as a
legacy adoption step: it checks SQLite `input_manifest` size records and
annotation peak/event IDs, then writes `lifms_project.json` and the SQLite
binding. This first adoption is compatibility evidence, not historical
cryptographic proof. Subsequent opens are strictly bound by full parquet hashes.

## Transfer Safety Rules

- If parquet files are missing, do not use the SQLite alone.
- If the project was regenerated on Windows, do not assume Linux SQLite still
  matches unless raw and parquet hashes match.
- If only file paths changed from Linux to Windows but file hashes match, the
  project is likely transferable.
- Imported annotations must not be used as calibration evidence for time model
  estimation.
- `export_runs` from Linux should be treated as historical provenance, not as
  a new Windows export.

## Practical Recommendation

First move a complete Linux project directory and try opening it on Windows.
If the project does not yet have `lifms_project.json`, open it once on Windows
from the standard directory structure to perform legacy adoption. After that,
share the adopted project directory with the generated `lifms_project.json` and
updated `annotation.sqlite`.

This avoids forcing you to redraw annotations while still moving the software
toward a professional project-based model.
