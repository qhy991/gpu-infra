# Changelog

## 0.14.0 — 2026-08-25

- Add `fleet-submit-many` for 1–64 prevalidated immutable candidates using one
  fleet probe and at most eight concurrent transports.
- Reject any invalid or duplicate candidate before SSH. Preserve the shared
  probe, exact catalog, one ordinary route receipt per item, and a derived batch
  summary without adding a batch digest or mutable campaign state.
- Assign candidates deterministically using projected queue, remaining idle
  cards, active runs, and node id while leaving final allocation to each node's
  broker.
- Preserve partial remote outcomes and never retry elsewhere, roll back, or
  cancel already accepted runs when a sibling item fails.

## 0.13.0 — 2026-08-25

- Add strict `kernelinfra.fleet-endpoints.v1` maps that update only current SSH,
  kernelctl, and socket reachability for an unchanged historical node id.
- Allow checked endpoint maps on route-based status, wait, cancel, frontier,
  snapshot, and artifact fetch; bare locators and new submissions cannot use
  them.
- Revalidate exact run/task/candidate and, when recorded, run-directory
  identity at the current endpoint. Route cancel uses one atomic checked daemon
  operation; frontier preflights the run. Wrong custody fails without mutation.
- Upgrade remote observations, fleet snapshots, and artifact mirrors to v2 so
  they record exact endpoint values. Add no endpoint-map digest.

## 0.12.0 — 2026-08-25

- Add `fleet-snapshot` so an agent can observe up to 256 accepted route
  receipts through at most 16 concurrent fixed-node status queries.
- Validate and deduplicate every route before opening SSH; malformed input
  cannot produce a partial view that silently omits an invalid route.
- Recheck run, task, and candidate identity against the owning route. Preserve
  transport or identity failure as a per-route unknown without retry/failover.
- Emit a create-only derived view with exact responses and state counts, but no
  new digest, campaign database, global queue, or lifecycle authority.

## 0.11.0 — 2026-08-25

- Add fixed-node `fleet-fetch` for bounded export and create-only local mirrors
  of terminal node-owned run directories.
- Reject traversal, links/devices, duplicates, truncation, oversize, missing or
  extra files, route identity drift, size/content drift, and output overwrite.
- Label mirrors `authority=mirror-only`; remote status, routing, cancellation,
  judge validity, and frontier remain node-owned.
- Use one aggregate artifact-set transfer digest and no per-file, manifest, or
  mirror digests.

## 0.10.0 — 2026-08-25

- Add fixed-node `fleet-status`, `fleet-wait`, and `fleet-cancel` over accepted
  `(node_id, run_id)` locators; never reroute or fail over a node-owned run.
- Add route-receipt-driven `fleet-frontier`, validating catalog, locator,
  bundle path, remote run, task digest, and remote frontier identity before use.
- Emit content-addressed `kernelinfra.remote-observation.v1` receipts for
  status/wait/cancel/frontier success or unknown transport state without
  copying node-owned lifecycle authority.
- Treat remote wait exit 3 as a valid nonterminal observation and preserve SSH,
  daemon, and command failures as unknown rather than inventing run state.
- Refuse observation output overwrite and reject malformed/tampered locators,
  route receipts, catalog drift, remote bundle paths, run ids, and frontier
  identities.

## 0.9.0 — 2026-08-25

- Make `kernel_infra.__version__` the single package-version owner and derive
  package metadata from it.
- Add read-only `node-status` with daemon/broker identity, GPU/queue projection,
  active runs, ready deployments, and state-disk capacity.
- Add strict `kernelinfra.fleet.v1` catalogs and parallel SSH probing; failed or
  timed-out probes are `unknown` and never treated as idle.
- Add deterministic capability/deployment/disk eligibility and queue/idle/run
  ranking while keeping the target broker authoritative for final allocation.
- Add content-addressed task/candidate tar transport, safe node-side extraction,
  immutable inbox reuse, remote submit, `(node_id, run_id)` locators, and
  `kernelinfra.route-receipt.v1` evidence.
- Reject traversal, links/devices, oversized archives, content drift, unsafe
  catalog transport strings, and fleet tasks with relative judge cwd paths.

## 0.8.0 — 2026-08-25

- Add an explicit managed `service_deployment` reference to materialized service
  stages and snapshot its deployment ids into every accepted run state.
- Derive active service consumers solely from nonterminal run ledger states;
  expose the projection in service status without creating a second mutable
  reference-count owner.
- Reject task submission when its managed deployment is not ready and reject
  service stop while any accepted/queued/running run consumes it.
- Add optional `idle_grace_s`: start/reset the timer only while consumer count is
  zero, clear it when a consumer appears, and stop through the guarded broker
  client only after a continuous zero-consumer grace window.
- Recover runs before services on startup and close runs before services on
  daemon shutdown, preserving consumer safety across lifecycle boundaries.

## 0.7.0 — 2026-08-25

- Add live-verified `service-bind-task` materialization from one ready managed
  deployment and one checked task template.
- Replace only the selected service stage's exact identity and deployment-path
  tokens; reject ambiguous/missing tokens, multiple implicit service stages,
  stopped/interrupted/stale deployments, and any unconsumed token.
