import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

from kernel_infra.contracts import ContractError
from kernel_infra.service_contracts import load_service_spec
from kernel_infra.service_store import ServiceStore
from kernel_infra.services import ServiceManager


FAKE_GPU_RUN = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import tempfile
import time

def value(name):
    return sys.argv[sys.argv.index(name) + 1]

def digest(item):
    encoded = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

receipt_path = value("--receipt-out")
job_id = "gpuq-managed-service"
content = {
    "schema": "gpuq.admission-receipt.v1",
    "job_id": job_id,
    "broker_version": "0.6.0",
    "broker_instance_id": "test-broker-instance",
    "submitted_at": "2026-08-25T00:00:00+00:00",
    "started_at": "2026-08-25T00:00:01+00:00",
    "owner": value("--owner"),
    "label": value("--label"),
    "mode": "exclusive",
    "gpu_count": int(value("--gpu-count")),
    "gpu_ids": [7],
    "cwd": os.getcwd(),
    "argv_count": 2,
    "argv_sha256": "a" * 64,
    "env_keys": sorted(
        item.split("=", 1)[0]
        for index, item in enumerate(sys.argv)
        if index and sys.argv[index - 1] == "--env"
    ),
    "env_sha256": "b" * 64,
    "launch_spec_sha256": "c" * 64,
    "resolved_executable": "/usr/bin/python3",
    "executable_sha256": "d" * 64,
    "effective_env_sha256": "e" * 64,
}
receipt = {**content, "receipt_sha256": digest(content)}
directory = os.path.dirname(receipt_path)
fd, temporary = tempfile.mkstemp(prefix=".admission.", dir=directory)
with os.fdopen(fd, "w") as handle:
    json.dump(receipt, handle)
