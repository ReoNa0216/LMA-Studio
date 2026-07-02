# LMA Studio Developer Notes

这是一个本地浏览器原型，用于人工辅助浏览 LIF-MS 同步数据，并按“QC 校正 / 后段局部校正 / QC 巡检 / 细胞标注”阶段审核候选连线。当前版本已经支持本地 accept/reject、手动 QC anchor、后段 QC 巡检、细胞二元组标注、右键线条审核，以及导出已接受 annotation CSV。

## 当前范围

当前版本支持以下功能：

- 读取由第一性原理前处理生成的 parquet 中间表；也可以通过顶部“新建项目”从 3 个 LIF 原始文件和 1 个 MS 原始文件生成这些中间表，或通过“打开项目”加载已有项目。
- 按 2.5 min 同步窗口显示 LIF/MS 数据。
- 左右翻页或输入起始时间跳转；普通浏览窗口固定为 2.5 min。
- 右侧 5-track 图上方的“图窗起点”只控制当前浏览窗口起点；它不是项目级 `annotation_start_min`。
- 在每个识别出的 LIF 峰和 MS event 峰旁显示对应时间。
- 每条轨道都有自己的时间轴，便于后续人工 QC anchor 对齐。
- 鼠标悬停峰点时显示该峰或 event 的源字段。
- 自动基于 0-10.5 min 全 QC 区段估计 shift-only 时间校正，并支持“校正后/原始”时间轴切换。
- 对自动 QC 三元组候选进行 `pending / accepted / rejected` 审核。
- 在手动模式下建立人工连线：QC 阶段点击 G2/R1/MS760 anchor；细胞标注阶段点击一个 LIF 峰（G2/R1/R2）和一个 MS760 峰建立严格二元组。
- 通过任务阶段切换候选列表和连线显示：`QC 校正 / 后段局部校正 / QC 巡检 / 细胞标注`。
- 后段局部校正冻结前，QC 巡检和细胞标注候选不会生成，侧栏也不显示后段人工审核工具；后端同时拒绝直接 review 后段候选。
- 10.5 min 后生成 QC 巡检候选三元组；这些候选在冻结的 time model 下显示，并和细胞标注结果隔离。
- 40 min 后生成高保守细胞候选；G2/R1/R2 分别用对应轨道颜色虚线连接到 MS760。
- 将人工审核状态和 audit log 保存到本地 SQLite。
- 顶部“新建项目”支持选择项目保存路径、3 个 LIF 文件和 1 个 MS 文件，生成标注所需中间表并切换到新项目；“打开项目”支持加载已有 parquet + SQLite 项目。
- 导出当前所有 `accepted + exportable` annotation CSV，并在 SQLite `export_runs` 中记录导出时间、过滤条件、行数和 CSV sha256。

当前不做：

- 不做 feature extraction、h5ad、UMAP 或下游分析。

## 显示轨道

当前窗口中显示五条同步轨道：

- `LIF G2 / Day0`
- `LIF R1 / Day9`
- `LIF R2 / Day3`
- `MS 760 / PC34`
- `MS 782 / QC`

MS 760 和 MS 782 使用线性强度显示，不使用 log 坐标。

每条轨道使用相同的同步时间范围，但在各自轨道底部单独显示时间轴和刻度。这样进入后续 0-10.5 min QC anchor 模式时，人工可以直接在每条 LIF/MS 轨道上读取峰时间。

0-10.5 min 全 QC 区段通过连续 2.5 min 页面覆盖：`0.0-2.5`、`2.5-5.0`、`5.0-7.5`、`7.5-10.0`，再通过 `10.0-12.5` 页面查看 10.0-10.5 min 的 QC 尾段。主窗口仍固定 2.5 min；为了避免边界峰断链，软件会额外载入并绘制 ±0.08 min 的边界上下文。这个边界上下文由三元组匹配容差推导，只用于显示和连线，不改变主窗口宽度。

## 自动 shift-only 时间校正

软件启动时会自动使用 0-10.5 min 全 QC 区段估计整体平移：

- `G2 显示平移`：G2 单独估计相对于 MS760 的屏幕显示平移。
- `R1/R2 显示平移`：R1 估计相对于 MS760 的屏幕显示平移，R2 复用同一 red detector 平移。
- `MS760/MS782`：共用原始 MS 时间轴，不移动。

