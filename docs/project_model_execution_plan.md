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

## Non-Goals

- No standalone SQLite import in this pass.
- No project archive/export wizard in this pass.
- No changes to V3 preprocessing scripts unless required by tests.
