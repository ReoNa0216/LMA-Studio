# LMA Studio v0.7.2 Release Notes

本机测试版，等待用户验收；尚未发布 GitHub Release。
Local test build awaiting user acceptance; not yet published on GitHub.

## 中文

“接受本屏待审”现在覆盖前段校准与事件标注中的明确关系，包括自动候选和人工保存的待审关系。附近候选峰、通道不完整或偏差评分不再单独阻止这次明确的人工批量审核。

- 仅处理当前窗口和当前筛选内的待审关系，保留已接受、已拒绝及窗口外记录。
- 真正互斥的关系默认不画普通待审线、不计入按钮数量，可从“显示冲突”展开。拒绝错误关系后，剩下的唯一关系恢复为普通待审；不会自动选择胜者或写入重复标签。
- 标注和审计记录整批保存；失败完整回滚，审核状态中途改变时要求刷新。
- 保留参考段确认、时间模型冻结和微调预览期间的限制。不改变检峰、候选评分、原始数据或已有标注；已有项目无需重建。

验证：Windows 全量522项测试，520通过、2跳过；补充UI/审核回归55项通过。MPP、LSK、CAR-T-Bez、P39副本重开通过，原项目未改变；MPP/LSK副本中的受控待审关系批量接受并重开成功。等待用户Dist验收，尚未做本轮macOS构建或真机测试。

## English

“Accept pending in this view” now covers explicit front-calibration and event-annotation relations, including automatic candidates and manually saved pending pairs. Nearby alternatives, incomplete channel sets and residual-score warnings no longer independently veto this explicit human review action.

- Only pending relations in the current window and filter are accepted. Existing accepted/rejected decisions and off-screen records are preserved.
- Mutually exclusive relations are hidden from ordinary pending connectors and counts. Expand “Show conflicts” to review them. Rejecting an incorrect relation restores the remaining unique relation to normal pending review; the application never chooses a winner or creates duplicate labels automatically.
- Annotation and audit writes are atomic, roll back on failure, and reject stale review state.
- Confirmed boundaries, frozen time models and unapplied-preview restrictions remain. Peak detection, candidate scoring, source data and existing decisions are unchanged; projects do not need rebuilding.

Validation: Windows full suite: 520 passed, 2 skipped out of 522; 55 focused UI/review checks passed. MPP, LSK, CAR-T-Bez and P39 project-copy reopening passed with original projects unchanged. Controlled pending relations in MPP/LSK copies were batch-accepted and persisted across reopening. User Dist acceptance and this release’s macOS build/GUI validation are pending.
