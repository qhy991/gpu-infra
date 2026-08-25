# Kernel Infra v0.12 design contract

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
5. **Service deployment**: one immutable service spec snapshot plus one unique
   deployment id, broker admission, health attestation, and lifecycle.
6. **Receipt**: immutable request identity plus observed lifecycle and exit.
7. **Result**: validated judge output; absence or malformation is an
   infrastructure error, never a failed correctness claim.
8. **Frontier**: a rebuildable per-workload projection over eligible results.
9. **Route receipt**: one catalog digest, parallel node observations,
   eligibility/rank decision, content-addressed transport, and node/run locator.
10. **Artifact mirror**: a verified, create-only local copy of one terminal
    node run; it is never a lifecycle or frontier authority.
11. **Fleet snapshot**: one ephemeral read model over prevalidated unique route
    receipts and concurrent fixed-node lifecycle observations.

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
| Service launch contract | checked service JSON |
| Service deployment lifecycle | `kernel-infrad` deployment state and event log |
| Service GPU launch/allocation | broker admission receipt |
| Correctness and raw timing | the named stage judge's result JSON |
| Cross-run best per workload | derived `frontier.json` |
| Docker/NVCC lifecycle policy | canonical `cuda_container` adapter |
| Static node capability/transport paths | checked fleet catalog |
| Routing observation and decision | client-side route receipt |
| Remote queue/allocation/run/deployment/result | selected node daemon/broker |
| Terminal artifact manifest | selected node run directory |
| Downloaded artifact copy | local mirror, explicitly non-authoritative |
| Multi-route current view | derived client snapshot; node runs remain authoritative |

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

Agent service-start
  -> kernel-infrad snapshots service spec and returns deployment_id
  -> guarded gpu-run acquires one exclusive broker allocation
  -> broker writes admission receipt; evaluator becomes healthy
  -> kernel-infrad writes deployment.v2 and marks deployment ready
  -> many service-stage runs reuse that deployment without another GPU request
  -> service-stop or daemon lease loss cancels gpu-run and releases the GPU

Agent fleet-submit
  -> validate catalog + absolute-path task + immutable candidate snapshot
  -> probe nodes in parallel; failures remain unknown
  -> filter static capabilities, ready deployment affinity, disk, broker health
  -> rank advisory snapshots deterministically
  -> transfer content-addressed safe bundle over SSH
  -> target daemon revalidates and submits through its local authority
  -> return route receipt + (node_id, run_id), never a global run copy

Agent fleet-fetch
  -> resolve the immutable route locator, never probe or reroute
  -> require a terminal node-owned run and stream its bounded file inventory
  -> verify route identity and one aggregate transfer digest
  -> atomically install a create-only mirror labeled mirror-only

Agent fleet-snapshot
  -> validate and deduplicate every route before SSH
  -> query fixed node/run locators concurrently without probe or reroute
  -> preserve exact ok responses and per-route unknown failures
  -> derive terminal/nonterminal/state counts without campaign state
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
13. Every managed service start creates a new immutable deployment id. Restart
    never overwrites an earlier spec, receipt, log, event stream, or state.
14. At most one nonterminal deployment may exist for a service id. Agents reuse
    its ready deployment instead of launching another evaluator GPU.
15. A managed service endpoint must be unused before launch. Readiness requires
    a started broker admission, healthy workers, clean source attestation, and a
    completed deployment v2 receipt.
16. Managed gpu-run clients use the same daemon-owned pipe lease as stages.
    Normal stop, daemon shutdown, daemon death, and startup reconciliation all
    release the broker allocation; uncertain deployments are never replayed.
17. A task may bind a managed deployment only while it is ready and its
    deployment receipt verifies live. Materialization replaces exactly one
    service identity token and one receipt-path token in one selected service
    stage, validates the complete task, and creates both task and binding
    receipt without overwriting existing files.
18. A materialized service stage records its managed deployment id explicitly.
    Every run snapshots those ids; active consumers are the projection of
    nonterminal run states, never a separately mutable counter.
19. Submit requires every referenced managed deployment to be ready. Explicit
    stop is illegal while consumers exist. Run acceptance and stop/idle
    transitions are serialized in the daemon event loop.
20. Optional idle grace advances only during a continuous zero-consumer window.
    Any accepted/queued/running consumer clears and resets the timer. Grace
    expiry stops through the ordinary guarded broker-client path.
21. Fleet routing cannot allocate a GPU or mutate a remote queue directly. It
    submits to exactly one eligible node daemon, whose broker makes the final
    allocation decision after routing.
22. A node probe is a timestamped observation, not a promise. SSH, daemon,
    broker, or payload failure makes that node unknown/ineligible and remains
    visible in the route receipt.
23. Fleet bundles are content-addressed by task/candidate digests, reject links,
    devices, traversal, duplicates, oversize, and drift, and install atomically
    into a node-owned inbox before ordinary submit.
