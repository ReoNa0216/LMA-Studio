# LMA Studio Developer Notes

这是一个本地桌面应用，用于人工辅助浏览 LIF-MS 同步数据，并按“QC 校正 / 后段局部校正 / 事件标注”三个界面阶段审核候选连线。事件标注阶段用显式筛选与手工模式区分 QC 巡检和细胞标注；SQLite 仍保留两种独立语义和历史 ID，不做破坏性迁移。

## 当前范围

当前版本支持以下功能：

- 读取由第一性原理前处理生成的 parquet 中间表；也可以通过顶部“新建项目”从 2-4 个 LIF 原始文件、1 个 MS 原始文件和 1 个单细胞事件坐标 CSV 生成项目。
- 按 2.5 min 同步窗口显示 LIF/MS 数据。
- 左右翻页或输入起始时间跳转；普通浏览窗口固定为 2.5 min。
- 右侧动态轨道图上方的“图窗起点”只控制当前浏览窗口起点；它不是项目级 `annotation_start_min`。
- 在每个识别出的 LIF 峰和 MS event 峰旁显示对应时间。
- 每条轨道都有自己的时间轴，便于后续人工 QC anchor 对齐。
- 鼠标悬停峰点时显示该峰或 event 的源字段。
- 自动基于 0-10.5 min 全 QC 区段估计 shift-only 时间校正，并支持“校正后/原始”时间轴切换。
- 对自动 QC anchor 组进行 `pending / accepted / rejected` 审核。项目可配置 2-4 个 QC anchor 通道。
- 每条 LIF 可独立配置为 QC-only、cell-only、both 或 disabled；QC 证据和细胞二元组只消费各自角色允许的通道。
- 在手动模式下建立人工连线：QC 模式按 anchor 配置选择 LIF 峰和 MS760；细胞模式只允许 cell 角色通道与 MS760 建立严格二元组。
- 通过任务阶段切换候选列表和连线显示：`QC 校正 / 后段局部校正 / 事件标注`。第三阶段内可切换“全部 / QC / 细胞”。
- 后段局部校正冻结前，QC 巡检和细胞标注候选不会生成，侧栏也不显示后段人工审核工具；后端同时拒绝直接 review 后段候选。
- QC 校正段后生成 QC 巡检候选 anchor 组；这些候选在冻结的 time model 下显示，并和细胞标注保持独立语义。
- `annotation_start_min` 后生成高保守细胞候选；只有 canonical event map 白名单内的 `ms_event_id` 能进入第三阶段候选或后端手工写入。
- 独立 UMAP 窗口只显示 canonical 五列表的坐标。颜色完全由当前 SQLite accepted 状态投影；主窗口写入会立即同步，点击 UMAP 点会让主窗口定位到同一 `ms_event_id`。
- 将人工审核状态和 audit log 保存到本地 SQLite。
- 顶部“新建项目”支持动态添加/删除 2-4 个 LIF 输入、配置两类角色并选择事件坐标 CSV；“打开项目”支持加载已有 parquet + SQLite 项目。
- 导出当前所有 `accepted + exportable` annotation CSV，并在 SQLite `export_runs` 中记录导出时间、过滤条件、行数和 CSV sha256。

当前不做：

- 不做 feature extraction、h5ad、UMAP 降维计算、标签传播、分类器训练或下游分析；这里只消费外部 UMAP 坐标用于审核导航。

## 显示轨道

当前窗口显示项目配置的 2-4 条 LIF 轨道，以及固定的 `MS 760 / PC34`、`MS 782 / QC`。原始 trace 不会因角色或 map 白名单被裁剪：角色只控制阶段参与，map 只控制第三阶段 MS event 的可交互性。

MS 760 和 MS 782 使用线性强度显示，不使用 log 坐标。

每条轨道使用相同的同步时间范围，但在各自轨道底部单独显示时间轴和刻度。这样进入后续 0-10.5 min QC anchor 模式时，人工可以直接在每条 LIF/MS 轨道上读取峰时间。

