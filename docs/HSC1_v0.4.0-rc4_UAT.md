# HSC1 Windows 验收指南（v0.4.0-rc4，已被 v0.4.0 替代）

当前请使用 `HSC1_v0.4.0_UAT.md`。本文件只保留 rc4 的历史操作记录。

这次不需要重建 HSC1，也不需要重新标注。rc4 没有改变项目 schema、峰表或已有人工关系；它主要修正候选审核速度、跨通道歧义显示、UMAP QC 图例和主 CSV 的完整细胞清单。

## 1. 只在项目副本上验收

关闭其他 LMA Studio 窗口，然后运行：

```text
lifms_annotation_windows_source\dist\LMAStudio\LMAStudio.exe
```

把整个现有 `HSC1` 项目复制为一个新的独立目录，例如 `HSC1_rc4_UAT`。在软件中只打开这个副本，不要把原 HSC1 当作验收写入目标，也不要修改 `HSC1_data`。

## 2. 本轮最小验收

在 `Events / QC` 中选择一个已经标注较多的窗口：

1. 接受或拒绝一条普通候选。按钮应立即显示 `Accepting…` 或 `Rejecting…`，连线刷新不应再持续约 5 秒。
2. 用 `Cell pair → Select peaks → Save pair` 保存一条人工关系。已有标注不受影响，无需重标。
3. 当 G1、G2 同时竞争同一个 MS event 时，普通列表和图中默认不显示容易误导的黄色关系。需要仲裁时打开 `Show conflicts`，每个 MS event 只出现一张分组卡，再选择 `Use G1` 或 `Use G2`。
4. HSC1 的 Post-run QC 为 `Off`。打开 UMAP 后不应出现 `QC` 图例；只有当前项目确有有效后段 QC 时才显示它。
5. 完成一次 Track → UMAP 和 UMAP → Track 定位。
6. 导出主 CSV，确认表头仍为 16 列、`CellNumber` 唯一且按事件坐标顺序排列。

## 3. CSV 如何理解

主 CSV 现在保留事件坐标表中的全部 MS event，而不只是已经接受的关系：

- 已标注细胞：`Type` 为所选 LIF 通道的样本标签，例如 `LSK` 或 `Lin−`；
- 当前有效后段 QC：`Type=QC`；
- 尚未标注的细胞：`Type=unknown`，LIF 峰和 annotation 字段留空；
- 前段 `QC anchor` 不进入主 CSV，仍完整保存在项目 SQLite 审计库。

因此 CSV 行数应与项目事件坐标表的有效行数一致。随着继续标注，原来的 `unknown` 行会在下一次导出时变成相应细胞类型，不会产生重复 `CellNumber`。

## 4. 如需从头创建 HSC1

只有专门测试导入时才使用新的空项目目录：

| 输入 | 设置 |
|---|---|
| G1 | Green；样本标签 `LSK`；勾选 Cell；选择 `HSC1_data\Lin-_LSK\G1.CSV` |
| G2 | Green；样本标签 `Lin−`；勾选 Cell；选择 `HSC1_data\Lin-_LSK\G2.CSV` |
| MS | `HSC1_data\Lin-_LSK.txt` |
| 事件坐标 | `HSC1_data\HSC-Lin-LSK-20260809-After-Batch-Correction.csv` |
| 事件起点 | `24` min |
| Post-run QC | `Off` |

不要添加 R1/R2。前段参考依次设置为 `LSK/G1` 和 `Lin−/G2`。不知道边界时先分析 LIF 取得建议，创建待确认草稿后在原始轨迹中核对，再确认边界。

## 5. CART 项目的填写方式

对典型的 CART G2+R1 前段 QC：

- LIF 输入分别加入 G2（Green）与 R1（Red）；是否勾选 `Cell` 只取决于该通道后段是否也用于真实细胞标注；
- 前段参考段可在同一段同时选择 G2+R1；
- 后段 QC 时间不固定时选 `QC signature`，已知只在若干时间段注入时选 `Scheduled windows`，完全没有后段 QC 时选 `Off`。

同一通道可以同时承担前段 `QC anchor` 和后段 `Cell pair`，两种角色互不排斥。

## 6. 报错记录

若操作仍持续超过 2 秒或出现错误，不要重复点击或删除项目。请记录当前 `Stage / Start / Window / Time`、所点按钮、完整提示和峰图截图；保留项目副本供复现。
