# LMA Studio v0.5.1 Release Notes

## 中文

LMA Studio 是面向项目的本地 LIF-MS 人工辅助标注桌面应用。

### 下载

- `LMA-Studio-v0.5.1-windows-x64.zip`：Windows x64。完整解压后运行 `LMAStudio.exe`。
- `LMA-Studio-v0.5.1-macos-arm64.zip`：Apple Silicon。解压后打开 `LMA Studio.app`。
- 每个压缩包均提供对应的 `.sha256` 完整性校验文件。

macOS 包采用 ad-hoc 签名，未使用 Apple Developer ID 签名或公证。首次打开时可能需要按住 Control 点击应用并选择“打开”。

### v0.5.1 修复

- 修复 Events / QC 调整时间轴后，已人工审核的自动 Cell/QC 关系未计入状态汇总、从而看起来由“已接受”变成“待审”的问题。
- 保留的 QC 关系现在与 Cell 关系一样进入审核列表；同一关系在自动候选、保留关系和绘图数据之间按稳定关系 ID 去重。
- 新模型生成的新组合仍保持待审，但会与原有审核决定分开呈现。跨时间模型的旧关系明确显示“原状态保留”，偏差超限时同时显示“需复核”。
- SQLite 审核状态、原始峰/event 身份、UMAP 分类和固定 16 列 CSV 语义不变。

### 验证

- 完整自动化测试：460 项通过，2 项按环境条件跳过。回归覆盖已接受的自动 Cell 和 QC 关系、时间轴调整后的状态计数、候选列表去重、需复核标记、UMAP 与 CSV 身份保持。
- 真实 HSC1 隔离副本保留了 120 条长时间标注关系及其 40/40/40 的已接受、待审、已拒绝状态；106 条超限关系继续标记需复核，40 条已接受 CSV 身份与 UMAP 分类保持不变。
- 9 个现行标准真实项目副本全部通过加载、Raw/Aligned Track、关系投影、UMAP、固定 16 列导出与重开检查，原项目逐文件校验保持不变。
- Windows 与 macOS 正式包均通过各自平台的完整测试和打包运行时检查后发布。

### 数据边界

源代码与应用包不包含用户项目、原始 LIF/MS 文件、SQLite 数据库、源或规范化 UMAP CSV、parquet 表、作者 CSV、h5ad、导出标注、凭据或本地绝对路径。

## English

LMA Studio is a local, project-based desktop application for human-assisted LIF-MS annotation review.

### Downloads

- `LMA-Studio-v0.5.1-windows-x64.zip`: Windows x64. Extract the complete archive and run `LMAStudio.exe`.
- `LMA-Studio-v0.5.1-macos-arm64.zip`: Apple Silicon. Extract it and open `LMA Studio.app`.
- A matching `.sha256` integrity file is provided for each archive.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. First launch may require Control-clicking the app and choosing Open.

### Fixes in v0.5.1

- Fixed Events / QC status summaries omitting reviewed automatic Cell/QC relationships after a timeline adjustment, which made accepted decisions appear to have reverted to pending.
- Retained QC relationships now appear in the review list like retained Cell relationships. Duplicate rows from generated candidates, retained decisions, and drawing data are collapsed by stable relationship ID.
- Newly generated combinations remain pending and are presented separately from prior review decisions. Cross-model decisions explicitly say Original state preserved and also show Needs review when their residual exceeds tolerance.
- SQLite review states, raw peak/event identities, UMAP classifications, and fixed 16-column CSV semantics are unchanged.

### Validation

- Full automated suite: 460 tests passed and 2 environment-dependent tests skipped. Coverage includes accepted automatic Cell and QC relationships, post-adjustment status counts, review-list deduplication, Needs review markers, and stable UMAP/CSV identities.
- An isolated real HSC1 copy retained 120 long-session relationships and their 40/40/40 accepted, pending, and rejected states. All 106 excessive-residual relationships remained flagged for review, while 40 accepted CSV identities and UMAP classifications stayed unchanged.
- Nine real current-standard project copies passed load, Raw/Aligned Track, relationship projection, UMAP, fixed 16-column export, and reopen checks, while every original project remained byte-for-byte unchanged.
- Formal Windows and macOS packages are published only after each platform passes its complete test and packaged-runtime checks.

### Data boundary

The source tree and application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
