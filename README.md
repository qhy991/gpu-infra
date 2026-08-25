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

See [DESIGN.md](DESIGN.md) for the frozen v0.2 contract and
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
PyTorch-only scheduling fixture. It pins a local CUDA 12.4 devel image by image
ID, compiles candidate CUDA with NVCC for `sm_80`, checks exact output, runs
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
example. A FIBServe adapter is included as `bin/kernelinfra-fibserve`; its
FIBServe daemon must be a separately broker-held foreground service. KDA can use
the same stage boundary through a thin command wrapper without moving its judge
or ledger into Kernel Infra.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

`.github/workflows/ci.yml` runs the same contract suite and checked-task
validation on Python 3.10, 3.11, and 3.12.
