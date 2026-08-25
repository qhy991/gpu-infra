# Existing evaluator integration

Kernel Infra owns transport, immutable snapshots, lifecycle, typed evidence,
and derived frontier decisions. PTXBench/FIBServe and KDA remain the factual
owners of their evaluator semantics and raw results; the GPU broker remains the
only allocator.

## PTXBench / FIBServe

FIBServe already owns asynchronous task IDs, per-GPU workers, baseline cache,
sanitization, evaluation, profiling, and raw traces. The preferred path is one
daemon-managed service contract:

```bash
kernelctl service-check /path/to/fibserve-service.json
deployment_id=$(kernelctl service-start /path/to/fibserve-service.json)
kernelctl service-wait "$deployment_id"
kernelctl service-status "$deployment_id" --json
kernelctl service-bind-task \
  --deployment "$deployment_id" \
  --template /path/to/task.template.json \
  --out /path/to/task.json
```

`service-start` snapshots the contract and returns immediately. The daemon runs
the broker client behind a pipe lease, saves the broker admission, waits for a
healthy worker, builds deployment v2, and marks the unique deployment id ready.
At most one deployment per service id may be nonterminal. `service-stop`
terminates the guarded broker client and releases the allocation. Restarting a
stopped service creates a new history rather than overwriting evidence.

Acceptance is preceded by a no-mutation compatibility gate. The live broker
must declare semantic version 0.6 or newer, a non-empty instance id, and no
probe error. The configured gpu-run executable is invoked only with
`--estimate unknown --help`; successful parsing and a visible `--receipt-out`
option prove the client can preserve unknown ETA and write broker admission
custody. Failure creates no deployment history and submits no broker job. This
keeps a stale machine installation from becoming an asynchronous readiness
failure or silently changing unknown into a numeric estimate.

An optional `idle_grace_s` policy automatically stops a ready deployment only
after its derived active-consumer set remains empty for the complete grace
window. Every materialized run records an explicit deployment reference;
accepted, queued, and running runs block stop and reset idle time. Terminal run
states remove consumers from the projection.

Task templates place `${KERNELINFRA_SERVICE_IDENTITY}` in the selected service
judge identity and `${KERNELINFRA_DEPLOYMENT_RECEIPT}` immediately after its
single `--deployment-receipt` option. Binding requires a ready deployment,
live-verifies it, appends deployment id and receipt SHA-256 to the judge
identity, adds the explicit `service_deployment` reference, validates the
complete output task, and emits a sibling binding
receipt. Existing outputs are never overwritten. v0.7 requires exactly one
service stage so it never emits a partially bound task.
Template, output task, and binding receipt must share one directory, preserving
the meaning of every relative path copied from the template.

The checked `kernelinfra.service.v1` contract owns owner, URL, source root,
identity prefix, cwd, command, explicit environment, exclusive GPU count,
queue/run estimates and limits, and readiness timeout. The endpoint must be
unused before launch, closing the possibility of attesting an unrelated service
already bound to that port.

For an externally managed lifecycle, the lower-level path remains available.
Run one FIBServe process as a long-lived exclusive broker v0.6 job, saving the
admission receipt at start:

```bash
gpu-run \
  --label fibserve-campaign \
  --mode exclusive \
  --gpu-count 1 \
  --estimate unknown \
  --run-timeout 2h \
  --receipt-out /path/to/gpuq-admission.json \
  --env SERVICE_PORT=10000 \
  -- /path/to/start-fibserve.sh
```

Once the service is healthy, attest it:

```bash
kernelctl service-attest \
  --broker-socket /tmp/agent-gpu-broker.sock \
  --broker-job-id gpuq-<job> \
  --broker-admission-receipt /path/to/gpuq-admission.json \
  --service-url http://127.0.0.1:10000 \
  --service-identity 'PTXBench@<commit>+FIBServe@<commit-or-image>+dataset@<digest>+admission@sha256:<launch-spec>+executable@sha256:<executable>' \
  --source-root /path/to/PTXBench \
  --out /path/to/deployment.json
```

The v2 deployment receipt is created only when all of these facts hold:

