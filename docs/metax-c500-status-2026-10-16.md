# MetaX C500 source and deployment status

This is a dated publication snapshot for the authorized four-card C500
installation. [Issue 8](https://github.com/qhy991/gpu-infra/issues/8) owns the
deployment task and subsequent status. Source support is accepted at the software
scope below. No C500 broker deployment or device qualification is accepted.

## Published source and verification

| Component | Source | Verification | Pull request |
| --- | --- | --- | --- |
| Agent GPU Broker | `cda21db42b05cf89813197e5882780f15b15fe73` | All 87 unfiltered Linux Python 3.12 CPU tests passed, with zero skips. Independent source and actual-result reviews accepted software scope. | [broker #13](https://github.com/qhy991/agent-gpu-broker/pull/13) |
| GPU Infra consumer | `bff7e9393b1e154727984729e5ca8029c2844d4e` | All 100 unfiltered macOS Python 3.14 CPU tests passed, with zero skips. The real Unix-socket broker integration passed. Independent review accepted the source and CPU result. | [GPU Infra #9](https://github.com/qhy991/gpu-infra/pull/9) |

Both pull requests are open and unmerged at this snapshot. The consumer pins the
accepted broker and lets its existing synthetic NVIDIA inventory inherit the
extended interface. GPU Infra production code, receipt schemas, and container
adapters are unchanged.

The broker uses explicit MetaX discovery and complete holder observations from
`mx-smi`. Zero-memory holders remain occupied. Malformed or incomplete observations
remain unknown. The inventory owns both MACA and CUDA child selectors through the
existing queue, physical scopes, locks, deadlines, receipts, and process cleanup.

The Linux broker result covers the frozen input tree of its committed source.
The consumer suite ran under one owner immediately before its commit, with no
intervening source change. Its log does not itself encode a source revision.
Earlier failed checks remain in the external experiment record
`c500-infra-20261016-01a13d43`; this snapshot does not replace those records.

## Publication check retains one failure

The documentation publication check used Python 3.14.6 and the unfiltered
`unittest discover -s tests -v` command. It ran all 100 tests with zero skips.
There were 99 passes and one failure in
`test_scoped_request_is_snapshotted_and_forwarded`. After its 10-second wait,
the test observed `submitting` where it expected `completed`.

The cause remains unknown. This publication changes documentation only and does
not repair the test or production behavior. The prior accepted 100-test consumer
result remains historical; the latest publication check is not a passing suite.
[Issue 8](https://github.com/qhy991/gpu-infra/issues/8) retains this open
verification gap. The raw log is `infra-publication-fullcpu-002.log` in the
external experiment record. An earlier publication command used unsupported
system Python 3.9; its passing result is excluded from acceptance and retained
as `infra-publication-fullcpu-001.log`.

## Deployment remains pending

The last SSH transport check at 2026-10-16 09:22:59 UTC reached the intermediate
host but timed out before the destination connection. No service, host device
permission, SSH configuration, or network route was changed.

The last successful host survey found Python 3.10, while the broker requires
Python 3.11 or later. The selected account also lacked access to `/dev/mxcd`.
The SDK container's Python 3.12 is not a host interpreter. Fresh interpreter,
device-permission, occupancy, and service observations remain prerequisites for
deployment. Existing user containers were left intact.

The final CPU-only test container was last observed exited with code 0, PID 0,
and no OOM. It had no GPU devices. Its removal remains unverified because the
destination connection failed.

## Remaining acceptance boundaries

The open deployment work comprises Linux consumer verification, one broker and
one GPU Infra daemon under their intended identity, physical allocation and native
PCI mapping on a nonzero card, ordered multi-card selection, foreign-holder
refusal, timeout and cancellation, and terminal child and lock release.
The [broker deployment guide](../agent-gpu-broker/docs/root-deployment.md) owns
the installation procedure. The [GPU lease lifecycle](../skills/gpu-infra/SKILL.md#gpu-lease-lifecycle)
owns preparation, device execution, and release.

The NVIDIA Docker adapter does not qualify a MACA container path. A Docker client
launched through the broker does not by itself prove that the container ends
before allocation release.

Earlier bounded framework diagnostics completed Torch import and device count,
but property initialization failed under both the original 4 GiB profile and a
separately admitted 16 GiB profile. Their timeout and OOM evidence remains
retained. A new diagnostic requires a concrete new hypothesis.

C500 requires its own `xcore1000` Target, host record, backend operation evidence,
and measurement qualification. It does not inherit C550's `xcore1002` admission.
Independent Bench reference preparation and formal Compiler comparison remain
outside infrastructure acceptance. No C500 kernel result, Compiler promotion,
or speedup is established by these CPU tests.

## Task owners

- [Broker issue 11](https://github.com/qhy991/agent-gpu-broker/issues/11) owns
  MetaX allocation and hardware qualification.
- [Broker issue 12](https://github.com/qhy991/agent-gpu-broker/issues/12) owns
  the repaired FIFO and inaccessible-directory fixtures in broker PR 13.
- [GPU Infra issue 8](https://github.com/qhy991/gpu-infra/issues/8) owns the
  consumer update and deployment.
- The [project board](https://github.com/users/qhy991/projects/1) projects task
  status. The issues and trusted result records retain their authority.
