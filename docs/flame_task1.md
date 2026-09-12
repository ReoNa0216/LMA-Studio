# FLAME 任务 1 接入说明

本轮候选使用独立包 `flame-ms-core==0.1.0`，两 Studio 不互相导入产品代码。共用解析、自动 calling、采集时间、身份和交换校验；UI、SQLite、项目保存仍属各 Studio。

## 安装与构建

固定来源为 `ReoNa0216/flame-ms-core` 的 `v0.1.0` 私有 release，代码 commit `90f31db270f2aa004220e3e84a6207393c15966e`。wheel SHA256、文件名和来源见 `packaging/flame-ms-core.json`。Windows/Mac 脚本在安装前校验哈希，冻结包收集正式安装的内核。

本地可用相邻仓库的 `dist/flame_ms_core-0.1.0-py3-none-any.whl`，或设置 `FLAME_MS_CORE_WHEEL` 指向已下载文件。已登录并有读取权限的 GitHub CLI 可执行：

```text
python scripts/resolve_flame_core.py --download
```

CI 首选 `FLAME_MS_CORE_WHEEL_BASE64` secret 中的锁定 wheel（仅约 25 KB 的安装产物，不是登录凭据），解码后必须与 checkout 的 SHA256 相同。未配置时仍可用 `FLAME_MS_CORE_READ_TOKEN` 读取私有 release；权限不足明确失败。secret 不会公开安装包源码或写入 Git。最终托管构建与 Mac 真机验收状态以共享交接为准。

## 事件传递

项目 ZIP 与事件包用途不同，见[项目分享](project_sharing.md)。

MS Event Studio 选择“传给 LMA Studio”，生成 `ms-lma-handoff-v1` ZIP，内含正式 v2 `events/` 和可选 `features/`。LMA 新建项目选择“LMA 事件包 ZIP（可含矩阵）”，并提供原 MS 文件和 LIF 输入；无矩阵也可建项。新建界面只保留 ZIP 入口；过去从正式 v2 文件夹建立的项目仍可打开，底层读取保留。分析 ZIP 不用于交接。CSV 审阅结果供人阅读；正式传递必须完整包，不能手改列名替代。

v2 包包含 `events.parquet`、`manifest.json`、`checksums.sha256`。完整合同见内核 `docs/event-package-v2.md`。原始自动身份包含 raw SHA、方法版本和 generation；当前事件身份、修订、原始及当前 scan/时间/支持窗、审阅状态分别保留。两个 Studio 的项目 UUID 可不同。

LMA 导入保留所有事件及顺序，只有 accepted 进入标注名单。pending、rejected、unreviewed 仍留在工作表和不可变原包中。raw 严格解析仅用于曲线及原始/当前物理位置校验，导入不重新 MS calling，不走旧 CSV roster 补峰。

MS 事件名单、峰顶或审阅状态变化后，用新版事件包新建 LMA 项目；当前配置不提供事件更新或差异预览入口。已有项目仅允许补充同事件、同版本的矩阵，保留人工标注和时间模型。发布前完整校验在 sibling staging 完成，失败回滚，原项目不受影响。

## 旧项目与科学边界

v0.4.0+ 项目加载沿用已保存事件、配对、标签、模型、名单顺序与绑定。无编辑打开不初始化/迁移 DB，不重算、不刷新绑定。缺 DB 或绑定错误明确失败。SQLite 关闭最后一个读取连接可能移除原本为空的 WAL 及配套 SHM；持久化科学内容不变。测试始终使用隔离副本。

新建独立分析使用共用 caller，得到稳定自动来源身份。旧 LMA 用扫描间隔近似计算峰宽，新核用实际采集时间；新分析不能覆盖历史投稿结果。±15 ppm 人工名单支持通道保留，自动 primary 使用 ±12 ppm，二者不合并。

任务 2 等待专门数据，仅未来验收 label-correct；人工标签不是独立真值。标签与 feature 按事件 ID 并行产出。LIF→MS 采集时间对齐 QC 与跨批参照细胞不同，FLAME 没有色谱保留时间。HSC 特定参数不作为通用默认。测量、背景/质量证据、置信度、算法表示、化学注释和 metabolic state 分别表达。任务 3 使用 Linux 交付的 HRGC，由 MS Event Studio 提取并交接至 LMA；任务 4 只保留接口，未实现批次校正模型。

