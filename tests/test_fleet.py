import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from kernel_infra.contracts import ContractError
from kernel_infra.fleet import (
    FleetCatalog,
    FleetNode,
    create_fleet_bundle,
    load_fleet_catalog,
    probe_node,
    receive_fleet_bundle,
    select_node,
)


def node_status(*, idle: int = 1, queue: int = 0, ready=()):
    return {
        "schema": "kernelinfra.node-status.v1",
        "observed_at": "2026-08-25T00:00:00+00:00",
        "kernelinfra_version": "0.9.0",
        "daemon_instance_id": "node-pid1",
        "state_root": "/srv/state",
        "disk": {"total_bytes": 1000, "used_bytes": 100, "free_bytes": 900},
        "active_runs": [],
        "services": [],
        "ready_deployments": list(ready),
        "broker": {
            "version": 2,
            "broker_version": "0.6.0",
            "instance_id": "broker-pid1",
            "probe_error": None,
            "shared_capacity": 2,
            "gpus": [
                {"gpu_id": index, "state": "idle"}
                for index in range(idle)
            ],
            "running": [],
            "queue": [{"job_id": f"q{index}"} for index in range(queue)],
        },
    }


class FleetCatalogTests(unittest.TestCase):
    def catalog(self, root: Path) -> Path:
        path = root / "fleet.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.fleet.v1",
                    "connect_timeout_s": 2,
                    "command_timeout_s": 5,
                    "nodes": [
                        {
                            "id": "a800",
                            "ssh": "A800",
                            "kernelctl": "/srv/kernelctl",
                            "socket": "/tmp/kernel.sock",
                            "inbox": "/srv/inbox",
                            "capabilities": ["a800", "cuda"],
                        },
                        {
                            "id": "b200",
                            "ssh": "verda-b200x4",
                            "kernelctl": "/srv/kernelctl",
                            "socket": "/tmp/kernel.sock",
                            "inbox": "/srv/inbox",
                            "capabilities": ["b200", "cuda"],
                        },
                    ],
                }
            )
        )
        return path

    def test_catalog_is_strict_and_ssh_values_cannot_inject_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.catalog(root)
            catalog = load_fleet_catalog(path)
            self.assertEqual([node.node_id for node in catalog.nodes], ["a800", "b200"])
            value = json.loads(path.read_text())
            value["nodes"][0]["ssh"] = "host;touch /tmp/injected"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "unsafe"):
                load_fleet_catalog(path)

    def test_probe_failure_is_unknown_and_selector_uses_only_eligible_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = load_fleet_catalog(self.catalog(Path(directory)))

            def successful(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(node_status(idle=2)), stderr=""
                )

            ok = probe_node(catalog.nodes[1], catalog, run=successful)
            self.assertEqual(ok["status"], "ok")

            def failed(*_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=[], returncode=255, stdout="", stderr="ssh timeout"
                )

            unknown = probe_node(catalog.nodes[0], catalog, run=failed)
            self.assertEqual(unknown["status"], "unknown")
            selected, _observation, decisions = select_node(
                catalog=catalog,
                observations=[unknown, ok],
                required_capabilities={"b200"},
                required_deployments=set(),
                min_free_bytes=1,
            )
            self.assertEqual(selected.node_id, "b200")
            self.assertFalse(decisions[0]["eligible"])

    def test_deployment_affinity_and_deterministic_queue_rank(self):
        nodes = (
            FleetNode("a", "a", "/k", "/s", "/i", frozenset({"cuda"})),
            FleetNode("b", "b", "/k", "/s", "/i", frozenset({"cuda"})),
        )
        catalog = FleetCatalog(nodes, 1, 1, Path("/fleet.json"), "digest")
        observations = [
            {
                "node_id": "a",
                "status": "ok",
                "node": node_status(idle=2, queue=1, ready=("deploy-a",)),
            },
            {
                "node_id": "b",
                "status": "ok",
                "node": node_status(idle=1, queue=0, ready=("deploy-b",)),
            },
        ]
        selected, _observation, _decisions = select_node(
            catalog=catalog,
            observations=observations,
            required_capabilities={"cuda"},
            required_deployments=set(),
            min_free_bytes=1,
        )
        self.assertEqual(selected.node_id, "b")
        selected, _observation, _decisions = select_node(
            catalog=catalog,
            observations=observations,
            required_capabilities={"cuda"},
            required_deployments={"deploy-a"},
            min_free_bytes=1,
        )
        self.assertEqual(selected.node_id, "a")


class FleetBundleTests(unittest.TestCase):
    def task(self, root: Path) -> Path:
        path = root / "task.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.task.v1",
                    "task_id": "fleet-task",
                    "workloads": ["w0"],
                    "comparison": {
                        "primary_workloads": ["w0"],
                        "relative_noise_floor": 0.01,
                    },
                    "stages": [
                        {
                            "id": "judge",
                            "kind": "judge",
                            "execution": "local",
                            "judge": {
                                "identity": "judge@test",
                                "cwd": "/srv/judge",
                                "command": ["/srv/judge/run"],
                            },
                        }
                    ],
                }
            )
        )
        return path

    def test_content_addressed_bundle_round_trip_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "kernel.cu").write_text("source\n")
            workspace = root / "workspace"
            workspace.mkdir()
            task, manifest, archive = create_fleet_bundle(
                task_path=self.task(root), candidate=candidate, workspace=workspace
            )
            inbox = root / "inbox"
            with archive.open("rb") as stream:
                received, observed, reused = receive_fleet_bundle(
                    stream=stream,
                    inbox=inbox,
                    expected_bundle_id=manifest["bundle_id"],
                )
            self.assertFalse(reused)
            self.assertEqual(observed, manifest)
            self.assertEqual(received.name, manifest["bundle_id"])
            self.assertEqual(task.digest, manifest["task_sha256"])
            with archive.open("rb") as stream:
                second, _observed, reused = receive_fleet_bundle(
                    stream=stream,
                    inbox=inbox,
                    expected_bundle_id=manifest["bundle_id"],
                )
            self.assertTrue(reused)
            self.assertEqual(second, received)

    def test_archive_traversal_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, member in (
                ("traversal", tarfile.TarInfo("../escape")),
                ("symlink", tarfile.TarInfo("candidate/link")),
            ):
                buffer = io.BytesIO()
                if name == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                else:
                    member.size = 0
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    archive.addfile(member, io.BytesIO(b""))
                buffer.seek(0)
                with self.assertRaisesRegex(RuntimeError, "unsafe|non-file"):
                    receive_fleet_bundle(
                        stream=buffer,
                        inbox=root / name,
                        expected_bundle_id="a" * 32,
                    )
            self.assertFalse((root / "escape").exists())

    def test_relative_task_cwd_is_not_fleet_relocatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_path = self.task(root)
            value = json.loads(task_path.read_text())
            value["stages"][0]["judge"]["cwd"] = "."
            task_path.write_text(json.dumps(value))
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "kernel.cu").write_text("source")
            workspace = root / "workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(ContractError, "absolute remote path"):
                create_fleet_bundle(
                    task_path=task_path, candidate=candidate, workspace=workspace
                )


if __name__ == "__main__":
    unittest.main()
