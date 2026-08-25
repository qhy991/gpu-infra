# Container image contracts

Each JSON file is the single factual owner for one evaluator image. CUDA task
files reference the contract; they do not duplicate tag, platform, manifest,
config, toolchain, or tool requirements.

The canonical CUDA 12.4.1 contract binds:

- official registry tag `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04`;
- `linux/amd64` platform manifest
  `sha256:0a1cb6e7bd047a1067efe14efdf0276352d5ca643dfd77963dab1a4f05a003a4`;
- config digest
  `sha256:edd3b6bf59a6acc4d56fdcdfade4d1bc9aa206359a6823a1a43a162c3021334d`;
- CUDA 12.4.1, NVCC 12.4.131, NVCC, cuobjdump, and compute-sanitizer.

For an online node, acquire by platform digest, restore the human tag, and
verify the config before starting the daemon:

```bash
docker pull \
  nvidia/cuda@sha256:0a1cb6e7bd047a1067efe14efdf0276352d5ca643dfd77963dab1a4f05a003a4
docker tag \
  nvidia/cuda@sha256:0a1cb6e7bd047a1067efe14efdf0276352d5ca643dfd77963dab1a4f05a003a4 \
  nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04
docker image inspect nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04
```

On the legacy overlay2 backend, `.Id` must equal the contract's config digest.
On Docker's containerd image store, `.Id` may instead equal the platform
manifest, but `RepoDigests` must contain the exact registry name plus that same
manifest. Any other combination is drift. Kernel Infra repeats this check on
every stage and records actual runtime ID, manifest, and config in result
fingerprints.

The initial A800 qualification node could not reach Docker Hub. It already held the
exact official base config as a content-addressed parent of a historical local
image, so qualification restored the official tag to that config and verified
NVCC, cuobjdump, compute-sanitizer, A800 visibility, task identities, and both
real CUDA ABIs. The later B200 integration qualification acquired the same
platform manifest from the registry through Docker's containerd image store and
exercised the manifest-plus-RepoDigest identity path. Do not substitute a
same-named tag with a different contract identity.
