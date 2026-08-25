# Kernel Infra

Kernel Infra 是编码 Agent、独立 kernel evaluator 与单机
[`agent-gpu-broker`](../agent-gpu-broker) 之间的持久控制/证据面。Agent 提交固定
task 与候选目录后立即获得 run id；GPU 排队和执行在后台继续，因此 Agent 可以并行
生成、审查和提交下一批候选。

v0.2 不复制已有能力：

- PTXBench/FIBServe 继续负责隔离编译、sanitize、evaluate 与 profile；
- KDA 继续拥有 fast/full 独立 judge 和 workload-specific score；
- `agent-gpu-broker` 是唯一 GPU 分配者；
- Kernel Infra 只负责输入快照、分阶段编排、持久 receipt 与逐 workload frontier
  投影。

完整所有权、失败语义和非目标见 [DESIGN.md](DESIGN.md)。
首个真实节点验收见 [A800 pilot](docs/a800-pilot-2026-08-24.md)。
从 GitHub 发布 commit 重新取源后的验收见
[GitHub-release A800 qualification](docs/github-release-a800-2026-08-24.md)。
首个 exact-commit 自定义 CUDA 验收见
[A800 NVCC/container 报告](docs/a800-real-cuda-qualification-2026-08-25.md)。

## 最小使用方式

先启动 broker，再启动控制 daemon：

```bash
../agent-gpu-broker/bin/gpuq serve --gpus 1 --shared-capacity 2
bin/kernelctl serve \
  --gpu-run ../agent-gpu-broker/bin/gpu-run \
  --local-capacity 2
```

校验 task，并一次提交多个候选：

```bash
bin/kernelctl task-check examples/a800_smoke/task.json
bin/kernelctl submit-many \
  --task examples/a800_smoke/task.json \
  examples/a800_smoke/candidate_mul \
  examples/a800_smoke/candidate_add
bin/kernelctl status
bin/kernelctl frontier --task examples/a800_smoke/task.json
```

`submit` / `submit-many` 默认立即返回。只有调用者明确需要同步等待时才使用
`kernelctl wait <run-id>`。

## 真实 CUDA 容器任务

`examples/a800_cuda_smoke/` 不再只是 PyTorch 调度 fixture。它按不可变 image ID
绑定本地 CUDA 12.4 devel 镜像，用 NVCC 为 `sm_80` 编译候选，执行 exact
correctness、compute-sanitizer memcheck/racecheck 和 balanced AB/BA timing，并为
source、binary、SASS、PTX 分别保存 SHA-256。

```bash
bin/kernelctl task-check examples/a800_cuda_smoke/task.json
bin/kernelctl submit-many \
  --task examples/a800_cuda_smoke/task.json \
  examples/a800_cuda_smoke/candidate_basic \
  examples/a800_cuda_smoke/candidate_grid_stride \
  examples/a800_cuda_smoke/candidate_incorrect \
  examples/a800_cuda_smoke/candidate_race
```

冻结 ABI、容器边界、证据和限制见
[任务说明](examples/a800_cuda_smoke/README.md)。

## 分阶段 GPU 复用

task 可以声明多个有序 stage。每个 stage 都有独立 judge identity、command 和 broker
资源请求：

- correctness：`shared`，允许可信的小型正确性检查按每卡容量重叠；
- benchmark / profile：`exclusive`，要求干净独占测量；
- 前一 stage 未产生合法 `passed` 结果时，后一 stage 不运行。

这使多个 Agent 的 CPU 推理/代码编辑与 GPU 队列解耦，也使多条 correctness 流可以
复用同一卡，而不会让性能测量被共享运行污染。

## Judge 输出合同

Kernel Infra 通过 broker 向每个 stage 显式传递：

- `KERNELINFRA_RUN_ID`
- `KERNELINFRA_TASK`
- `KERNELINFRA_CANDIDATE_DIR`
- `KERNELINFRA_STAGE_ID` / `KERNELINFRA_STAGE_KIND`
- `KERNELINFRA_STAGE_DIR`
- `KERNELINFRA_RESULT`

judge 必须把 `kernelinfra.stage-result.v1` 写到 `KERNELINFRA_RESULT`。进程返回 0 但
缺失或损坏该文件时，Infra 将其判为基础设施错误和 `validity=unknown`，绝不把“脚本
跑完”升级为“kernel 正确”或“进入性能前沿”。A800 示例给出了 shared correctness +
exclusive balanced AB/BA benchmark 的完整实现。

## 当前边界

v0.2 面向同一 Unix 身份下相互信任的 Agent，并在每个 GPU 节点各运行一个 daemon；
还不承担恶意多租户隔离、跨机全局调度、优先级/抢占、显存配额或 daemon 崩溃后的
活任务恢复。先在 A800 上证明单节点合同，再决定是否增加跨机 dispatcher。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`.github/workflows/ci.yml` 会在 Python 3.10、3.11、3.12 上运行同一套合同测试
与 checked-task 校验。