- Bind deployment id and canonical deployment-receipt SHA-256 into the judge
  identity, validate the complete output task before writing, and emit a
  content-addressed `kernelinfra.service-task-binding.v1` receipt.
- Refuse to overwrite task or binding outputs using atomic create-only writes,
  so task materialization preserves immutable experiment inputs.

## 0.6.0 — 2026-08-25

- Add strict `kernelinfra.service.v1` contracts and immutable deployment ids for
  daemon-managed broker-held evaluator services.
- Add non-blocking `service-start`, `service-status`, `service-wait`, and
  `service-stop`, plus service contract validation.
- Automatically run `gpu-run --receipt-out`, wait for a healthy worker, bind
  broker admission/executable digests, and create
  `kernelinfra.service-deployment.v2` without manual tmux orchestration.
- Preserve per-deployment spec, request, state, events, logs, admission, and
  deployment receipts; never overwrite historical deployments on restart.
- Run every service client behind the daemon-owned pipe lease, stop it through
  the broker on normal shutdown, and reconcile persisted broker jobs before
  marking deployments interrupted after restart.
- Reject a pre-existing service endpoint before broker admission so a managed
  deployment cannot accidentally attest an unrelated process already bound to
  the declared port.

## 0.5.0 — 2026-08-25

- Require a broker v0.6 admission receipt for every long-running evaluator
  deployment and re-query the active receipt before and after each request.
- Bind launch-spec, argv, explicit/effective environment, resolved executable,
  and executable-content digests into service identity and run fingerprints.
- Reject saved/live receipt mismatch, broker/job/allocation mismatch, receipt
  content tampering, and service identities that omit launch or executable
  custody.
- Upgrade the deployment contract to `kernelinfra.service-deployment.v2` and
  close the v0.4 command/environment attestation gap.

## 0.4.0 — 2026-08-25

- Attest long-running evaluator services against a live exclusive broker job,
  broker peer process, healthy worker set, loopback endpoint, and clean Git
  source commit/tree before and after each service request.
- Make the FIBServe adapter consume the deployment receipt, bind it into the
  task judge identity, and preserve the broker job, GPU, raw response, source,
  service, and receipt fingerprints in the run evidence.
- Add a fail-closed KDA authoritative-report importer that recomputes
  per-workload geomeans and preserves speedup-only results without incorrectly
  admitting them to the absolute-timing frontier.
- Accept both Docker overlay2 config IDs and containerd manifest IDs, requiring
  the exact RepoDigest for the latter and retaining manifest, config, and actual
  runtime identity in result fingerprints.
- Add checked FIBServe and KDA integration task templates.

## 0.3.1 — 2026-08-25

- Add one canonical container image contract for registry reference, platform
  manifest, config digest, CUDA/NVCC versions, required tools, and provenance.
- Bind the image contract into each CUDA judge identity and emit both manifest
  and config digests in every result fingerprint.
- Run both real CUDA ABIs on the official CUDA 12.4.1 cuDNN devel base config,
  removing the unrelated PyTorch and KernelEval layers from evaluator runtime.
- Keep registry acquisition distinct from exact local-cache validation when the
  node cannot reach Docker Hub.

## 0.3.0 — 2026-08-25

- Promote Docker/NVCC orchestration from the vector-add example to the canonical
  reusable `cuda_container` adapter.
- Add a second real custom-CUDA ABI for FP32 RMSNorm with two A800 workloads,
  a tolerance oracle, shared-reduction baseline, warp-reduction candidate, and
  incorrect control.
- Recognize compiler-equivalent runs by cubin, then SASS, then binary
  fingerprint so identical generated code cannot refresh the frontier.
- Validate three checked tasks in CI while retaining bounded local compile,
  sanitizer, AB/BA, fingerprint, and crash fail-closed contracts.

## 0.2.1 — 2026-08-25

- Run every local, service, and broker command behind a parent-owned pipe lease.
- Terminate and reap the complete child process group when the daemon dies,
  including deterministic cleanup of labeled Docker evaluator containers.
- Reconcile persisted broker job ids before interrupted runs become visible
  after restart; broker unreachability now fails daemon startup closed.
- Preserve interrupted as a terminal evidence outcome; never replay an
  uncertain candidate automatically.

## 0.2.0 — 2026-08-25

- Add bounded `local` stages so CPU-only compilation does not reserve a GPU.
- Add the exact-image A800 NVCC qualification task with a frozen candidate ABI.
- Add exact correctness, compute-sanitizer memcheck/racecheck, balanced AB/BA
  timing, and source/binary/SASS/PTX fingerprints.
- Add arithmetic-error, illegal-access, and numerically-correct race controls.
- Validate the task and local-capacity invariant in the dependency-free suite.

## 0.1.0 — 2026-08-24

- Introduce immutable task/candidate snapshots, persistent asynchronous runs,
  broker-backed staged GPU execution, typed judge results, and derived
  per-workload frontiers.
- Qualify shared correctness, exclusive benchmark, cancellation, and snapshot
  isolation on one A800 node.
