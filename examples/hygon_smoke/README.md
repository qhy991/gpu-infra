# Hygon HIP correctness smoke

Requires a qualified Hygon broker and HIP-enabled PyTorch. Set judge.command's
Python path to the site's HIP environment. See [the backend guide](../../docs/heterogeneous-backends.md)
for the SMI probe and device-ordinal qualification requirement.

```sh
bin/kernelctl submit --task examples/hygon_smoke/task.json examples/hygon_smoke/candidate_good
bin/kernelctl submit --task examples/hygon_smoke/task.json examples/hygon_smoke/candidate_bad
```

Good must be valid; bad must be invalid. Missing/wrong runtime remains unknown.
This example was not executed on BW1100: host runtime/container permissions
prevented qualification. It records no timings or frontier performance claims.