0-10.5 min 全 QC 区段通过连续 2.5 min 页面覆盖：`0.0-2.5`、`2.5-5.0`、`5.0-7.5`、`7.5-10.0`，再通过 `10.0-12.5` 页面查看 10.0-10.5 min 的 QC 尾段。主窗口仍固定 2.5 min；为了避免边界峰断链，软件会额外载入并绘制边界上下文。该上下文只用于显示和连线，不改变主窗口宽度。

## 自动 shift-only 时间校正

软件启动时会自动使用 0-10.5 min 全 QC 区段估计整体平移：

- 每条物理 `time_axis` 只估计一个 shift。默认 G1/G2 共用 `green_axis`，R1/R2 共用 `red_axis`。
- 同轴多个 QC anchor 通道共同增强该轴估计的证据，但不各自投票为多个独立 shift，也不会改变物理自由度。
- `MS760/MS782` 共用原始 MS 时间轴，在全局 QC 校正中不移动。
- 没有新布局配置的旧项目继续按 G2/R1/R2 加 G2/R1 anchor 的旧二通道算法解释，候选 ID 和既有审核状态保持兼容。只有该规范旧布局允许把没有 layout hash 的历史 time model 一次性绑定到当前布局；其他新布局不得静默继承未绑定的 frozen model，必须清除旧模型并重新校正。

这个结果只是自动建议，不是人工 annotation，也不会写入任何结果文件。

界面默认显示“校正后”视图，也可以切换到“原始”视图对照。校正后视图只改变各轨道在屏幕上的显示位置；每条轨道底部时间轴和峰旁数字仍显示该轨道的原始时间，便于后续保存原始峰时间。

校正后视图中的黑色虚线表示 0-10.5 min 内算法建议的 QC anchor 组，用于人工审查 shift 是否合理。每条线按项目配置顺序串联当前事件中存在的 LIF anchor，最后连接到 MS760。多通道算法要求每条物理轴至少有一个 anchor；同轴通道彼此超出容差时只保留相干证据，并将其余通道记为冲突，避免拼出虚假的完整 anchor 组。

密集尾段如果出现多个 G2/R1 pair 和多个 MS760 峰互相竞争，软件不会用单个最近残差让某个峰抢占整段。它会把同一局部组件内的 G2/R1 pair 和 MS760 峰按时间顺序配对，多余的末端候选保存在 `skipped_pair_ids` / `skipped_ms_event_ids` 字段中，供后续人工 accept/reject 或手动修正。

这些虚线只是自动建议，不是最终 annotation。人工 accept/reject 后只写入 `annotation_app/annotations/` 下的本地状态和审计日志，不写回前处理中间表。

完成 QC anchor 审核后，用户可在 `QC 校正` 侧栏显式执行“基于已接受 anchors 预览重算”：

- 每条 accepted QC annotation 按原始 peak/event ID 还原为 `MS760 raw time - LIF axis raw time`，同轴多通道先取中位数，不增加时间自由度。
- 每条物理轴至少需要 2 个独立且一致的 accepted anchors；同轴跨度超限、重复 MS 证据矛盾、非 MS760 事件、布局不一致和超出支持范围的 shift 都会被拒绝或记为冲突。
- 各轴使用 median/MAD 稳健拟合，同时限制异常阈值不超过 QC 匹配容差；稳健内点的 P90 绝对残差超过 3 sec 时拒绝应用。界面显示旧 shift、新 shift、内点数和冲突数。预览不写 SQLite，也不会让轨道在审核过程中自行跳动。
- 只有再次点击“应用 QC 对齐”才把模型、证据摘要和 `preview_hash` 写入项目 SQLite。若 accepted anchors 在预览后发生变化，旧预览不能应用。
- 应用新的 QC 基础模型会让旧的后段 draft/frozen time model 失效，并创建 `MS local delta = 0` 的新 draft；用户必须重新进行后段局部校正。

