# Existing evaluator integration

## PTXBench / FIBServe

FIBServe already owns an asynchronous task id, per-GPU workers, baseline cache,
sanitizer, evaluator, profiler, and raw traces. Kernel Infra does not replace
those parts. Run the FIBServe foreground service itself as a long-lived
broker-managed allocation, then use a Kernel Infra stage with
`"execution": "service"` to send the HTTP request and translate its result:

```json
{
  "id": "fibserve-evaluate",
  "kind": "judge",
  "execution": "service",
  "judge": {
    "identity": "PTXBench@<commit>+FIBServe@<image-digest>",
    "cwd": "/path/to/KernelInfra",
    "command": [
      "bin/kernelinfra-fibserve",
      "--service-url", "http://127.0.0.1:10000",
      "--definition", "gemm_n7168_k5120",
      "--entry-point", "kernel.cu::run",
      "--source", "kernel.cu",
      "--workload", "<frozen-workload-uuid>"
    ]
  }
}
```

The service request is CPU-only. Its GPU work is legal only while the named
FIBServe deployment is itself held by a broker job; that deployment receipt and
image/commit identity remain part of the judge identity. v0.1 records this
identity but does not automatically attest the service deployment.

## KDA Internal

KDA keeps its independent fast/full judge, 16-workload correctness, per-workload
absolute timing, and submission ledger. A KDA task uses broker-executed stages:

1. `fast` correctness/canary as `shared` only if KDA declares the measurement
   latency-insensitive;
2. affected-cell or full benchmark as `exclusive`;
3. a thin wrapper translates the exact KDA judge receipt into
   `kernelinfra.stage-result.v1`.

The wrapper must preserve KDA's candidate commit/source bundle, workload ids,
correctness fields, absolute candidate/baseline samples, and judge identity. It
must not turn `SUBMISSION ACCEPTED` into a frontier decision. Live KDA source is
currently required before checking in that wrapper; historical output is not a
stable parser contract.
