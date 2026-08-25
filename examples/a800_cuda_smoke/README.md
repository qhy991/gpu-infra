# A800 real CUDA qualification task

This task proves the first real custom-CUDA path through Kernel Infra. It is an
infrastructure qualification and control comparison, not a vector-add
optimization claim.

## Frozen contract

- Hardware: NVIDIA A800, compute capability 8.0.
- Toolchain image contract:
  `../../images/cuda-12.4.1-cudnn-devel-ubuntu22.04-amd64.json`.
- Registry platform manifest:
  `sha256:0a1cb6e7bd047a1067efe14efdf0276352d5ca643dfd77963dab1a4f05a003a4`.
- Local config digest:
  `sha256:edd3b6bf59a6acc4d56fdcdfade4d1bc9aa206359a6823a1a43a162c3021334d`.
- Toolchain: CUDA 12.4.1, NVCC 12.4.131.
- Compile target: `sm_80`, `-O3`, C++17, line info, ptxas verbose report.
- Workloads: 4,194,304 and 16,777,216 FP32 elements.
- Correctness: exact FP32 equality against the trusted host oracle.
- Measurement: 10 warmups and 20 balanced AB/BA pairs per workload.
- Stability: baseline coefficient of variation no greater than 5%.
- Guardrail: candidate speedup at least 0.5x on each workload.

The checked task binds the SHA-256 of the canonical
`src/kernel_infra/adapters/cuda_container.py` adapter, `harness.cu`, and the
canonical image contract. The adapter rejects the local tag unless it resolves
to the frozen config digest and the task identity contains both registry
manifest and config digests.

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

1. `compile` (`local`): use bounded CPU capacity to compile once and archive
   binary, SASS, PTX, ptxas output, and hashes without reserving a GPU.
2. `correctness` (`shared`): reuse the exact binary and run both exact
   workloads.
3. `sanitize` (`exclusive`): reuse the exact binary and run compute-sanitizer
   memcheck followed by racecheck.
4. `benchmark` (`exclusive`): reuse the same binary and compiler evidence for
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

The CPU-only compile container runs under bounded local capacity and receives no
GPU. Every container that executes CUDA runs inside a broker allocation. All
containers have no network, drop Linux capabilities, enable
`no-new-privileges`, mount candidate and judge source read-only, and mount only
the per-run compiler artifact directory read-write. The broker-selected physical
GPU is the only GPU exposed to GPU stages.

The current A800 node could not reach Docker Hub during qualification. It reused
an exact config object already cached from the official base image and restored
the frozen official tag. This proves content and runtime identity on that node,
but not a fresh cross-node registry acquisition; see the image contract guide.
