# A800 RMSNorm qualification task

This task is the second real custom-CUDA ABI for Kernel Infra. It reuses the
canonical container adapter and proves that the task contract is not specific
to elementwise vector addition.

## Candidate ABI

```cpp
extern "C" void launch_candidate(
    float* output,
    const float* input,
    const float* weight,
    int rows,
    int columns,
    float epsilon,
    cudaStream_t stream);
```

The trusted harness owns deterministic inputs, FP64 host accumulation, the
tolerance policy, a shared-reduction CUDA baseline, AB/BA timing, and result
serialization.

## Frozen workloads

- 4,096 rows × 1,024 columns;
- 2,048 rows × 4,096 columns;
- FP32 input, weight, and output;
- epsilon `1e-5`;
- correctness tolerance `2e-4 + 2e-4 * abs(reference)`;
- baseline CV no greater than 5%;
- speed guardrail at least 0.5x.

## Included controls

- `candidate_basic`: the same shared-memory reduction structure as the trusted
  CUDA baseline.
- `candidate_warp`: a two-level warp-shuffle reduction with one block per row.
- `candidate_incorrect`: applies weight without RMS normalization and must stop
  at correctness.

All candidates use the same bounded local compile, shared correctness,
exclusive memcheck/racecheck, and exclusive balanced AB/BA stages as the
vector-add task. Source, binary, SASS, PTX, task, adapter, harness, image, stage,
and broker identities remain independently recoverable.