已应用模型随项目重新打开时自动恢复。修改 `qc_calibration_end_min` 会要求明确清除该模型；修改标注起点或 seed 窗口不会改变 QC 基础 shift。

模型应用后若继续接受、拒绝或清除 QC 校正段 anchor，软件会先要求确认，并在同一 SQLite 事务中清除已应用 QC 模型、停用下游 draft/frozen time model、再写入新的审核状态。轨道退回自动 QC 建议，用户需重新预览并应用 QC 对齐。QC 巡检和细胞标注记录不属于该基础模型证据，不触发这项失效规则。

## 后段局部 MS 平移规划

软件使用前 10.5 min 的全局 shift-only 结果作为 base model。根据人工流程反馈，冲洗和阀切换后，正式进入待测细胞段前增加一个低自由度的 `MS local delta`，用于轻微移动后段 MS760/MS782 的显示位置。

该节点不写死为 40 min。软件提供项目配置和人工确认：

- `qc_calibration_end_min`：全 QC 校正段结束，当前数据建议为 10.5 min。
- `annotation_start_min`：正式进入后段局部校正、QC 巡检和细胞标注的起点。
- `local_delta_seed_window_min`：从 `annotation_start_min` 开始，用于估计后段 MS local delta 的开头窗口长度。

这些项目级时间节点放在顶部“配置”窗口中。修改已冻结模型依赖的时间节点时，软件会先请求确认；确认后旧 frozen time model 失效，用户必须重新进入“后段局部校正”并冻结新 delta。

`MS local delta` 的自动估计观察 `annotation_start_min` 后 seed 窗口中的未标注峰拓扑。严格旧 G2/R1 项目保留经现有项目验证的 pair matcher；其他二通道和多通道 anchor 项目使用 axis-aware anchor set matcher，并优先比较唯一事件数、完整 anchor 组数、通道支持数、冲突数和残差。所有 matcher 都要求至少 2 个唯一事件；存在跨轴不一致、证据不足或相隔较远的近似并列解时，不自动应用建议，要求用户扩大 seed 窗口或通过轨道图和滑块人工确认。算法不能使用作者 CSV/h5ad、人工 cell label、Day 类型结论或已经人工接受的细胞标注。

特别注意：post-`annotation_start_min` 的 normal-cell-like peaks 只能作为“未标注峰拓扑”参与初始 delta 估计，不能把后续人工 cell label、accepted/rejected 状态、manual_created cell annotation、导出 CSV 或作者参考结果反馈进 time model。local delta estimator 不应读取 `annotations` 表中的细胞标注；如果校准界面允许人工点选对应关系，应保存为独立的 `calibration_anchor_unlabeled/time_delta_calibration`，`exportable=false`，不进入 annotation CSV。

交互上应提供：

- “自动估计 MS 后段平移”按钮。
- `MS local delta sec` 滑块。
- `-0.25 sec / +0.25 sec` 微调按钮。
- 实时 residual 分布、证据数量、相对 QC 段偏移和超限警告。

该平移只改变后段 MS 的 `plot_time_min`，不改变 raw time、峰 ID、event ID 或已接受 anchor 对应关系。时间模型冻结后，细胞候选和人工细胞标注只能消费该冻结版本。冻结模型记录 `time_model_version`、输入 parquet manifest/hash、acquisition layout hash、seed window、matcher、delta、残差统计、证据数量、冲突数量、`contains_cell_labels=false` 和 `max_training_time_min`。

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

人工误选的手动 anchor 可以使用“清除”。清除只允许作用于 `source=manual_created` 的记录，会从 SQLite 中删除当前记录和对应 audit 事件，不作为 `pending/rejected` 审核状态保留。自动候选不能清除，因为它们来自可重建的算法候选集，只能在 `pending/accepted/rejected` 之间切换。

