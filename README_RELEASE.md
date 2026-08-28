# LMA Studio v0.5.0 Release Notes

## 中文

LMA Studio 是面向项目的本地 LIF-MS 人工辅助标注桌面应用。

### 下载

- `LMA-Studio-v0.5.0-windows-x64.zip`：Windows x64。完整解压后运行 `LMAStudio.exe`。
- `LMA-Studio-v0.5.0-macos-arm64.zip`：Apple Silicon。解压后打开 `LMA Studio.app`。
- 每个压缩包均提供对应的 `.sha256` 完整性校验文件。

macOS 包采用 ad-hoc 签名，未使用 Apple Developer ID 签名或公证。首次打开时可能需要按住 Control 点击应用并选择“打开”。

### v0.5.0 更新

- `Events / QC` 新增紧凑的“调整时间轴”入口。冻结模型的修改采用明确的预览、取消和应用流程。
- 关系身份现在严格由原始 LIF 峰 ID 和 MS event ID 决定。轨迹、峰、刻度和既有连线在预览时同步重新投影。
- G1/G2 始终共享 Green axis，R1/R2 始终共享 Red axis；MS 保持固定参考。前段物理轴校正与后段 MS Δt 分开建模和显示。
- 已接受、待审核、已拒绝、人工建立以及已审核的自动 Cell/QC 关系均保留原身份、来源和状态。新模型下偏差过大的已审核关系不会被删除或降级，而会醒目标记为“需复核”。
- 未经人工审核的自动候选在应用新模型后失效并重建；时间偏差、候选排序和显示位置使用新投影重新计算。
- 应用调整会原子写入新的冻结时间模型修订、上一修订引用、审计记录和下游失效说明。取消预览不写入项目。
- Track、UMAP 与固定 16 列 CSV 继续使用稳定原始身份，现有人工标注和导出语义不变。

### 验证

- 完整自动化测试：457 项通过，2 项按环境条件跳过。
- 真实 HSC1 数据在隔离项目副本中完成长时间标注回归：120 条关系覆盖已接受、待审核和已拒绝状态；应用时间轴修订后全部身份和状态保留，106 条超限关系标记需复核，40 条已接受 CSV 身份与 UMAP 分类保持不变。
- 9 个现行标准真实项目副本覆盖 v0.4.0、v0.4.1、v0.4.4 和 v0.4.10 创建记录；全部通过加载、Raw/Aligned Track、关系投影、UMAP、固定 16 列导出与重开检查，原项目逐文件校验保持不变。v0.4.0-v0.4.11 使用同一 schema 3、layout 4、detector 2 加载契约。
- Windows 构建已通过打包运行时探针、科学依赖来源与 ABI 审计。所有真实项目写入测试只使用副本。

### 数据边界

源代码与应用包不包含用户项目、原始 LIF/MS 文件、SQLite 数据库、源或规范化 UMAP CSV、parquet 表、作者 CSV、h5ad、导出标注、凭据或本地绝对路径。

## English

LMA Studio is a local, project-based desktop application for human-assisted LIF-MS annotation review.

### Downloads

- `LMA-Studio-v0.5.0-windows-x64.zip`: Windows x64. Extract the complete archive and run `LMAStudio.exe`.
- `LMA-Studio-v0.5.0-macos-arm64.zip`: Apple Silicon. Extract it and open `LMA Studio.app`.
- A matching `.sha256` integrity file is provided for each archive.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. First launch may require Control-clicking the app and choosing Open.

### What changed in v0.5.0

- Events / QC now has a compact Adjust timeline entry. Frozen-model changes follow an explicit preview, cancel, and apply flow.
- Relationship identity is defined solely by raw LIF peak IDs and MS event IDs. Tracks, peaks, ticks, and existing connectors are reprojected together during preview.
- G1/G2 always share the Green axis, R1/R2 always share the Red axis, and MS remains the fixed reference. Front physical-axis correction and downstream MS delta are modeled and displayed separately.
- Accepted, pending, rejected, manual, and reviewed automatic Cell/QC relationships retain their identities, sources, and review states. Reviewed relationships with excessive residuals under the new model remain intact and receive a prominent Needs review marker.
- Unreviewed automatic candidates are invalidated and rebuilt after applying a new model. Residuals, candidate ranking, and display positions are recomputed from the new projection.
- Applying an adjustment atomically records a new frozen time-model revision, the previous revision, an audit event, and downstream invalidation details. Canceling preview performs no project write.
- Track, UMAP, and the fixed 16-column CSV retain stable raw identities and existing annotation/export semantics.

### Validation

- Full automated suite: 457 tests passed and 2 environment-dependent tests skipped.
- A long-session regression used an isolated copy of real HSC1 data: 120 relationships covered accepted, pending, and rejected states. After a timeline revision, every identity and state was retained, 106 excessive-residual relationships were flagged for review, and 40 accepted CSV identities plus UMAP classifications remained unchanged.
- Nine real current-standard project copies covered creation records from v0.4.0, v0.4.1, v0.4.4, and v0.4.10. Every copy passed load, Raw/Aligned Track, relationship projection, UMAP, fixed 16-column export, and reopen checks, while every original project remained byte-for-byte unchanged. v0.4.0-v0.4.11 use the same schema 3, layout 4, detector 2 loading contract.
- The Windows build passed packaged-runtime probes and scientific dependency provenance/ABI audits. Tests that write real project data use copies only.

### Data boundary

The source tree and application package contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
