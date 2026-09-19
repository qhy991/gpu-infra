# Heterogeneous GPU nodes

## Ownership and execution

Fleet routes a task to a node; the node daemon owns the run and its immutable
inputs; that node's broker alone allocates devices. A device backend implements
inventory, occupancy observation and child environment. It does not introduce a
queue, retry loop, lifecycle database or automatic migration. The evaluator still
owns compilation, correctness and measurement.

One broker uses one backend (`nvidia`, `metal`, or `hygon`). Existing invocations
continue to select NVIDIA. Shared/exclusive requests, card locks, process cleanup
and job IDs use the existing scheduler. Broker status, started events and result
records include `backend` and `occupancy_scope`; node-status forwards them.
These are additive fields in existing status/event records. Managed service
admission contracts are unchanged; heterogeneous managed services have not been
qualified.

## NVIDIA

Uses the existing nvidia-smi inventory and process observation. The broker sets
CUDA_VISIBLE_DEVICES after removing inherited device-selection variables.
Existing CUDA evaluators and tasks remain unchanged.

## Apple Metal

```sh
agent-gpu-broker/bin/gpuq serve --backend metal \
  --occupancy-scope cooperative
bin/kernelctl serve --gpu-run "$PWD/agent-gpu-broker/bin/gpu-run"
```

Initial support is deliberately limited to exactly one Apple GPU on macOS.
Discovery checks system_profiler; the example judge verifies the actual Metal
API device. `GPUQ_DEVICE_IDS=0` identifies the assigned physical device. The
backend does not pretend CUDA/HIP masks can select Metal devices.

`cooperative` must be selected explicitly. The broker coordinates only its own
jobs and cooperating per-card locks. External/system GPU occupancy is reported
as `unknown`, including while the managed capacity is idle. Exclusive means
exclusive among cooperating jobs, not a guarantee of a quiet display GPU.
Without the explicit scope the broker rejects startup. An evaluator must own
its measurement acceptance policy; the supplied smoke has no timing/frontier
claim.

Run `examples/metal_smoke/task.json` with `candidate_good` or `candidate_bad`.
The local compile stage builds the Swift host with xcrun; actual Metal library
compilation and dispatch happen inside the broker stage. Good/bad candidates
must produce valid/invalid respectively. Missing compiler, allocation, malformed
output or Metal execution failures remain unknown.

## Hygon / BW1101

```sh
agent-gpu-broker/bin/gpuq serve --backend hygon \
  --hygon-library /usr/local/hyhal/lib/librocm_smi64.so
```

The bundled read-only helper uses the installed ROCm SMI v2 ABI to enumerate
monitor indices and map process IDs to devices. It executes as a bounded child
process; API errors, races, malformed observations, missing PID lists and device
inventory changes fail closed. It never turns an unsupported hy-smi text format
into an empty list. The host helper also runs standalone on Python 3.7; broker
and daemon still require Python 3.10+.

The HIP execution environment selects broker-assigned ordinals through
HIP_VISIBLE_DEVICES and clears conflicting CUDA/ROCR masks. A deployment must
qualify SMI-to-HIP ordinal correspondence with its actual runtime before using
GPU jobs. This is a HIP path; it is not an automatic adapter for every DTK or
CUDA-compatible runtime. The actual evaluator/toolchain remains task-owned.

For a site-specific inventory adapter, `--probe-command 'python3 /path/probe.py'`
accepts argv quoting without a shell. Its output must be:

```json
{"schema":"gpuq.device-probe.v1","devices":[{"id":0,"compute_pids":[]}]}
```

IDs are unique nonnegative runtime ordinals. Every device must have a complete
list of positive process IDs. Missing/null occupancy is an error. The adapter
must report system occupancy, not only the caller's own processes.

BW1100 qualification on 2026-09-19 observed eight BW1101 cards via hy-smi.
Kernel execution is not qualified: `/opt/rocm` resolves into an inaccessible
root directory; host Python is 3.7; docker exec into dcu-dev is rejected by the
host device plugin. No containers, drivers or production daemons were changed.

## AMD ROCm

AMD is a distinct broker backend, selected with `--backend amd`. It requires an
explicit `--probe-command` producing the existing device-probe schema with
complete system process observations and qualified HIP runtime ordinals. No
Hygon library or device mapping is silently reused. The backend clears competing
visibility masks and sets HIP_VISIBLE_DEVICES from its own allocation. Fleet
`--require amd` checks the observed broker backend, not just catalog labels.
This addition has CPU protocol coverage; qualify the probe/runtime on each real
AMD host before admitting kernel experiments.

Broker status now declares `allocation_environment: gpuq_v1`. The daemon injects
GPUQ_JOB_ID and GPUQ_MODE after caller environment merging, alongside the existing
backend/device/scope facts. Cake's adapter uses this protocol to reject stale
daemons before starting authoring. This is an execution boundary, not an
independent correctness or performance judgment.

## Local and SSH fleet nodes (routing)

Catalog v1 remains SSH-only. Catalog v2 requires a `transport` field on each
node (`ssh` or `local`); other fields retain their existing meaning. For local,
`ssh` must be `localhost`, paths are resolved locally, and commands execute as
argv without a shell or SSH. Use a local catalog on its owning workstation;
it is not a portable alias for whichever host opens the file.

```json
{
  "schema": "kernelinfra.fleet.v2",
  "nodes": [
    {"id":"mac","transport":"local","ssh":"localhost",
     "kernelctl":"/absolute/gpu-infra/bin/kernelctl",
     "socket":"/tmp/kernel-infra.sock","inbox":"/absolute/inbox",
     "capabilities":["metal"]},
    {"id":"bw1100","transport":"ssh","ssh":"bw1100",
     "kernelctl":"/absolute/gpu-infra/bin/kernelctl",
     "socket":"/tmp/kernel-infra.sock","inbox":"/absolute/inbox",
     "capabilities":["hygon"]}
  ]
}
```

`--require metal|hygon|cuda` checks both catalog capability and observed broker
backend. Missing backend on a legacy broker means NVIDIA only. A catalog label
cannot fabricate Metal/Hygon support. Backend availability still does not
establish evaluator/toolchain availability.

Fleet tasks use absolute judge.cwd paths on the selected node. Submit separate
platform tasks with `fleet-submit --require metal ...` or `--require hygon ...`.
Batch submission still takes one task and many candidates; it is not a mixed
backend task matrix. A run stays on its accepted node. Status/wait/cancel/fetch
and collection share the same transport selection and original route identity.
Endpoint maps may change paths on a local node but cannot turn it into SSH.

A correctness-only HIP PyTorch task is supplied at
`examples/hygon_smoke/task.json`, with good and bad candidates. Use the site's
HIP Python executable in judge.command. It checks backend identity, HIP runtime,
exactly one visible device, device-resident output, and an independent CPU
reference. It intentionally records no timings. This example has not run on
BW1100 because of the environment access limitations above.

The standalone SMI helper was executed read-only on BW1100 and returned device
indices 0 through 7 with empty process lists at the observation time. Busy-PID
mapping and API-error handling have unit coverage; live busy-PID mapping and HIP
ordinal correspondence still need qualification.
