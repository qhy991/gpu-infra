# Manifest/config image-contract qualification on A800 — 2026-08-25

## Verdict

KernelInfra v0.3.1 commit
`54d0292cfe27d7d91e93f0a019dcc89f0de2703f` passed exact-commit A800
qualification with one canonical image contract shared by the vector-add and
RMSNorm tasks.

Every stage result now binds both the official `linux/amd64` platform manifest
and the exact local config digest. The evaluator runtime uses the official CUDA
12.4.1 cuDNN devel base config directly and no longer depends on the unrelated
PyTorch, KernelEval source, or Python packages in the historical 14GB local
image.

The A800 node could not reach Docker Hub during the qualification window. It
already held the exact official config object as a content-addressed parent of
the historical image. Therefore this report qualifies manifest-bound exact
local-cache execution; it does not qualify fresh registry acquisition or a
second node.

## Frozen identity

- KernelInfra version: `0.3.1`.
- Commit: `54d0292cfe27d7d91e93f0a019dcc89f0de2703f`.
- Git bundle SHA-256:
  `bb3613d62f6817bee58f26a7b73e097d645e0522614317ba727202b37d329855`.
- Image contract:
  `images/cuda-12.4.1-cudnn-devel-ubuntu22.04-amd64.json`.
- Registry reference: `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04`.
- Platform manifest:
  `sha256:0a1cb6e7bd047a1067efe14efdf0276352d5ca643dfd77963dab1a4f05a003a4`.
- Config digest:
  `sha256:edd3b6bf59a6acc4d56fdcdfade4d1bc9aa206359a6823a1a43a162c3021334d`.
- CUDA 12.4.1; NVCC 12.4.131; required tools NVCC, cuobjdump, and
  compute-sanitizer.
- Vector-add task SHA-256:
  `cf6335f74c77ece0aa8753225de9077df9c85e989952fa0a17145a332e4a1880`.
- RMSNorm task SHA-256:
  `0ef2bde8361a89ab6a0f3e35804962adf94868785f54f6eb4139fe3065b51544`.

The detached release checkout passed seventeen dependency-free tests, installed
package version verification, and all three checked-task validations.

## Image custody

The cached official base config was created on 2024-04-23 and had no remaining
RepoTag or RepoDigest because it was retained only as a parent of a later local
build. Qualification restored the official human tag to that exact config and
then verified:

- `docker image inspect` returned the frozen config digest;
- logical image size was about 8.29GB versus about 13.97GB for the historical
  final image;
- NVCC reported CUDA 12.4.131;
- cuobjdump and compute-sanitizer were present;
- a broker-managed container saw only physical GPU 1 as logical GPU 0.

The image contract is included in the judge bundle hash. Tasks contain one
reference to that contract rather than duplicating mutable image facts in every
stage. The adapter independently validates the local tag-to-config mapping on
every stage and emits manifest plus config digests in the result fingerprint.

## Exact vector-add control

Run `a800-cuda-vector-add-511380a3f77a` passed bounded compile, exact
correctness, memcheck, racecheck, and stable AB/BA timing. Its SASS and PTX
fingerprints remained identical to the historical final-image run:

- SASS: `7fe07c9a0b7ec96486b52f1aede3c1ad174a5f94abbbfeb208a08464133df007`;
- PTX: `88e0f031e283ce697446887ed0a68fa64a5def7313501764d2dda950bb0606a4`.

Result image fields were the frozen config and platform manifest. The 4M
workload remained 1.0x; the 16M row was 0.9885x, inside the 5% noise boundary.

## Exact RMSNorm controls

Warp-reduction run `a800-cuda-rmsnorm-f649d19a7f42` passed tolerance
correctness, memcheck, racecheck, and stable timing:

- 2,048 × 4,096: 1.0152x, baseline CV 0.85%;
- 4,096 × 1,024: 1.1000x, baseline CV 1.51%.

Its SASS and PTX were unchanged from the prior exact v0.3 run. Incorrect run
`a800-cuda-rmsnorm-5674a041ef8a` failed both tolerance workloads and never
reached sanitizer or benchmark. Both results emitted the same frozen manifest
and config digests.

## Resource state

No Kernel Infra labeled container remained after the runs. The sole broker
reported GPU1 idle, no running job, and an empty queue. The exact release root
is `~/kernel-infra-github-release-54d0292/`; the live control session is
`kernelinfra-control-gh-54d0292`.

## Remaining acquisition gate

`docker pull` by the frozen platform manifest timed out while awaiting the
Docker Hub registry on both the local publishing machine and the A800 node.
Consequently:

- the platform manifest and config are now canonical and included in evidence;
- the exact cached config runtime is qualified on A800;
- fresh registry acquisition, RepoDigest materialization in Docker metadata,
  and a second-node pull remain unqualified.

A future node must acquire by the platform digest, verify that the resulting
config equals the contract, and rerun both real CUDA ABIs. A same-named tag with
a different config is a hard failure, not a fallback.