候选线显示也应遵循这个语义：

- `pending + auto_candidate` 使用灰黑色虚线，明确表示“软件建议，尚未人工确认”。
- `accepted` 使用有一定透明度的黑色细实线，明确表示“人工已接受”。如果后续人工试用发现遮挡峰形或时间标签，再调整为端点标记或按需显示。
- `rejected` 默认从主视图隐藏，只在“显示已拒绝”审计层中弱化显示。

不建议把 accepted 画成红色虚线。虚线已经表示候选/未确认；红色又容易被理解为错误、警告、拒绝，且会和 R1/red detector 的语义冲突。

候选列表中的 QC `残差` 指 `MS760 event 时间 - LIF 轴组合时间`，单位为秒。同轴多个通道先取中位数得到轴时间，再对各物理轴等权聚合，防止 G1/G2 这类同轴重复测量压过 red_axis。细胞标注候选中的 `残差` 指 `MS760 event 时间 - LIF 峰校正后显示时间`。这些残差只是时间对齐证据，不是人工标签。

“接受本窗口待审自动候选”只作用于当前 tab、当前 2.5 min 主窗口内的 `pending + auto_candidate`，不包含边界上下文，不覆盖已拒绝候选，也不修改人工 anchor。动态 anchor 缺少同轴冗余通道、跨轴跨度超限、存在竞争匹配冲突或任一轴残差超限时都不会被整窗批量接受。细胞标注阶段当前禁用整窗接受，要求逐条人工确认。

图上已经绘出的连线支持右键菜单，作为侧栏按钮的快捷入口：

- 自动候选线：`接受 / 拒绝 / 待审`。
- 人工创建线：`接受 / 拒绝 / 清除`。
- 右键菜单调用的仍是同一套后端审核接口，不新增审核状态。
- 开启“选择峰”手动选峰时，连线不响应鼠标事件，避免候选线抢占峰点点击。

## 阶段任务

### QC 校正

用于前 10.5 min 全 QC 区段。候选线按项目配置的 anchor 通道依次连接到 MS760，人工接受的 anchor 用于确认 axis-aware shift-only 时间校正是否合理。已接受的 QC 段 anchor 属于校正证据。

### 后段局部校正

用于冲洗和阀切换后、正式细胞标注前的局部 MS 平移确认。软件从 `annotation_start_min` 后开头一段峰估计 `MS local delta`，人工可以微调并冻结 time model version。

### 事件标注

第三阶段把 QC 巡检和细胞标注放在同一时间窗口中，以“全部 / QC / 细胞”筛选和“QC anchor / 细胞二元组”手工模式区分。两类审核可任意先后，但同一个 `ms_event_id` 在当前 time model 下只能有一种 active accepted 语义；改判必须先撤销原记录。第三阶段禁用整窗批量接受。

#### QC 巡检

用于后段局部校正冻结后继续检查 QC 事件。候选线沿用项目的动态 anchor 集合，逻辑和 QC 校正段一致，但语义不同：它们是后段 QC 证据和巡检对象，不等同于细胞 annotation。

未冻结 delta 时不生成 QC 巡检候选，也不提供 accept/reject。delta 是否合适只在 `后段局部校正` 阶段通过同步图和残差统计判断。

#### 细胞标注

用于 `annotation_start_min` 之后的细胞区段。高保守候选只在满足以下第一性原理条件时生成：LIF 峰 SNR 足够高、LIF 峰间距足够大、MS760 峰强度足够高、MS event 与 LIF 峰在冻结 time model 上唯一近邻、且没有 close/merge/collision/low-quality 风险。连线不使用三元组折线，而是单通道连接到 MS760：

- `G2 / Day0 -> MS760`：绿色虚线。
- `R1 / Day9 -> MS760`：紫色虚线。
- `R2 / Day3 -> MS760`：橙色虚线。

