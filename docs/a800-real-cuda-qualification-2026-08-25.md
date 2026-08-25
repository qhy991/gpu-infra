# Exact-commit A800 CUDA qualification — 2026-08-25

## Verdict

KernelInfra commit `930cbf70c5137f7988cbf0d39134faca7ca8307c` passed
the first real custom-CUDA node qualification on one NVIDIA A800 80GB PCIe
card. The experiment compiled candidate `.cu` source with NVCC, executed exact
GPU correctness, ran compute-sanitizer memcheck and racecheck, performed
balanced AB/BA timing, and retained binary, SASS, PTX, compiler, stage, and
broker evidence.

The result qualifies the infrastructure path. It does not claim that vector
addition was optimized: the basic candidate is the 1.0x control and the
grid-stride candidate was dominated.

## Frozen identity

- KernelInfra commit:
  `930cbf70c5137f7988cbf0d39134faca7ca8307c`.
- Git bundle SHA-256:
  `0d30421fa675ae0c8affe915efcac9e9f94510e7d189c515a8e92d441b5813ca`.
- agent-gpu-broker commit:
  `dee58c6bbf8d2e8bb27d901faf633662f12d53c7`.
- Task SHA-256:
  `53cf88220b5e59902e6e833d5a2671f9e239402bb8a231b18593801ed4a71b19`.
- Evaluator + harness SHA-256:
  `306fd1672891f3c510ffbe9ef63945c38e7e10fce40128e58390f6ef8894a90c`.
- Container image ID:
  `sha256:01fb061898c1391f77073da003e3bfa2b92b33679d2e024a932fa9d1ed635cf0`.
- CUDA 12.4.1; NVCC 12.4.131; compile target `sm_80`.
- Workloads: 4,194,304 and 16,777,216 FP32 elements.
- Noise floor: baseline CV no greater than 5%.

The A800 source was created from the verified Git bundle and checked out
detached at the exact commit. All eleven dependency-free tests and the checked
task validation passed on the node before GPU submission.

## Five-candidate batch

One `submit-many` call returned five immutable run ids in 115 ms:

- basic control: `a800-cuda-vector-add-c29ea2615e5e`;
- grid-stride: `a800-cuda-vector-add-711bf5212935`;
- wrong arithmetic: `a800-cuda-vector-add-26ac49b74414`;
- illegal global write: `a800-cuda-vector-add-f699afe13c4e`;
- numerically correct shared-memory race:
  `a800-cuda-vector-add-40affee215f0`.

The basic and grid-stride correctness jobs started at `02:25:41.562059` and
`02:25:41.562594` UTC and overlapped on physical GPU 1 as broker `shared 2/2`.
Later correctness jobs reused each available shared slot. The control plane did
not block the submitting caller while NVCC and GPU work continued.

## Ordered gate evidence

### Basic control

Run `c29ea2615e5e` passed all stages:

- correctness: `02:25:41.562059–02:25:48.883256`;
- memcheck + racecheck: `02:26:03.142553–02:26:06.258797`;
- benchmark: `02:26:12.449574–02:26:13.200952`.

Both workloads were exact-correct and stable. Candidate and baseline medians
were equal on both workloads, so it became `first-valid` at 1.0x. Its compiler
evidence reported 12 registers and zero spills for both baseline and candidate.

Fingerprints:

- source: `c96993c80de91a75164b73d15f24b0de98a0f032852ed77224c96f53f5cf640d`;
- binary: `6735cff388b8ff7c81970bc3bfaef5e88d6818258a5787e04c1eee74f91ad56a`;
- SASS: `71c73a7b985961662b0b02b403eb74fe5ac9912fc1bbd2c4352c4d23ae8c3b3e`;
- PTX: `e2599022aa3be7dc5b464ca623bbf6c87079e42a9ebada032aabae3638236879`.

### Grid-stride control

Run `711bf5212935` passed correctness, sanitizer, and benchmark. Its exclusive
sanitizer interval (`02:26:06.277485–02:26:09.121154`) began only after the
basic sanitizer ended. Its benchmark
(`02:26:13.222388–02:26:13.976186`) began only after the basic benchmark ended.
Both workloads were stable, but neither improved beyond the 5% noise floor, so
the reducer classified it `dominated`.

### Wrong arithmetic

Run `26ac49b74414` compiled but produced a maximum absolute error of 1.96875 on
both workloads. It terminated as candidate-invalid after correctness. No
sanitize or benchmark stage was created.

### Illegal global write

Run `f699afe13c4e` compiled, then CUDA reported illegal memory access during the
correctness synchronization. The evaluator classified the candidate as invalid,
not as an infrastructure error. No sanitize or benchmark stage was created.

### Numerically correct race

Run `40affee215f0` passed exact correctness on both workloads. Racecheck then
reported two cross-warp shared-memory WAW hazards at the candidate source lines,
so the sanitizer stage returned candidate-invalid and benchmark was never
created. This proves that numerical correctness and memory/concurrency safety
are independent gates.

## Resource and frontier outcome

All sanitizer and benchmark stages were broker-exclusive and non-overlapping.
The broker returned to physical GPU 1 idle with no running or queued jobs.

The final per-workload frontier is the basic 1.0x control. Grid-stride is
dominated, and the arithmetic, illegal-access, and race candidates are visibly
ineligible for three distinct evidence-backed reasons. No local kernel speedup
is claimed.

## Evidence custody

The release root is `~/kernel-infra-github-release-930cbf70/`. Canonical run
evidence is under `state/kernelinfra/runs/<run-id>/`; broker request, lifecycle,
output, and result evidence remains under the broker state directory. The live
control session is `kernelinfra-control-gh-930cbf70`; the sole GPU allocator is
the existing exact-commit broker session.

## Remaining gates

- The evaluator image is pinned by local image ID but has no registry
  RepoDigest. Multi-node use requires a published content-addressed image.
- This task proves one stable A800 CUDA ABI and two workloads, not general
  operator coverage.
- Non-graceful daemon crash recovery, cross-host dispatch, and service-stage
  deployment attestation remain unqualified.
- PTXBench/FIBServe and live KDA adapters still require their own frozen
  evaluator and workload qualifications.
