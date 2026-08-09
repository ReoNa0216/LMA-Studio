# HSC1 — LMA Studio v0.4.0-rc1 验收指南

本指南只使用 `HSC1_data` 中的原始文件作为只读输入。请把新项目保存到另一个新目录，例如：

```text
E:\path\to\scMetab\HSC1_LMA_UAT_v040rc1
```

不要把项目保存到 `HSC1_data` 内，也不要覆盖任何已有项目。

## 0. 先准备 Lin-LSK 专用事件坐标

`HSC-Lin-LSK-MPP-CLP-LK-20260809-After-Batch-Correction.csv` 是多个批次的合并表，**不能直接作为 HSC1/Lin-LSK 项目的事件坐标输入**。请先在 `HSC1_data` 之外新建一个 UAT 输入目录，从合并表中筛选 `batch == Lin-LSK`，并且只保存以下三列：

```text
scan_start_time,UMAP1,UMAP2
```

例如把结果另存为：

```text
E:\path\to\scMetab\LMAStudio_UAT_Copies\HSC1_Lin-LSK_coordinates-only.csv
```

可以用 Excel 筛选后另存，也可以在源码目录运行以下只读源文件的 Python 片段；它只会在 UAT 目录创建新文件：

```powershell
@'
from pathlib import Path
import pandas as pd

source = Path(r"E:\path\to\scMetab\HSC1_data\HSC-Lin-LSK-MPP-CLP-LK-20260809-After-Batch-Correction.csv")
target = Path(r"E:\path\to\scMetab\LMAStudio_UAT_Copies\HSC1_Lin-LSK_coordinates-only.csv")
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists():
    raise FileExistsError(f"拒绝覆盖已有文件: {target}")
frame = pd.read_csv(source, usecols=["scan_start_time", "UMAP1", "UMAP2", "batch"])
selected = frame.loc[frame["batch"].astype(str).eq("Lin-LSK"), ["scan_start_time", "UMAP1", "UMAP2"]]
if selected.empty:
    raise RuntimeError("未找到 batch == Lin-LSK 的坐标")
selected.to_csv(target, index=False)
print(f"已生成 {len(selected)} 行: {target}")
'@ | python -
```

不要把 `batch`、作者标签、细胞类型或其他源列带进项目。当前实测应得到 971 行；如果行数不同，先确认源文件版本和筛选值，再继续。

## 1. 新建项目

1. 启动 `LMAStudio.exe`，点击“新建项目”。
2. 点击“套用 HSC1 配置预设”。预设应显示：
   - G1，样本标签 `LSK`，Green，`green_axis`，用于细胞标注；
   - G2，样本标签 `Lin−`，Green，`green_axis`，用于细胞标注；
   - 事件标注起点 `24 min`；
   - 后段 QC `disabled`。
3. 推荐选择“外部引用”，避免复制 8 GB 以上的 MS 文件。
4. 选择只读输入：
   - G1：`HSC1_data\Lin-_LSK\G1.CSV`
   - G2：`HSC1_data\Lin-_LSK\G2.CSV`
   - MS：`HSC1_data\Lin-_LSK.txt`
   - 事件坐标：第 0 节生成的 `HSC1_Lin-LSK_coordinates-only.csv`，不要直接选多批次合并表
5. 项目保存路径填写上面的全新 UAT 目录。

软件只从专用坐标文件读取白名单坐标，并在项目内建立 canonical 的 `ms_event_id / scan_id / scan_start_time / UMAP1 / UMAP2`；源表中的作者标签不会成为候选或导出标签。

## 2. 建议并确认前段参考窗口

1. 点击“分析已选 LIF 并建议窗口”。该操作只读扫描 G1/G2 峰形，不创建项目，也不改原始文件。
2. 当前 HSC1 文件的实测建议大约为：
   - G1/LSK：`1.9–8.3 min`
   - G2/Lin−：`13.1–20.0 min`
