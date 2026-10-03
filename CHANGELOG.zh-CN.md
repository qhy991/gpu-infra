# 变更记录

历史版本的完整记录见 [CHANGELOG.md](CHANGELOG.md)。从本轮开始同步记录中文变更。

## [Unreleased]

### Added

- 整合此前未发布的只读 `kernelctl diagnose`，新增显式 broker-only 入口，供没有
  `kernel-infrad` 的节点使用。broker 长排队退出 3，观察未知退出 1；run/service
  关联仍由 daemon 拥有（`diagnostics.py`、`cli.py`、`server.py`）。

### Fixed

- daemon 在恢复可能取消 live job 前拒绝重复 owner，覆盖同一 state directory
  配不同 socket 的情况；启动失败释放所有权（`server.py`、生命周期回归）。
- run/service 恢复复用一个有超时的 broker 客户端，并严格验证取消回执；修复
  service 路径把畸形响应当成完成核对的问题（`broker.py`、`runner.py`、
  `services.py`、`service_attestation.py`）。
- 拒绝非有限诊断阈值/计时，停止仅凭 CPU 阶段耗时判定卡死
  （`diagnostics.py`、诊断回归）。

### Changed

- 精简双语 README，按直接 broker、分阶段评测、service、fleet 选择入口；共享
  节点复用已安装 broker。架构收敛为六个核心概念，派生视图与生命周期 owner
  分开说明（`README*`、`DESIGN.md`、Agent Skill）。
- 用带日期的架构审计记录 B300-M3 部署漂移、限定卡 FIFO 阻塞以及仍缺少的 probe
  新鲜度证据；保留历史验收记录与兼容标识（`docs/`）。
