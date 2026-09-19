# Heterogeneous qualification, 2026-09-19

Local Apple M1 Pro, using this working checkout and an isolated broker/daemon:

- Catalog v2 local transport submitted immutable Metal candidates.
- `metal-vector-double-smoke-497f80d2ecb1`: completed, valid.
- `metal-vector-double-smoke-a9fa1b5baf93`: rejected, invalid.
- Both collected successfully using ordinary route receipts and artifact mirrors.
- Neither run is frontier eligible: this is correctness-only qualification.
- Cooperative occupancy scope; external GPU occupancy is unknown.
- Isolated daemon and broker both exited with code 0. Their sockets were removed.
- Original state root: `/private/tmp/ki-metal-rwbdez8o/runs`.
- `metal-collection` preserves the create-only derived collection; its historical
  route/catalog paths are retained without rewriting identity or authority.

BW1100 was read-only probed under testuser01:

- hy-smi reports eight BW1101 cards.
- The bundled `hygon_probe.py`, using the installed ROCm SMI v2 ABI, returned
  ordinals 0–7, each with an empty process list at observation time.
- Busy PID mapping is unit-tested, not live qualified.
- HIP-to-SMI ordinal correspondence and kernel correctness remain unqualified.
- Host Python is 3.7.9 (broker requires 3.10+).
- `/opt/rocm -> /root/swang/rocm_install` is inaccessible to this account.
- `docker exec dcu-dev` fails with permission denied connecting to
  `/var/lib/opt-container/device-plugins/hcu.sock`.
- No remote GPU workload, persistent broker, driver/container change or production
  process mutation was performed.

Unit suites: GPU Infra 77 tests passed; broker 43 tests run, one skipped,
42 passed. Existing uncommitted broker edits were retained. No commit created.