人工细胞标注是严格二元组：必须选择一个具有 cell 角色的 LIF 峰和一个 map 白名单内的 MS760 PC34 primary event。若该二元组正好等于当前自动 cell candidate，软件直接接受自动候选，避免重复记录；否则保存为 `source=manual_created`、`candidate_type=manual_cell_pair`、`review_status=accepted`。细胞二元组的残差为 `MS760 校正后时间 - LIF 校正后时间`。

细胞标注阶段会显示前面 QC 巡检中已经接受的 QC anchor；如果某个 MS760 event 已经在 QC 巡检中被接受为 QC，它不会再进入自动细胞候选，也不能直接手动建立为 cell pair。若需要改判，应先回到 QC 巡检清除或修改对应 QC 记录。

当前 SQLite 保留 `g2_peak_id/r1_peak_id/ms_event_id` 作为旧项目索引兼容列；动态 QC anchor 的完整通道、peak ID、raw/plot time 和 time axis 保存在 `payload_json` 的 `lif_anchor_*` 字段中。CSV 导出会从 payload 重建 G1/G2/R1/R2 兼容列和标准 JSON 映射，不依赖固定二通道表列。

## CSV 导出

顶部“导出已接受 CSV”会导出整个项目当前状态，而不是当前 2.5 min 窗口。导出规则：

- 只导出 `review_status=accepted` 且 `exportable=true` 的记录。
- `pending/rejected` 不进入最终 CSV。
- QC 校正、QC 巡检和细胞标注在同一 CSV 中，用 `annotation_kind`、`review_stage`、`candidate_type` 区分语义。
- QC 行按项目 anchor 集合导出；缺失通道在兼容峰 ID 列写为 `NA`，完整动态映射写入 `qc_anchor_*_json`。
- 细胞行是严格 LIF-MS760 二元组，包含 `lif_channel/lif_peak_id/lif_raw_time_min/lif_plot_time_min`。
- 后段 QC 巡检和细胞标注默认只导出当前 frozen time model version 下的记录，避免重新冻结 delta 后导出旧模型结果。
- 第三阶段行追加 `UMAP1/UMAP2/cell_event_map_sha256`；前段 QC 校正行保持空值。
- source CSV 的 `Type/leiden/CellNumber` 从未载入 canonical map，也不会进入导出。
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
2. 用户动态配置 2-4 个 LIF 原始文件，为每个文件填写真实通道名、身份先验，并独立选择 QC 与 cell 角色；通道不固定为 G2/R1/R2。
3. 用户填写 1 个 MS 原始文件路径。
4. 用户选择事件坐标 CSV。导入器只读取 `scan_start_time/UMAP1/UMAP2`，以 0.01 sec 容差一对一匹配 `pc34_primary / pc34_760_max_intensity`，并把 canonical 五列表复制进项目。
5. QC anchor 必须选择 2-4 个、覆盖所有 cell 角色使用的物理时间轴，并至少包含一个 green 和一个 red detector；至少一个通道必须具有 cell 角色。
6. 用户选择 raw inputs 管理方式：`external_reference` 不复制原始文件；`copy_into_project` 将原始文件复制到 `raw_inputs/`。
7. 软件在同级 staging 目录运行前处理、写入 manifest/canonical map 并验证完整性；全部成功后用单次目录重命名发布，失败不会留下半成品项目。
8. 最终路径发布后才初始化项目 SQLite 并切换到新项目。

新建过程不读取作者 CSV、h5ad、manual、V2 或 archive 输入。

顶部“打开项目”按项目目录加载标准中间表和 `annotation_app/annotations/annotation.sqlite`。如果存在 `lifms_project.json`，软件先校验 4 个 parquet 的全量 SHA256、SQLite `project_table_binding`，并确认历史 peak/event ID 仍存在。schema-v1 项目按原布局只读解释，打开不会补写 manifest、binding、input manifest 或 time-model layout hash。没有 event map 的旧项目仍按原未过滤第三阶段工作；用户可在配置页显式执行一次性附加，附加前会确认所有已有 accepted 后段 event 都在新 map 中。

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

