# Changelog

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
