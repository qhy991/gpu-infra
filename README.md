# Kernel Infra

Kernel Infra is the durable control and evidence plane between coding agents,
independent kernel evaluators, and the machine-local
[`agent-gpu-broker`](../agent-gpu-broker). An agent submits a task and a bounded
candidate directory, immediately receives a run id, and can continue exploring
while correctness checks and clean-card benchmarks queue in the background.

The first release deliberately reuses existing owners:

- PTXBench/FIBServe supplies isolated CUDA/PTX compilation, sanitization,
  evaluation, and profiling.
- KDA keeps its fast/full independent judge and workload-specific scoring.
- `agent-gpu-broker` is the only component that allocates a GPU.
- Kernel Infra snapshots inputs, drives staged judges, persists receipts, and
  derives the per-workload frontier.

See [DESIGN.md](DESIGN.md) for the frozen v0.5 contract and
[README.zh-CN.md](README.zh-CN.md) for the Chinese guide. The first real-node
acceptance records are [the initial A800 pilot](docs/a800-pilot-2026-08-24.md)
and the [GitHub-release A800 qualification](docs/github-release-a800-2026-08-24.md).
The first exact-commit custom-CUDA qualification is
[the A800 NVCC/container report](docs/a800-real-cuda-qualification-2026-08-25.md).
The v0.2 CPU/GPU pipeline qualification is
[the bounded-local compilation report](docs/a800-bounded-local-compile-2026-08-25.md).
The v0.2.1 ungraceful-daemon qualification is
[the pipe-lease crash report](docs/a800-crash-recovery-qualification-2026-08-25.md).
The v0.3 canonical-adapter and second-operator qualification is
[the A800 RMSNorm report](docs/a800-rmsnorm-qualification-2026-08-25.md).
The v0.3.1 manifest/config image qualification is
[the A800 image-contract report](docs/a800-image-contract-qualification-2026-08-25.md).
The v0.4.0 evaluator-integration and dual-node qualification is
[the A800/B200 report](docs/v0.4.0-a800-b200-qualification-2026-08-25.md).
The v0.5.0 broker-admission qualification is
[the admission report](docs/v0.5.0-broker-admission-qualification-2026-08-25.md).
The v0.6.0 daemon-managed service qualification is
[the managed-service report](docs/v0.6.0-managed-service-qualification-2026-08-25.md).
The evaluator adapters and their trust boundaries are documented in
[the integration guide](docs/integrations.md).

## Quick start

Start the existing broker first, then the control daemon:

```bash
../agent-gpu-broker/bin/gpuq serve --gpus 1 --shared-capacity 2
bin/kernelctl serve \
  --gpu-run ../agent-gpu-broker/bin/gpu-run \
  --local-capacity 2
```

Validate and submit a staged task:

```bash
bin/kernelctl task-check examples/a800_smoke/task.json
bin/kernelctl submit-many \
  --task examples/a800_smoke/task.json \
  examples/a800_smoke/candidate_mul \
  examples/a800_smoke/candidate_add
bin/kernelctl status
bin/kernelctl frontier --task examples/a800_smoke/task.json
```

`submit` and `submit-many` are non-blocking by default. Use `kernelctl wait
<run-id>` only when a caller intentionally wants to wait.

## Crash fail-closed

Every stage command runs behind a small pipe-lease execution guard. The daemon
owns the write end; `SIGKILL`, process exit, or descriptor loss closes it in the
kernel. The guard then terminates and reaps the actual child process group.
Container evaluators additionally use deterministic names and labels so signal
cleanup removes the Docker daemon object, not only its CLI process.

On restart, Kernel Infra first reconciles every persisted broker job id through
the broker socket. Only after that succeeds are unfinished runs made terminal as
`interrupted`; they are never automatically replayed.

## Real CUDA container task

`examples/a800_cuda_smoke/` is a real A800 qualification path rather than the
PyTorch-only scheduling fixture. It binds an official CUDA 12.4 devel image by
platform manifest and config digest, compiles candidate CUDA with NVCC for
`sm_80`, checks exact output, runs
compute-sanitizer memcheck and racecheck, reuses the same binary for balanced
AB/BA timing, and fingerprints source, binary, SASS, and PTX.

```bash
bin/kernelctl task-check examples/a800_cuda_smoke/task.json
bin/kernelctl submit-many \
  --task examples/a800_cuda_smoke/task.json \
  examples/a800_cuda_smoke/candidate_basic \
  examples/a800_cuda_smoke/candidate_grid_stride \
  examples/a800_cuda_smoke/candidate_incorrect \
  examples/a800_cuda_smoke/candidate_race
```

