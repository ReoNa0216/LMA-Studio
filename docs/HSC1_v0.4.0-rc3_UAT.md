# HSC1 Windows 验收指南（v0.4.0-rc3，已被后续候选替代）

当前请使用 `HSC1_v0.4.0_UAT.md`。本文件只保留 rc3 的历史操作记录。

这次验收不要求把 HSC1 全部标完。目标是确认：前段校准可审核、MS 时间差可锁定、Weak peak 可人工配对、Track/UMAP 同步、主 CSV 易于继续标注。

## 1. 启动与现有项目

先关闭其他 LMA Studio 窗口，再运行：

```text
lifms_annotation_windows_source\dist\LMAStudio\LMAStudio.exe
```

如果 `scMetab\HSC1` 已由现行峰识别标准创建，**不需要重建**。验收前先把整个项目目录复制到一个新的独立目录（例如 `scMetab\HSC1_rc3_UAT`），然后点“打开项目”并只选择这个副本；不要把原 `HSC1` 当作验收写入目标。rc3 没有改变项目 schema、峰表或 SQLite 契约。

只有需要从头验证导入时，才使用另一个不存在或为空的项目目录。`HSC1_data` 始终只作只读原始输入，不能作为项目输出目录；所有会保存、接受、导出或冻结模型的验收操作都在项目副本中完成。

## 2. 从头创建 HSC1（可选）

| 项目 | 设置 |
|---|---|
| 保存方式 | 外部引用 |
| LIF G1 | Green；样本标签 `LSK`；启用 Cell pair；`HSC1_data\Lin-_LSK\G1.CSV` |
| LIF G2 | Green；样本标签 `Lin−`；启用 Cell pair；`HSC1_data\Lin-_LSK\G2.CSV` |
| MS | `HSC1_data\Lin-_LSK.txt` |
| 事件坐标 CSV | `HSC1_data\HSC-Lin-LSK-20260809-After-Batch-Correction.csv` |
| 事件起点 | `24` min |
| Post-run QC | `Off` |

不要添加 R1/R2。G1、G2 都选 Green 后会自动共享同一采集时间基准，不需要填写内部轴名。

事件坐标 CSV 只要求能找到 `scan_start_time`、`UMAP1`、`UMAP2`；其他列允许保留并会被忽略。

参考段保留为：

- `LSK`：只勾 G1；
- `Lin−`：只勾 G2。

不知道边界时先点“分析已选 LIF 并建议窗口”。建议不会自动确认；也可以保留合法、不重叠的待确认范围，点“生成草稿并进入项目”。按钮会显示创建进度，大型 MS 文件读取期间不要重复点击。

## 3. 确认边界与查看 QC anchor

草稿只允许浏览 Raw 前段轨迹。打开“配置”，核对两段顺序和范围，分别勾选“边界已确认”，再保存。

全部确认后软件应自动切换到：

```text
Calibration · Time = Aligned
```

并提示“QC anchor 候选已生成”。关闭配置窗口后应看到候选虚线。若手工切到了 `Raw`，虚线不会显示；把 `Time` 改回 `Aligned`。

校正后的 LIF 峰和 MS760 可能恰好分处图窗边界两侧。此时关系归入 **MS760 所在的窗口**；LIF 峰只要仍在界面已加载的 ±0.08 min 边界上下文内，候选就应显示，并可逐条接受或用 `Save anchor` 保存。它不应在相邻窗口重复出现，也不会参与整窗批量接受。

验收时检查：

1. 候选按时间先后匹配，不出现前后交叉。
2. 附近有多个可选峰时标为需逐条审核。
3. 两个参考段各接受若干可信 QC anchor。
4. 点“用已接受参考峰预览重算”，再应用前段时间校正。
5. G1/G2 应共同估计一个绿色信号时间平移，不要求同时出现峰。

## 4. MS Δt 与 Events / QC

进入 `MS Δt`，起点应为 24 min。估计 MS 时间差、检查证据与残差，必要时微调，然后锁定。

锁定后进入 `Events / QC`。HSC1 的 Post-run QC 为 `Off`，所以这里只有 Cell 候选，不应出现后段 QC 候选。

顶部浏览控件为：