这个结果只是自动建议，不是人工 annotation，也不会写入任何结果文件。

界面默认显示“校正后”视图，也可以切换到“原始”视图对照。校正后视图只改变各轨道在屏幕上的显示位置；每条轨道底部时间轴和峰旁数字仍显示该轨道的原始时间，便于后续保存原始峰时间。

校正后视图中的黑色虚线表示 0-10.5 min 内算法建议的 QC 三元组对齐关系，用于人工审查 shift 是否合理。每条虚线按 `G2 峰 -> R1 峰 -> MS760 event` 串联同一个候选 QC 事件，而不是把 G2 和 R1 分别画成两条独立关系。这样可以避免把本来属于同一个 QC 细胞/事件的三条轨道误读成多个匹配。

密集尾段如果出现多个 G2/R1 pair 和多个 MS760 峰互相竞争，软件不会用单个最近残差让某个峰抢占整段。它会把同一局部组件内的 G2/R1 pair 和 MS760 峰按时间顺序配对，多余的末端候选保存在 `skipped_pair_ids` / `skipped_ms_event_ids` 字段中，供后续人工 accept/reject 或手动修正。

这些虚线只是自动建议，不是最终 annotation。人工 accept/reject 后只写入 `annotation_app/annotations/` 下的本地状态和审计日志，不写回前处理中间表。

## 后段局部 MS 平移规划

软件使用前 10.5 min 的全局 shift-only 结果作为 base model。根据人工流程反馈，冲洗和阀切换后，正式进入待测细胞段前增加一个低自由度的 `MS local delta`，用于轻微移动后段 MS760/MS782 的显示位置。

该节点不写死为 40 min。软件提供项目配置和人工确认：

- `qc_calibration_end_min`：全 QC 校正段结束，当前数据建议为 10.5 min。
- `sample_valve_switch_min`：阀切到上样待测细胞的时间，当前数据约 36 min，但新项目必须重新确认。
- `annotation_start_min`：正式进入后段局部校正、QC 巡检和细胞标注的起点。
- `local_delta_seed_window_min`：从 `annotation_start_min` 开始，用于估计后段 MS local delta 的开头窗口长度。

当前 MVP 暂时把这些项目级时间节点放在 `后段局部校正` 侧栏中填写。未来包装成桌面软件时，更合理的流程是在导入数据后提供一个“项目设置/时间节点确认”步骤：用户先翻看完整 5-track，再填写或确认这些节点，然后进入 QC 校正、后段局部校正、QC 巡检和细胞标注。

`MS local delta` 的自动估计应模拟人工：观察 `annotation_start_min` 后开头一段峰的整体对齐，而不是只看 QC-like anchor。可用证据包括 QC-like 三元组和未标注高可信单通道峰，但只能使用第一性原理字段，如峰时间、SNR、峰间距、MS760 强度、MS782 support、collision/low-quality 风险。不能使用作者 CSV/h5ad、人工 cell label、Day 类型结论或已经人工接受的细胞标注。

特别注意：post-`annotation_start_min` 的 normal-cell-like peaks 只能作为“未标注峰拓扑”参与初始 delta 估计，不能把后续人工 cell label、accepted/rejected 状态、manual_created cell annotation、导出 CSV 或作者参考结果反馈进 time model。local delta estimator 不应读取 `annotations` 表中的细胞标注；如果校准界面允许人工点选对应关系，应保存为独立的 `calibration_anchor_unlabeled/time_delta_calibration`，`exportable=false`，不进入 annotation CSV。

交互上应提供：

- “自动估计 MS 后段平移”按钮。
- `MS local delta sec` 滑块。
- `-0.25 sec / +0.25 sec` 微调按钮。
- 实时 residual 分布、证据数量、相对 QC 段偏移和超限警告。

该平移只改变后段 MS 的 `plot_time_min`，不改变 raw time、峰 ID、event ID 或已接受三元组对应关系。时间模型冻结后，细胞候选和人工细胞标注只能消费该冻结版本。冻结模型需要记录 `time_model_version`、输入 parquet manifest/hash、seed window、方法、delta、残差统计、证据数量、冲突数量、`contains_cell_labels=false` 和 `max_training_time_min`。