- 四个第一性原理 parquet 仍是 trace/peak/event 的唯一数据源。
- event-map source CSV 只允许坐标三列；`Type/leiden/CellNumber` 等额外列不载入。
- 不读取 h5ad、V2、manual、override 或 archive 数据。
- 所有长期关联与状态同步使用 canonical `ms_event_id`，不使用浮点时间或 UMAP 行号。

当前 QC anchor 组和高保守细胞候选由 app 内基于 LIF peaks、MS events、MS scan summary 的第一性原理时间匹配生成，不读取作者 CSV/h5ad。后续如果接入外部候选 parquet，也必须只使用不依赖作者 CSV/h5ad 的第一性原理字段。

## QC anchor 人工规则

`QC 校正` 和 `QC 巡检` 的人工规则不同：

- `QC 校正` 用于 QC 段的 shift-only 校正确认，人工 anchor 必须包含 `MS760`，并从配置的 anchor 通道中选择足以覆盖全部物理时间轴的峰；同轴冗余通道可缺失。
- `QC 巡检` 用于后段 QC 证据巡检，允许任意 LIF anchor 通道缺失；人工 anchor 必须包含 `MS760`，并至少选择一个 LIF 峰。
- 缺失 anchor 在 SQLite payload、hover 和列表中保存/显示为 `NA`，兼容数据库列为 `NULL`。
- partial anchor 的残差定义为 `MS760 校正后时间 - 已选择 LIF 的轴组合时间`；只有一个 LIF 峰时就是 MS760 与该 LIF 峰的时间差。
- API 会校验手动选择的 MS 事件必须是 `pc34_primary / pc34_760_max_intensity`，避免把非 MS760 事件写成 QC anchor。
- 如果在 QC 巡检中人工选择的动态 anchor 集合正好等同于当前 post-QC 自动候选，则直接接受该自动候选，不额外创建重复的 `manual_created` 记录。

## 泄露与泛化边界

当前实现把坐标输入和标签输入严格分离：

- 后端用 4 个第一性原理中间表生成轨道、peak/event 与候选；event-map CSV 只提供三列坐标和第三阶段白名单。
- LIF 峰来自原始 LIF trace 的 baseline/noise/peak calling，不来自作者 CSV、h5ad 或人工补峰。
- MS event 来自原始 MS txt 中预先指定的 PC34/760.5851 和 QC support 782.5616 marker，不来自作者标签或 h5ad。
- UMAP 初始颜色不来自 source CSV；未标注为灰、accepted QC 为黑、accepted cell 使用对应 LIF 通道色，冲突由 SQLite 当前状态显式计算。
- shift-only 校正和 QC anchor 匹配只使用 QC 段实验事实、项目配置 anchor/MS760 的原始峰时间、物理 time axis、时间平移和残差容差。
- `Rd0 / Day0`、`Rd1 / Day3`、`Rd3 / Day9` 是实验通道身份先验。当前 app 会优先从原始数据文件名（例如 `CAR-T_Day0-G2_Day3-R2_Day9-R1_batch03.txt` 或 LIF CSV 文件名）推断 `G2/R2/R1 -> Day0/Day3/Day9`，导出时写入 `channel_identity_prior` 和 `channel_identity_prior_source`，不从作者 CSV/h5ad 推断。
- `phase` 字段是按时间切段生成的辅助字段，不是人工标签；当前 app 的 QC anchor 匹配不依赖该字段。

因此，这套流程可迁移到任意 2-4 通道布局，前提是项目明确记录通道角色、QC anchor 集合与 time axis、MS 760/782 marker、QC 校准段和 canonical event map，并重新运行第一性原理前处理。新数据如果更换 marker、采集窗口或 QC 时长，需要先扩展并确认这些显式实验先验，而不是导入旧人工标签。

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
