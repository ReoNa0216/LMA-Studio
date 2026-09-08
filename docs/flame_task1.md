# FLAME 任务 1 接入说明

本轮候选使用独立包 `flame-ms-core==0.1.0`，两 Studio 不互相导入产品代码。共用解析、自动 calling、采集时间、身份和交换校验；UI、SQLite、项目保存仍属各 Studio。

## 安装与构建

固定来源为 `ReoNa0216/flame-ms-core` 的 `v0.1.0` 私有 release，代码 commit `90f31db270f2aa004220e3e84a6207393c15966e`。wheel SHA256、文件名和来源见 `packaging/flame-ms-core.json`。Windows/Mac 脚本在安装前校验哈希，冻结包收集正式安装的内核。

本地可用相邻仓库的 `dist/flame_ms_core-0.1.0-py3-none-any.whl`，或设置 `FLAME_MS_CORE_WHEEL` 指向已下载文件。已登录并有读取权限的 GitHub CLI 可执行：

```text
python scripts/resolve_flame_core.py --download
```

CI 使用 `FLAME_MS_CORE_READ_TOKEN` secret 读取私有内核仓库（仅需该仓库 contents:read）；缺少跨仓权限将明确失败。没有将本机登录凭据复制到 secret。本轮本机已验证下载和哈希；不能据此宣称托管 CI 或 macOS 真机验收通过。

## 事件传递

MS Event Studio 导出“完整审计数据包”，LMA 新建项目选择“MS Event Studio 审阅包”，提供原 MS 文件和 LIF 输入。CSV 审阅结果供人阅读；正式传递必须完整包，不能手改列名替代。

v2 包包含 `events.parquet`、`manifest.json`、`checksums.sha256`。完整合同见内核 `docs/event-package-v2.md`。原始自动身份包含 raw SHA、方法版本和 generation；当前事件身份、修订、原始及当前 scan/时间/支持窗、审阅状态分别保留。两个 Studio 的项目 UUID 可不同。

LMA 导入保留所有事件及顺序，只有 accepted 进入标注名单。pending、rejected、unreviewed 仍留在工作表和不可变原包中。raw 严格解析仅用于曲线及原始/当前物理位置校验，导入不重新 MS calling，不走旧 CSV roster 补峰。

LMA 项目配置可只读检查新版审阅包，逐事件比较新增/删除/修改及生成版本。更新创建新项目；不在原项目原位替换事件或自动迁移标注。同名 ID 的峰顶、窗口、状态变化也被识别。发布前完整校验在 sibling staging 完成，失败回滚，原项目不受影响。

## 旧项目与科学边界

v0.4.0+ 项目加载沿用已保存事件、配对、标签、模型、名单顺序与绑定。无编辑打开不初始化/迁移 DB，不重算、不刷新绑定。缺 DB 或绑定错误明确失败。SQLite 关闭最后一个读取连接可能移除原本为空的 WAL 及配套 SHM；持久化科学内容不变。测试始终使用隔离副本。

新建独立分析使用共用 caller，得到稳定自动来源身份。旧 LMA 用扫描间隔近似计算峰宽，新核用实际采集时间；新分析不能覆盖历史投稿结果。±15 ppm 人工名单支持通道保留，自动 primary 使用 ±12 ppm，二者不合并。

任务 2 等待专门数据，仅未来验收 label-correct；人工标签不是独立真值。标签与 feature 按事件 ID 并行产出。LIF→MS 采集时间对齐 QC 与跨批参照细胞不同，FLAME 没有色谱保留时间。HSC 特定参数不作为通用默认。测量、背景/质量证据、置信度、算法表示、化学注释和 metabolic state 分别表达。任务 3 可由 Linux 继续；任务 4 只保留接口，未实现自动标签或批次校正模型。

## 验收证据

当前构建、真实数据与旧项目的最终结果见共享仓库 `handoff/WINDOWS_STATUS.md` 及任务 1 验收报告。本机原项目不等于尚未取得的正式投稿项目，不能宣称逐投稿项目验收。Windows 人工 UAT 后再安排 macOS 真机可见验收。
