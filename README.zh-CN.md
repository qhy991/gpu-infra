# Kernel Infra

Kernel Infra 是编码 Agent、独立 kernel evaluator 与单机
[`agent-gpu-broker`](../agent-gpu-broker) 之间的持久控制/证据面。Agent 提交固定
task 与候选目录后立即获得 run id；GPU 排队和执行在后台继续，因此 Agent 可以并行
生成、审查和提交下一批候选。

Kernel Infra 不复制已有 evaluator 能力：

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
v0.2 CPU/GPU pipeline 验收见
[bounded-local compilation 报告](docs/a800-bounded-local-compile-2026-08-25.md)。
v0.2.1 非优雅 daemon 退出验收见
[pipe-lease crash 报告](docs/a800-crash-recovery-qualification-2026-08-25.md)。
v0.3 canonical adapter 与第二 operator ABI 验收见
[A800 RMSNorm 报告](docs/a800-rmsnorm-qualification-2026-08-25.md)。
v0.3.1 manifest/config 镜像验收见
[A800 image-contract 报告](docs/a800-image-contract-qualification-2026-08-25.md)。
v0.4.0 evaluator integration 与双节点验收见
[A800/B200 报告](docs/v0.4.0-a800-b200-qualification-2026-08-25.md)。
v0.5.0 broker admission receipt 验收见
[admission 报告](docs/v0.5.0-broker-admission-qualification-2026-08-25.md)。
v0.6.0 daemon-managed service 验收见
[managed-service 报告](docs/v0.6.0-managed-service-qualification-2026-08-25.md)。
v0.7.0 live service-task binding 验收见
[task-binding 报告](docs/v0.7.0-service-task-binding-qualification-2026-08-25.md)。
v0.8.0 consumer protection 与 idle-grace 验收见
[consumer-policy 报告](docs/v0.8.0-service-consumer-qualification-2026-08-25.md)。
v0.9.0 cross-host routing 验收见
[fleet-routing 报告](docs/v0.9.0-fleet-routing-qualification-2026-08-25.md)。
v0.10.0 routed-run operations 验收见
[fleet-operations 报告](docs/v0.10.0-fleet-operations-qualification-2026-08-25.md)。
v0.11.0 terminal artifact mirror 验收见
[artifact-mirror 报告](docs/v0.11.0-fleet-artifact-mirror-qualification-2026-08-25.md)。
v0.12.0 parallel route snapshot 验收见
[fleet-snapshot 报告](docs/v0.12.0-fleet-snapshot-qualification-2026-08-25.md)。
v0.13.0 immutable-route endpoint upgrade 验收见
[endpoint-upgrade 报告](docs/v0.13.0-fleet-endpoint-upgrade-qualification-2026-08-25.md)。
FIBServe、KDA 和容器后端的合同与信任边界见
[integration guide](docs/integrations.md)。

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

## Crash fail-closed

每个 stage command 都由 pipe-lease execution guard 托管。daemon 持有写端；
`SIGKILL`、进程退出或 descriptor 丢失会由内核关闭 lease，guard 随即终止并回收真实
子进程组。容器 evaluator 还使用确定性 name/label，确保清理 Docker daemon 中的
container，而不只是杀掉 CLI。

重启时，Kernel Infra 先通过 broker socket 对所有持久化 broker job id 做幂等
reconciliation，成功后才把未终态 run 归档为 `interrupted`；绝不自动重放不确定候选。

## 真实 CUDA 容器任务

`examples/a800_cuda_smoke/` 不再只是 PyTorch 调度 fixture。它按 platform
manifest 与 config digest 绑定官方 CUDA 12.4 devel 镜像，用 NVCC 为 `sm_80`
编译候选，执行 exact
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

`examples/a800_rmsnorm_smoke/` 用同一个 canonical adapter 接入第二个带归约的
ABI，并使用容差 oracle。它包含 1.0x shared-reduction 控制、warp-reduction 候选和
错误控制，覆盖两个冻结 A800 workload。见
[RMSNorm 任务说明](examples/a800_rmsnorm_smoke/README.md)。

容器镜像 identity 的唯一 owner 位于 [images/](images/README.md)。

## Broker 持有的长驻 evaluator

FIBServe 可以让一个 broker job 长期独占一张 GPU 并保留 compiler/baseline
cache；多个 Agent 的 `service` stage 只提交 HTTP 请求，不再逐请求申请第二张 GPU。
现在可以直接用 `kernelctl service-start` 非阻塞启动 checked service spec；daemon
自动执行 guarded `gpu-run --receipt-out`、等待健康 worker、生成 deployment v2，并
返回唯一 deployment id。`service-status` / `service-wait` / `service-stop` 管理完整
生命周期。同一个 service id 存活时禁止再次启动；停止后重启会创建新的 immutable
history。

```bash
kernelctl service-check /path/to/fibserve-service.json
deployment_id=$(kernelctl service-start /path/to/fibserve-service.json)
kernelctl service-wait "$deployment_id"
kernelctl service-status "$deployment_id" --json
kernelctl service-bind-task \
  --deployment "$deployment_id" \
  --template examples/fibserve_service/task.json \
  --out examples/fibserve_service/bound-task.json
kernelctl service-stop "$deployment_id"
```

`service-bind-task` 只接受 live-verified ready deployment，只替换一个 service stage
中的两个精确 token，并加入显式 managed deployment reference；随后校验完整 task，
并以 create-only 原子写入 task 与 sibling binding receipt；已有输出不会被覆盖。
三者必须位于同一目录，确保 task 中相对路径的语义不会因 materialization 改变。

