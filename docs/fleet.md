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

## Current endpoint map

A route receipt must continue validating against the exact catalog used when
the run was accepted. If the same node later moves to a new installed
`kernelctl` or daemon socket, do not edit or re-sign either historical file.
Create a strict current transport map instead:

```json
{
  "schema": "kernelinfra.fleet-endpoints.v1",
  "nodes": [
    {
      "id": "b200",
      "ssh": "verda-b200x4",
      "kernelctl": "/opt/kernel-infra/current/bin/kernelctl",
      "socket": "/run/kernel-infra/control.sock"
    }
  ]
}
```

```bash
kernelctl fleet-endpoints-check \
  --catalog historical-catalog.json current-endpoints.json
kernelctl fleet-status \
  --catalog historical-catalog.json \
  --endpoints current-endpoints.json \
  --route historical-route.json
```

The endpoint map owns only current SSH host, executable, and socket for an
existing catalog node id. It may be used by route-based status, wait, cancel,
frontier, snapshot, and fetch. It cannot be used by submit or a bare locator,
cannot add a node absent from the historical catalog, and does not replace
inbox, capability, task, candidate, or locator facts.

Status, wait, and snapshot validate the exact returned run/task/candidate and
recorded run directory. Route cancel sends those expected values in one atomic
daemon request, so identity validation and cancellation share one authority
boundary. Frontier first runs the same status continuity check. Artifact
install checks those identities and the manifest run directory. All v2 outputs
embed the exact endpoint values used. The map adds no digest; it is current
checked configuration, not historical evidence or node identity.

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

Each command optionally creates `kernelinfra.remote-observation.v2`, binding
catalog digest, locator, exact transport endpoint, operation, response/error,
timestamp, and the existing canonical observation SHA-256 without overwriting.
SSH/daemon/command failure is `unknown`. Route or catalog tampering, remote
bundle/run drift, endpoint state-custody drift, and frontier identity drift are
rejected before use.

## Parallel route snapshot

An agent with several accepted candidates can observe them in one nonblocking
batch instead of issuing serial SSH calls:

```bash
kernelctl fleet-snapshot \
  --catalog catalog.json \
  --out fleet-snapshot.json \
  routes/*.json
```

The client accepts 1–256 submitted route receipts. It validates the complete
input set and rejects duplicate locators before opening any SSH connection,
then uses at most 16 concurrent fixed-node `status` calls. Each response must
match the route's run, task, and candidate identity. Results are sorted by
`(node_id, run_id)` and contain exact responses/errors plus derived counts for
ok, unknown, terminal, nonterminal, and lifecycle states.

The `kernelinfra.fleet-snapshot.v2` output is a read model, not campaign state.
It does not probe for a new
node, submit, retry, fail over, wait, cancel, fetch, or change any run. A timeout
or identity mismatch is visible as `unknown` only for that route. If all routes
are unknown the command writes the requested view but exits 1; any ok route
makes the command exit 0. Existing outputs are never overwritten. The schema
adds no digest and reuses identities already owned by catalog and route
receipts.

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
endpoint.json            exact transport values used for this fetch
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
`kernelinfra.artifact-mirror.v2` points to `endpoint.json`; changing software
reachability does not rewrite the historical catalog or route stored beside it.
