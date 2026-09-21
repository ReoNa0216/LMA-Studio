# LMA Studio v0.7.2 Release Notes

正式版本。Windows 用户验收于 2026-09-21 完成。
Stable release. Windows user acceptance completed on 2026-09-21.

## 中文

“接受本屏待审”现在覆盖前段校准与事件标注中的明确关系，包括自动候选和人工保存的待审关系。附近候选峰、通道不完整或偏差评分不再单独阻止这次明确的人工批量审核。

- 仅处理当前窗口和当前筛选内的待审关系，保留已接受、已拒绝及窗口外记录。
- 真正互斥的关系默认不画普通待审线、不计入按钮数量，可从“显示冲突”展开。拒绝错误关系后，剩下的唯一关系恢复为普通待审；不会自动选择胜者或写入重复标签。
- 标注和审计记录整批保存；失败完整回滚，审核状态中途改变时要求刷新。
- 单条审核完成后批量按钮恢复可用，一键接受有进度和完成提示。已保存关系所依赖的弱峰及连线不再被“LIF 弱峰”开关隐藏。
- 保留参考段确认、时间模型冻结和微调预览期间的限制。不改变检峰、候选评分、原始数据或已有标注；已有项目无需重建。

验证：本轮 Windows 全量 524 项测试，522 通过、2 跳过；末次按钮状态修改后 29 项定向回归通过。真实 WebView2 页面在 MPP 副本验证“三条虚线→拒绝一条→一键接受两条→重开”，其余决定不变。此前 MPP、LSK、CAR-T-Bez、P39 副本重开检查通过，原项目未改变。Windows Dist 已通过用户验收。双平台构件仅在 CI 测试、打包运行检查和 ZIP 校验均通过后发布；macOS 未进行真机界面验收。

## English

“Accept pending in this view” now covers explicit front-calibration and event-annotation relations, including automatic candidates and manually saved pending pairs. Nearby alternatives, incomplete channel sets and residual-score warnings no longer independently veto this explicit human review action.

- Only pending relations in the current window and filter are accepted. Existing accepted/rejected decisions and off-screen records are preserved.
- Mutually exclusive relations are hidden from ordinary pending connectors and counts. Expand “Show conflicts” to review them. Rejecting an incorrect relation restores the remaining unique relation to normal pending review; the application never chooses a winner or creates duplicate labels automatically.
- Annotation and audit writes are atomic, roll back on failure, and reject stale review state.
- Confirmed boundaries, frozen time models and unapplied-preview restrictions remain. Peak detection, candidate scoring, source data and existing decisions are unchanged; projects do not need rebuilding.

The batch button becomes available again after individual review, with progress and completion feedback. Weak peaks needed by saved relations remain visible with their connectors even when additional weak candidates are hidden.

Validation: Windows full suite: 522 passed, 2 skipped out of 524; 29 focused checks passed after the final button-state fix. The real WebView2 page verified three dashed relations, one rejection, batch acceptance of the remaining two, and reopening on an MPP copy; other decisions were unchanged. Earlier MPP, LSK, CAR-T-Bez and P39 reopening checks passed with source projects unchanged. The Windows Dist passed user acceptance. Both platform packages are published only after CI tests, packaged runtime checks and ZIP checksum verification pass. Physical Mac GUI acceptance has not been performed.
