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

See [DESIGN.md](DESIGN.md) for the frozen v0.1 contract and
[README.zh-CN.md](README.zh-CN.md) for the Chinese guide. The first real-node
acceptance records are [the initial A800 pilot](docs/a800-pilot-2026-08-24.md)
and the [GitHub-release A800 qualification](docs/github-release-a800-2026-08-24.md).

## Quick start

Start the existing broker first, then the control daemon:

```bash
../agent-gpu-broker/bin/gpuq serve --gpus 1 --shared-capacity 2
bin/kernelctl serve --gpu-run ../agent-gpu-broker/bin/gpu-run
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
