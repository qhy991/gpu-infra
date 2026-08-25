# Cross-host routing catalog template

Replace every remote path with the exact installed checkout, daemon socket, and
content-addressed inbox on that node. Keep host aliases and paths free of shell
metacharacters; `fleet-check` enforces the transport-safe subset.

```bash
kernelctl fleet-check examples/fleet/catalog.json
kernelctl fleet-probe --catalog examples/fleet/catalog.json
kernelctl fleet-submit \
  --catalog examples/fleet/catalog.json \
  --require a800 \
  --route-out route.json \
  /path/to/task.json /path/to/candidate
kernelctl fleet-wait \
  --catalog examples/fleet/catalog.json \
  --route route.json \
  --timeout 900
kernelctl fleet-snapshot \
  --catalog examples/fleet/catalog.json \
  --out fleet-snapshot.json \
  routes/*.json
```

The dispatcher observes but never owns remote queues. An SSH/daemon/probe
failure is `unknown` and ineligible, never an idle node. Tasks transported by
v0.9 must use absolute remote judge cwd paths; evaluator installations remain
node-owned rather than copied into every candidate bundle.

`fleet-snapshot` is the agent-facing parallel read path after submission. It
prevalidates every route, queries each immutable locator once, and preserves
per-route unknown failures without adding campaign state or retry behavior.
