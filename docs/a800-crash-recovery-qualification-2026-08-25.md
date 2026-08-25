# Pipe-lease crash qualification on A800 — 2026-08-25

## Verdict

KernelInfra v0.2.1 commit
`0e46589dea2b33ce24bb36c3f13efe0db7b906b1` passed two exact-commit
`SIGKILL` qualifications on A800:

1. daemon death during a bounded local NVCC container;
2. daemon death during a broker-managed GPU correctness job.

Both runs became terminal `interrupted` after restart. No local evaluator,
execution guard, labeled Docker container, broker job, or GPU allocation
survived. Neither run was replayed.

## Frozen identity

- KernelInfra version: `0.2.1`.
- Commit: `0e46589dea2b33ce24bb36c3f13efe0db7b906b1`.
- Git bundle SHA-256:
  `2f8df5edf88c0d33bd91f40df77a2f3d620935c3e092b5bfae2d4cc941811cad`.
- Real CUDA task SHA-256:
  `4e94e1a42fd23f5da442c838a78be7b99074e31515d8bd7cc8a2b0a4fd4ecf57`.
- Scheduling smoke task SHA-256:
  `d807766148ff98c4a658a3571688ba02121ac8717c47b493a9ff9b88d0ddb18d`.
- agent-gpu-broker commit:
  `dee58c6bbf8d2e8bb27d901faf633662f12d53c7`.

The detached release checkout passed fifteen dependency-free tests, package
version verification, and checked-task validation before destructive testing.

## Mechanism

Every stage command is now a child of `exec_guard.py`. The daemon owns the write
end of an otherwise empty pipe; the guard owns its read end. `SIGKILL` closes
the daemon descriptor in the kernel, so the guard observes EOF without relying
on Python cleanup and terminates the real child process group.

Container evaluators add deterministic names and `kernelinfra.run_id` /
`kernelinfra.stage_id` labels. Their signal handler removes the Docker daemon
object, rather than merely killing the attached Docker CLI. On restart, persisted
broker job ids are reconciled through the broker socket before runs become
terminal `interrupted`.

## Experiment 1: local NVCC crash

Run `a800-cuda-vector-add-27593b37dd1c` reached:

```text
state=running  stage=compile  broker=-  gpus=-
```

The control daemon then received `SIGKILL`. Two seconds later:

- exact-release `exec_guard.py` process count: 0;
- containers with the run label: 0;
- no broker job or GPU allocation existed, because compile was local.

Restarting the exact release against the same state directory reported
`recovered_interrupted=1`. The run became terminal `interrupted`, retained
`stage=compile`, and was not replayed.

## Experiment 2: broker-managed GPU crash

Run `a800-vector-add-smoke-e2edadb4fbcb` reached:

```text
state=running
stage=correctness
broker_job_id=gpuq-91f0feb0af19
```

The control daemon then received `SIGKILL`. Two seconds later:

- exact-release execution guard count: 0;
- broker running jobs: none;
- broker queue: empty;
- physical GPU 1: idle.

The lost pipe lease terminated the `gpu-run` client group; socket closure caused
the broker to cancel and release the server-side evaluator. Restart then made an
idempotent cancel request for the persisted job id. The event recorded
`active_cancelled=false`, which proves the guard-triggered disconnect had already
removed the active job before reconciliation. The run was then archived
`interrupted` and not replayed.

## Failure semantics now qualified

- Graceful cancel and daemon shutdown terminate stage groups and preserve the
  existing cancelled/interrupted evidence boundary.
- Ungraceful daemon death relies on an OS pipe lease, not a destructor or tmux.
- Local and service children are process-group terminated and reaped.
- Docker evaluators remove the daemon-side container by deterministic identity.
- Broker clients disconnect; startup repeats cancellation by durable job id.
- Broker reconciliation failure prevents daemon startup rather than assuming a
  missing job.
- An interrupted run remains non-promotable and is never automatically retried.

## Post-release normal-path control

After the crash tests, the exact v0.2.1 production control ran basic candidate
`a800-cuda-vector-add-a3d1f2cf45b5` through the ordinary four-stage path.
Compile, exact correctness, memcheck, racecheck, and balanced AB/BA benchmark all
passed; both workloads were stable and frontier-eligible. No labeled container
remained, and the broker returned to GPU1 idle with an empty queue. This rules
out a cleanup implementation that succeeds only by breaking normal execution.

## Live state after qualification

The exact crash-test release root is
`~/kernel-infra-github-release-0e46589/`. Its current control session is
`kernelinfra-control-crash-0e46589`. The production v0.2 GPU service remains
separate. At the final check the sole broker reported GPU1 idle, no running job,
and an empty queue.

## Remaining gates

- Process cleanup is qualified on the current Unix/Linux + Docker execution
  path; another container runtime needs an equivalent daemon-object cleanup
  adapter.
- v0.2.1 deliberately interrupts rather than resumes uncertain live work.
- Cross-host dispatcher recovery and network partition semantics remain
  unqualified.
- The evaluator image still needs a portable registry RepoDigest before
  multi-node qualification.
