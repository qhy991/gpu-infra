# Canonical-adapter RMSNorm qualification on A800 — 2026-08-25

## Verdict

KernelInfra v0.3.0 commit
`2c32861237a1d9f2b5fa46e727a304c24309f638` passed exact-commit A800
qualification for two independent custom-CUDA ABIs through one canonical
container adapter:

- elementwise vector addition with exact correctness;
- row-wise FP32 RMSNorm with a tolerance oracle and reduction semantics.

The RMSNorm warp-reduction candidate produced a replicated, stable improvement
on one frozen workload. This remains an operator-level result and is not an
end-to-end inference claim.

## Frozen identity

- KernelInfra version: `0.3.0`.
- Commit: `2c32861237a1d9f2b5fa46e727a304c24309f638`.
- Git bundle SHA-256:
  `a6cc8b692157fa93baf65ad81cd018c59c9803ac893cd98fcc4db0e995c04718`.
- RMSNorm task SHA-256:
  `ccfd045df4511059f492fb84309ace9d54a1596bccadb1cd1340a351901b2f33`.
- RMSNorm adapter + harness SHA-256:
  `24c30c2d8510ce2a32d52e14320bb9cdc85c4a8d5abb7efba778037677727954`.
- Vector-add task SHA-256:
  `e91024b7a8f9d976bcc7796f59802c3b6b382aa9627c302d9d2f4e775f2273af`.
- Container image ID:
  `sha256:01fb061898c1391f77073da003e3bfa2b92b33679d2e024a932fa9d1ed635cf0`.
- agent-gpu-broker commit:
  `dee58c6bbf8d2e8bb27d901faf633662f12d53c7`.

The detached checkout passed sixteen dependency-free tests, package version
verification, and all three checked-task validations before GPU execution.

## Canonical adapter ownership

Docker/NVCC lifecycle policy now has one code owner:
`src/kernel_infra/adapters/cuda_container.py`. It owns image drift checks,
container isolation and labels, bounded local compilation, binary/SASS/PTX
evidence, signal cleanup, sanitizer execution, AB/BA result translation, and
container removal.

Each operator directory owns only its task, harness, candidate ABI, oracle,
tolerance, baseline, workloads, and bounded candidates. The vector-add task was
rerun after this refactor as run `a800-cuda-vector-add-a783a7ff05f2`; compile,
correctness, memcheck, racecheck, benchmark, stability, and fingerprints all
passed, proving the ownership move did not break the original ABI.

## RMSNorm contract

- FP32 input, weight, and output.
- Epsilon `1e-5`.
- Workloads: 4,096 × 1,024 and 2,048 × 4,096.
- FP64 host accumulation for the trusted reference.
- Tolerance: `2e-4 + 2e-4 * abs(reference)`.
- Baseline CV no greater than 5%.
- 10 warmups and 20 balanced AB/BA pairs.
- Guardrail at least 0.5x on both workloads.

The basic and warp candidates compiled concurrently under bounded local
capacity. Their correctness jobs began within about one millisecond and shared
physical GPU 1. Broker lifecycle evidence, rather than control-plane postprocess
timestamps, proves exclusive serialization:

- basic sanitizer: `03:41:39.123176–03:41:41.893349` UTC;
- warp sanitizer: `03:41:41.930827–03:41:44.722158`;
- basic benchmark: `03:41:44.760530–03:41:45.527603`;
- warp benchmark: `03:41:46.520702–03:41:47.302716`.

No exclusive intervals overlap.

## Basic control

Run `a800-cuda-rmsnorm-61991965a552` passed compile, tolerance correctness,
memcheck, racecheck, and stable benchmark on both workloads. Candidate and
baseline medians were equal:

- 2,048 × 4,096: `0.068608 ms` / `0.068608 ms`;
- 4,096 × 1,024: `0.033792 ms` / `0.033792 ms`.

It became the 1.0x `first-valid` control. Both baseline and candidate used 15
registers, 1,024 bytes shared memory, and zero spills.

## Warp-reduction candidate

Run `a800-cuda-rmsnorm-385dccbaef11` passed every gate and produced:

- 2,048 × 4,096: 1.0152x;
- 4,096 × 1,024: 1.0645x.

Baseline CV was 0.74% and 1.81%, respectively. Because the 4,096 × 1,024 gain
exceeded the frozen 5% noise floor without a regression, the reducer classified
the run `improves-primary`.

The candidate used 17 registers, 36 bytes shared memory, and zero spills. Its
fingerprints were:

- source: `7cdd4422772b25ca0f73865bddcf3518ee01d37cf6bdbd7963637a8b26bb3e15`;
- binary: `1d731eb951d9d59118291596484ee34911fdd0826453d49be52d85a4a9b84d81`;
- SASS: `a12b786ca7d667fdad9f6bc959669e858713e1e5d1579f1fc01c3a595d988138`;
- PTX: `436f6892ef3408a815940af6a7e778a1cbbd3635479098aba5c50cec049768ef`.

## Replication and compiler equivalence

Two additional exact-source runs retained the same source, binary, SASS, and
PTX fingerprints:

| Run | 2,048 × 4,096 | 4,096 × 1,024 | Max baseline CV |
| --- | ---: | ---: | ---: |
| `694e9e0356b9` | 1.0152x | 1.1000x | 1.08% |
| `09810145c40a` | 1.0076x | 1.0645x | 1.67% |

The reducer did not spend these replicas as new frontier updates. It classified
both `compiler-equivalent` with the reason that their `sass_sha256` matched run
`385dccbaef11`. This qualifies the cubin→SASS→binary fallback used when an
evaluator does not expose a standalone cubin.

## Incorrect control

Run `a800-cuda-rmsnorm-fbe8177504c1` compiled but omitted RMS normalization. It
failed both workloads with maximum absolute error about 0.278 and maximum
relative error about 0.161. Sanitizer and benchmark were never created.

## Final state and boundary

After qualification, the sole broker reported GPU1 idle, no running jobs, an
empty queue, and zero labeled containers. The exact release root is
`~/kernel-infra-github-release-2c328612/`; the live control session is
`kernelinfra-control-gh-2c328612`.

The accepted result is limited to this RMSNorm ABI, A800, FP32, two workloads,
the frozen baseline, and this toolchain image. It is not evidence for BF16,
framework integration, another GPU, or end-to-end serving performance.
