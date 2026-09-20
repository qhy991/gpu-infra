---
name: gpu-infra
description: "Operate GPU Infra for agent-driven CUDA/kernel evaluation: validate task contracts, submit immutable candidates, monitor fixed-node runs, collect evidence, manage GPU lease acquisition/release and broker-held FIBServe services, and diagnose GPU-broker failures. Use for GPU Infra, kernelctl, PTXBench/FIBServe, KDA imports, agent-gpu-broker-backed experiments, or requests to release an owned experiment's GPU; do not use for ordinary kernel editing without an evaluation or infrastructure task."
---

# GPU Infra

Use GPU Infra as the control and evidence plane between coding agents,
independent evaluators, and the machine-local GPU broker. Preserve the existing
owners:

- the checked task owns workloads, judges, gates, and resource modes;
- PTXBench/FIBServe or KDA owns correctness and raw measurements;
- `agent-gpu-broker` is the only GPU allocator;
- the node daemon owns run and service lifecycle;
- route receipts own routing history; artifact mirrors are read-only copies;
- frontier is a rebuildable projection, never model memory or agent prose.

## Before acting

1. Locate the active `gpu-infra` checkout and read its nearest `AGENTS.md`.
   Treat the current checkout, `kernel_infra.__version__`, `kernelctl --help`,
   task/service JSON, and live node state as authoritative. If this skill and
   the checkout differ, follow the checkout.
2. Inspect before mutation: clean/dirty Git state, exact commit/version, daemon
   socket, broker status, state-disk free bytes, active runs/services, production
   jobs, and output-path existence.
3. Label SSH, daemon, broker, or collector failure `unknown`. Never infer idle,
   completion, correctness, or success from an unavailable observer.
4. Apply the GPU lease lifecycle below, then select the execution mode. Do not add a campaign database, second
   queue, alternate allocator, automatic failover, or new digest merely for
   convenience.

## GPU lease lifecycle

**Reserve a GPU only for the minimum necessary device-work interval.** Treat
host preparation, device execution and host-only result processing as separate
resource phases. A lease is owned by the broker, not by an agent's activity guess.

1. **Prepare without a lease.** Complete provider authoring, downloads, CPU-only
   compilation, input generation and CPU oracle work before requesting a GPU.
   Use bounded local stages with no device access for that work. A CPU-only
   visibility mask belongs to that local child, never to the later broker request;
   the broker owns device selection and mapping.
2. **Bound the device phase.** Acquire just before device loading, transfers and
   execution. Include required synchronization and device-to-host observations.
   Move bulk host-only comparisons and report generation after the device phase
   when retained host snapshots allow the same checks. Do not submit a known
   CPU-dominated monolithic evaluator as if all of its wall time needed an
   exclusive GPU: first isolate its phases or establish the shortest indivisible
   interval required by its evaluation protocol.
3. **Release at a real boundary.** Complete outstanding device operations, retain
   all outputs needed by host checks, and end the worker's device ownership before
   the broker releases the allocation. Any later GPU work needs a fresh admitted
   lease and valid device state. Keep required correctness checks, fresh outputs,
   warmup/cache policy and paired-measurement boundaries unchanged; do not split
   a comparison that requires one continuous exclusive allocation. Host validation
   may remain pending after resource release, so release is not result acceptance.
4. **Do not infer release safety from utilization.** Zero utilization, a quiet log,
   elapsed time or a CPU-busy process does not prove that it cannot launch another
   kernel. Never unlink lock files, mark a live lease free, or suspend a
   device-owning worker and lend its GPU to someone else. Persistent services stay
   within their declared lifecycle and active-consumer/idle-grace policy.
5. **Honor an explicit release request for the identified task.** Established
   ownership plus the user's request authorizes stopping that task; do not ask
   again merely because cancellation is required. Prevent its launcher/watchdog
   from immediately resubmitting, then cancel the exact run through its owning
   daemon/broker. If a declared checkpoint/release/resume protocol is unavailable,
   cancellation ends that evaluation; do not invent an in-place unlock/resume.
   For necessary process cleanup, verify UID, PID start identity and task ancestry,
   signal only those owned processes, prefer graceful termination and verify exit.
   A read-only status request or idle observation alone does not authorize this.
6. **Verify the release.** Check the allocator's terminal/absent lease, the end of
   the task's device processes/contexts, and the absence of its unintended queued
   resubmissions. If observation fails, report release as unverified. Report other
   workloads separately: another job taking the GPU does not mean this release
   failed, and authorizes no intervention in that job. Preserve logs, candidates
   and receipts; cancelled work is not a completed or accepted experiment.

Change phase boundaries on a successor source/executor and validate the preserved
workload, oracle and timing contract before a new run. Do not patch a running
frozen experiment, silently weaken validation, or claim this agent guidance adds
automatic phase release to an evaluator that does not implement it.

## Select the smallest complete path

### One node, direct staged evaluation

Validate the task, submit immutable candidates, and return immediately:

```bash
kernelctl task-check task.json
kernelctl submit-many --task task.json candidate-a candidate-b
kernelctl status
kernelctl frontier --task task.json
```

`submit` and `submit-many` are asynchronous by default. Correctness may use
broker `shared` capacity; sanitizer, benchmark, and profiler stages must use
`exclusive`. CPU-only compilation uses bounded `local` stages and must not
inherit or select a GPU.

### Reusable PTXBench/FIBServe GPU service

Never launch one FIBServe GPU per candidate. Use one broker-held deployment and
many CPU-only service-stage consumers:

