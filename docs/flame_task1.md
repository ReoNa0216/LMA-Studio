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

MS Event Studio 导出“分析结果”并勾选矩阵后，LMA 新建项目选择“MS 分析结果 ZIP（含矩阵）”，一次导入事件和矩阵，并提供原 MS 文件和 LIF 输入。只传事件时仍可使用“LMA 事件包”文件夹。CSV 审阅结果供人阅读；正式传递必须完整包，不能手改列名替代。

v2 包包含 `events.parquet`、`manifest.json`、`checksums.sha256`。完整合同见内核 `docs/event-package-v2.md`。原始自动身份包含 raw SHA、方法版本和 generation；当前事件身份、修订、原始及当前 scan/时间/支持窗、审阅状态分别保留。两个 Studio 的项目 UUID 可不同。

LMA 导入保留所有事件及顺序，只有 accepted 进入标注名单。pending、rejected、unreviewed 仍留在工作表和不可变原包中。raw 严格解析仅用于曲线及原始/当前物理位置校验，导入不重新 MS calling，不走旧 CSV roster 补峰。

LMA 项目配置可只读检查新版审阅包，逐事件比较新增/删除/修改及生成版本。更新创建新项目；不在原项目原位替换事件或自动迁移标注。同名 ID 的峰顶、窗口、状态变化也被识别。发布前完整校验在 sibling staging 完成，失败回滚，原项目不受影响。

## 旧项目与科学边界

v0.4.0+ 项目加载沿用已保存事件、配对、标签、模型、名单顺序与绑定。无编辑打开不初始化/迁移 DB，不重算、不刷新绑定。缺 DB 或绑定错误明确失败。SQLite 关闭最后一个读取连接可能移除原本为空的 WAL 及配套 SHM；持久化科学内容不变。测试始终使用隔离副本。

新建独立分析使用共用 caller，得到稳定自动来源身份。旧 LMA 用扫描间隔近似计算峰宽，新核用实际采集时间；新分析不能覆盖历史投稿结果。±15 ppm 人工名单支持通道保留，自动 primary 使用 ±12 ppm，二者不合并。

任务 2 等待专门数据，仅未来验收 label-correct；人工标签不是独立真值。标签与 feature 按事件 ID 并行产出。LIF→MS 采集时间对齐 QC 与跨批参照细胞不同，FLAME 没有色谱保留时间。HSC 特定参数不作为通用默认。测量、背景/质量证据、置信度、算法表示、化学注释和 metabolic state 分别表达。任务 3 使用 Linux 交付的 HRGC，由 MS Event Studio 提取并交接至 LMA；任务 4 只保留接口，未实现批次校正模型。

## 验收证据

当前构建、真实数据与旧项目的最终结果见共享仓库 `handoff/WINDOWS_STATUS.md` 及任务 1 验收报告。本机原项目不等于尚未取得的正式投稿项目，不能宣称逐投稿项目验收。Windows 人工 UAT 后再安排 macOS 真机可见验收。

## 矩阵与原生 UMAP（Windows 联合验收候选）

MS 的分析 ZIP 内包含原始 HRGC 矩阵和完整 v2 事件包。LMA 新建时一起接收；已有正式 v2 项目在“配置 → 矩阵与原生 UMAP”导入同一 ZIP。必须匹配原 MS SHA256、完整上游事件版本及矩阵逐行事件 ID；不按时间近似补绑。QC 排除允许矩阵是事件名单的子集。旧 CSV 项目仍可照常打开及使用原坐标，但无法证明正式事件身份时，应另建项目接入矩阵。

“计算 UMAP”在后台执行并显示进度。结果保存在项目内；重开无需重算。配置中可切换“原有坐标 / 原生 UMAP”，CSV 导出使用当前视图的坐标；矩阵未包含的事件坐标留空。切换不修改事件名单、人工标注、配对或时间模型。原生视图默认按采集时间着色，也可查看人工标注，点击点仍定位同一事件。

原始 float64/NaN 矩阵保持原样。独立副本仅 NaN→0，显式 PCA(arpack) → 邻居图(X_pca, euclidean) → 二维 UMAP。默认 PCA 50、邻居 15、种子 1；小样本分别限制到 min(事件数, feature 数)-1 和事件数-1，并显示实际参数。至少 4 个事件、2 个 feature；不默认归一化、log、缩放、删 feature 或批次校正。采集时间和人工标签均不作为距离输入。计算记录包含输入哈希、事件版本、请求/实际参数、依赖版本和坐标哈希；固定种子不代表跨平台逐位一致。

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

本机联合验证使用独立 MPP 项目：22 个事件 × 5897 features，实际 21 PCs / 15 neighbors / seed 1；重开坐标一致且 22 个标签保持 unknown，原矩阵哈希不变。另一个旧项目副本的 906 个事件及持久文件在只读重开前后完全一致。证据集中于 `build/umap-qa/`；此结果不代表所有投稿项目或 macOS 已验收。Windows 用户联合 UAT、双平台 Release 完成后再更新共享交接。