3. 这些数值不是软件全局常量。请结合轨迹和实验记录核对；如需调整，直接编辑边界。
4. 建议回填或手工修改后，“边界已确认”必须保持未勾选。逐段核对完毕后，再由你亲自勾选两个确认框。
5. 确认 G1 在前、G2 在后，窗口不重叠，最后一段结束时间不晚于 `24 min`。

## 3. 生成项目与时间模型

1. 点击“生成并进入项目”。首次解析 `Lin-_LSK.txt` 会耗时；不要在处理中关闭软件或移动原始文件。
2. 在“前段参考校准”逐段审核 anchor。G1/G2 共享一个 `green_axis` 平移；两通道不需要在同一个时刻同时出现峰。
3. 点击“用已接受参考 anchors 预览重算”，检查内点、冲突和残差。
4. 预览合理后点击“应用 QC 对齐（按物理轴）”。HSC1 应只应用一个 Green 轴平移。
5. 进入“无标签后段 delta”，从 24 min 起检查无标签峰拓扑。若默认 2.5 min 种子窗显示“证据不足”，应扩大观察/种子窗口或调整容差后重新预览；不要为了继续流程而盲目填入 delta 或冻结。真实副本测试中，24–26.5 min 的严格匹配证据不足，而 50–55 min 可产生高置信细胞候选，这属于数据证据分布而不是按钮故障。
6. HSC1 后段 QC 为 `disabled`，事件阶段默认只做细胞候选/人工细胞二元组，不应生成后段 QC 巡检候选。

## 4. 必测行为

- G1 和 G2 匹配同一个 MS event 时，两条候选必须显示为跨通道歧义，并要求逐条选择通道；不能批量或静默接受。
- 未冻结 time model 时，第三阶段接受按钮和后端写入必须被阻止。
- UMAP 点击事件后，Track 应定位到同一 `ms_event_id`；Track 写入后 UMAP 颜色应同步。
- 修改已确认的参考边界、24 min 起点或 delta 依赖参数时，应出现明确失效提示。
- 确认修改后，旧 frozen model、后段 delta 和第三阶段候选必须失效并重算；已有人工标注记录必须保留，不能静默删除。
- 重新打开项目后，上述配置、冻结状态和人工审核应恢复。

## 5. 精简 CSV 验收

点击“导出 Cell/QC 主 CSV”。主 CSV 只含当前有效的第三阶段 Cell/后段 QC；前段参考 anchor 完整保留在 SQLite 审计中，不混入 CSV。固定为以下 16 列：

```text
CellNumber,scan_Id,scan_start_time,TIC,PC(34:1)_mz,PC(34:1)_intensity,
UMAP1,UMAP2,Type,annotation_kind,review_stage,LIF_channel,LIF_peak_id,
MS_event_id,residual_sec,annotation_id
```

重点检查：

- `CellNumber` 是按 canonical event-map 顺序生成的稳定编号，方便后续按 cell 读取。
- `Type` 对细胞行来自当前项目中已接受的 LIF 通道科学身份（本项目为 `LSK` 或 `Lin−`），QC 行为 `QC`；它不读取 source CSV 的作者 `Type`。
- `scan_Id / scan_start_time / TIC / PC(34:1)` 便于和后续代谢矩阵连接。
- `UMAP1 / UMAP2` 只来自白名单坐标 map。
- 模型 hash、协议 payload、候选冲突详情和审计元数据不进入主 CSV；它们仍保存在项目 SQLite 中。
- 同一 `MS_event_id` 在当前模型下不应同时出现 active accepted QC 和 cell 语义。

验收时请保留项目副本、导出的 CSV、截图和软件版本号；不要移动或修改 `HSC1_data` 原件。

开发者也可用下面的回归脚本一次性建立全新的隔离项目副本。脚本拒绝覆盖现有目录、临时筛选坐标，并在结束时复核 `HSC1_data` 文件树和输入指纹未改变：

```powershell
python scripts/regression_hsc1_project_copy.py `
  --hsc-data-dir "E:\path\to\scMetab\HSC1_data" `
  --project-dir "E:\path\to\scMetab\LMAStudio_UAT_Copies\HSC1_v0.4.0_rc1_new_copy"
```