See [the task guide](examples/a800_cuda_smoke/README.md) for its frozen ABI,
container boundary, evidence, and deliberate limitations.

`examples/a800_rmsnorm_smoke/` reuses the same canonical adapter for a second,
reduction-based ABI with a tolerance oracle. It contains a 1.0x shared-reduction
control, a warp-reduction candidate, and an incorrect control across two frozen
A800 workloads. See [the RMSNorm task guide](examples/a800_rmsnorm_smoke/README.md).

Container image identity has one owner under [images/](images/README.md).

## Broker-held evaluator services

FIBServe can keep one GPU and its baseline/compiler caches alive while many
agents submit CPU-only service stages concurrently. The daemon can own the
complete service lifecycle from one checked service contract:

```bash
bin/kernelctl service-check examples/fibserve_service/service.json
deployment_id=$(bin/kernelctl service-start examples/fibserve_service/service.json)
bin/kernelctl service-wait "$deployment_id"
bin/kernelctl service-status "$deployment_id" --json
bin/kernelctl service-bind-task \
  --deployment "$deployment_id" \
  --template examples/fibserve_service/task.json \
  --out examples/fibserve_service/bound-task.json
bin/kernelctl submit-many \
  --task examples/fibserve_service/bound-task.json \
  /path/to/candidate-a /path/to/candidate-b
bin/kernelctl service-stop "$deployment_id"
```

`service-start` is non-blocking. It snapshots the spec, launches guarded
`gpu-run --receipt-out`, waits for healthy workers, creates deployment v2, and
returns a unique deployment id. Starting the same service id again while it is
active is rejected; stopping and restarting creates another immutable history.

`kernelinfra-fibserve` then verifies the broker peer, exclusive job/GPU,
healthy workers, broker-issued launch/executable/environment digests, service
root, and clean source commit/tree both before and after each request. The
checked service/task templates are in
[`examples/fibserve_service/`](examples/fibserve_service/).

`service-bind-task` accepts only a ready deployment, live-verifies its receipt,
replaces two exact tokens in one service stage, validates the whole task, and
atomically creates both `task.json` and `task.json.binding.json`. It never
overwrites existing outputs.
Template, task output, and binding receipt stay in one directory so relative
task paths retain exactly the template's meaning.

`kernelctl service-attest` remains available as a low-level import path for an
externally launched broker-held service.

## KDA authoritative evidence

`kernelinfra-kda-import` validates and copies one authoritative KDA report row
and its per-workload receipt. It recomputes geomeans and preserves KDA speedups,
but a speedup-only export without absolute candidate/baseline timing remains
closed to the generic frontier. See
[`examples/kda_report_import/`](examples/kda_report_import/) and the
[integration guide](docs/integrations.md).

## Run artifacts

The daemon stores each run under
`~/.local/share/kernel-infra/runs/<run-id>/`:

```text
request.json              immutable identity and resolved judge paths
task.json                 exact task contract snapshot
candidate/                immutable candidate snapshot
state.json                current lifecycle projection
events.jsonl              append-only lifecycle events
stages/<stage>/
  stdout.log
  stderr.log
  result.json             judge-owned result
  receipt.json            observed broker/process receipt
result.json               validated aggregate run result
```

`frontiers/<task-id>/<task-digest>.json` is derived from those run artifacts
and can be rebuilt at any time.

Managed services use an independent immutable history under
`services/deployments/<deployment-id>/`:

```text
spec.json                 exact checked service contract
request.json              resolved launch/resource identity
state.json                accepted/starting/ready/stopped/failed/interrupted
events.jsonl              append-only deployment lifecycle
stdout.log / stderr.log   complete guarded gpu-run/service stream
admission.json            broker-issued started-job receipt
deployment.json           live service/source/broker attestation
```

Each materialized task has a sibling
`kernelinfra.service-task-binding.v1` receipt recording template, deployment,
receipt, output task, stage, and canonical SHA-256 identities.

## Evaluator contract

Every stage command receives these explicit environment variables through the
broker:

- `KERNELINFRA_RUN_ID`
- `KERNELINFRA_TASK`
- `KERNELINFRA_CANDIDATE_DIR`
- `KERNELINFRA_STAGE_ID`
- `KERNELINFRA_STAGE_KIND`
- `KERNELINFRA_STAGE_DIR`
- `KERNELINFRA_RESULT`

The judge writes `KERNELINFRA_RESULT` using schema
`kernelinfra.stage-result.v1`. See the A800 smoke evaluator for a complete
example. FIBServe and KDA adapters are included without moving evaluator
semantics or factual ledgers into Kernel Infra.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

`.github/workflows/ci.yml` runs the same contract suite and checked-task
validation on Python 3.10, 3.11, and 3.12.
