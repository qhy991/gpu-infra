# Changelog

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
