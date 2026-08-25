# Kernel Infra

## Ownership

- `kernel-infrad` owns run lifecycle, managed service deployment lifecycle,
  immutable input snapshots, and receipts.
- `agent-gpu-broker` remains the sole owner of per-host GPU allocation and queue
  policy. A broker stage runs through `gpu-run`; a CPU-only service request is
  legal only when the GPU service itself is held by a broker allocation.
- A `local` stage must be provably CPU-only and uses the daemon's bounded local
  capacity. It must never inherit or select a GPU outside the broker.
- `src/kernel_infra/adapters/cuda_container.py` is the canonical owner of
  Docker/NVCC lifecycle policy. Operator examples own only their task, harness,
  ABI, candidates, and task-specific oracle/measurement semantics.
- A task's named judge owns correctness and raw measurements. Kernel Infra may
  validate and reduce judge output, but must not silently reinterpret a failed
  or missing judge result as success.
- `frontier.json` is a rebuildable projection. Per-run receipts and judge
  results are its factual inputs.

## Contracts

- Keep task, stage-result, run-result, receipt, and frontier schemas explicit
  and versioned.
- Every managed service start creates a unique immutable deployment history.
  Never overwrite an earlier spec, admission, deployment receipt, log, or state.
  Reject a second active deployment for the same service id so agents reuse it.
- Separate execution completion, validity, and frontier decision.
- Snapshot candidate inputs before queueing so an agent may continue editing
  without changing a queued run.
- Correctness stages may use broker `shared` capacity. Sanitizers, benchmarks,
  and profilers must use `exclusive` capacity.
- A `service` stage is a CPU transport call, not a second allocator. Record the
  service commit/image and broker-held deployment identity in the judge field.
  Verify a strict deployment receipt and broker-issued admission receipt before
  and after the request. Bind launch-spec and executable digests into the task.
- Every stage command runs behind `exec_guard.py`; its pipe lease and process
  group are the canonical crash-cleanup boundary. Startup must reconcile any
  persisted broker job id before marking a run interrupted.
- Managed gpu-run service clients use the same pipe-lease cleanup and startup
  reconciliation boundary. Never auto-relaunch an uncertain deployment.
- Service task binding may replace only the exact service identity and
  deployment receipt tokens in one selected service stage. Validate the whole
  output, emit a binding receipt, and refuse all output overwrites.
- Managed service consumers are derived from nonterminal run states carrying an
  explicit deployment reference. Never persist an independently mutable
  reference count. Reject stop with active consumers; idle grace begins only at
  zero and resets whenever a consumer appears.
- Treat connection, broker, timeout, missing-result, and malformed-result
  failures as `unknown` validity and fail closed for frontier admission.

## Scope

- The current trust boundary is cooperating agents under one Unix identity.
- Add a new backend or policy only for a concrete evaluator or hardware need.
- Run `python3 -m unittest discover -s tests -v` before committing.