```bash
kernelctl service-check service.json
kernelctl service-preflight --socket /path/to/kernel.sock service.json
kernelctl service-start --socket /path/to/kernel.sock service.json
kernelctl service-wait --socket /path/to/kernel.sock DEPLOYMENT_ID
kernelctl service-bind-task \
  --socket /path/to/kernel.sock \
  --deployment DEPLOYMENT_ID \
  --template task.template.json \
  --out task.bound.json
kernelctl submit-many --socket /path/to/kernel.sock \
  --task task.bound.json candidate-a candidate-b
```

The live broker must declare v0.6+, a stable instance id, and no probe error;
the selected `gpu-run` must parse `--estimate unknown` and support
`--receipt-out`. `service-preflight` is read-only; `service-start` reruns it.
Do not replace unknown ETA with a numeric guess. Do not stop a deployment while
active consumers exist. After consumers are terminal, use explicit stop or the
checked idle-grace policy and verify that the broker job/GPU/endpoint disappear.

### Cross-host single candidate

```bash
kernelctl fleet-check catalog.json
kernelctl fleet-probe --catalog catalog.json
kernelctl fleet-submit \
  --catalog catalog.json \
  --require HARDWARE_CAPABILITY \
  --route-out route.json \
  task.json candidate
kernelctl fleet-status --catalog catalog.json --route route.json
kernelctl fleet-wait --catalog catalog.json --route route.json --timeout 900
```

Routing observations are advisory. After acceptance, `(node_id, run_id)` is
fixed: status, wait, cancel, frontier, fetch, and collection never reroute or
fail over. The selected node's broker still makes the allocation decision.

### Parallel agent exploration

Preflight all candidates, probe once, and submit through bounded concurrent
transports:

```bash
kernelctl fleet-submit-many \
  --catalog catalog.json \
  --require HARDWARE_CAPABILITY \
  --label-prefix explore- \
  --route-dir exploration-routes \
  task.json candidate-a candidate-b candidate-c
```

Duplicate candidate content must fail before probe. Each item gets an ordinary
route receipt under `exploration-routes/routes/`; `summary.json` is only a
derived index. Partial remote success is retained. Never rollback, cancel, or
resubmit accepted siblings because another item failed.

Observe the routes without blocking:

```bash
kernelctl fleet-snapshot \
  --catalog exploration-routes/catalog.json \
  --out snapshot-001.json \
  exploration-routes/routes/*.json
```

Collect only evidence that is currently terminal:

```bash
kernelctl fleet-collect \
  --catalog exploration-routes/catalog.json \
  --out collection-001 \
  exploration-routes/routes/*.json
```

Collection exit 0 means every route was mirrored, 3 means nonterminal work
remains, and 1 means unknown or fetch failure. Use a new output directory for a
later pass. Collection never waits, polls, cancels, retries, or changes a run.

### Operate historical routes after a node upgrade

Do not edit or re-sign the original catalog or route. Check a current endpoint
map that replaces only SSH host, kernelctl path, and socket for the same node id:

```bash
kernelctl fleet-endpoints-check \
  --catalog historical-catalog.json current-endpoints.json
kernelctl fleet-status \
  --catalog historical-catalog.json \
  --endpoints current-endpoints.json \
  --route historical-route.json
```

Endpoint maps require route receipts and cannot be used with a bare locator or
for new submission. Cancel is identity-checked atomically by the node daemon;
status/wait/snapshot/frontier/fetch validate the route-owned run evidence.

## Interpret evidence correctly

- Process exit or terminal lifecycle is not correctness.
- `completed + validity=valid` is not automatically frontier eligible; require
  complete task-owned timing/provenance and stable measurement.
- `invalid` is a judge correctness result. `unknown` is infrastructure or
  evidence failure. Do not merge them.
- A KDA speedup-only authoritative import may be valid KDA evidence while
  remaining ineligible for a generic absolute-timing frontier.
- A leaf/kernel gain is not an end-to-end service gain without the matched
  interface, workload, and complete serving trajectory.
- `artifact-mirror.v2` is `authority=mirror-only`. Status, validity, routing,
  cancellation, and frontier remain node-owned.
- Reuse existing task/candidate/route identities. Do not add SHA-256 fields,
  per-file fingerprints, or repeated tree hashing as evidence decoration.

## Remote operation and cleanup

- Use exact source commit/version and isolated socket/state/evidence paths for
  qualification. Preserve production jobs and unrelated dirty work.
- For tmux experiment monitoring, use the available `operator-exp-monitor`
  skill when applicable. Capture panes and live processes before intervening;
  never send keys without explicit authorization.
- Follow [GPU lease lifecycle](#gpu-lease-lifecycle) for task release and scoped
  cancellation. Unrelated or production jobs are outside that authorization.
- On completion, stop isolated services/daemons/brokers gracefully; verify
  sockets, tmux servers, processes, broker jobs, GPU allocations, locks, and
  endpoints are gone. Preserve route/run/mirror/report evidence.

## Stop and request authority

Do not proceed without explicit user authorization when the next action would:

- restart, upgrade, or replace a production broker/daemon;
- bypass a shared GPU lock or risk double allocation;
- delete node-owned runs, inboxes, mirrors, or other material evidence;
- merge `main`, create a PR, tag, or release;
- change the declared task/evaluator/workload acceptance boundary.

Report the exact blocker and the smallest safe authorization or external change
needed. An A800 SSH timeout, stale broker protocol, or unavailable GPU remains
an open gate—not evidence that the infrastructure or experiment succeeded.

## Handoff

Report the feature branch and commits, tests/CI, exact host/source/state/evidence
paths, route locators, lifecycle/validity/frontier distinctions, GPU/broker
custody, negative controls, cleanup state, and every remaining unqualified
gate. Do not claim broader completion than the preserved evidence proves.