- the URL is loopback HTTP with an explicit port;
- `/health` reports a non-empty, fully healthy worker set;
- the named broker job is uniquely running, exclusive, and owns at least one
  GPU;
- broker status version and, where `SO_PEERCRED` is available, Unix peer
  PID/UID/GID are recorded, so a broker process restart invalidates the receipt
  even when the socket path is reused;
- the source checkout is clean, and its commit appears in the declared service
  identity;
- the saved broker admission receipt is internally valid and byte-for-byte
  equivalent as JSON to the live receipt re-queried from the active job;
- broker version/instance, job id/timestamps/owner/label/mode/allocation,
  launch-spec digest, argv digest, explicit/effective environment digests,
  resolved executable, and executable-content digest agree;
- the task service identity contains both launch-spec and executable digests;
- the service root document is captured.

A task then uses an `execution: service` judge with no per-request GPU resource:

```json
{
  "id": "fibserve-evaluate",
  "kind": "judge",
  "execution": "service",
  "judge": {
    "identity": "PTXBench@<commit>+FIBServe@<commit-or-image>+dataset@<digest>+admission@sha256:<launch-spec>+executable@sha256:<executable>+deployment-receipt@sha256:<canonical-receipt>",
    "cwd": "/path/to/KernelInfra",
    "command": [
      "bin/kernelinfra-fibserve",
      "--deployment-receipt", "/path/to/deployment.json",
      "--definition", "mha_bwd_d128",
      "--entry-point", "kernel.cu::run",
      "--binding", "tvm-ffi",
      "--source", "kernel.cu",
      "--workload", "<frozen-workload-uuid>"
    ]
  }
}
```

The adapter verifies the receipt and task identity before submission, copies the
receipt into the stage, submits and polls FIBServe, saves the raw response, then
verifies the same broker peer/job/admission, source commit/tree, worker health,
and service root again. A missing job, dirty source, unhealthy worker, changed
broker process, admission drift, endpoint drift, or malformed response yields
`validity=unknown` and cannot enter the frontier.

The task judge identity must bind both the service identity and the canonical
deployment-receipt SHA-256. `service-bind-task` adds both automatically;
lower-level manual integrations must do so explicitly.

The admission receipt proves the actual submitted launch contract without
exposing environment values. It does not hash arbitrary files merely referenced
by argv, so task authors still bind dataset, model/image, config, definition,
workload, and compatibility-asset identities into `judge.identity`.

See `examples/fibserve_service/` for the checked template.

## KDA Internal

KDA keeps its fast/full judge, task-specific correctness, scoring, and submission
ledger. `kernelinfra-kda-import` consumes a two-field reference to one
authoritative ledger row:

```json
{
  "ledger_path": "/path/to/authoritative-report.csv",
  "ledger_row": 108
}
```

The task freezes expected kernel, benchmark revision, scoring schema, workload
count, and KDA judge identity. The importer then:

1. requires the ledger row's source to be `authoritative`;
2. verifies the kernel, benchmark SHA, scoring schema, and workload count;
3. cross-checks submission and kernel identity in every workload row;
4. validates correctness, status, size class, and positive finite speedup;
5. recomputes the all/large/small geomeans from workload rows;
6. copies the exact reference, ledger row, and per-workload CSV into the stage;
7. fingerprints the ledger, workload receipt, submission, benchmark, artifact,
   GPU, CUDA, and scoring identities.

When the KDA export has speedups but not candidate/baseline absolute timings,
the result is valid KDA evidence but intentionally remains
`frontier_eligible=false`. The importer records missing judge-image and driver
provenance rather than inventing them. A later KDA export may become frontier
eligible only by owning full absolute timings and the missing provenance.

See `examples/kda_report_import/` for the checked template.

## CUDA containers across Docker backends

The canonical CUDA image contract owns both platform manifest and config
digest. Docker overlay2 normally reports the config as `.Id`; Docker's
containerd image store may report the manifest instead. Kernel Infra accepts
the latter only when `RepoDigests` contains the exact registry name and platform
manifest. Results always preserve actual runtime ID, config, and manifest, so
backend representation differences do not weaken image identity.
