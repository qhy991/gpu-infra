# A800 real CUDA qualification task

This task proves the first real custom-CUDA path through Kernel Infra. It is an
infrastructure qualification and control comparison, not a vector-add
optimization claim.

## Frozen contract

- Hardware: NVIDIA A800, compute capability 8.0.
- Toolchain image:
  `sha256:01fb061898c1391f77073da003e3bfa2b92b33679d2e024a932fa9d1ed635cf0`.
- Toolchain: CUDA 12.4.1, NVCC 12.4.131.
- Compile target: `sm_80`, `-O3`, C++17, line info, ptxas verbose report.
- Workloads: 4,194,304 and 16,777,216 FP32 elements.
- Correctness: exact FP32 equality against the trusted host oracle.
- Measurement: 10 warmups and 20 balanced AB/BA pairs per workload.
- Stability: baseline coefficient of variation no greater than 5%.
- Guardrail: candidate speedup at least 0.5x on each workload.

The checked task binds the SHA-256 of `evaluator.py + harness.cu` and the exact
container image ID. The evaluator rejects a moving `latest` tag if it no longer
resolves to that image.

## Candidate ABI

Every candidate directory supplies exactly `kernel.cu` with this function:

```cpp
extern "C" void launch_candidate(
    float* output,
    const float* left,
    const float* right,
    std::size_t count,
    cudaStream_t stream);
```

The trusted harness owns allocation, deterministic inputs, the host oracle,
baseline kernel, launch-error checks, AB/BA order, and JSON result production.

## Ordered stages

1. `correctness` (`shared`): compile once, run both exact workloads, and archive
   binary, SASS, PTX, ptxas output, and hashes.
2. `sanitize` (`exclusive`): reuse the exact binary and run compute-sanitizer
   memcheck followed by racecheck.
3. `benchmark` (`exclusive`): reuse the same binary and compiler evidence for
   balanced AB/BA timing.

Any failed stage prevents later stages. A compile or candidate runtime failure
is candidate-invalid; missing image, identity drift, or missing compiler
artifacts remains infrastructure-unknown.

## Included controls

- `candidate_basic`: the same coalesced kernel shape as the trusted baseline.
- `candidate_grid_stride`: a bounded-grid alternative; expected to be a control,
  not an assumed win.
- `candidate_incorrect`: subtracts instead of adding and must stop at
  correctness.
- `candidate_oob`: performs an illegal global write and must be classified as
  candidate-invalid.
- `candidate_race`: is numerically correct but has a cross-warp shared-memory
  WAW hazard; correctness must pass, racecheck must reject it, and benchmark
  must not run.

## Container boundary

The host wrapper invokes Docker only inside a broker allocation. The container
has no network, drops Linux capabilities, enables `no-new-privileges`, mounts
candidate and judge source read-only, and mounts only the per-run compiler
artifact directory read-write. The broker-selected physical GPU is the only GPU
exposed to the container.

The current image is a local image without a registry RepoDigest, so its image
ID is node-specific custody rather than portable supply-chain provenance. A
future multi-node task must build and publish a content-addressed evaluator
image instead of weakening this check.