materialized run 会成为该 deployment 的 active consumer。`service-status` 显示
consumer run ids/count；只要仍有非终态 consumer，`service-stop` 就会拒绝。service
spec 可设置 `idle_grace_s`，仅当 consumer 持续为零时自动释放 GPU；新 consumer 会
清除并重置计时。consumer projection 始终从 run state 推导，不单独保存可变计数。

## 跨主机路由

`fleet-submit` 可把一个 immutable task/candidate bundle 路由到 A800/B200，而不建立
全局 queue 或复制节点 result state：

```bash
kernelctl fleet-check examples/fleet/catalog.json
kernelctl fleet-probe --catalog examples/fleet/catalog.json
kernelctl fleet-submit \
  --catalog examples/fleet/catalog.json \
  --require a800 \
  --route-out route.json \
  /path/to/task.json /path/to/candidate

kernelctl fleet-status \
  --catalog examples/fleet/catalog.json \
  --route route.json
kernelctl fleet-wait \
  --catalog examples/fleet/catalog.json \
  --route route.json \
  --timeout 900
kernelctl fleet-frontier \
  --catalog examples/fleet/catalog.json \
  --route route.json
kernelctl fleet-fetch \
  --catalog examples/fleet/catalog.json \
  --route route.json \
  --out mirrors/run-001
kernelctl fleet-snapshot \
  --catalog examples/fleet/catalog.json \
  --out fleet-snapshot.json \
  routes/*.json
```

节点并行 probe；checked capability、ready deployment affinity、free disk 与 broker
health 决定 eligibility，queue 长度、idle GPU、active run 与 node id 形成确定性排序。
这只是 routing observation，最终 GPU allocation 仍由目标节点 broker 决定。

bundle 按 task/candidate digest 内容寻址，拒绝 traversal、symlink/device、oversize 与
digest drift，在目标 node-owned inbox 原子安装后通过该节点 daemon submit。route
receipt 保存所有 ok/unknown observation、选择理由、remote bundle/run 和
`(node_id, run_id)` locator。SSH 失败只表示 unknown，绝不能解释成节点空闲。详见
[fleet guide](docs/fleet.md)。

run 接受后 locator 固定在原节点；不会自动 retry/failover。`fleet-status`、
`fleet-wait`、`fleet-cancel` 与 `fleet-frontier` 都生成 content-addressed remote
observation receipt；SSH/daemon 失败只表示 unknown，不会伪造生命周期状态。

run 进入终态后，`fleet-fetch` 可以 create-only 地建立完整本地镜像。节点生成
`kernelinfra.artifact-manifest.v1`；接收端拒绝 traversal、link、重复、截断、超限和
内容漂移，并复用既有 route identity，只增加一个确有拒收动作的 artifact-set 传输
摘要。镜像保存 checked catalog、route 与 `artifacts/`，但明确标记
`authority=mirror-only`；status、frontier、routing 与 cancellation 仍只读取原节点。
SSH 失败不会留下可误认成成功的镜像目录。

Agent 并行探索多个候选时，可用 `fleet-snapshot` 先完整校验并去重所有 route，再并发
查询固定 node/run locator。create-only 输出只是一份包含逐 route `ok/unknown` 与状态
计数的派生当前视图；它不创建 campaign database 或全局队列，不 retry/failover，也不
增加新摘要，run lifecycle 仍由各节点拥有。

节点软件路径或 daemon socket 在 run 接受后变化时，保留历史 catalog 与 route receipt
原文不变，另传 checked current endpoint map：

```bash
kernelctl fleet-endpoints-check \
  --catalog historical-catalog.json current-endpoints.json
kernelctl fleet-status \
  --catalog historical-catalog.json \
  --endpoints current-endpoints.json \
  --route route.json
```

`kernelinfra.fleet-endpoints.v1` 只能为同一 node id 替换 SSH host、kernelctl path 与
socket；不能用于 submit，也不能和裸 locator 一起使用。cancel 使用一次原子 identity-
checked daemon 操作，frontier 先验证原 route 的精确 run，status/wait/snapshot/
artifact 也会验证返回身份。
observation、snapshot 与 mirror v2 保存实际 transport 值，但不增加 endpoint 摘要。

每次候选请求前后，adapter 都会复核 broker peer、独占 job/GPU、launch spec、
executable/environment digest、健康 worker、service root，以及干净源码 checkout
的 commit/tree。`service-attest` 仍可导入外部手工启动的 broker-held service。
可校验模板位于
[`examples/fibserve_service/`](examples/fibserve_service/)。

## KDA authoritative evidence

`kernelinfra-kda-import` 校验并复制一条 authoritative KDA ledger row 与逐 workload
receipt，重新计算 geomean，并保留 KDA speedup。若导出只有 speedup、没有 candidate 与
baseline 的绝对时间，该结果可以是合法 KDA 证据，但不能进入 Kernel Infra 的通用
frontier。模板见
[`examples/kda_report_import/`](examples/kda_report_import/)。

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

v0.6 面向同一 Unix 身份下相互信任的 Agent，并在每个 GPU 节点各运行一个 daemon；
还不承担恶意多租户隔离、跨机全局调度、优先级/抢占、显存配额或 daemon 崩溃后的
活任务恢复。broker admission receipt 已拥有真实 command/environment digest；argv
所引用的 compatibility library、image、dataset 与 config 内容仍须由 task/evaluator
分别 fingerprint，不能仅从路径名推断。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`.github/workflows/ci.yml` 会在 Python 3.10、3.11、3.12 上运行同一套合同测试
与 checked task/service/fleet 校验。
