import asyncio
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from kernel_infra.runner import RunManager
from kernel_infra.store import RunStore

from test_contracts import task_value


FAKE_GPU_RUN = r'''#!/usr/bin/env python3
import os
import subprocess
import sys
import uuid

args = sys.argv[1:]
environment = dict(os.environ)
index = 0
command = None
while index < len(args):
    item = args[index]
    if item == "--":
        command = args[index + 1:]
        break
    if item == "--env":
        key, value = args[index + 1].split("=", 1)
        environment[key] = value
        index += 2
        continue
    if item in {"--socket", "--label", "--mode", "--gpu-count", "--estimate", "--queue-timeout", "--run-timeout"}:
        index += 2
        continue
    raise SystemExit(f"unknown fake gpu-run argument: {item}")
if not command:
    raise SystemExit("missing command")
job = "gpuq-" + uuid.uuid4().hex[:12]
print(f"[gpu-run] accepted job {job} label=fake mode=shared gpus=1", file=sys.stderr, flush=True)
print("[gpu-run] queued position=1/1 eta=0s", file=sys.stderr, flush=True)
print("[gpu-run] running on physical GPUs 1 (run limit 30s)", file=sys.stderr, flush=True)
completed = subprocess.run(command, env=environment)
print(f"[gpu-run] finished state=completed exit={completed.returncode}", file=sys.stderr, flush=True)
raise SystemExit(completed.returncode)
'''


JUDGE = r'''
import hashlib
import json
import os
from pathlib import Path

candidate = Path(os.environ["KERNELINFRA_CANDIDATE_DIR"])
assert Path(os.environ["KERNELINFRA_RUN_DIR"]).is_dir()
config = json.loads((candidate / "candidate.json").read_text())
stage = os.environ["KERNELINFRA_STAGE_KIND"]
result_path = Path(os.environ["KERNELINFRA_RESULT"])
correct = bool(config["correct"])
if not correct:
    result = {
        "schema": "kernelinfra.stage-result.v1",
        "status": "failed",
        "validity": "invalid",
        "summary": "incorrect fixture",
        "workloads": [{"id": "w0", "correct": False}],
    }
elif stage == "correctness":
    result = {
        "schema": "kernelinfra.stage-result.v1",
        "status": "passed",
        "validity": "valid",
        "summary": "correct",
        "workloads": [{"id": "w0", "correct": True}],
    }
else:
    result = {
        "schema": "kernelinfra.stage-result.v1",
        "status": "passed",
        "validity": "valid",
        "summary": "timed",
        "workloads": [{
            "id": "w0",
            "correct": True,
            "candidate_ms": float(config["candidate_ms"]),
            "baseline_ms": 2.0,
            "candidate_samples_ms": [float(config["candidate_ms"])],
            "baseline_samples_ms": [2.0],
            "stable": True,
        }],
        "fingerprints": {"source_sha256": hashlib.sha256((candidate / "candidate.json").read_bytes()).hexdigest()},
    }
result_path.write_text(json.dumps(result))
raise SystemExit(0 if result["status"] == "passed" else 1)
'''


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake_gpu_run = self.root / "gpu-run"
        self.fake_gpu_run.write_text(FAKE_GPU_RUN)
        self.fake_gpu_run.chmod(0o755)
        (self.root / "judge.py").write_text(textwrap.dedent(JUDGE))
        self.task_path = self.root / "task.json"
        self.task_path.write_text(json.dumps(task_value(self.root)))
        self.store = RunStore(self.root / "state")
        self.manager = RunManager(
            store=self.store,
            gpu_run=self.fake_gpu_run,
            broker_socket=self.root / "broker.sock",
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    def candidate(self, name, *, correct=True, candidate_ms=1.0):
        path = self.root / name
        path.mkdir()
        (path / "candidate.json").write_text(
            json.dumps({"correct": correct, "candidate_ms": candidate_ms})
        )
        return path

    async def test_submit_is_nonblocking_and_run_is_durable(self):
        state = self.manager.submit(
            task_path=self.task_path,
            candidate=self.candidate("good", candidate_ms=0.8),
            label="good",
        )
        self.assertEqual(state["state"], "accepted")
        terminal = await self.manager.wait(state["run_id"], timeout=10)
        self.assertEqual(terminal["state"], "completed")
        run_dir = self.store.run_dir(state["run_id"])
        result = json.loads((run_dir / "result.json").read_text())
        self.assertTrue(result["frontier_eligible"])
        self.assertEqual(result["validity"], "valid")
        for stage in ("correctness", "bench"):
            receipt = json.loads(
                (run_dir / "stages" / stage / "receipt.json").read_text()
            )
            self.assertTrue(receipt["broker_job_id"].startswith("gpuq-"))
            self.assertEqual(receipt["gpu_ids"], [1])

    async def test_incorrect_candidate_never_reaches_benchmark(self):
        state = self.manager.submit(
            task_path=self.task_path,
            candidate=self.candidate("bad", correct=False),
            label="bad",
        )
        terminal = await self.manager.wait(state["run_id"], timeout=10)
        self.assertEqual(terminal["state"], "rejected")
        run_dir = self.store.run_dir(state["run_id"])
        self.assertFalse((run_dir / "stages" / "bench").exists())
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(result["validity"], "invalid")
        self.assertFalse(result["frontier_eligible"])


if __name__ == "__main__":
    unittest.main()