24. After acceptance, a `(node_id, run_id)` locator is immutable. Remote
    status/wait/cancel/frontier operations target only that node and never
    resubmit or fail over the run.
25. Remote operation receipts bind catalog, locator, operation, response/error,
    timestamp, and canonical digest. Transport failure is `unknown`; it cannot
    change node-owned run state.
26. Artifact export is legal only for a terminal run. Fetch stays pinned to the
    route node, rejects unsafe/partial/oversize/drifted content, and creates no
    successful output on failure.
27. A local artifact mirror is never an authority for run state, routing,
    cancellation, judge validity, or frontier. One aggregate transfer digest is
    sufficient; per-file, manifest, and mirror digests are excluded.
28. A fleet snapshot validates all route inputs before remote observation,
    targets each unique locator once, and rechecks run/task/candidate identity.
    One unknown route cannot be retried elsewhere or erase other observations.
29. Fleet snapshots are create-only derived views with no independent campaign
    lifecycle, queue, retry state, or digest.

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
- A service endpoint already responds before launch: reject before broker
  admission rather than attest an unrelated process.
- Service admission timeout, unhealthy worker, attestation failure, or client
  exit before readiness: deployment `failed`; no task may use it.
- Requested service stop or normal daemon shutdown: deployment `stopped` after
  the guarded client and broker allocation terminate.
- Daemon restart with a nonterminal deployment: reconcile its broker job, mark
  `interrupted`, and require a new deployment id; never auto-relaunch.
- Task binding from a stopped/interrupted/stale deployment, ambiguous service
  stage, malformed token, or existing output path: reject without writing an
  executable task.
- Submit against a stopped/failed/interrupted managed deployment: reject before
  candidate snapshot. Stop with active consumers: reject without changing the
  deployment. Idle grace with a consumer: remain ready and reset the timer.
- No eligible fleet node or selected-node transport failure: no fallback claim
  of success; preserve observations/error in route receipt when requested.
- Remote bundle traversal, link/device, digest drift, unsafe catalog string, or
  relative task cwd: reject before target run acceptance.
- Locator/catalog/route digest drift or remote status/frontier identity drift:
  reject locally. SSH/daemon timeout after acceptance: write unknown observation
  when requested; never claim terminal or dispatch another run.
- Artifact fetch from a nonterminal/unreachable node, unsafe or incomplete tar,
  route/run identity drift, transfer-digest mismatch, or existing destination:
  reject without installing a mirror or changing node state.
- Invalid or duplicate snapshot routes: reject before SSH and create no view.
  Remote timeout or identity drift after validation: retain that route as
  unknown while preserving independent observations from other locators.

## Deliberate exclusions

v0.12 adds trusted cross-host routing, multi-route observation, and
terminal-artifact clients over independently authoritative
single-host services. It attests broker-issued launch and
environment digests plus live broker/service/source custody. Files referenced by
the admitted argv—dataset, model, image, config, and compatibility assets—remain
task-owned identities and must be fingerprinted by the task/evaluator rather
than inferred from path names. Fleet transport requires tasks with absolute
remote judge cwd paths; it does not copy evaluator installations. Kernel Infra
does not provide hostile tenant isolation, a global queue,
priority/preemption, GPU memory quotas, automatic agent spawning, evaluator
implementation, automatic reroute/failover after acceptance, or live-command
resumption after daemon failure. Fleet remains a client-side projection over
independently authoritative node daemons rather than a global scheduler.

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
- `service-start` returns before queueing/readiness completes; `service-wait`
  reaches ready only after admission and deployment receipts exist.
- Starting the same service id twice while it is active is rejected; stopping
  and restarting creates two immutable deployment histories.
- Managed stop and ungraceful daemon death leave no service process, broker job,
  GPU allocation, card lock, or service endpoint.
- A ready deployment plus checked task template materializes one validated task
  and binding receipt; workload, comparison, ABI, and non-service judge fields
  remain byte-for-byte JSON values from the template.
- Active consumer status equals the set of nonterminal run ids referencing the
  deployment. A consumer prevents explicit and automatic stop; after all runs
  become terminal, configured idle grace releases the service GPU.
- Parallel fleet probe preserves ok/unknown nodes, capability/deployment
  filtering selects only eligible nodes, the remote bundle digest matches the
  local manifest, and the returned locator resolves to the node-owned run.
- Fleet status/wait observations match the locator run, cancel targets that
  exact node once, and frontier response matches the routed task digest; all
  observation receipts recompute to their canonical hashes.
- A terminal KDA and service-backed run can be fetched from its exact B200
  locator into a create-only mirror; every file matches the node manifest while
  mirror metadata remains explicitly non-authoritative.
- An unreachable A800 locator yields no local mirror and no fallback to B200.
- One snapshot observes several running and terminal B200 locators concurrently;
  an unreachable fixed-node locator remains independently unknown, and no
  snapshot creates, retries, cancels, or reroutes a run.
