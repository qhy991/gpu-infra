# Kernel Infra agent-skill qualification — 2026-08-25

## Verdict

The repository now contains one versioned, automatically discoverable Agent
entrypoint at `skills/kernel-infra/SKILL.md`. It passed the Codex skill
validator from both its canonical repository path and the installed local
skill path.

The skill tells an Agent how to choose and operate the existing v0.16 paths
without creating another scheduler, evidence owner, or mutable campaign model.

## Canonical ownership and installation

- Canonical source: `skills/kernel-infra/` in this repository.
- Required entrypoint: `skills/kernel-infra/SKILL.md`.
- UI metadata: `skills/kernel-infra/agents/openai.yaml`.
- Local installation:
  `~/.codex/skills/kernel-infra -> <gpu-infra>/skills/kernel-infra`.

The installation is a symbolic link, not a copy. Repository updates therefore
change the discovered skill through the same canonical source and cannot leave
a second stale instruction tree.

Automatic invocation remains enabled. The description is limited to
KernelInfra/kernelctl, PTXBench/FIBServe or KDA evaluation integration, fleet
operation, and agent-gpu-broker-backed experiments; ordinary kernel editing
without an evaluation/infrastructure task is explicitly excluded.

## Workflow coverage

The entrypoint contains the decision-changing guidance needed for:

- one-node asynchronous staged evaluation;
- shared correctness versus exclusive measurement;
- reusable broker-held FIBServe services;
- live `service-preflight` compatibility and unknown-estimate custody;
- single-candidate fleet routing;
- one-probe `fleet-submit-many` parallel exploration;
- nonblocking `fleet-snapshot` observation;
- terminal-only `fleet-collect` evidence return;
- immutable historical routes through current endpoint maps;
- lifecycle/validity/frontier and KDA speedup-only distinctions;
- mirror-only authority;
- remote observation, exact-source qualification, and cleanup;
- explicit authorization boundaries for production restart, lock bypass,
  evidence deletion, main merge, PR, tag, release, or acceptance changes.

It states that SSH/daemon/broker/collector failure is unknown, never idle or
success, and preserves the GPU broker as the only allocator. It also carries
the project ban on routine SHA-256 fields and repeated tree hashing.

## Validation

The official `quick_validate.py` accepted:

- the canonical repository directory;
- the installed `~/.codex/skills/kernel-infra` symlink.

The generated `agents/openai.yaml` has quoted interface strings, a 25–64
character short description, and a default prompt that invokes
`$kernel-infra`. No scaffold TODO remains.

The commands named by the skill were checked against the exact current CLI
help surfaces:

- `service-preflight`;
- `fleet-submit-many`;
- `fleet-snapshot`;
- `fleet-collect`;
- `fleet-endpoints-check`.

Repository tests remain the implementation contract; the skill does not copy
schemas or claim capabilities absent from the current checkout.

## Deliberate limits

The skill does not authorize production mutations. It instructs the Agent to
stop for explicit authority before restarting/upgrading a production broker,
bypassing GPU locks, deleting evidence, merging main, opening a PR, tagging,
releasing, or changing experiment acceptance boundaries.

Current live gates remain visible: A800 SSH is unavailable, and the production
B200 broker/client must be upgraded to the verified v0.6 implementation before
the real multi-candidate managed FIBServe batch can safely run.

