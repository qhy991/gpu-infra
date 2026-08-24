# GitHub release qualification on A800 — 2026-08-24

## Verdict

The exact source published as private GitHub commit
`95a85ebeb044f7e77d56355de219f925e0329dec` passed the v0.1 node contract on
one NVIDIA A800 80GB PCIe card. The dependency was the exact public
`agent-gpu-broker` commit
`dee58c6bbf8d2e8bb27d901faf633662f12d53c7`.

This is an infrastructure qualification, not a custom-kernel speedup claim. The
fixture uses two equivalent PyTorch vector expressions so the experiment can
attribute queueing, gating, cancellation, snapshot, and evidence behavior
without claiming an optimized A800 kernel.

## GitHub custody

Direct cloning from the A800 node failed because its GitHub HTTPS transport was
unstable. No credential was copied to the node. Instead, GitHub's authenticated
archive API produced exact-commit tarballs on the publishing machine; the
archives were hashed, transferred, re-hashed on A800, and unpacked into a new
release directory.

| Artifact | Commit | Archive SHA-256 |
| --- | --- | --- |
| KernelInfra | `95a85ebeb044f7e77d56355de219f925e0329dec` | `0c9a3e2ca266b6e293d9cc75e68cb48221b99112289f358b4e5e6906be7f1b5c` |
| agent-gpu-broker | `dee58c6bbf8d2e8bb27d901faf633662f12d53c7` | `b3639c0e775be7c58da5ada2edab105b2211f3489345acacf0d520dbc00d48e0` |

The unpacked KernelInfra source passed all eight dependency-free tests under
Python 3.10. Its checked task and evaluator identities were:

- task SHA-256:
  `d807766148ff98c4a658a3571688ba02121ac8717c47b493a9ff9b88d0ddb18d`;
- evaluator SHA-256:
  `7020635f429c5015b1c73320229aaa1b194f1e4d9e51ded87cda26a8952adb0d`.

## Experiment 1: non-blocking batch, sharing, exclusivity, and gate

Three candidates were accepted in one `submit-many` call in 109 ms:

- valid multiply: `a800-vector-add-smoke-e19d92d9458d`;
- valid add: `a800-vector-add-smoke-c152baa1b50d`;
- intentionally invalid: `a800-vector-add-smoke-86ca8e50b5c7`.

The two valid correctness stages started at `13:36:07.899957` and
`13:36:07.899629` UTC. Broker status observed physical GPU 1 as `shared 2/2`.
Their intervals overlapped until `13:36:14.259681` and `13:36:14.160989`.

The invalid correctness stage started when a shared slot became free and was
rejected at `13:36:16.413846`. It recorded `validity=invalid` and never created a
benchmark stage.

The two exclusive benchmarks did not overlap:

- add: `13:36:16.419794` to `13:36:18.492710`;
- multiply: `13:36:18.494485` to `13:36:20.560127`.

Both correct timings were marked `measurement-unstable`; consequently the
frontier remained empty after this batch. This is the intended fail-closed
decision, not a missing result.

## Experiment 2: cancellation releases the card

Run `a800-vector-add-smoke-626419218c25` reached the running correctness stage
and was then explicitly cancelled. Its terminal evidence was:

- outcome: `cancelled`;
- validity: `unknown`;
- frontier eligible: `false`;
- terminal reason: `run cancelled by request`.

Immediately afterward, the broker reported physical GPU 1 idle, with no running
or queued jobs.

## Experiment 3: queued source is immutable

A bounded copy of the valid multiply candidate was submitted as run
`a800-vector-add-smoke-cb0ec72d2b80`. After `submit` returned, the original
candidate was changed from multiply-by-two to multiply-by-three.

- mutated source SHA-256:
  `ee3a51862fa4d049787abb3ecb3577293d9f33bb38c9f7f2ff9057c60886277c`;
- run snapshot SHA-256:
  `4e94cb249061b88cd5678601eb9da5d8cbca77540f1f83b967fb5614167cee2b`.

The run completed with `validity=valid`, proving that execution used the
pre-queue snapshot instead of the subsequently edited source. Its stable 1.0x
control became `first-valid`; it is not an optimization gain.

## Live deployment after qualification

The release services remain available in tmux and manage physical GPU 1 only:

- `kernelinfra-broker-gh-95a85eb`;
- `kernelinfra-control-gh-95a85eb`.

The release root is `~/kernel-infra-github-release-95a85eb/`. Canonical run
evidence is below `state/kernelinfra/runs/`; broker request, output, result, and
lifecycle evidence is below `state/gpuq/`. At the final check, the card was idle
and both queues were empty.

## Remaining gates

- The A800 host does not expose `nvcc` on its normal PATH, so the next real
  kernel task needs a broker-managed CUDA-toolkit evaluator container.
- The FIBServe adapter still requires qualification against a broker-held
  PTXBench deployment on supported hardware and a frozen trace set.
- The KDA adapter remains intentionally unimplemented until the live owning
  repository, judge output, and ledger are reachable and pinned.