os.replace(temporary, receipt_path)
print(
    f"[gpu-run] accepted job {job_id} label={content['label']} "
    "mode=exclusive gpus=1 admission=" + content["launch_spec_sha256"][:12],
    file=sys.stderr,
    flush=True,
)
print("[gpu-run] running on physical GPUs 7 (run limit 30s)", file=sys.stderr, flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    raise SystemExit(130)
'''


class ManagedServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_gpu_run = self.root / "gpu-run"
        self.fake_gpu_run.write_text(FAKE_GPU_RUN)
        self.fake_gpu_run.chmod(0o755)
        self.spec_path = self.root / "service.json"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            service_port = listener.getsockname()[1]
        self.spec_path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.service.v1",
                    "service_id": "fibserve-test",
                    "description": "managed service fixture",
                    "owner": "test-owner",
                    "service_url": f"http://127.0.0.1:{service_port}",
                    "source_root": str(self.root),
                    "identity_prefix": "FIBServe@test+dataset@test",
                    "launch": {
                        "cwd": str(self.root),
                        "command": ["python3", "service.py"],
                        "env": {"SERVICE_PORT": str(service_port)},
                    },
                    "resources": {
                        "gpu_count": 1,
                        "estimate_s": None,
                        "queue_timeout_s": 5,
                        "run_timeout_s": 30,
                    },
                    "readiness_timeout_s": 3,
                }
            )
        )
        self.attest_calls = []

        def attest(**kwargs):
            self.attest_calls.append(kwargs)
            admission = json.loads(
                Path(kwargs["broker_admission_receipt"]).read_text()
            )
            return {
                "schema": "kernelinfra.service-deployment.v2",
                "service_identity": kwargs["service_identity"],
                "service_url": kwargs["service_url"],
                "broker_job_id": admission["job_id"],
                "broker_gpu_ids": admission["gpu_ids"],
            }

        self.manager = ServiceManager(
            store=ServiceStore(self.root / "state"),
            gpu_run=self.fake_gpu_run,
            broker_socket=self.root / "broker.sock",
            attest=attest,
            health_check=lambda _url: ({"status": "ok"}, {"name": "test"}),
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def test_start_is_nonblocking_ready_is_durable_and_stop_releases(self):
        accepted = self.manager.start(self.spec_path)
        self.assertEqual(accepted["state"], "accepted")
        ready = await self.manager.wait(accepted["deployment_id"], timeout=5)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["broker_job_id"], "gpuq-managed-service")
        self.assertEqual(ready["gpu_ids"], [7])
        self.assertIn("admission@sha256:" + "c" * 64, ready["service_identity"])
        self.assertIn("executable@sha256:" + "d" * 64, ready["service_identity"])
        directory = Path(ready["deployment_dir"])
        for name in (
            "spec.json",
            "request.json",
            "state.json",
            "events.jsonl",
            "stdout.log",
            "stderr.log",
            "admission.json",
            "deployment.json",
        ):
            self.assertTrue((directory / name).exists(), name)
        with self.assertRaisesRegex(ValueError, "already has active deployment"):
            self.manager.start(self.spec_path)
        self.assertTrue(await self.manager.stop(ready["deployment_id"]))
        stopped = self.manager.store.read_state(ready["deployment_id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertIn("stop requested", stopped["reason"])

        restarted = self.manager.start(self.spec_path)
        self.assertNotEqual(restarted["deployment_id"], ready["deployment_id"])
        second_ready = await self.manager.wait(restarted["deployment_id"], timeout=5)
        self.assertEqual(second_ready["state"], "ready")
        await self.manager.stop(second_ready["deployment_id"])

    async def test_attestation_failure_is_terminal_and_process_is_reaped(self):
        def fail_attest(**_kwargs):
            raise RuntimeError("attestation rejected")

        manager = ServiceManager(
            store=ServiceStore(self.root / "failed-state"),
            gpu_run=self.fake_gpu_run,
            broker_socket=self.root / "broker.sock",
            attest=fail_attest,
            health_check=lambda _url: ({"status": "ok"}, {"name": "test"}),
        )
        accepted = manager.start(self.spec_path)
        terminal = await manager.wait(accepted["deployment_id"], timeout=5)
        self.assertEqual(terminal["state"], "failed")
        self.assertIn("attestation rejected", terminal["reason"])
        self.assertFalse(
            (Path(terminal["deployment_dir"]) / "deployment.json").exists()
        )
        await manager.close()

    async def test_immediate_stop_cannot_orphan_spawn(self):
        accepted = self.manager.start(self.spec_path)
        self.assertTrue(await self.manager.stop(accepted["deployment_id"]))
        stopped = self.manager.store.read_state(accepted["deployment_id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertNotIn(accepted["deployment_id"], self.manager._processes)
        self.assertNotIn(accepted["deployment_id"], self.manager._tasks)

    async def test_preexisting_endpoint_is_rejected_before_broker_launch(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            value = json.loads(self.spec_path.read_text())
            value["service_url"] = f"http://127.0.0.1:{listener.getsockname()[1]}"
            occupied = self.root / "occupied.json"
            occupied.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "already in use"):
                self.manager.start(occupied)
        self.assertEqual(self.manager.store.list_states(), [])

    async def test_recovery_cancels_broker_before_marking_interrupted(self):
        spec = load_service_spec(self.spec_path)
        state = self.manager.store.create_deployment(spec)
        self.manager.store.update_state(
            state["deployment_id"],
            "test_ready",
            state="ready",
            broker_job_id="gpuq-orphan",
            gpu_ids=[7],
        )
        calls = []

        async def cancel(job_id):
            calls.append(job_id)
            return True

        self.manager._cancel_broker_job = cancel
        self.assertEqual(await self.manager.recover_interrupted(), 1)
        self.assertEqual(calls, ["gpuq-orphan"])
        recovered = self.manager.store.read_state(state["deployment_id"])
        self.assertEqual(recovered["state"], "interrupted")


class ManagedServiceContractTests(unittest.TestCase):
    def test_contract_rejects_dynamic_identity_and_non_loopback_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {
                "schema": "kernelinfra.service.v1",
                "service_id": "service",
                "owner": "owner",
                "service_url": "http://127.0.0.1:10000",
                "source_root": ".",
                "identity_prefix": "service@test",
                "launch": {"cwd": ".", "command": ["true"]},
                "resources": {"gpu_count": 1, "run_timeout_s": 10},
            }
            path = root / "service.json"
            path.write_text(json.dumps(value))
            spec = load_service_spec(path)
            self.assertEqual(spec.service_id, "service")
            self.assertEqual(spec.source_root, root.resolve())

            value["identity_prefix"] = "service+admission@sha256:bad"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "must not predeclare"):
                load_service_spec(path)

            value["identity_prefix"] = "service@test"
            value["service_url"] = "http://example.com:10000"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "loopback"):
                load_service_spec(path)


if __name__ == "__main__":
    unittest.main()