`后段局部校正` tab 不能提供细胞 label/accept 按钮，也不通过 `QC 巡检` preview 来判断 delta；人工直接在该 tab 的同步轨道中肉眼检查峰整体对齐，并使用 delta 估计、slider 微调和“冻结 delta”。如果已有 cell annotation 后重新做 delta calibration，软件必须忽略已有 cell labels，并把晚于首个 cell annotation 的新模型标记为 exploratory，不能覆盖生产模型。

冻结后的 delta 仍允许修改，但拖动 slider 或再次点击“自动估计 MS 后段平移”都只是临时预览：浏览器会用 `preview_ms_delta_sec` 重新绘图，不写 SQLite，也不产生新的 `time_model_version`。只有点击“重新冻结”时，软件才把当前预览 delta 写成新的 time model version；旧版本的后段审核记录保留在 SQLite 中但不计入当前版本候选状态。重新冻结后，QC 巡检和细胞标注只消费新的 frozen version。

## 人工审核状态语义

软件把“审核状态”和“来源”分开：

- `review_status=pending`：算法候选已生成，但人工还没有判断。它只用于待审提示，不进入最终 annotation CSV。
- `review_status=accepted`：人工已经接受，可以进入最终 annotation CSV。
- `review_status=rejected`：人工已经拒绝，主视图默认隐藏，但保留在 audit log 和可选审计层中，便于复核和撤销。
- `source=auto_candidate`：来自软件候选生成。
- `source=manual_created`：来自人工手动点击峰建立连线。它不是审核状态；手动创建后通常同时记录为 `accepted`。

MVP 阶段不单独引入 `superseded`。如果一条连线被撤销、更新或替代，软件通过 audit log 记录完整事件链；当前状态表只保留 `pending / accepted / rejected`，避免状态机过早复杂化。

人工误选的手动三元组可以使用“清除”。清除只允许作用于 `source=manual_created` 的记录，会从 SQLite 中删除当前记录和对应 audit 事件，不作为 `pending/rejected` 审核状态保留。自动候选不能清除，因为它们来自可重建的算法候选集，只能在 `pending/accepted/rejected` 之间切换。

候选线显示也应遵循这个语义：

- `pending + auto_candidate` 使用灰黑色虚线，明确表示“软件建议，尚未人工确认”。
- `accepted` 使用有一定透明度的黑色细实线，明确表示“人工已接受”。如果后续人工试用发现遮挡峰形或时间标签，再调整为端点标记或按需显示。
- `rejected` 默认从主视图隐藏，只在“显示已拒绝”审计层中弱化显示。

不建议把 accepted 画成红色虚线。虚线已经表示候选/未确认；红色又容易被理解为错误、警告、拒绝，且会和 R1/red detector 的语义冲突。

候选列表中的 `残差` 在 QC 三元组中指 `MS760 event 时间 - mean(G2 校正后显示时间, R1 校正后显示时间)`，单位为秒。它接近 0 表示 G2、R1 和 MS760 在 shift-only 校正后对齐较好；正值表示 MS760 晚于 LIF 组合中心，负值表示 MS760 早于 LIF 组合中心。细胞标注候选中的 `残差` 指 `MS760 event 时间 - LIF 峰校正后显示时间`。这些残差只是时间对齐证据，不是人工标签。

“接受本窗口待审自动候选”只作用于当前 tab、当前 2.5 min 主窗口内的 `pending + auto_candidate`，不包含 ±0.08 min 边界上下文，不覆盖已拒绝候选，也不修改人工三元组。细胞标注阶段当前禁用整窗接受，要求逐条人工确认，避免把高保守候选误当成自动最终标签。

图上已经绘出的连线支持右键菜单，作为侧栏按钮的快捷入口：

- 自动候选线：`接受 / 拒绝 / 待审`。
- 人工创建线：`接受 / 拒绝 / 清除`。
- 右键菜单调用的仍是同一套后端审核接口，不新增审核状态。
- 开启“选择峰”手动选峰时，连线不响应鼠标事件，避免候选线抢占峰点点击。

## 阶段任务

### QC 校正