## 验收证据

当前 Windows 候选及真实数据回归见父工作区 `studio-validation/validation.json`；共享交接待用户验收和双平台 Release 完成后更新。本机原项目不等于尚未取得的正式投稿项目，不能宣称逐投稿项目验收。Windows 人工 UAT 后再安排 macOS 真机可见验收。

## 带标签分析结果

顶部“导出结果”统一生成可自命名的 ZIP，包含 `cells_and_qc.csv`、`export_record.json`；项目有矩阵时可同时包含 `labeled_matrix.h5ad`。这是下游分析副本，不是 MS → LMA 事件包，也不替代分享项目。已有 CSV 投影规则及内部旧导出接口保留。

H5AD 按稳定事件 ID 关联当前已接受的人工标签，新增 Type、annotation_status、is_qc、LIF_channel、annotation_id 和 UMAP1/UMAP2。未标注、待审和已拒绝均保持 unknown；不使用模型预测补充人工标签。is_qc 仅来自已接受的后段 QC 关系，不从 MS 信号自动推断。

导出矩阵须已确认前段边界并保存，仅保留“事件标注起点”及之后的原矩阵行，后段 QC 保留并标记。CSV 沿用完整事件表范围，因此两者行数可以不同；记录包含范围、排除数量、原矩阵哈希及产物校验。原矩阵的强度、NaN、feature 轴及项目标注不改写。若当前原生 UMAP 对应旧范围，CSV 和 H5AD 同时省略坐标，避免两份结果不一致。ZIP 在临时目录完整生成后无覆盖发布，失败不留下半成品。

## 矩阵与原生 UMAP（Windows 联合验收候选）

MS 的 LMA 交接 ZIP 可包含原始 HRGC 矩阵和完整 v2 事件包。LMA 新建时一起接收；已有正式 v2 项目在“配置 → 矩阵与原生 UMAP → 从 ZIP 导入矩阵…”导入同一 ZIP。必须匹配原 MS SHA256、完整上游事件版本及矩阵逐行事件 ID；不按时间近似补绑。提取时手动填写的 QC 时间段可使矩阵成为事件名单的子集；MS 不自动识别 QC。旧 CSV 项目仍可照常打开及使用原坐标，但无法证明正式事件身份时，应另建项目接入矩阵。

“计算 UMAP”在后台执行并显示进度。结果保存在项目内；重开无需重算。同时存在外部 CSV 和原生坐标时，配置可切换“导入的 CSV 坐标 / 原生 UMAP”；CSV 导出使用当前视图，未参与该坐标计算的事件坐标留空。新导入 CSV 严格使用 scan_start_time、UMAP1、UMAP2，不接受 UMAP 别名；旧保存项目的 canonical 坐标仍可读取。切换不修改事件名单、人工标注、配对或时间模型。原生视图默认按采集时间着色，也可查看人工标注，点击点仍定位同一事件。

原始 float64/NaN 矩阵保持原样。计算须先确认全部前段边界并保存项目配置；未确认和已确认未保存时，禁用按钮分别显示下一步操作，修改事件起点或参考段后也需重新保存。在 PCA 前按当前 MS 峰顶时间选取“事件标注起点”及之后的矩阵行，后段 QC 暂保留。这是用户确认的时间范围选择，不是自动识别 QC；全部事件继续留作校准。范围与选入/排除行数随结果保存，范围变化或旧结果缺少范围记录时提示重算，失败保留旧结果。独立副本仅 NaN→0，显式 PCA(arpack) → 邻居图(X_pca, euclidean) → 二维 UMAP。默认 PCA 50、邻居 15、种子 1；小样本分别限制到 min(事件数, feature 数)-1 和事件数-1，并显示实际参数。至少 4 个事件、2 个 feature；不默认归一化、log、缩放、删 feature 或批次校正。采集时间和人工标签均不作为距离输入。计算记录包含输入哈希、事件版本、请求/实际参数、依赖版本和坐标哈希；固定种子不代表跨平台逐位一致。

