import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

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


class NodeStatusTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(status["kernelinfra_version"], "0.10.0")
        self.assertEqual(status["disk"]["free_bytes"], 900)
        self.assertEqual([item["run_id"] for item in status["active_runs"]], ["active-run"])
        self.assertEqual(status["ready_deployments"], ["service-ready"])
        self.assertEqual(status["broker"]["broker_version"], "0.6.0")
        self.assertNotIn("_kernelinfra_peer_pid", status["broker"])


if __name__ == "__main__":
    unittest.main()
