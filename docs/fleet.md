# Cross-host fleet routing

## Ownership

The fleet client owns only:

- checked static node catalog;
- timestamped read-only node observations;
- eligibility/rank decision;
- immutable bundle transport;
- route receipt and `(node_id, run_id)` locator.

The selected node remains the sole owner of its daemon, broker queue/allocation,
deployment history, run lifecycle, judge results, and frontier. There is no
global run database or scheduler queue.

## Node status

Each node daemon serves `kernelinfra.node-status.v1` through its existing Unix
socket. The projection includes KernelInfra/daemon identity, state-disk bytes,
active runs, service states/consumer counts, ready deployment ids, and a bounded
broker GPU/running/queue snapshot.

```bash
kernelctl node-status --socket /tmp/kernel-infra.sock --json
```

If SSH, daemon, broker, or JSON validation fails, fleet observation is
`unknown`. Unknown nodes are ineligible; they are never treated as empty or
idle.

## Catalog

`kernelinfra.fleet.v1` owns SSH aliases, installed kernelctl path, daemon socket,
immutable inbox root, and static capabilities. Transport fields use a strict
shell-safe subset and remote paths must be absolute.

```bash
kernelctl fleet-check /path/to/catalog.json
kernelctl fleet-probe --catalog /path/to/catalog.json
```

Static capabilities are declarations, not live evidence. Use them to exclude
incompatible hardware/toolchains; use node status only for current advisory
load and deployment affinity.

## Eligibility and rank

A node is eligible only when:

- probe status is ok;
- all `--require` capabilities are present;
- every managed deployment referenced by the task is ready on that node;
- free state-disk space meets `--min-free-gb`;
- broker probe has no error.

Eligible nodes rank by fewer queued jobs, more idle GPUs, fewer active runs, and
lexicographic node id. This is deterministic routing over a snapshot, not an
allocation promise. The remote broker may queue the submitted run immediately
after routing.

## Bundle transport

The client validates the task, requires absolute judge cwd paths, snapshots the
candidate, and creates `kernelinfra.fleet-bundle.v1`. Its bundle id is derived
from exact task and candidate digests.

The hidden node-side `fleet-receive` path reads tar from stdin and rejects:

- absolute paths or `..` traversal;
- duplicate members;
- symlink, hardlink, device, FIFO, or other non-file members;
- unexpected roots;
- extracted content above 256 MiB;
- missing task/candidate/manifest;
- task, candidate, manifest, or content-address drift.

A valid bundle installs atomically under `<inbox>/<bundle-id>/`. Repeated exact
content reuses the immutable directory but creates a new node-owned run.

## Route receipt

`--route-out` creates `kernelinfra.route-receipt.v1` without overwriting. It
records catalog/task/candidate/bundle identities, requirements, every node
observation, eligibility reasons, selected node observation, remote receive
receipt, locator, status/error, and canonical route-receipt SHA-256.

Fleet tasks currently must name absolute remote evaluator cwd paths. Fleet moves
task/candidate input, not evaluator installations or arbitrary dependency trees.

## Locator operations

After `fleet-submit`, use either the printed `node_id:run_id` locator or the
route receipt. Every operation remains pinned to that node:

```bash
kernelctl fleet-status --catalog catalog.json --route route.json --out status.json
kernelctl fleet-wait --catalog catalog.json --route route.json --timeout 900 --out wait.json
kernelctl fleet-cancel --catalog catalog.json --route route.json --out cancel.json
kernelctl fleet-frontier --catalog catalog.json --route route.json --out frontier.json
```

`fleet-status` and `fleet-wait` validate that the remote response owns the exact
run id. A timed wait may return a valid nonterminal observation. `fleet-cancel`
targets the locator once; it never resubmits elsewhere. `fleet-frontier` accepts
only a valid submitted route receipt, reconstructs the node-owned task path from
the checked inbox/bundle id, and requires the remote frontier task digest to
match the route.

Each command optionally creates `kernelinfra.remote-observation.v1`, binding
catalog digest, locator, operation, response/error, timestamp, and canonical
SHA-256 without overwriting. SSH/daemon/command failure is `unknown`. Route or
catalog tampering, remote bundle/run drift, and frontier identity drift are
rejected before use.

## Terminal artifact mirror

The node remains the lifecycle and evidence authority, but a caller may fetch a
complete terminal run for offline inspection:

```bash
kernelctl fleet-fetch \
  --catalog catalog.json \
  --route route.json \
  --out mirrors/my-run \
  --timeout 300
```

The hidden node-side exporter accepts only the exact routed terminal run and
streams a manifest followed by regular files. Export and receive both enforce a
1 GiB default byte limit and 10,000-file limit. The receiver rejects traversal,
links/devices, duplicates, missing/extra files, truncation, size drift, and
content drift. It checks task, candidate, run, route, and catalog identity,
then atomically installs a create-only directory:

```text
catalog.json
route.json
artifact-manifest.json   node-run authority and file inventory
mirror.json              mirror-only validation statement
artifacts/               exact node run-directory copy
```

Only one aggregate artifact-set SHA-256 crosses this transfer boundary. There
are no per-file, manifest, or mirror digests. This digest changes the action:
a mismatch rejects the whole mirror. Existing task/candidate/route identities
are reused rather than fingerprinted again.

`authority=mirror-only` is deliberate. A mirror never supplies remote status,
frontier, cancellation, routing load, or failover decisions. A nonterminal run,
SSH/daemon error, malformed archive, identity mismatch, or existing output path
fails without creating a successful mirror directory.
