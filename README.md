# GPU Infra

[中文](README.zh-CN.md) · [Agent skill](skills/gpu-infra/SKILL.md) · [Architecture](DESIGN.md)

GPU Infra connects coding agents, independent evaluators, and a machine-local
GPU broker. Use the smallest path that owns the work you need:

| Need | Entry point | Required owner |
| --- | --- | --- |
| Run an existing test, benchmark or profiler | `gpu-run` | Existing node broker |
| Inspect that broker without a GPU Infra daemon | `kernelctl diagnose --broker-socket SOCKET` | Existing node broker |
| Snapshot candidates and run staged judges asynchronously | `kernelctl submit[-many]` | `kernel-infrad` plus broker |
| Reuse a persistent evaluator | `kernelctl service-*` | Broker-held service deployment |
| Route candidates and collect evidence across hosts | `kernelctl fleet-*` | Existing node daemons |

The evaluator owns workloads, correctness and raw timing. The broker owns GPU
allocation and FIFO scheduling. The optional GPU Infra daemon owns immutable
inputs and run/service lifecycle. Fleet receipts fix the selected node;
frontiers and downloaded mirrors are derived views.

## Install

Python 3.10+ is required. GPU nodes also need the task's driver and toolchain.

```bash
git clone --recurse-submodules https://github.com/qhy991/gpu-infra.git
cd gpu-infra
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

From a source checkout, `python3 bin/kernelctl` also works without installation.
The package/commands remain `kernel-infra`, `kernel_infra`, and `kernelctl` for
compatibility. The product and skill are named GPU Infra / `gpu-infra`.

## Existing shared GPU node: reuse its broker

Discover the installed client, inspect its status, then run a device command:

```bash
command -v gpuq gpu-run
gpuq status --json
kernelctl diagnose --broker-socket /tmp/agent-gpu-broker.sock --json
gpu-run --label kernel-correctness --mode shared --gpu-count 1 \
  --queue-timeout 30m --run-timeout 5m -- python test.py
```

Use `exclusive` for benchmarks, sanitizers, and profilers. Prepare downloads,
CPU compilation and inputs before acquiring a GPU. The broker assigns device
visibility. Queue time and execution time have separate limits.

A shared node already has one allocator: do not start another broker from this
checkout. An authorized new-node installation follows the
[broker deployment guide](agent-gpu-broker/docs/root-deployment.md).

On B300-M3, constrained requests can wait for GPU 0 while other cards are idle.
Strict FIFO also holds later requests behind that head request. Inspect the
job's admission receipt and GPU scope before treating this as a stall. The
currently deployed broker and the bundled submodule can differ; see the
[dated B300-M3 audit](docs/b300-m3-audit-2026-10-03.md).

## Optional staged evaluation

Start a daemon only when you need durable task/candidate runs. Point it at the
existing broker and installed client. Each state directory and daemon socket
has one owner; duplicate startup fails before recovery touches active work.

```bash
kernelctl serve --gpu-run "$(command -v gpu-run)" \
  --broker-socket /tmp/agent-gpu-broker.sock --local-capacity 2
```

In another terminal:

```bash
kernelctl task-check examples/a800_smoke/task.json
kernelctl submit-many --task examples/a800_smoke/task.json \
  examples/a800_smoke/candidate_mul examples/a800_smoke/candidate_add
kernelctl status
kernelctl diagnose
kernelctl frontier --task examples/a800_smoke/task.json
```

The smoke example demonstrates the evaluator contract; it does not qualify a
B300 CUDA operator. Real CUDA examples freeze A800 workloads and toolchains;
read their task guides before adapting them. `submit` returns immediately.
Use `wait RUN_ID` only when synchronous completion is needed.

## Diagnose without taking control

`diagnose --broker-socket SOCKET` reads only that broker and reports
`scope=broker`. `diagnose [RUN_ID] --socket SOCKET` also correlates daemon run
requests and services and reports `scope=node`. These targets are explicit;
there is no silent fallback between them.

Exit codes are 0 for no observed attention condition, 3 for long queue waits or
suspected stalls, and 1 for unavailable/malformed evidence. `--attention-after`
is advisory; task queue/run timeouts remain authoritative. A long CPU stage or
zero utilization alone does not prove a stall. Broker `updated_at` is a status
response time, not proof of a fresh GPU probe. Diagnosis never cancels,
restarts, submits, or reroutes work.

## Agent entry point and advanced paths

Install the canonical skill by symlink:

```bash
GPU_INFRA_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$GPU_INFRA_SKILLS_DIR"
ln -s "$PWD/skills/gpu-infra" "$GPU_INFRA_SKILLS_DIR/gpu-infra"
```

Ask the agent to use `$gpu-infra`, or copy the bounded
[AGENTS.md snippet](docs/AGENTS.gpu-infra.snippet.md) into a downstream project.

- [Skill](skills/gpu-infra/SKILL.md): lease lifecycle, direct/staged execution,
  services, fleet submission, status and collection.
- [Fleet guide](docs/fleet.md): immutable routes, endpoint updates and mirrors.
- [Integrations](docs/integrations.md): PTXBench/FIBServe/KDA acceptance boundaries.
- [Design](DESIGN.md): owners, persisted contracts and failure semantics.
- [Changelog](CHANGELOG.md) and [dated qualification reports](docs/): historical
  evidence, not a statement of the current production deployment.

`completed` is lifecycle, `valid` is judge acceptance, and frontier eligibility
requires complete comparable timing. Connection failure is `unknown`, never
success or idle. Preserve receipts and historical results.

## Verify changes

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests exercise contracts and lifecycle with local sockets and fake GPU inventory.
They do not establish device correctness or performance. CI also validates the
checked-in task, service, fleet and endpoint examples.