依赖固定 Scanpy 1.11.5、AnnData 0.12.17、umap-learn 0.5.9.post2。冻结包用 PyInstaller 的源文件收集模式保留 JIT 模块位置，Numba 缓存放在应用用户目录。打包时实际计算小矩阵 UMAP，不以仅导入成功代替科学运行检查。参考：[Scanpy neighbors](https://scanpy.readthedocs.io/en/stable/api/generated/scanpy.pp.neighbors.html)、[Scanpy UMAP](https://scanpy.readthedocs.io/en/stable/generated/scanpy.tl.umap.html)、[PyInstaller module collection](https://pyinstaller.org/en/latest/hooks.html)。

### 用户原始参考与产品实现差异

2026-09-12 用户提供 Scanpy `embed_and_cluster` 参考；以下保留原始研究入口，不宣称复现历史原图。参考函数复制 AnnData；支持 PCA、t-SNE、UMAP、Leiden，默认 `n_pcs=50`、`n_neighbors=15`、`leiden_resolution=1.0`、`random_state=42`，可按 obs 字段着色。PCA 分支显式 `sc.tl.pca(..., svd_solver='arpack')`；各分支之后均调用 `sc.pp.neighbors(..., n_neighbors=n_neighbors, n_pcs=n_pcs)`，UMAP 分支调用 `sc.tl.umap(..., random_state=random_state)`。用户实际使用的调用为：

```python
madata_hrgc = mc.pp.fill_nan_values(madata_hrgc, fill_method='zero')
madata_hrgc = embed_and_cluster(
    madata_hrgc, method='umap', random_state=1, color='scan_start_time'
)
madata_hrgc.obs['UMAP1'] = madata_hrgc.obsm['X_umap'][:, 0]
madata_hrgc.obs['UMAP2'] = madata_hrgc.obsm['X_umap'][:, 1]
```

原参考 UMAP 分支没有显式 PCA，邻居计算会依赖自动表示选择或已有 PCA；产品在独立副本显式计算，所有随机阶段均传入种子 1。`mc.pp.fill_nan_values` 的实现和原环境未交付，因此只实现用户明确给出的零填充策略，不宣称历史流程逐值复现。首轮仅 UMAP，不扩展 t-SNE/Leiden。

此前未启用前段过滤的完整真实数据回归：MPP 1,023 × 3,549、LSK 1,794 × 2,888、CAR-T-Bez 1,389 × 7,837（事件 × features），均通过 MS 提取、独立分析 ZIP、LMA 交接及原生 UMAP。实际参数均为 50 PCs / 15 neighbors / seed 1（在配置的“UMAP 计算设置”内查看）；三组重开、坐标切换、重复导入、独立重算及 CSV 坐标检查通过，矩阵值/NaN/轴/事件身份保持一致，原项目文件不变，新 LMA 标签均为 unknown。错项目矩阵导入被拒绝且项目不变。另有 4 个 HSC 和 6 个 CAR-T 旧项目副本通过现有工作流兼容检查，10 个原项目持久文件哈希不变。

MPP 沿用原项目已有的 1,023 个保留事件；LSK 与 CAR-T 仅在新测试副本中以工程审计批量保留真实检出事件，用于负载检查，不能作为人工审阅或标签真值。三组首次 UMAP 约 21–27 秒；CAR-T 16.8 GB 原始 MS 的首次 LMA 建项约 13.3 分钟，重开约 7 秒。数据规模测试不证明生物学分群准确率，也不代表未取得的投稿项目或 macOS 已验收。

当前范围选择另用 MPP 临时副本验证：工程配置确认边界并以 24 min 为起点，1,023 行中 815 行参与 UMAP、208 行排除；与独立子集重算逐值一致，原 H5AD 哈希不变。此计数不证明 208 行均为 QC，也不自动确认原验收项目的边界。

可复用检查脚本为本仓库 `scripts/regression_feature_projects.py` 与 MS 仓库 `scripts/validate_real_features.py`；当前项目、结果和统一验证记录集中于父工作区 `studio-validation/`，入口为 `validation.json`，界面证据为 `ui-matrix.zip`。人工步骤见 MS 仓库 `docs/guided_test_zh.md`。Windows 用户联合 UAT、双平台 Release 完成后再更新共享交接。

独立小样例验证 MS 实际导出的含/无矩阵 ZIP 新建 LMA、重开和误选分析 ZIP 拒绝。