用于前 10.5 min 全 QC 区段。候选线按 `G2 峰 -> R1 峰 -> MS760 event` 串联，人工接受的 anchor 用于确认 shift-only 时间校正是否合理。已接受的 0-10.5 min anchor 属于校正证据。

### 后段局部校正

用于冲洗和阀切换后、正式细胞标注前的局部 MS 平移确认。软件从 `annotation_start_min` 后开头一段峰估计 `MS local delta`，人工可以微调并冻结 time model version。

### QC 巡检

用于后段局部校正冻结后继续检查 QC 事件。候选线仍按 `G2 -> R1 -> MS760` 三元组显示，逻辑和 QC 校正段一致，但语义不同：它们是后段 QC 证据和巡检对象，不等同于细胞 annotation。

未冻结 delta 时不生成 QC 巡检候选，也不提供 accept/reject。delta 是否合适只在 `后段局部校正` 阶段通过同步图和残差统计判断。

### 细胞标注

用于 `annotation_start_min` 之后的细胞区段。高保守候选只在满足以下第一性原理条件时生成：LIF 峰 SNR 足够高、LIF 峰间距足够大、MS760 峰强度足够高、MS event 与 LIF 峰在冻结 time model 上唯一近邻、且没有 close/merge/collision/low-quality 风险。连线不使用三元组折线，而是单通道连接到 MS760：

- `G2 / Day0 -> MS760`：绿色虚线。
- `R1 / Day9 -> MS760`：紫色虚线。
- `R2 / Day3 -> MS760`：橙色虚线。

人工细胞标注是严格二元组：必须选择一个 LIF 峰（G2、R1 或 R2）和一个 MS760 PC34 primary event。若该二元组正好等于当前自动 cell candidate，软件直接接受自动候选，避免重复记录；否则保存为 `source=manual_created`、`candidate_type=manual_cell_pair`、`review_status=accepted`。细胞二元组的残差为 `MS760 校正后时间 - LIF 校正后时间`。

细胞标注阶段会显示前面 QC 巡检中已经接受的 QC anchor；如果某个 MS760 event 已经在 QC 巡检中被接受为 QC，它不会再进入自动细胞候选，也不能直接手动建立为 cell pair。若需要改判，应先回到 QC 巡检清除或修改对应 QC 记录。

当前 SQLite 表列只提升了 QC 常用的 `g2_peak_id/r1_peak_id/ms_event_id` 等字段；细胞二元组的 `lif_channel/lif_peak_id/r2_peak_id/lif_raw_time_min/lif_plot_time_min` 等完整字段保存在 `payload_json` 中。CSV 导出会从 payload 重建完整字段，而不是只依赖表列。

## CSV 导出

顶部“导出已接受 CSV”会导出整个项目当前状态，而不是当前 2.5 min 窗口。导出规则：

- 只导出 `review_status=accepted` 且 `exportable=true` 的记录。
- `pending/rejected` 不进入最终 CSV。
- QC 校正、QC 巡检和细胞标注在同一 CSV 中，用 `annotation_kind`、`review_stage`、`candidate_type` 区分语义。
- QC 行允许 `G2/R1` 缺失，缺失峰 ID 在导出中写为 `NA`。
- 细胞行是严格 LIF-MS760 二元组，包含 `lif_channel/lif_peak_id/lif_raw_time_min/lif_plot_time_min`。
- 后段 QC 巡检和细胞标注默认只导出当前 frozen time model version 下的记录，避免重新冻结 delta 后导出旧模型结果。
- 导出的 `annotation_label` 同时配套 `label_source`。例如 `Day3 cell` 表示人工接受了 R2-MS760 二元组，`Day3` 来自原始文件名/项目配置中的通道身份先验，不是作者 CSV/h5ad 标签。
- 导出文件同时下载到浏览器，并保存到 `annotation_app/annotations/exports/`。

## 本地保存

人工审核结果保存在：

- `annotation_app/annotations/annotation.sqlite`：SQLite 数据库，用于重启后恢复界面、保存 audit log、记录输入 manifest，并记录 CSV 导出运行。

数据库中当前使用三类表：

