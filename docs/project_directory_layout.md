# LMA Studio 项目目录说明

## 新建项目的当前目录

当前版本新建的项目使用下面的简洁结构，不再把内部处理步骤或版本号写进用户目录：

```text
MyProject/
├─ lifms_project.json
├─ README.md
├─ data/
│  ├─ lif_traces.parquet
│  ├─ lif_peaks.parquet
│  ├─ ms_events.parquet
│  ├─ ms_scan_summary.parquet
│  └─ cell_event_map.csv
├─ annotations/
│  ├─ annotation.sqlite
│  └─ exports/
├─ provenance/
│  ├─ input_manifest.csv
│  ├─ project_protocol.json
│  ├─ preprocessing.log
│  └─ preprocessing_report.md
├─ diagnostics/
│  ├─ lif/
│  └─ ms/
└─ raw_inputs/                 # 仅“复制原始文件到项目”模式出现
```

各目录的职责只有一层：

- `data/` 是应用浏览、匹配和 UMAP 映射所需的数据。
- `annotations/` 是人工标注、时间模型和用户导出。
- `provenance/` 记录输入、项目参数和处理过程。
- `diagnostics/` 是峰识别与 MS event 识别的质量检查，不代表 QC 身份或细胞类型。
- `raw_inputs/` 只在用户明确选择复制模式时保存原始 LIF/MS 文件。

新项目中的 MS 背景诊断使用“background estimation”语义；它只是自动阈值估计的低信号区间，不是 QC 群体，也不是 event 类型。

UMAP 坐标可以在项目“配置”中切换，例如比较批次校正前后的坐标。新建时事件 CSV 只强制要求 `scan_start_time`；`UMAP1`/`UMAP2` 可选且必须成对提供。软件只接受能映射到同一批 MS event 的 CSV，并把规范化事件表写入项目自己的 `cell_event_map.csv`；不会把创建电脑上的外部 CSV 绝对路径作为运行依赖。切换不改变标注、峰表或时间模型，校验失败时保留原坐标。

如果新建时有任一事件名单行无法唯一匹配，整个 staging 目录会回滚，不会形成上述项目结构。此时可从新建窗口下载逐行诊断 CSV；该报告是用户下载文件，不会被误放进失败项目，也不包含源 CSV 的身份或作者标签列。

## 哪些文件必须一起保留

软件运行边界由 `lifms_project.json` 声明，至少包括四张 parquet、`data/cell_event_map.csv` 和 `annotations/annotation.sqlite`。这些文件的路径和内容摘要绑定在一起，不能单独移动、替换或重新命名。

分享项目时最稳妥的做法是：

1. 先关闭 LMA Studio；
2. 压缩整个项目根目录；
3. 对方解压后选择该项目根目录打开。

项目最外层文件夹可以整体重命名，也可以整体移动。项目内部文件不要挪动。只复制 `annotation.sqlite`、或把它接到另一批重新生成的 parquet 上，都不安全。

外部引用模式不会把大型原始 LIF/MS 文件复制进项目。接收方仍可查看、继续标注和导出；若要重新运行原始前处理，还需要另行取得原始输入。复制模式下 `raw_inputs/` 才包含这部分重跑输入。

## 与 v0.4.0 项目的兼容边界

- 已有 v0.4.0 项目继续使用自己 manifest 中记录的旧目录，不自动迁移，也不会因为打开而被整理目录。
- 当前软件同时能按 manifest 打开旧 v0.4.0 布局和新简洁布局。
- 旧项目内若保留历史命名的诊断文件，它们仍只是审计材料，不会改变当前峰识别或标注语义。
- 已停用峰识别标准生成的更早项目不是本次“目录兼容”的对象；应从原始输入在新目录重建，不能靠移动文件伪装升级。

这一区分是刻意的：目录布局由 manifest 驱动，科学峰识别标准由峰表和配置哈希驱动，两者不能混为一谈。
