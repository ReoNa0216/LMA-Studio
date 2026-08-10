# HSC1 Windows 验收指南（v0.4.0-rc5）

不需要重建 HSC1，也不需要重新标注。rc5 只修复跨图窗人工关系的显示归属，并增加 UMAP 的 PC(34:1) m/z 定位。

## 准备

1. 复制整个 `HSC1` 项目为一个独立目录，例如 `HSC1_rc5_UAT`。
2. 只在软件中打开该副本；不要打开原 HSC1，也不要修改 `HSC1_data`。
3. 运行 `lifms_annotation_windows_source\dist\LMAStudio\LMAStudio.exe`。

## 验收 49.001 min 关系

1. 进入 `Events`，设 `Start=49.00`、`Window=1.00`、`Time=Aligned`。
2. 已保存的 49.001 min MS 关系应显示在 49–50 min 图窗，LIF 与 MS 两端及连线都应可见。
3. 切到 48–49 min，同一关系不应重复显示。
4. 不需要重新保存这条关系；它原本就存在于 SQLite，本轮只修复显示。

## 验收 UMAP m/z 定位

1. 打开 UMAP 窗口。
2. 在 `PC(34:1) m/z` 输入 `760.591883`，保持 `± 0.0001 Da`，点 `Find`。
3. 应找到 1 个点。该点出现红色外圈，视图自动放大；悬停可看到 MS760 时间和精确 m/z。若以后使用更宽容差，多个合格点会同时标红，并明确显示数量。
4. 点红圈事件仍应能定位回 Track；点 `Clear` 后红圈消失。
5. `Find/Clear` 只查询，不会修改标注。

## 最后快速检查

- 接受或拒绝一条候选，反馈应立即出现且不再长时间停顿。
- `Cell pair → Select peaks → Save pair` 仍可保存 core 或显示出的 weak LIF 峰。
- HSC1 的 Post-run QC 为 `Off`，UMAP 不应出现 QC 图例。
- 导出主 CSV 仍为 16 列、共 971 行；未标注事件为 `Type=unknown`。

若仍有异常，请保留 UAT 副本，并记录 `Stage / Start / Window / Time`、完整提示和截图；不要删除或重建原 HSC1。
