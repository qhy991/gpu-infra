import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from kernel_infra import __version__
from kernel_infra.server import KernelInfraServer


class FakeRunStore:
    def __init__(self, root: Path):
        self.root = root

    def list_states(self):
        return [
            {
                "run_id": "active-run",
                "task_id": "task",
                "state": "running",
                "service_deployment_ids": ["service-ready"],
            },
            {
                "run_id": "done-run",
                "task_id": "task",
                "state": "completed",
                "service_deployment_ids": [],
            },
        ]


class FakeRunManager:
    def __init__(self, root: Path):
        self.store = FakeRunStore(root)
        self.broker_socket = root / "broker.sock"


class FakeServices:
    def list_statuses(self):
        return [
            {
                "deployment_id": "service-ready",
                "service_id": "service",
                "state": "ready",
                "active_consumer_count": 1,
            },
            {
                "deployment_id": "service-stopped",
                "service_id": "service",
                "state": "stopped",
                "active_consumer_count": 0,
            },
        ]


class FakeCheckedStore:
    def __init__(self, root: Path):
        self.root = root
        self.state = {
            "run_id": "checked-run",
            "task_id": "task",
            "task_sha256": "1" * 64,
            "candidate_sha256": "2" * 64,
            "run_dir": "/srv/state/runs/checked-run",
            "state": "running",
        }

    def read_state(self, run_id):
        if run_id != self.state["run_id"]:
            raise KeyError(run_id)
        return dict(self.state)


class FakeCheckedManager:
    def __init__(self, root: Path):
        self.store = FakeCheckedStore(root)
        self.broker_socket = root / "broker.sock"
        self.cancel_calls = []

    async def cancel(self, run_id):
        self.cancel_calls.append(run_id)
        self.store.state["state"] = "cancelled"
        return True


class NodeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_checked_cancel_validates_identity_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = FakeCheckedManager(root)
            server = KernelInfraServer(manager, FakeServices(), root / "kernel.sock")
            expected = {
                "task_id": "task",
                "task_sha256": "1" * 64,
                "candidate_sha256": "2" * 64,
                "run_dir": "/srv/state/runs/checked-run",
            }
            wrong = {**expected, "candidate_sha256": "3" * 64}
            with self.assertRaisesRegex(ValueError, "candidate_sha256 drift"):
                await server._dispatch(
                    {
                        "op": "cancel_checked",
                        "run_id": "checked-run",
                        "expected": wrong,
                    }
                )
            self.assertEqual(manager.cancel_calls, [])

            response = await server._dispatch(
                {
                    "op": "cancel_checked",
                    "run_id": "checked-run",
                    "expected": expected,
                }
            )
            self.assertTrue(response["cancelled"])
            self.assertEqual(response["run"]["state"], "cancelled")
            self.assertEqual(manager.cancel_calls, ["checked-run"])

    async def test_node_status_is_a_read_only_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = KernelInfraServer(
                FakeRunManager(root), FakeServices(), root / "kernel.sock"
            )
            broker = {
                "version": 2,
                "broker_version": "0.6.0",
                "instance_id": "broker-instance",
                "probe_error": None,
                "shared_capacity": 2,
                "gpus": [{"gpu_id": 0, "state": "idle"}],
                "running": [],
                "queue": [],
                "_kernelinfra_peer_pid": 123,
            }
            Usage = namedtuple("Usage", "total used free")
            with mock.patch(
                "kernel_infra.server.query_broker", return_value=broker
            ), mock.patch(
                "kernel_infra.server.shutil.disk_usage",
                return_value=Usage(1000, 100, 900),
            ):
                status = await server._node_status()
        self.assertEqual(status["schema"], "kernelinfra.node-status.v1")
        self.assertEqual(status["kernelinfra_version"], __version__)
        self.assertEqual(status["disk"]["free_bytes"], 900)
        self.assertEqual([item["run_id"] for item in status["active_runs"]], ["active-run"])
        self.assertEqual(status["ready_deployments"], ["service-ready"])
        self.assertEqual(status["broker"]["broker_version"], "0.6.0")
        self.assertNotIn("_kernelinfra_peer_pid", status["broker"])


if __name__ == "__main__":
    unittest.main()
