# Metal correctness smoke

Requires macOS with a single Apple GPU and Xcode Command Line Tools.
Start the Metal broker with explicit cooperative scope, then the node daemon,
as described in [the backend guide](../../docs/heterogeneous-backends.md).

From the repository root:

```sh
bin/kernelctl submit --task examples/metal_smoke/task.json examples/metal_smoke/candidate_good
bin/kernelctl submit --task examples/metal_smoke/task.json examples/metal_smoke/candidate_bad
bin/kernelctl status --json
```

Good must be valid; bad must be invalid. The CPU-only stage compiles the Swift
host. Metal source compilation and execution run inside the broker allocation.
For Fleet, make a task copy with absolute judge.cwd pointing at this directory.
No timing measurements or frontier admission are claimed.