- `annotations`：当前人工状态，一条 annotation 保留最新 `review_status`。
- `audit_events`：追加式审计事件，记录每次 accept/reject/manual create/reset。人工误选清除是例外：它会删除对应当前记录和 audit 事件，不保留误操作痕迹。
- `input_manifest`：当前 app 使用的 4 个第一性原理 parquet 的相对路径、大小和 mtime。
- `project_config.project_table_binding`：4 个 parquet 中间表的全量 SHA256 绑定，用于阻止 SQLite 接到错误项目。
- `export_runs`：每次 CSV 导出的时间、过滤条件、行数、CSV 文件路径和 sha256。

写入时，当前状态更新和 audit 事件插入在同一个 SQLite 事务中提交，避免 JSON/JSONL 方案中“状态和审计日志不一致”的问题。旧的 `annotation_state.json` 如果存在且 SQLite 为空，会在启动时自动迁移一次；之后新的写入只走 SQLite。

该目录已加入 `.gitignore`，不应提交到代码仓库。浏览器端只提交候选 ID、动作或手动选择的峰 ID；label、residual、原始时间等证据字段由后端从第一性原理 parquet 和候选表重建，避免客户端注入或混入作者 CSV/h5ad 信息。

## 项目导入与输入文件

打包版用户界面应使用“第一性原理前处理 / 中间表 / 标注项目”命名，不向用户暴露历史版本号。当前仓库内部仍保留历史脚本和目录作为兼容层，但新增 UI、导出字段和项目导入说明不应使用历史版本号命名。

顶部“新建项目”执行以下步骤：

1. 用户填写新的项目保存路径。
2. 用户填写 3 个 LIF 原始文件路径：`G2`、`R1`、`R2`。
3. 用户填写 1 个 MS 原始文件路径。
4. 用户确认通道身份先验，默认 `G2=Day0`、`R1=Day9`、`R2=Day3`。
5. 用户选择 raw inputs 管理方式：`external_reference` 不复制原始文件；`copy_into_project` 将原始文件复制到 `raw_inputs/`。
6. 软件写入 `lifms_project.json` 和输入锁，只包含这 4 个原始输入。
7. 软件运行 LIF trace/peak calling 和 MS event calling，生成浏览标注所需的 4 个 parquet 中间表。
8. 新建成功后，浏览器切换到新项目自己的 SQLite 和中间表。

新建过程不读取作者 CSV、h5ad、manual、V2 或 archive 输入。

顶部“打开项目”按项目目录加载标准中间表和 `annotation_app/annotations/annotation.sqlite`。如果存在 `lifms_project.json`，软件会先按 manifest 指向的路径加载并校验 4 个 parquet 的全量 SHA256；随后校验 SQLite 自身保存的 `project_table_binding`，并确认 SQLite 中引用的 LIF peak_id 和 MS event_id 均存在于当前 parquet，校验通过后才写入运行态记录。没有 manifest 的旧 Linux 项目只允许走一次 legacy adoption：通过 SQLite `input_manifest` 大小记录和 ID 完整性检查后，软件会补写 `lifms_project.json` 和 SQLite binding。

当前兼容层读取的 4 个中间表：

- LIF trace table
- LIF peak table
- MS event table
- MS scan summary table

默认项目路径模型：

- `--project-dir` 指向项目根目录，默认是当前 `scMetab` 仓库。
- `--raw-data-dir` 指向原始数据目录，当前 MVP 只记录该路径，不直接读取作者 CSV/h5ad。
- `--annotation-db` 指向人工标注 SQLite，默认是 `annotation_app/annotations/annotation.sqlite`。

输入边界：

- MVP-1 不读取作者 CSV。
- MVP-1 不读取 h5ad。
- MVP-1 不读取 V2、manual、override 或 archive 数据。
- 当前候选生成不加载作者 CSV/h5ad，也不加载 V2、manual、override 或 archive 数据。

当前 QC 三元组候选和高保守细胞候选由 app 内基于 LIF peaks、MS events、MS scan summary 的第一性原理时间匹配生成，不读取作者 CSV/h5ad。后续如果接入外部候选 parquet，也必须只使用不依赖作者 CSV/h5ad 的第一性原理字段。

## QC anchor 人工规则

`QC 校正` 和 `QC 巡检` 的人工规则不同：