```text
Start · Window · Time · Y · Labels · Weak peaks · Show
```

候选太密时把 `Window` 调为 `0.25`、`0.5` 或 `1.0` min，再点 `Show`。这只改变显示范围，不会改变项目的 MS Δt 取证范围。

自动待审列表不是全部可人工标注的峰。要补一条没有自动候选线的关系，选择 `Cell pair`，再点 `Select peaks`：主窗口内启用了细胞角色的 core LIF 峰会恢复为可点击；依次选择 LIF 与 MS760 后点 `Save pair`。`Labels = Auto` 只给每个时间分区的一个显著峰显示时间，其他峰可悬停查看；需要时改成 `Labels = All`。

即使项目已经积累数百条标注，点 `Save pair` 后也应很快出现连线并刷新 Track，不需要重建项目或重标已有关系。本候选在含 760 条标注的完整 HSC1 临时副本上实测保存加刷新约 0.66 秒；若你的机器持续超过 2 秒，请记录发生时的窗口范围和日志，先不要重复点击。

校正后的 MS760 与 LIF 峰也可能刚好分处窗口边界两侧。人工保存的 Cell pair 归入 **MS760 所在的主窗口**，LIF 峰允许位于界面已加载的 ±0.08 min 边缘上下文中。若从相邻窗口完成保存，软件会提示保存成功并自动转到能完整显示该关系的窗口；关系不会丢失，也不需要重复标注。例如 HSC1 的 MS760 `26.513` min / G1 `26.902` min 在校正后正好跨过 `26.500` min 边界，应按这一规则显示。

MS 轨道没有 core/weak 分层。浅色 MS 圆点表示它不在事件坐标 CSV，不能用于 Cell pair，但现在可以悬停或点击查看原因；较大的红边圆点表示相邻事件或信号质量需注意，也不是 Weak peak。只有非浅色、属于事件坐标表的 MS760 才可选择。

## 5. Weak peak 如何人工配对

`Weak peaks` 默认关闭。打开后，弱候选峰显示为空心虚线圆点，但始终不参与自动校准、QC、时间差估计或模型训练。

- 在 Calibration 或 MS Δt 点击 Weak peak：只提示“仅在事件标注段生效”。
- 在 Events / QC：选择 `Cell pair`，点 `Select peaks`，依次点击一个 Weak LIF 峰和对应 MS760 事件，再点 `Save pair`。
- 选中的 LIF 峰会用更深描边标出。
- 只有这条人工关系明确保存后，Weak peak 才能进入主 CSV。

`QC anchor` 与 `Cell pair` 不是互斥通道角色。同一 LIF 通道可以在参考段中作为 QC anchor，同时在事件段用于 Cell pair。

## 6. 三种 Post-run QC 模式

- `Off`：后段没有再次注入 QC；HSC1 用这个。
- `QC signature`：后段确实有 QC，但时间未知或不规律；在整个 Events 段寻找所选 QC 通道组合。
- `Scheduled windows`：后段 QC 时间已知；只在填写的窗口内寻找，减少误匹配。

CART 风格项目可在同一个前段参考段选择 G2+R1。若 G2/R1 同时也是实际细胞通道，继续启用它们的 Cell pair 角色即可；两项设置相互独立。

## 7. 最小验收范围

赶时间时至少完成：

- 两个前段各审核若干 QC anchor，并应用校正；
- 锁定一次 MS Δt；
- 24 min 后检查若干 G1 与 G2 候选；
- 检查一个同一 MS event 的跨通道冲突；
- 完成一条可信 Weak peak 人工 Cell pair（若数据中有明确证据）；
- 完成一次 Track ↔ UMAP 双向定位；
- 导出一次主 CSV。

## 8. CSV 与报错记录

“导出细胞/质控主 CSV”只含当前有效的 Cell 与后段 QC 关系。HSC1 选择 `Off`，通常只有 Cell 行。前段 QC anchor、完整模型信息和审计历史留在 SQLite，不混入主 CSV。

报错时不要删除项目。请记录：点击的按钮、完整错误、当前 Stage、`Start/Window/Time`、是否打开 Weak peaks，以及截图中的峰点和候选线。
