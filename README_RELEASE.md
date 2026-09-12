# LMA Studio v0.7.0 Release Notes

## 中文

本版完成“MS 事件与矩阵导入 → 原生 UMAP → 人工标签与 QC → 带标签矩阵导出”，与 MS Event Studio v0.6.0 配合使用。LIF 读取和提峰继续集成在 LMA Studio 中。

- 从 **LMA 事件包 ZIP** 新建项目并同时导入矩阵；已有对应事件包项目可通过“从 ZIP 导入矩阵”补充矩阵。严格核对稳定事件 ID 和版本，不按近似时间拼接。
- 新增 **原生 UMAP**：直接从矩阵计算 PCA、邻居和 UMAP，无需另外准备坐标 CSV。NaN 仅在计算副本中填 0，原矩阵不改写；默认随机种子为 1。
- 原生 UMAP 排除已确认并保存的标注起点之前的行，全部事件仍保留供校准。尚未确认的后段 QC 不自动排除。未满足计算条件时明确显示原因。
- **导出结果**生成一个自定义名称 ZIP，包含细胞/QC CSV 和可选带标签 H5AD。标签按事件 ID 关联；仅已接受人工关系提供标签，其余为 unknown，QC 状态来自人工 QC 关系。
- 导出矩阵保留原强度、NaN 和 feature 轴，排除已确认前段范围。同名 ZIP 不覆盖，失败不留半成品。
- 精简配置与工具栏，明确原生/外部 CSV 坐标来源，改善路径和着色控件；移除无法应用更新的“检查 MS 审阅更新”入口。

**下载**：Windows x64 完整解压 `LMA-Studio-v0.7.0-windows-x64.zip`，运行 `LMAStudio.exe`，保留全部随附文件。Apple Silicon Mac 解压 `LMA-Studio-v0.7.0-macos-arm64.zip`，打开 `LMA Studio.app`。两个 ZIP 均附 SHA-256 文件；macOS 使用 ad-hoc 签名，未进行 Developer ID 签名或公证。

**兼容**：沿用现行峰识别标准的 v0.4.0 及之后项目继续按保存的 manifest 打开，不自动重算、重编号或覆盖标注。没有矩阵的旧项目仍可使用已有坐标 CSV，无需重建。已退役峰识别标准的项目仍按既有规则拒绝打开。

**验证**：Windows 桌面检查覆盖目录选择/取消、自定义名称、同名拒绝、工程标签接受、CSV/H5AD 身份一致性和原生 UMAP 关闭重开。MPP 工程样例原矩阵 1,023 行，确认前段后导出 815 × 3,549 矩阵；验收用细胞/QC 标记不是科学真值。另有三套真实副本和十个已有项目的回归证据。509 项测试在本机 Windows 为 507 通过、2 跳过；此前双平台 CI 各为 503 通过、6 跳过，且实际执行打包后的 UMAP 计算。

macOS 无可用真机，可见界面和鼠标交互尚未验收。Codex 视觉标签路线仍为独立研究原型，未声称通过盲测或可替代人工标签；正式投稿项目未在本机逐项验收。

## English

This release completes the workflow from MS events and feature matrices to native UMAP, human cell/QC labels and labeled-matrix export. Use it with MS Event Studio v0.6.0. LIF reading and peak detection remain integrated in LMA Studio.

- Create projects from an **LMA event-package ZIP** with an optional matrix, or attach a matrix to a matching package-based project. Validate stable event IDs and versions without approximate-time joins.
- Compute **native UMAP** directly through PCA and neighbors. Fill NaNs with zero only in the computation copy; preserve the source matrix. The default random seed is 1.
- Exclude rows before the confirmed, saved annotation start from native UMAP while retaining all events for calibration. Do not automatically exclude unconfirmed later QC. Explain unmet calculation prerequisites.
- **Export results** creates one named ZIP containing the cell/QC CSV and optional labeled H5AD. Join labels by event ID; only accepted human relationships contribute labels, while other rows remain unknown. QC status comes from human QC relationships.
- Preserve raw intensities, NaNs and the feature axis, excluding the confirmed front range. Refuse filename collisions and avoid partial output.
- Simplify configuration and toolbar wording, distinguish native from external CSV coordinates, improve path and coloring controls, and remove the non-applying upstream review-check entry.

**Installation:** Extract the complete Windows x64 ZIP and run `LMAStudio.exe` with all bundled files present. On Apple Silicon macOS, extract the macOS ZIP and open `LMA Studio.app`. Both archives include SHA-256 files. The macOS app is ad-hoc signed, not Developer ID signed or notarized.

**Compatibility:** Projects created by v0.4.0 or later using the active peak-recognition standard keep their saved identities, events, labels and models. Existing projects without matrices may continue using external coordinates without rebuilding. Retired peak-standard projects remain subject to the existing rejection rule.

**Validation:** Windows desktop checks covered native selection/cancellation, custom filenames, collision refusal, engineering label acceptance, CSV/H5AD consistency and reopening native UMAP. The MPP example retains 1,023 source rows and exports an 815-by-3,549 matrix after confirmed front exclusion; its test labels are not scientific ground truth. Regression evidence covers three real-data copies and ten existing projects. Of 509 tests, local Windows passed 507 with 2 skipped; preceding CI passed 503 with 6 skipped per platform, including actual packaged UMAP computation.

No physical Mac was available, so visible macOS interaction remains untested. The Codex visual-label workflow remains a separate research prototype without a validated blind-test accuracy claim. Unavailable publication projects were not individually validated.
