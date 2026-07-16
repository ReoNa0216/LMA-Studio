# Project Model Execution Plan

**Goal:** Make project management explicit: new projects can either copy raw inputs into the project or reference external raw inputs, and existing projects can be opened by project directory.

**Architecture:** Add `lifms_project.json` as the project authority while keeping the current parquet and SQLite layout compatible. Keep preprocessing scripts unchanged by continuing to write `results/tables/v3/00_allowed_inputs.csv`; the `path` values can be either project-relative copied raw files or absolute external references.

**Tech Stack:** Python standard library, pandas, SQLite, browser-native HTML/JS UI, PyInstaller packaging.

---

## Tasks

- [x] Add manifest helpers and unit tests for raw input modes.
- [x] Update new project creation to write `lifms_project.json` and honor `copy_into_project` vs `external_reference`.
- [x] Add an open-existing-project API that loads a project directory using its standard parquet and SQLite layout.
- [x] Update UI: rename import to new project, add raw input mode controls, add open existing project modal.
- [x] Verify with unit tests, Python compile, local HTTP smoke, and rebuilt exe smoke.
- [x] Generalize QC anchor configuration from a fixed pair to a 2-4 channel set with physical time-axis coverage validation.
- [x] Add axis-aware global QC shift and post-QC local delta matching while preserving the legacy two-channel path.
- [x] Store dynamic QC anchor IDs/times in SQLite payload JSON and export valid structured JSON columns.
- [x] Adversarially harden non-legacy two-anchor payloads, cross-axis coherence, same-axis clustering, batch acceptance, local-delta ambiguity, and unhashed frozen-model migration.
- [x] Generalize same-detector preprocessing audit so projects with only one red channel are reported as not applicable instead of being mislabeled R1/R2.

## Non-Goals

- No standalone SQLite import in this pass.
- No project archive/export wizard in this pass.
- No changes to V3 preprocessing scripts unless required by tests.