- `QC 校正` 用于前 10.5 min 的 shift-only 校正确认，人工 anchor 必须同时包含 `G2 + R1 + MS760`。
- `QC 巡检` 用于后段 QC 证据巡检，允许某个 LIF 通道峰缺失；人工 anchor 必须包含 `MS760`，并且 `G2/R1` 至少选择一个。
- QC 巡检中缺失的 `G2` 或 `R1` 在 SQLite payload、hover 和列表中保存/显示为 `NA`，对应数据库列为 `NULL`。
- partial anchor 的残差定义为 `MS760 校正后时间 - 已选择 G2/R1 校正后时间均值`；只有一个 LIF 峰时就是 MS760 与该 LIF 峰的时间差。
- API 会校验手动选择的 MS 事件必须是 `pc34_primary / pc34_760_max_intensity`，避免把非 MS760 事件写成 QC anchor。
- 如果在 QC 巡检中人工选择了完整 `G2 + R1 + MS760`，且它正好等同于当前 post-QC 自动候选，则直接接受该自动候选，不额外创建重复的 `manual_created` 记录。

## 泄露与泛化边界

当前 MVP 不涉及人工标签泄露：

- 后端只读取 4 个第一性原理前处理中间表：LIF trace、LIF peaks、MS events、MS scan summary。
- LIF 峰来自原始 LIF trace 的 baseline/noise/peak calling，不来自作者 CSV、h5ad 或人工补峰。
- MS event 来自原始 MS txt 中预先指定的 PC34/760.5851 和 QC support 782.5616 marker，不来自作者标签或下游 h5ad/UMAP。
- shift-only 校正和 QC 三元组匹配只使用 0-10.5 min 全 QC 实验事实、G2/R1/MS760 的原始峰时间、时间平移和残差容差。
- `Rd0 / Day0`、`Rd1 / Day3`、`Rd3 / Day9` 是实验通道身份先验。当前 app 会优先从原始数据文件名（例如 `CAR-T_Day0-G2_Day3-R2_Day9-R1_batch03.txt` 或 LIF CSV 文件名）推断 `G2/R2/R1 -> Day0/Day3/Day9`，导出时写入 `channel_identity_prior` 和 `channel_identity_prior_source`，不从作者 CSV/h5ad 推断。
- `phase` 字段是按时间切段生成的辅助字段，不是人工标签；当前 app 的 QC 三元组匹配不依赖该字段。

因此，这套 MVP 原理上可以迁移到新的 LIF-MS 数据，但前提是新数据具备同等的原始输入和实验先验：LIF G2/R1/R2 原始轨道、MS 760/782 marker、前 10.5 min 全 QC 校准段，并重新运行对应的第一性原理前处理。新数据如果更换 marker、通道命名、采集窗口或 QC 时长，需要先更新这些显式实验先验，而不是导入旧人工标签。

## 本机启动

使用项目 conda 环境：

```bash
conda run -n scMetab python annotation_app/app.py --host 127.0.0.1 --port 8050
```

浏览器打开：

```text
http://127.0.0.1:8050/
```

## Tailscale 远程访问

如果需要从同一 Tailscale 网络中的其他设备访问，需要绑定到非 loopback 地址：

```bash
conda run -n scMetab python annotation_app/app.py --host 0.0.0.0 --port 8050
```

然后打开：

```text
http://100.76.38.103:8050/
```

注意：IPv4 地址不要加方括号。方括号只用于 IPv6 literal。

绑定 `0.0.0.0` 时服务没有认证，请只在可信 Tailscale 网络内使用。

## Windows 打包

Windows 打包文件位于：

```text
packaging/windows/
```

构建：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
```

运行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1
```

首次启动如果没有加载项目，页面会进入“等待新建或打开项目”状态。用户可以直接新建项目，或打开已有项目目录。打包产物不包含当前 SQLite 标注库、exports 或 evaluation；这些运行数据都保存在用户选择的项目目录。

打包后必须按 `packaging/windows/SMOKE_TEST.md` 在 Windows 真机上测试，尤其是路径含空格/中文、大 MS 文件导入、重启恢复和 CSV 导出。

## 依赖

不需要 Dash 或 Plotly。后端使用 Python 标准库 HTTP server 加 `pandas`/`pyarrow`，前端使用浏览器原生 SVG 绘图。
