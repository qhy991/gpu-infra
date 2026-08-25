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

See [DESIGN.md](DESIGN.md) for the current v0.11 contract and
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
The v0.7.0 live service-task binding qualification is
[the task-binding report](docs/v0.7.0-service-task-binding-qualification-2026-08-25.md).
The v0.8.0 consumer protection and idle-grace qualification is
[the consumer-policy report](docs/v0.8.0-service-consumer-qualification-2026-08-25.md).
The v0.9.0 cross-host routing qualification is
[the fleet-routing report](docs/v0.9.0-fleet-routing-qualification-2026-08-25.md).
The v0.10.0 routed-run operations qualification is
[the fleet-operations report](docs/v0.10.0-fleet-operations-qualification-2026-08-25.md).
The v0.11.0 terminal artifact-mirror qualification is
[the artifact-mirror report](docs/v0.11.0-fleet-artifact-mirror-qualification-2026-08-25.md).
The v0.12.0 parallel route-snapshot qualification is
[the fleet-snapshot report](docs/v0.12.0-fleet-snapshot-qualification-2026-08-25.md).
The v0.13.0 immutable-route endpoint-upgrade qualification is
[the endpoint-upgrade report](docs/v0.13.0-fleet-endpoint-upgrade-qualification-2026-08-25.md).
The v0.14.0 one-probe parallel fleet submission qualification is
[the fleet-submit-many report](docs/v0.14.0-fleet-submit-many-qualification-2026-08-25.md).
The v0.15.0 terminal multi-route collection qualification is
[the fleet-collect report](docs/v0.15.0-fleet-collect-qualification-2026-08-25.md).
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
bin/kernelctl service-preflight examples/fibserve_service/service.json
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

Before creating that history, service start requires the live broker to
declare version 0.6 or newer, a stable instance id, and no probe error. It also
checks the selected gpu-run executable without submitting work: the client must
parse `--estimate unknown` and expose `--receipt-out`. An incompatible deployed
broker/client is rejected with no deployment directory, event, broker job, or
GPU request; unknown estimates are never replaced by a made-up duration.
`service-preflight` exposes this same live gate as a read-only command and
returns the checked spec, broker identity, gpu-run path, and required capability
booleans without creating history.

`kernelinfra-fibserve` then verifies the broker peer, exclusive job/GPU,
healthy workers, broker-issued launch/executable/environment digests, service
root, and clean source commit/tree both before and after each request. The
checked service/task templates are in
[`examples/fibserve_service/`](examples/fibserve_service/).

`service-bind-task` accepts only a ready deployment, live-verifies its receipt,
replaces two exact tokens, adds the explicit managed deployment reference to
that service stage, validates the whole task, and
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

Materialized runs become active consumers of that deployment. `service-status`
shows `active_consumers` and `active_consumer_count`; `service-stop` is rejected
until every consumer run is terminal. A service spec may set `idle_grace_s` so
zero consumers automatically release the GPU after a continuous grace window.
Any new consumer clears and resets that timer. The count is derived from run
states and is never stored as independent authority.

## Cross-host routing

`fleet-submit` routes one immutable task/candidate bundle without creating a
global queue or copying node-owned result state:

```bash
kernelctl fleet-check examples/fleet/catalog.json
kernelctl fleet-probe --catalog examples/fleet/catalog.json
kernelctl fleet-submit \
  --catalog examples/fleet/catalog.json \
  --require a800 \
  --route-out route.json \
  /path/to/task.json /path/to/candidate
kernelctl fleet-submit-many \
  --catalog examples/fleet/catalog.json \
  --require b200 \
  --label-prefix explore- \
  --route-dir exploration-routes \
  /path/to/task.json \
  /path/to/candidate-a /path/to/candidate-b /path/to/candidate-c

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
kernelctl fleet-collect \
  --catalog exploration-routes/catalog.json \
  --out collection-001 \
  exploration-routes/routes/*.json
```

Nodes are probed in parallel. Eligibility combines checked static capabilities,
ready deployment affinity, minimum free disk, and healthy broker probes;
ranking uses observed queue length, idle cards, and active runs with node id as
a deterministic tie-break. The selected node's broker still decides whether
and when a GPU is allocated.

`fleet-submit-many` snapshots and validates every candidate before SSH, rejects
duplicate content, probes the fleet once, and deterministically projects queue,
remaining idle cards, and active runs to assign up to 64 candidates. At most
eight transports run concurrently. Each item gets an ordinary route receipt;
`catalog.json`, `probe.json`, and `summary.json` are stored in one create-only
directory. Partial remote success is preserved and never causes retry,
failover, rollback, or cancellation of accepted sibling runs. The summary is a
derived index, not campaign state, and adds no digest.

Transport snapshots the candidate, addresses the bundle by task/candidate
digests, rejects unsafe tar members, installs it in the node-owned immutable
inbox, and submits through that node's daemon. The route receipt preserves every
ok/unknown observation, decision, remote bundle/run identity, and
`(node_id, run_id)` locator. SSH failure is unknown, never evidence of an idle
node. See [the fleet guide](docs/fleet.md).

Once submitted, locator operations remain pinned to that node. There is no
automatic retry/failover: `fleet-status`, `fleet-wait`, `fleet-cancel`, and
`fleet-frontier` emit content-addressed remote-observation receipts, and SSH or
daemon failure is `unknown` rather than a fabricated lifecycle state.

After a run is terminal, `fleet-fetch` may create one verified local mirror of
the complete node run directory. The node emits `kernelinfra.artifact-manifest.v1`;
the receiver rejects unsafe/partial/drifted archives, checks the existing route
identity plus one artifact-set transfer digest, and installs create-only. The
mirror keeps the checked catalog and route alongside `artifacts/`, but is
explicitly `authority=mirror-only`: status, frontier, routing, and cancellation
continue to consult the owning node. SSH failure creates no mirror directory.

For an agent exploring several candidates, `fleet-snapshot` validates every
route receipt first, rejects duplicate locators, and then queries the fixed
node/run pairs concurrently. Its create-only output is a derived current view
with per-route `ok` or `unknown` responses and state counts. It creates no
campaign database or global queue, performs no retry/failover, and adds no new
digest; node run states remain authoritative.

`fleet-collect` closes the evidence-return loop without adding a watcher or
campaign database. It takes one snapshot of up to 64 ordinary routes and uses
at most eight concurrent fetches only for currently terminal runs. The
create-only collection copies catalog/routes/snapshot, installs independent v2
mirrors, and derives `summary.json`. Running items remain `nonterminal` (exit
3); unknown or fetch failure is retained (exit 1); all mirrored is exit 0. It
never waits, cancels, retries, fails over, or reinterprets judge validity.

When software paths or daemon sockets change after acceptance, keep the
historical catalog and route receipt unchanged and provide a checked current
endpoint map:

```bash
kernelctl fleet-endpoints-check \
  --catalog historical-catalog.json current-endpoints.json
kernelctl fleet-status \
  --catalog historical-catalog.json \
  --endpoints current-endpoints.json \
  --route route.json
```

`kernelinfra.fleet-endpoints.v1` may replace only SSH host, kernelctl path, and
socket for the same node id. It cannot be used for submission or with a bare
locator. Cancel performs one atomic identity-checked daemon operation; frontier
preflights the exact historical run, and status/wait/snapshot/artifact validate
returned identity. Observation, snapshot, and mirror v2 outputs preserve exact
transport values used without adding an endpoint digest.

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
and service/fleet validation on Python 3.10, 3.11, and 3.12.
