# Bounded-local compile qualification on A800 — 2026-08-25

## Verdict

KernelInfra v0.2.0 commit
`8be97a8b0512b84ca8d090e02581700dba3c899a` passed the exact-commit A800
qualification for the split CPU/GPU pipeline:

```text
bounded local compile
  -> shared correctness
  -> exclusive memcheck + racecheck
  -> exclusive benchmark
```

NVCC and `cuobjdump` no longer reserve a GPU. The release preserved every
correctness, sanitizer, measurement, fingerprint, cancellation, and frontier
boundary established by the earlier GPU-held compile path.

## Frozen identity

- KernelInfra version: `0.2.0`.
- Commit: `8be97a8b0512b84ca8d090e02581700dba3c899a`.
- Git bundle SHA-256:
  `bd426f076837e80a2de06324a286e0e4aee6fe6cbd17f53d5e733a9677758370`.
- Task SHA-256:
  `933b02e8457afd4bd7dd692827a456a3e7483a81ad9d85a0248f1c069c6bfc62`.
- Evaluator + harness SHA-256:
  `c36321a0208e42a31fc4e2032fb9ef56df48e4e2ca93d6779e5c7d8fd94091ce`.
- Container image ID:
  `sha256:01fb061898c1391f77073da003e3bfa2b92b33679d2e024a932fa9d1ed635cf0`.
- agent-gpu-broker commit:
  `dee58c6bbf8d2e8bb27d901faf633662f12d53c7`.

The detached release checkout passed all twelve dependency-free tests, package
version verification, and both checked-task validations before submission.

## Local-capacity observation

Five candidates were snapshotted and accepted in 118 ms:

- basic: `a800-cuda-vector-add-30b2e77f36fc`;
- grid-stride: `a800-cuda-vector-add-35ea36183b3a`;
- wrong arithmetic: `a800-cuda-vector-add-313f79f114e8`;
- illegal global write: `a800-cuda-vector-add-630af89ddcd6`;
- numerically correct race: `a800-cuda-vector-add-5895d4bef74c`.

Immediately after submission, basic and grid-stride were `running` in the local
compile stage, while the other three runs were `waiting_local`. Every run showed
`broker=-` and `gpus=-`; the broker independently reported physical GPU 1 idle,
with no running or queued GPU job. This directly verifies local capacity two.

The compile intervals also show bounded admission:

- basic: `02:48:38.219995–02:48:43.928684` UTC;
- grid-stride: `02:48:38.222596–02:48:44.369385`;
- wrong arithmetic began at `02:48:43.930770`, after basic released a slot;
- illegal-write began at `02:48:44.372328`, after grid-stride released a slot;
- race began at `02:48:49.607122`, after the next slot became free.

Compile receipts have no broker job id or GPU id. GPU correctness started only
after each compile-stage result and compiler evidence became durable.

## GPU-time reduction

For the basic control, the earlier GPU-held correctness interval was about 7.32
seconds because it included NVCC and `cuobjdump`. Under v0.2:

- CPU-only compile: 5.71 seconds with no GPU allocation;
- GPU correctness: `02:48:44.058106–02:48:45.151160`, about 1.09 seconds;
- exclusive sanitizer: `02:48:45.644978–02:48:48.771428`;
- exclusive benchmark: `02:48:51.725080–02:48:52.483206`.

Grid-stride shows the same separation. Sanitizer and benchmark intervals remain
strictly non-overlapping across candidates.

## Gate outcomes

- Basic passed compile, exact correctness, memcheck, racecheck, and stable AB/BA
  timing. It became the 1.0x `first-valid` control.
- Grid-stride passed every semantic gate, but its 4M baseline CV was 5.34%, just
  above the frozen 5% floor. It was correctly classified
  `measurement-unstable`, not promoted.
- Wrong arithmetic compiled, failed exact correctness with maximum absolute
  error 1.96875, and never reached sanitizer or benchmark.
- Illegal global write compiled, failed CUDA synchronization, was classified
  candidate-invalid, and never reached sanitizer or benchmark.
- The race candidate compiled and passed exact numerical correctness. Racecheck
  reported two cross-warp WAW hazards; sanitizer rejected it and benchmark was
  absent.

The basic compiler evidence remained 12 registers and zero spills. Each run
retained source, binary, SASS, PTX, image, task, stage, and broker identities.

## Final resource state

After the batch, the broker reported physical GPU 1 idle, no running jobs, and
an empty queue. The live exact-commit control session is
`kernelinfra-control-gh-8be97a8b`; the release root is
`~/kernel-infra-github-release-8be97a8b/`.

## Remaining gates

- Local compile capacity is daemon-wide but not yet resource-weighted by CPU or
  memory estimates.
- The evaluator image still has a node-local image ID rather than a portable
  registry RepoDigest.
- Cross-host dispatch, crash recovery, and external service attestation remain
  unqualified.
- The current real-CUDA task covers one ABI and two A800 workloads; it does not
  establish general operator coverage or a performance win.
