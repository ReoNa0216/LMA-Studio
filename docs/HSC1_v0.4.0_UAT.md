# HSC1 Windows 验收指南（v0.4.0）

不需要重建 HSC1，也不需要重新标注。验收必须使用完整项目副本；不要打开原 HSC1，也不要修改 `HSC1_data`。

## 准备

1. 复制整个 `HSC1` 项目为独立目录，例如 `HSC1_v0.4.0_UAT`。
2. 运行 `lifms_annotation_windows_source\dist\LMAStudio\LMAStudio.exe`，只打开该副本。

## 检查 49.001 min 关系

1. 进入 `Events`，设 `Start=49.00`、`Window=1.00`、`Time=Aligned`。
2. 已保存的 49.001 min MS 关系应显示在 49–50 min 图窗，LIF、MS 和连线都应可见。
3. 切到 48–49 min，同一关系不应重复显示。
4. 不需要重新保存；该关系一直保留在 SQLite，本版本只修正显示归属。

## 检查 UMAP 的 MS760 时间定位

1. 打开 UMAP 窗口。
2. 在 `MS760 time (min)` 输入 `49.001`，保持 `± 0.001 min`，点 `Find`。
3. 应找到 1 个点。该点显示红色外圈，视图自动定位；悬停显示其完整 MS760 时间 `49.001300 min`。
4. 单击红圈事件仍应能定位回 Track；点 `Clear` 后红圈消失。
5. `Find/Clear` 只查询，不会修改标注。容差内若有多个事件，会同时标红并显示数量。

## 最后快速检查

- 接受或拒绝一条候选，反馈应立即出现。
- `Cell pair → Select peaks → Save pair` 可保存 core 或已显示的 weak LIF 峰。
- HSC1 的 Post-run QC 为 `Off`，UMAP 不显示 QC 图例。
- 导出主 CSV 保持 16 列、共 971 行；未标注事件为 `Type=unknown`。

若仍有异常，请保留 UAT 副本，并记录 `Stage / Start / Window / Time`、完整提示和截图；不要删除或重建原 HSC1。
