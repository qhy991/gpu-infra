import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kernel_infra.cli import _atomic_new_json
from kernel_infra.contracts import ContractError, digest_json, load_task
from kernel_infra.service_binding import (
    DEPLOYMENT_RECEIPT_TOKEN,
    SERVICE_IDENTITY_TOKEN,
    materialize_service_task,
)
from kernel_infra.service_contracts import load_service_spec
from kernel_infra.service_store import ServiceStore
from kernel_infra.services import (
    ServiceManager,
    validate_managed_broker,
    validate_managed_gpu_run,
)
from kernel_infra.store import RunStore


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
                    "readiness_timeout_s": 10,
                }
            )
        )
        self.attest_calls = []
        self.run_store = RunStore(self.root / "run-state")

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
            run_store=self.run_store,
            attest=attest,
            health_check=lambda _url: ({"status": "ok"}, {"name": "test"}),
            broker_probe=lambda _socket: {
                "broker_version": "0.6.0",
                "instance_id": "test-broker-instance",
                "probe_error": None,
            },
            gpu_run_probe=lambda _path: None,
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    def register_consumer(self, deployment_id: str, label: str = "consumer"):
        task_path = self.root / f"{label}.task.json"
        task_path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.task.v1",
                    "task_id": f"task-{label}",
                    "workloads": ["w0"],
                    "comparison": {
                        "primary_workloads": ["w0"],
                        "relative_noise_floor": 0.01,
                    },
                    "stages": [
                        {
                            "id": "service",
                            "kind": "judge",
                            "execution": "service",
                            "service_deployment": deployment_id,
                            "judge": {
                                "identity": "service@test",
                                "cwd": str(self.root),
                                "command": ["true"],
                            },
                        }
                    ],
                }
            )
        )
        candidate = self.root / f"{label}.candidate"
        candidate.mkdir()
        (candidate / "kernel.py").write_text("value = 1\n")
        return self.run_store.create_run(
            task=load_task(task_path), candidate=candidate, label=label
        )

    async def wait_for_service_state(
        self, deployment_id: str, expected: str, timeout: float = 5
    ):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            state = self.manager.status(deployment_id)
            if state["state"] == expected:
                return state
            await asyncio.sleep(0.05)
        self.fail(
            f"deployment {deployment_id} did not reach {expected}: "
            f"{self.manager.status(deployment_id)}"
        )

    async def test_start_is_nonblocking_ready_is_durable_and_stop_releases(self):
        preflight = self.manager.preflight(self.spec_path)
        self.assertEqual(preflight["schema"], "kernelinfra.service-preflight.v1")
        self.assertEqual(preflight["broker_version"], "0.6.0")
        self.assertTrue(preflight["estimate_unknown"])
        self.assertTrue(preflight["admission_receipt_output"])
        self.assertEqual(self.manager.store.list_states(), [])
        accepted = self.manager.start(self.spec_path)
        self.assertEqual(accepted["state"], "accepted")
        ready = await self.manager.wait(accepted["deployment_id"], timeout=15)
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
        checked_template = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "fibserve_service"
            / "task.json"
        )
        template = self.root / "task.template.json"
        template.write_bytes(checked_template.read_bytes())
        output = self.root / "bound-task.json"
        binding_output = self.root / "bound-task.binding.json"
        deployment = {"service_identity": ready["service_identity"]}
        with mock.patch(
            "kernel_infra.services.load_service_receipt", return_value=deployment
        ), mock.patch(
            "kernel_infra.services.verify_service_receipt",
            side_effect=RuntimeError("stale deployment"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stale deployment"):
                await self.manager.bind_task(
                    deployment_id=ready["deployment_id"],
                    template_path=template,
                    output_path=output,
                    binding_path=binding_output,
                )
        with mock.patch(
            "kernel_infra.services.load_service_receipt", return_value=deployment
        ), mock.patch("kernel_infra.services.verify_service_receipt"):
            task, binding = await self.manager.bind_task(
                deployment_id=ready["deployment_id"],
                template_path=template,
                output_path=output,
                binding_path=binding_output,
            )
        self.assertEqual(task["task_id"], "b200-ptxbench-mha-bwd-service")
        self.assertEqual(binding["deployment_id"], ready["deployment_id"])
        self.assertTrue(await self.manager.stop(ready["deployment_id"]))
        stopped = self.manager.store.read_state(ready["deployment_id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertIn("stop requested", stopped["reason"])
        with self.assertRaisesRegex(ValueError, "not ready"):
            await self.manager.bind_task(
                deployment_id=ready["deployment_id"],
                template_path=template,
                output_path=output,
                binding_path=binding_output,
            )

        restarted = self.manager.start(self.spec_path)
        self.assertNotEqual(restarted["deployment_id"], ready["deployment_id"])
        second_ready = await self.manager.wait(restarted["deployment_id"], timeout=15)
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
            broker_probe=lambda _socket: {
                "broker_version": "0.6.0",
                "instance_id": "test-broker-instance",
                "probe_error": None,
            },
            gpu_run_probe=lambda _path: None,
        )
        accepted = manager.start(self.spec_path)
        terminal = await manager.wait(accepted["deployment_id"], timeout=15)
        self.assertEqual(terminal["state"], "failed")
        self.assertIn("attestation rejected", terminal["reason"])
        self.assertFalse(
            (Path(terminal["deployment_dir"]) / "deployment.json").exists()
        )
        await manager.close()

    async def test_incompatible_broker_or_client_is_rejected_before_history(self):
        broker_store = ServiceStore(self.root / "old-broker-state")
        client_probe = mock.Mock()
        manager = ServiceManager(
            store=broker_store,
            gpu_run=self.fake_gpu_run,
            broker_socket=self.root / "broker.sock",
            broker_probe=lambda _socket: {
                "version": 2,
                "broker_version": None,
                "instance_id": None,
                "probe_error": None,
            },
            gpu_run_probe=client_probe,
        )
        with self.assertRaisesRegex(RuntimeError, "version 0.6"):
            manager.start(self.spec_path)
        client_probe.assert_not_called()
        self.assertEqual(broker_store.list_states(), [])
        await manager.close()

        client_store = ServiceStore(self.root / "old-client-state")
        manager = ServiceManager(
            store=client_store,
            gpu_run=self.fake_gpu_run,
            broker_socket=self.root / "broker.sock",
            broker_probe=lambda _socket: {
                "broker_version": "0.6.0",
                "instance_id": "test-broker-instance",
                "probe_error": None,
            },
            gpu_run_probe=mock.Mock(
                side_effect=RuntimeError(
                    "managed gpu-run client does not accept unknown estimates"
                )
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "unknown estimates"):
            manager.start(self.spec_path)
        self.assertEqual(client_store.list_states(), [])
        await manager.close()

    async def test_immediate_stop_cannot_orphan_spawn(self):
        accepted = self.manager.start(self.spec_path)
        self.assertTrue(await self.manager.stop(accepted["deployment_id"]))
        stopped = self.manager.store.read_state(accepted["deployment_id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertNotIn(accepted["deployment_id"], self.manager._processes)
        self.assertNotIn(accepted["deployment_id"], self.manager._tasks)

    async def test_active_consumer_projection_blocks_stop_until_run_terminal(self):
        accepted = self.manager.start(self.spec_path)
        ready = await self.manager.wait(accepted["deployment_id"], timeout=15)
        self.assertEqual(ready["state"], "ready")
        consumer = self.register_consumer(ready["deployment_id"], "active")
        consumer_task = load_task(
            self.run_store.run_dir(consumer["run_id"]) / "task.json"
        )
        self.manager.assert_task_deployments_ready(consumer_task)
        status = self.manager.status(ready["deployment_id"])
        self.assertEqual(status["active_consumers"], [consumer["run_id"]])
        self.assertEqual(status["active_consumer_count"], 1)
        with self.assertRaisesRegex(ValueError, "active consumers"):
            await self.manager.stop(ready["deployment_id"])
        self.assertEqual(
            self.manager.store.read_state(ready["deployment_id"])["state"],
            "ready",
        )
        self.run_store.update_state(
            consumer["run_id"], "test_completed", state="completed"
        )
        self.assertEqual(
            self.manager.status(ready["deployment_id"])["active_consumer_count"],
            0,
        )
        self.assertTrue(await self.manager.stop(ready["deployment_id"]))
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.manager.assert_task_deployments_ready(consumer_task)

    async def test_idle_grace_resets_for_consumer_then_auto_stops(self):
        value = json.loads(self.spec_path.read_text())
        value["idle_grace_s"] = 0.4
        idle_spec = self.root / "idle-service.json"
        idle_spec.write_text(json.dumps(value))
        accepted = self.manager.start(idle_spec)
        ready = await self.manager.wait(accepted["deployment_id"], timeout=15)
        self.assertEqual(ready["state"], "ready")
        for _ in range(40):
            if self.manager.store.read_state(ready["deployment_id"])[
                "idle_since"
            ]:
                break
            await asyncio.sleep(0.025)
        else:
            self.fail("idle timer did not start")
        consumer = self.register_consumer(ready["deployment_id"], "idle-reset")
        await asyncio.sleep(0.6)
        protected = self.manager.status(ready["deployment_id"])
        self.assertEqual(protected["state"], "ready")
        self.assertEqual(protected["active_consumer_count"], 1)
        self.assertIsNone(protected["idle_since"])
        self.run_store.update_state(
            consumer["run_id"], "test_completed", state="completed"
        )
        stopped = await self.wait_for_service_state(
            ready["deployment_id"], "stopped", timeout=2
        )
        self.assertIn("idle grace elapsed", stopped["reason"])
        self.assertEqual(stopped["active_consumer_count"], 0)

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

    def test_managed_broker_and_gpu_run_capability_preflight(self):
        with self.assertRaisesRegex(RuntimeError, "version 0.6"):
            validate_managed_broker(
                {"version": 2, "broker_version": None, "instance_id": None}
            )
        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            validate_managed_broker(
                {
                    "broker_version": "0.6.0",
                    "instance_id": "broker-instance",
                    "probe_error": "inventory unavailable",
                }
            )
        validate_managed_broker(
            {
                "broker_version": "1.0.0",
                "instance_id": "broker-instance",
                "probe_error": None,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old-gpu-run"
            old.write_text("#!/bin/sh\nexit 2\n")
            old.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "unknown estimates"):
                validate_managed_gpu_run(old)
            current = root / "current-gpu-run"
            current.write_text("#!/bin/sh\necho --receipt-out\n")
            current.chmod(0o755)
            validate_managed_gpu_run(current)


class ServiceTaskBindingTests(unittest.TestCase):
    def template(self, root: Path) -> Path:
        path = root / "task.template.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.task.v1",
                    "task_id": "bound-task",
                    "workloads": ["w0"],
                    "comparison": {
                        "primary_workloads": ["w0"],
                        "relative_noise_floor": 0.01,
                    },
                    "stages": [
                        {
                            "id": "service-judge",
                            "kind": "judge",
                            "execution": "service",
                            "judge": {
                                "identity": SERVICE_IDENTITY_TOKEN + "+workload:w0",
                                "cwd": str(root),
                                "command": [
                                    "kernelinfra-fibserve",
                                    "--deployment-receipt",
                                    DEPLOYMENT_RECEIPT_TOKEN,
                                    "--definition",
                                    "fixture",
                                ],
                            },
                        }
                    ],
                }
            )
        )
        return path

    def test_materialization_binds_only_owned_fields_and_emits_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = self.template(root)
            output = root / "task.json"
            binding_output = root / "task.binding.json"
            deployment_path = root / "deployment.json"
            deployment = {"service_identity": "FIBServe@commit+admission@sha256:a"}
            state = {
                "deployment_id": "fibserve-abc123",
                "service_id": "fibserve",
                "service_sha256": "s" * 64,
                "deployment_receipt": str(deployment_path),
            }
            task, binding = materialize_service_task(
                template_path=template,
                output_path=output,
                binding_path=binding_output,
                deployment_state=state,
                deployment_receipt=deployment,
            )
            judge = task["stages"][0]["judge"]
            self.assertEqual(
                task["stages"][0]["service_deployment"], "fibserve-abc123"
            )
            self.assertIn("FIBServe@commit", judge["identity"])
            self.assertIn("deployment:fibserve-abc123", judge["identity"])
            self.assertIn(
                "deployment-receipt@sha256:" + digest_json(deployment),
                judge["identity"],
            )
            self.assertEqual(judge["command"][2], str(deployment_path.resolve()))
            self.assertEqual(binding["task_sha256"], digest_json(task))
            content = dict(binding)
            claimed = content.pop("binding_sha256")
            self.assertEqual(claimed, digest_json(content))

    def test_materialization_rejects_missing_tokens_and_new_writer_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = self.template(root)
            value = json.loads(template.read_text())
            value["stages"][0]["judge"]["identity"] = "static"
            template.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "identity token"):
                materialize_service_task(
                    template_path=template,
                    output_path=root / "out.json",
                    binding_path=root / "binding.json",
                    deployment_state={
                        "deployment_id": "d",
                        "service_id": "s",
                        "service_sha256": "x",
                        "deployment_receipt": str(root / "deployment.json"),
                    },
                    deployment_receipt={"service_identity": "service"},
                )

            template = self.template(root)
            other = root / "other"
            with self.assertRaisesRegex(ContractError, "share one directory"):
                materialize_service_task(
                    template_path=template,
                    output_path=other / "task.json",
                    binding_path=other / "binding.json",
                    deployment_state={
                        "deployment_id": "d",
                        "service_id": "s",
                        "service_sha256": "x",
                        "deployment_receipt": str(root / "deployment.json"),
                    },
                    deployment_receipt={"service_identity": "service"},
                )
            output = root / "new.json"
            _atomic_new_json(output, {"value": 1})
            with self.assertRaises(FileExistsError):
                _atomic_new_json(output, {"value": 2})
            self.assertEqual(json.loads(output.read_text()), {"value": 1})

            template = self.template(root)
            value = json.loads(template.read_text())
            second = json.loads(json.dumps(value["stages"][0]))
            second["id"] = "second-service"
            value["stages"].append(second)
            template.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "exactly one service stage"):
                materialize_service_task(
                    template_path=template,
                    output_path=root / "multi.json",
                    binding_path=root / "multi.binding.json",
                    deployment_state={
                        "deployment_id": "d",
                        "service_id": "s",
                        "service_sha256": "x",
                        "deployment_receipt": str(root / "deployment.json"),
                    },
                    deployment_receipt={"service_identity": "service"},
                )

if __name__ == "__main__":
    unittest.main()
