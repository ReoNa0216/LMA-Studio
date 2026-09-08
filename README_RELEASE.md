# LMA Studio v0.6.0 Release Notes

LMA Studio v0.6.0 正式版，提供 Windows x64 和 macOS Apple Silicon 桌面包。

## 本版更新

- 新建独立 MS 分析使用共用的 `flame-ms-core 0.1.0` 内核。
- 正式导入 MS Event Studio 的“LMA 事件包”v2，保留事件 ID、scan、时间、窗口、版本和审阅/纳入状态；读取 raw 用于展示，不重新检峰覆盖导入事件。
- 修复已接受的自动 Cell/QC 关系在调整时间轴后无法再次审核的问题。已接受关系保持实线，时间偏差超限以橙色和“需复核”提示，原物理关系与审核决定保留。
- 新增“打包分享项目”：选择保存位置即可生成一致的项目 ZIP 快照，自动排除 `__MACOSX`、`._*` 和 `.DS_Store`，同事解压后可继续工作。

## 下载使用

- Windows：完整解压 `LMA-Studio-v0.6.0-windows-x64.zip`，运行 `LMAStudio.exe`，保留全部随附文件。
- macOS：解压 `LMA-Studio-v0.6.0-macos-arm64.zip`，打开 `LMA Studio.app`，适用于 Apple Silicon。
- 两个 ZIP 均附 `.sha256` 校验文件。macOS 包为 ad-hoc 签名，未进行 Apple Developer ID 签名/公证；首次启动可能需在系统设置中允许打开。

## 兼容与验证

沿用现行峰识别标准的 v0.4.0 及后续项目保留已存事件、标注、模型和绑定，打开不会自动重算、重编号或刷新绑定；新版重分析应另建副本或项目。使用已退役峰识别标准的项目仍按既有规则拒绝打开。

发布工作流从同一标签构建两平台，通过完整自动测试和打包运行时检查后发布。开发阶段已完成真实数据与旧项目隔离副本回归、时间轴再审核回归、项目 ZIP 解压重开及科学内容核对。正式投稿项目不在本机，未声称逐项目验收。

本版由维护者明确授权正式发布；课题组成员的实际标注 UAT 仍待完成。macOS CI 检查不替代真机可见交互验收。

源码与应用包不含用户原始数据或项目。项目 ZIP 包含项目内文件；项目外引用的 raw 不会自动打包。事件接口和使用说明见 [Task 1 integration](docs/flame_task1.md) 与 [项目分享](docs/project_sharing.md)。
