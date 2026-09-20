# LMA Studio v0.7.1 Release Notes

## 中文

本版修复外部事件 CSV 导入时的一处保存错误：当常规检峰结果与补充复核事件同时出现，扫描编号的数据类型不一致可能导致项目创建失败。现在合并前统一编号的存储类型。

- 不改变检峰阈值、峰顶、强度、事件身份、CSV 坐标或人工标签。
- MS 事件表保存失败时显示具体阶段，并在运行日志保留原始异常；失败项目仍完整回滚。
- 已有项目继续使用保存的数据，无需迁移或重新检峰。

验证：P39 完整原始数据建项与重开通过，369 行外部事件坐标保持不变；MPP、LSK、CAR-T-Bez 旧项目的新旧版本对照一致。Windows 本机 511 项测试中 509 通过、2 跳过，Windows/macOS 构建及打包运行检查通过。macOS 尚未进行本轮真机界面验收。

## English

This release fixes project creation from an external event CSV when shared-core events and roster-supported events have different scan-ID storage types. IDs are normalized before the tables are merged.

- Peak thresholds, apex positions, intensities, event identities, CSV coordinates and human labels are unchanged.
- Failed MS event-table writes identify the failing stage and retain the original exception in the runtime log. Incomplete projects are rolled back.
- Existing projects retain their saved data; no migration or re-detection is required.

Validation: full P39 raw-input project creation and reopening passed, preserving all 369 external event-coordinate rows. Saved-project comparisons for MPP, LSK and CAR-T-Bez are unchanged. Local Windows tests: 509 passed, 2 skipped out of 511; Windows/macOS builds and packaged runtime checks passed. Mac GUI acceptance has not been performed for this release.
