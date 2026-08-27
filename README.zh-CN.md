# GPU Infra（Kernel Infra）

[English](README.md) | [Agent Skill](skills/kernel-infra/SKILL.md) | [GitHub](https://github.com/qhy991/gpu-infra)

GPU Infra 是面向编码 Agent 的 GPU kernel 评测基础设施。Agent 提交 task 和候选目录后会立即获得 run id；编译、正确性检查和性能测试在后台排队执行，因此 Agent 可以继续生成和提交其他候选，不必占着 GPU 等待。

它不负责生成 kernel，也不取代 evaluator：

- task/evaluator（如 PTXBench、FIBServe、KDA）定义 workload、正确性和原始测量；
- `agent-gpu-broker` 是每台机器唯一的 GPU 分配者；
- `kernel-infrad` 负责输入快照、分阶段执行、生命周期和证据；
- route receipt 固定远程节点，frontier 只是可重建的有效结果视图。

## 现在能做什么

- **单机评测**：校验 task，异步提交一个或多个不可变候选，查询状态并重建 frontier。
- **分阶段复用 GPU**：CPU 编译使用受限的本地并发；正确性检查可使用 `shared`；benchmark、sanitizer 和 profiler 必须使用 `exclusive`。
- **Agent 并行探索**：一次预检并提交多份候选，Agent 可继续编码，GPU 由 broker 排队复用。
- **跨节点路由**：根据 A800/B200 等能力、节点健康和观测负载选择节点；run 接受后固定到该节点，不自动漂移。
- **长驻 evaluator**：由 broker 独占托管 FIBServe 等服务，多次评测复用同一 GPU 进程和缓存，不为每个候选重复启动 GPU 服务。
- **证据回收**：保存 task、候选、judge 输出、receipt、route 和终态 artifact mirror；区分 `invalid` 与基础设施 `unknown`。
- **故障闭合**：SSH、daemon、broker、超时或结果缺失都记为 `unknown`，不会伪装成空闲、正确或成功。

## 安装

要求 Python 3.10+，GPU 节点还需要可用的 NVIDIA 驱动以及 task 所声明的 evaluator/toolchain。

```bash
git clone --recurse-submodules https://github.com/qhy991/gpu-infra.git
cd gpu-infra
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

若已经克隆但缺少 broker 子模块：

```bash
git submodule update --init --recursive
```

## 让 Agent 知道如何使用

仓库中的 [`skills/kernel-infra/SKILL.md`](skills/kernel-infra/SKILL.md) 是 Agent 的规范入口。Codex 可通过符号链接安装它，避免复制后产生两份说明：

```bash
KERNEL_INFRA_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$KERNEL_INFRA_SKILLS_DIR"
ln -s "$PWD/skills/kernel-infra" "$KERNEL_INFRA_SKILLS_DIR/kernel-infra"
```

随后可直接要求 Agent：

```text
使用 $kernel-infra 校验这个 task，并行提交这些候选；异步观察并回收终态证据，不要阻塞后续探索。
```

下游 kernel 项目还可以把 [`docs/AGENTS.kernel-infra.snippet.md`](docs/AGENTS.kernel-infra.snippet.md) 中的代码块复制到自己的 `AGENTS.md`。该片段只声明触发条件和安全边界，完整流程仍由 Skill 维护。

## 单机：最小闭环

在 GPU 节点的两个终端中分别启动 broker 和 daemon。把 `--gpus` 改成允许使用的物理 GPU 编号。

```bash
# 终端 1：唯一 GPU 分配者
agent-gpu-broker/bin/gpuq serve --gpus 0 --shared-capacity 2
```

```bash
# 终端 2：Kernel Infra 控制面
kernelctl serve \
  --gpu-run "$PWD/agent-gpu-broker/bin/gpu-run" \
  --local-capacity 2
```

另开终端校验并提交候选：

```bash
kernelctl task-check examples/a800_smoke/task.json
kernelctl submit-many \
  --task examples/a800_smoke/task.json \
  examples/a800_smoke/candidate_mul \
  examples/a800_smoke/candidate_add
kernelctl status
kernelctl frontier --task examples/a800_smoke/task.json
```

`submit` 和 `submit-many` 默认立即返回。只有调用者明确需要同步等待时才运行 `kernelctl wait RUN_ID`。

## 多 Agent / 多节点并行探索

先将 [`examples/fleet/catalog.json`](examples/fleet/catalog.json) 改为每个节点的真实 SSH 地址、`kernelctl` 路径、daemon socket、inbox 和能力：

```bash
kernelctl fleet-check examples/fleet/catalog.json
kernelctl fleet-probe --catalog examples/fleet/catalog.json
kernelctl fleet-submit-many \
  --catalog examples/fleet/catalog.json \
  --require a800 \
  --label-prefix explore- \
  --route-dir exploration-routes \
  /path/to/task.json \
  /path/to/candidate-a \
  /path/to/candidate-b \
  /path/to/candidate-c
```

非阻塞观察所有固定 route：

```bash
kernelctl fleet-snapshot \
  --catalog exploration-routes/catalog.json \
  --out snapshot-001.json \
  exploration-routes/routes/*.json
```

只回收当前已经终态的证据：

```bash
kernelctl fleet-collect \
  --catalog exploration-routes/catalog.json \
  --out collection-001 \
  exploration-routes/routes/*.json
```

collection 退出码：`0` 表示全部已镜像，`3` 表示仍有非终态任务，`1` 表示存在 unknown 或 fetch failure。后续再次回收时使用新的输出目录。

## 复用长驻 FIBServe 服务

先按实际服务修改 [`examples/fibserve_service/service.json`](examples/fibserve_service/service.json) 和 task 模板，再执行：

```bash
kernelctl service-check examples/fibserve_service/service.json
kernelctl service-preflight examples/fibserve_service/service.json
deployment_id=$(kernelctl service-start examples/fibserve_service/service.json)
kernelctl service-wait "$deployment_id"
kernelctl service-bind-task \
  --deployment "$deployment_id" \
  --template examples/fibserve_service/task.json \
  --out /path/to/task.bound.json
kernelctl submit-many \
  --task /path/to/task.bound.json \
  /path/to/candidate-a /path/to/candidate-b
```

service 仍有 active consumer 时不能停止。所有 consumer 终态后，才可显式执行：

```bash
kernelctl service-stop "$deployment_id"
```

## Agent 必须正确解释结果

- 进程结束或 lifecycle=`completed` 不等于 kernel 正确。
- `validity=valid` 仍需完整且稳定的 task-owned timing/provenance 才能进入 frontier。
- `invalid` 表示 judge 判定候选不正确；`unknown` 表示基础设施或证据不足，二者不能混用。
- route 一旦接受就固定 `(node_id, run_id)`；节点暂时不可访问时报告 unknown，不自动换节点重跑。
- artifact mirror 只是只读副本；run 状态、validity、取消和 frontier 仍由原节点拥有。
- 不要绕过 broker 直接占 GPU，也不要在未获授权时重启生产 broker/daemon、停止共享服务或删除证据。

## 更多资料

- 架构、所有权和非目标：[DESIGN.md](DESIGN.md)
- Agent 完整操作流程：[skills/kernel-infra/SKILL.md](skills/kernel-infra/SKILL.md)
- Fleet 合同：[docs/fleet.md](docs/fleet.md)
- PTXBench/FIBServe/KDA 集成边界：[docs/integrations.md](docs/integrations.md)
- 已完成的版本和验收记录：[CHANGELOG.md](CHANGELOG.md)、[docs/](docs/)
