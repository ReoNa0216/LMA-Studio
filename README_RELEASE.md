# LMA Studio v0.7.1 Release Notes

## 中文

本次候选修复外部事件 CSV 导入时的一处保存错误：当常规检峰结果与补充复核事件同时出现，扫描编号的数据类型不一致可能导致项目创建失败。现在合并前统一编号的存储类型。

- 不改变检峰阈值、峰顶、强度、事件身份、CSV 坐标或人工标签。
- MS 事件表保存失败时显示具体阶段，并在运行日志保留原始异常；失败项目仍完整回滚。
- 已有项目继续使用保存的数据，无需迁移或重新检峰。

这是待验收构建，正式发布仍为 v0.7.0。macOS 可构建并自动检查，但本轮未做真机界面验收。

## English

This candidate fixes project creation from an external event CSV when shared-core events and roster-supported events have different scan-ID storage types. IDs are normalized before the tables are merged.

- Peak thresholds, apex positions, intensities, event identities, CSV coordinates and human labels are unchanged.
- Failed MS event-table writes identify the failing stage and retain the original exception in the runtime log. Incomplete projects are rolled back.
- Existing projects retain their saved data; no migration or re-detection is required.

This is a candidate awaiting acceptance; v0.7.0 remains the formal release. macOS builds and automated checks do not constitute Mac GUI acceptance.
