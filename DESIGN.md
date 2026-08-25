# Kernel Infra v0.5 design contract

## Goal

Let several coding agents submit immutable kernel candidates without waiting on
GPU availability, run independent staged judges through one reusable per-host
GPU allocator, and receive durable evidence that keeps execution completion,
kernel validity, and performance-frontier decisions separate.

## Smallest primitive set

1. **Task contract**: fixed workloads, comparison policy, judge identities, and
   ordered stages.
2. **Candidate snapshot**: a content-addressed, self-contained copy made before
   queueing.
3. **Run**: one task digest plus one candidate digest.
4. **Stage**: one judge command plus either a broker resource request or a
   CPU-only request to an already broker-managed evaluator service.
5. **Receipt**: immutable request identity plus observed lifecycle and exit.
6. **Result**: validated judge output; absence or malformation is an
   infrastructure error, never a failed correctness claim.
7. **Frontier**: a rebuildable per-workload projection over eligible results.

No separate campaign state, agent memory, or experiment database is required.
Agents can submit several runs, and a task digest groups the comparable
set.

## Canonical owners

| Fact | Owner |
| --- | --- |
| GPU queue and allocation | `agent-gpu-broker` daemon |
| Workloads, judges, gates, resource modes | checked task JSON |
| Candidate source used by a run | content-addressed run snapshot |
| Run lifecycle | `kernel-infrad` run state and event log |
| Correctness and raw timing | the named stage judge's result JSON |
| Cross-run best per workload | derived `frontier.json` |
| Docker/NVCC lifecycle policy | canonical `cuda_container` adapter |

Kernel Infra never edits evaluator code, selects a winner from agent prose, or
uses a live aggregate score as the factual timing owner.

Operator tasks remain data and bounded judge assets: each owns its ABI, trusted
harness, workloads, oracle, tolerances, baseline, and candidates. Shared
container lifecycle, image identity, compilation, fingerprints, signal cleanup,
and result translation stay in one adapter.

## Execution path

```text
Agent submit
  -> kernel-infrad snapshots task + candidate and returns run_id
  -> bounded local CPU stages, one background gpu-run call per direct GPU stage,
     or CPU request to a broker-held evaluator service
  -> agent-gpu-broker queues shared/exclusive capacity
  -> independent judge writes stage-result.v1
  -> kernel-infrad validates and aggregates run-result.v1
  -> frontier reducer compares valid, stable, same-task-digest runs
```

Multiple runs advance concurrently. CPU compilation uses a separate bounded
local capacity and therefore does not occupy a GPU slot. Correctness stages may
overlap under the broker's bounded `shared` capacity; sanitizer, benchmark, and
profiler stages request `exclusive` capacity and wait without occupying the
submitting agent.

## Invariants

1. A run's task and candidate digests cannot change after acceptance.
2. Every direct GPU stage goes through `gpu-run`; an external evaluator service
   must itself be a broker-held long-running job. The control plane never
   assigns a physical GPU.
3. A local stage is CPU-only, bounded by daemon capacity, and receives no broker
   GPU assignment.
4. Later stages run only after the previous stage returns a valid `passed`
   result.
5. A process exit of zero without a valid judge result is an infrastructure
   error.
6. Cancellation interrupts `gpu-run`, which asks the broker to cancel and
   release its allocation.
7. Every stage command is a child of a pipe-lease execution guard. Daemon death
   closes the lease in the kernel; the guard terminates and reaps the real child
   process group.
8. Startup reconciles every persisted broker job id before marking unfinished
   runs `interrupted`. It never invents results or resubmits uncertain work.
9. Only `completed + valid + complete workload timing` results enter the
   frontier reducer.
10. Frontier comparison never crosses task digests.
11. A service request is legal only while its receipt still resolves to the
    same exclusive broker job and broker peer, a fully healthy worker set, and
    the same clean source commit/tree. The service adapter verifies this before
    submission and after result retrieval.
12. A service deployment must include the broker-issued active admission
    receipt. Saved and live receipts, broker instance/peer, job timestamps,
    allocation, launch-spec digest, and executable digest must agree; the task
    judge identity binds the launch-spec and executable digests.

## Failure semantics

- Broker unreachable, queue timeout, run timeout, judge crash, missing result,
  or malformed result: run validity is `unknown`; frontier closed.
- Ungraceful daemon death closes every stage lease. Guards terminate local and
  client process groups; broker connection loss or startup reconciliation
  cancels GPU jobs. Restart records `interrupted` and never replays the run.
- Judge reports incorrect: validity is `invalid`; later stages do not run.
- Judge passes correctness but a later benchmark fails: correctness remains
  recorded, run outcome is not completed, frontier closed.
- Measurement marked unstable: correctness may be valid, but frontier decision
  is `measurement-unstable`.
- Candidate contains a symlink or unsupported special file: reject before
  queueing so the snapshot is self-contained.

## Deliberate exclusions

v0.5 is a trusted single-host service. It attests broker-issued launch and
environment digests plus live broker/service/source custody. Files referenced by
the admitted argv—dataset, model, image, config, and compatibility assets—remain
task-owned identities and must be fingerprinted by the task/evaluator rather
than inferred from path names. Kernel Infra does not provide hostile tenant
isolation, a cross-host global scheduler,
priority/preemption, GPU memory quotas, automatic agent spawning, evaluator
implementation, or live-command resumption after daemon failure. Remote use
runs one service beside one broker and reaches it via SSH.

## Acceptance evidence

- Submit returns a run id before GPU execution completes.
- Two shared correctness stages overlap on one broker-managed card when
  capacity is two.
- Their exclusive benchmark stages serialize on that card.
- Both runs preserve task/candidate digests, per-stage logs, broker job ids,
  judge results, final results, and a rebuildable per-workload frontier.
- An incorrect candidate never reaches its benchmark stage.
- Missing/malformed judge output is reported as infrastructure failure rather
  than incorrectness or success.
- Killing the daemon leaves no evaluator process, labeled container, active
  broker job, or GPU allocation; restart preserves the run as interrupted.
- Two concurrent service-stage runs are accepted without a per-request GPU
  allocation, queue behind one broker-held FIBServe worker, and preserve
  independent results and frontier decisions.
- A stale broker process, missing broker job, shared service allocation, dirty
  source checkout, unhealthy worker, changed service identity, or malformed
  deployment receipt fails closed before frontier admission.
