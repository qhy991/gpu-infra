# GPU Infra

[English](README.md) · [Agent Skill](skills/gpu-infra/SKILL.md) · [架构合同](DESIGN.md)

GPU Infra 连接编码 Agent、独立 evaluator 和节点上的 GPU broker。按实际需要选择入口：

| 需要做什么 | 使用入口 | 必需的运行组件 |
| --- | --- | --- |
| 执行已有测试、benchmark、NCU 命令 | `gpu-run` | 节点已有 broker |
| 不启动 GPU Infra daemon，直接诊断队列 | `kernelctl diagnose --broker-socket SOCKET` | 节点已有 broker |
| 固定 task/候选，异步执行多阶段评测 | `kernelctl submit[-many]` | `kernel-infrad` 与 broker |
| 多个候选复用长驻 evaluator | `kernelctl service-*` | broker 持有的 service deployment |
| 跨节点提交和回收证据 | `kernelctl fleet-*` | 各节点已有 daemon |

职责只有四层：task/evaluator 拥有 workload、正确性和原始测量；broker 拥有 GPU 分配与 FIFO 队列；可选的 GPU Infra daemon 拥有输入快照与 run/service 生命周期；fleet 负责固定节点和传输。frontier 与下载的 mirror 都是派生视图。

## 安装

要求 Python 3.10+；GPU 节点另需 task 声明的驱动和工具链。

```bash
git clone --recurse-submodules https://github.com/qhy991/gpu-infra.git
cd gpu-infra
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

源码 checkout 也可直接运行 `python3 bin/kernelctl`。产品与 Skill 名称为 GPU Infra / `gpu-infra`；包名、命令、schema 和存储路径中的 `kernel-infra`、`kernel_infra`、`kernelctl` 等保留兼容性，不是待批量替换的旧名字。

## 已有共享节点：复用 broker

先确认已安装的 client 和当前状态，再通过它执行设备命令：

```bash
command -v gpuq gpu-run
gpuq status --json
kernelctl diagnose --broker-socket /tmp/agent-gpu-broker.sock --json
gpu-run --label kernel-correctness --mode shared --gpu-count 1 \
  --queue-timeout 30m --run-timeout 5m -- python test.py
```

benchmark、sanitizer、profiler 使用 `exclusive`。下载、CPU 编译、输入准备应在获取 GPU 前完成；可见设备由 broker 分配，排队和执行分别设置超时。

共享节点已有唯一 allocator，不要照着仓库示例再启动一份。获授权的新节点部署参见 [broker 部署文档](agent-gpu-broker/docs/root-deployment.zh-CN.md)。

B300-M3 的限定卡作业可能只允许 GPU 0，因此其他卡空闲时它仍需等待。严格 FIFO 还会挡住后面的不限卡作业。应核对 admission receipt 的 GPU 范围，再判断是否异常。节点部署与仓库子模块可能不同，具体证据见 [2026-10-03 审计](docs/b300-m3-audit-2026-10-03.md)。

## 需要持久评测记录时，再启动 daemon

daemon 使用节点已有 broker 和 client。每个 state directory 与 daemon socket 只能有一个 owner；重复启动会在恢复处理触碰现有作业前失败。

```bash
kernelctl serve --gpu-run "$(command -v gpu-run)" \
  --broker-socket /tmp/agent-gpu-broker.sock --local-capacity 2
```

在另一个终端执行：

```bash
kernelctl task-check examples/a800_smoke/task.json
kernelctl submit-many --task examples/a800_smoke/task.json \
  examples/a800_smoke/candidate_mul examples/a800_smoke/candidate_add
kernelctl status
kernelctl diagnose
kernelctl frontier --task examples/a800_smoke/task.json
```

smoke 示例演示 evaluator 合同，不代表 B300 CUDA 算子通过验收。真实 CUDA 示例固定的是 A800 workload/toolchain，修改前先读各自 task guide。`submit` 默认立即返回；只有确实需要同步完成时才用 `wait RUN_ID`。

## 如何诊断

- `diagnose --broker-socket SOCKET`：直接读取 broker，输出 `scope=broker`；没有 daemon run 的含义。
- `diagnose [RUN_ID] --socket SOCKET`：关联 daemon 的 request/state、broker 与 service，输出 `scope=node`。

两个目标显式选择，不会连接失败后悄悄换目标。退出码 `0` 表示未观察到需关注条件，`3` 表示长排队或疑似停滞，`1` 表示观察不可用或证据畸形。`--attention-after` 只是提醒阈值，不替代 task timeout。CPU 阶段耗时久或 GPU 利用率为零都不能单独证明卡死；固定版本 broker 输出 `gpu_observed_at` 与 `gpu_observation_age_seconds`。探测失败/陈旧或旧版缺少这些字段时输出 `unknown`，不会拿 `updated_at` 代替探测新鲜度。诊断不会自动取消、重启、提交或迁移作业。

## Agent 与进阶用法

用符号链接安装唯一的 Skill，避免维护两份说明：

```bash
GPU_INFRA_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$GPU_INFRA_SKILLS_DIR"
ln -s "$PWD/skills/gpu-infra" "$GPU_INFRA_SKILLS_DIR/gpu-infra"
```

随后要求 Agent 使用 `$gpu-infra`，或把 [下游 AGENTS.md 片段](docs/AGENTS.gpu-infra.snippet.md) 加入项目。

- [Skill](skills/gpu-infra/SKILL.md)：租约阶段、直接/分阶段执行、service、fleet 提交和回收。
- [Fleet 合同](docs/fleet.md)：固定 route、更新 endpoint、回收只读 mirror。
- [集成合同](docs/integrations.md)：PTXBench/FIBServe/KDA 的验收边界。
- [架构合同](DESIGN.md)：所有权、持久格式和失败语义。
- [Changelog](CHANGELOG.md) 与 [历史验收记录](docs/)：保留当时事实，不代表当前生产部署。

`completed` 是生命周期，`valid` 是 judge 接受，进入 frontier 还需要完整且可比的计时。连接失败是 `unknown`，不能解释为成功或空闲。保留 receipt 和历史结果。

## 验证修改

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用本机 socket 和 fake GPU inventory，验证合同与生命周期，不证明设备正确性或性能。CI 另校验仓库中的 task、service、fleet 和 endpoint 示例。
