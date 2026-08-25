# Existing evaluator integration

Kernel Infra owns transport, immutable snapshots, lifecycle, typed evidence,
and derived frontier decisions. PTXBench/FIBServe and KDA remain the factual
owners of their evaluator semantics and raw results; the GPU broker remains the
only allocator.

## PTXBench / FIBServe

FIBServe already owns asynchronous task IDs, per-GPU workers, baseline cache,
sanitization, evaluation, profiling, and raw traces. Run one FIBServe process as
a long-lived exclusive broker job, then attest it:

```bash
kernelctl service-attest \
  --broker-socket /tmp/agent-gpu-broker.sock \
  --broker-job-id gpuq-<job> \
  --service-url http://127.0.0.1:10000 \
  --service-identity 'PTXBench@<commit>+FIBServe@<commit-or-image>+dataset@<digest>' \
  --source-root /path/to/PTXBench \
  --out /path/to/deployment.json
```

The v1 deployment receipt is created only when all of these facts hold:

- the URL is loopback HTTP with an explicit port;
- `/health` reports a non-empty, fully healthy worker set;
- the named broker job is uniquely running, exclusive, and owns at least one
  GPU;
- broker status version and, where `SO_PEERCRED` is available, Unix peer
  PID/UID/GID are recorded, so a broker process restart invalidates the receipt
  even when the socket path is reused;
- the source checkout is clean, and its commit appears in the declared service
  identity;
- the service root document is captured.

A task then uses an `execution: service` judge with no per-request GPU resource:

```json
{
  "id": "fibserve-evaluate",
  "kind": "judge",
  "execution": "service",
  "judge": {
    "identity": "PTXBench@<commit>+FIBServe@<commit-or-image>+dataset@<digest>",
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
verifies the same broker peer/job, source commit/tree, worker health, and service
root again. A missing job, dirty source, unhealthy worker, changed broker
process, endpoint drift, or malformed response yields `validity=unknown` and
cannot enter the frontier.

The current broker status v2 does not expose the admitted command or environment.
The task author must therefore bind launch-wrapper, compatibility library,
container image, dataset, definition, and workload identities into
`judge.identity`. The receipt must not be presented as proof of facts the broker
does not expose.

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
