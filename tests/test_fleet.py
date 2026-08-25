import argparse
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kernel_infra.contracts import ContractError, digest_json
from kernel_infra.cli import _fleet_fetch, _fleet_remote_run, _fleet_snapshot
from kernel_infra.fleet import (
    FleetCatalog,
    FleetNode,
    build_artifact_manifest,
    create_fleet_bundle,
    fetch_artifact_export,
    fleet_snapshot,
    install_artifact_mirror,
    load_fleet_catalog,
    load_fleet_endpoints,
    load_route_receipt,
    parse_locator,
    probe_node,
    receive_fleet_bundle,
    receive_artifact_export,
    remote_kernelctl_json,
    remote_observation_receipt,
    resolve_fleet_endpoint,
    select_node,
    write_artifact_export,
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

    def test_locator_route_and_remote_observation_are_content_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = load_fleet_catalog(self.catalog(Path(directory)))
            node, run_id = parse_locator(catalog, "b200:task-run-123")
            self.assertEqual((node.node_id, run_id), ("b200", "task-run-123"))
            with self.assertRaisesRegex(ContractError, "node_id:run_id"):
                parse_locator(catalog, "bad")

            content = {
                "schema": "kernelinfra.route-receipt.v1",
                "catalog_sha256": catalog.digest,
                "status": "submitted",
                "selected_node": "b200",
                "bundle_id": "a" * 32,
                "task_sha256": "t" * 64,
                "locator": {"node_id": "b200", "run_id": "task-run-123"},
                "remote": {
                    "bundle_id": "a" * 32,
                    "bundle_dir": "/srv/inbox/" + "a" * 32,
                    "run": {"run_id": "task-run-123"},
                },
            }
            route = {**content, "route_receipt_sha256": digest_json(content)}
            path = Path(directory) / "route.json"
            path.write_text(json.dumps(route))
            self.assertEqual(load_route_receipt(path, catalog), route)
            route["locator"]["run_id"] = "tampered"
            path.write_text(json.dumps(route))
            with self.assertRaisesRegex(ContractError, "digest"):
                load_route_receipt(path, catalog)

            observation = remote_observation_receipt(
                catalog=catalog,
                node=node,
                run_id=run_id,
                operation="status",
                response={"run_id": run_id, "state": "running"},
                error=None,
            )
            observed_content = dict(observation)
            claimed = observed_content.pop("observation_sha256")
            self.assertEqual(claimed, digest_json(observed_content))
            self.assertEqual(
                observation["schema"], "kernelinfra.remote-observation.v2"
            )
            self.assertEqual(observation["endpoint"]["owner"], "historical-catalog")

    def test_remote_wait_exit_three_is_an_observation_not_transport_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = load_fleet_catalog(self.catalog(Path(directory)))
            node = catalog.nodes[1]
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=3,
                stdout=json.dumps({"run_id": "run", "state": "running"}),
                stderr="",
            )
            with mock.patch("kernel_infra.fleet.subprocess.run", return_value=completed):
                value = remote_kernelctl_json(
                    node=node,
                    catalog=catalog,
                    arguments=["wait"],
                    allowed_exit_codes=frozenset({0, 3}),
                )
            self.assertEqual(value["state"], "running")


class FleetSnapshotTests(unittest.TestCase):
    def catalog(self, root: Path):
        return load_fleet_catalog(FleetCatalogTests().catalog(root))

    def route(
        self,
        root: Path,
        catalog: FleetCatalog,
        *,
        node_id: str,
        run_id: str,
        name: str,
        task_id: str = "fleet-task",
        task_sha256: str = "1" * 64,
        candidate_sha256: str = "2" * 64,
    ) -> Path:
        node = next(node for node in catalog.nodes if node.node_id == node_id)
        content = {
            "schema": "kernelinfra.route-receipt.v1",
            "catalog_sha256": catalog.digest,
            "status": "submitted",
            "selected_node": node_id,
            "bundle_id": "a" * 32,
            "task_id": task_id,
            "task_sha256": task_sha256,
            "candidate_sha256": candidate_sha256,
            "locator": {"node_id": node_id, "run_id": run_id},
            "remote": {
                "bundle_id": "a" * 32,
                "bundle_dir": node.inbox + "/" + "a" * 32,
                "run": {"run_id": run_id},
            },
        }
        value = {**content, "route_receipt_sha256": digest_json(content)}
        path = root / name
        path.write_text(json.dumps(value))
        return path

    def endpoints(self, root: Path, catalog: FleetCatalog):
        path = root / "endpoints.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.fleet-endpoints.v1",
                    "nodes": [
                        {
                            "id": "b200",
                            "ssh": "b200-upgraded",
                            "kernelctl": "/srv/v13/kernelctl",
                            "socket": "/tmp/kernel-v13.sock",
                        }
                    ],
                }
            )
        )
        return path, load_fleet_endpoints(path, catalog)

    @staticmethod
    def response(route, state):
        value = json.loads(route.read_text())
        return [
            {
                "run_id": value["locator"]["run_id"],
                "task_id": value["task_id"],
                "task_sha256": value["task_sha256"],
                "candidate_sha256": value["candidate_sha256"],
                "state": state,
            }
        ]

    def test_snapshot_observes_mixed_routes_in_locator_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            running = self.route(
                root,
                catalog,
                node_id="b200",
                run_id="run-z",
                name="running.json",
            )
            unknown = self.route(
                root,
                catalog,
                node_id="a800",
                run_id="run-a",
                name="unknown.json",
            )
            completed = self.route(
                root,
                catalog,
                node_id="b200",
                run_id="run-b",
                name="completed.json",
            )

            def query(*, node, arguments, **_kwargs):
                run_id = arguments[-1]
                self.assertEqual(
                    arguments[:4], ["status", "--socket", node.socket, "--json"]
                )
                if node.node_id == "a800":
                    raise RuntimeError("ssh timeout")
                route = completed if run_id == "run-b" else running
                return self.response(
                    route, "completed" if run_id == "run-b" else "running"
                )

            snapshot = fleet_snapshot(
                catalog=catalog,
                route_paths=[running, unknown, completed],
                query=query,
            )
            self.assertEqual(snapshot["schema"], "kernelinfra.fleet-snapshot.v2")
            self.assertEqual(
                [
                    (item["locator"]["node_id"], item["locator"]["run_id"])
                    for item in snapshot["observations"]
                ],
                [("a800", "run-a"), ("b200", "run-b"), ("b200", "run-z")],
            )
            self.assertEqual(
                [item["status"] for item in snapshot["observations"]],
                ["unknown", "ok", "ok"],
            )
            self.assertEqual(
                snapshot["summary"],
                {
                    "total": 3,
                    "ok": 2,
                    "unknown": 1,
                    "terminal": 1,
                    "nonterminal": 1,
                    "states": {"completed": 1, "running": 1},
                },
            )

    def test_snapshot_prevalidates_and_deduplicates_before_remote_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            first = self.route(
                root,
                catalog,
                node_id="b200",
                run_id="same-run",
                name="first.json",
            )
            duplicate = self.route(
                root,
                catalog,
                node_id="b200",
                run_id="same-run",
                name="duplicate.json",
            )
            query = mock.Mock()
            with self.assertRaisesRegex(ContractError, "duplicate locator"):
                fleet_snapshot(
                    catalog=catalog,
                    route_paths=[first, duplicate],
                    query=query,
                )
            query.assert_not_called()

            tampered = self.route(
                root,
                catalog,
                node_id="a800",
                run_id="other-run",
                name="tampered.json",
            )
            value = json.loads(tampered.read_text())
            value["locator"]["run_id"] = "changed"
            tampered.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "digest"):
                fleet_snapshot(
                    catalog=catalog,
                    route_paths=[first, tampered],
                    query=query,
                )
            query.assert_not_called()

    def test_snapshot_marks_remote_identity_drift_unknown_without_failover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            route = self.route(
                root,
                catalog,
                node_id="a800",
                run_id="a800-run",
                name="route.json",
            )
            calls = []

            def query(*, node, **_kwargs):
                calls.append(node.node_id)
                return [
                    {
                        "run_id": "a800-run",
                        "task_id": "different-task",
                        "task_sha256": "1" * 64,
                        "candidate_sha256": "2" * 64,
                        "state": "completed",
                    }
                ]

            snapshot = fleet_snapshot(
                catalog=catalog, route_paths=[route], query=query
            )
            self.assertEqual(calls, ["a800"])
            observation = snapshot["observations"][0]
            self.assertEqual(observation["status"], "unknown")
            self.assertIsNone(observation["response"])
            self.assertIn("task_id drift", observation["error"])
            self.assertEqual(snapshot["summary"]["ok"], 0)

    def test_endpoint_map_rebinds_transport_only_and_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            path, endpoints = self.endpoints(root, catalog)
            historical = next(node for node in catalog.nodes if node.node_id == "b200")
            effective, record = resolve_fleet_endpoint(
                catalog=catalog, node=historical, endpoints=endpoints
            )
            self.assertEqual(effective.ssh_host, "b200-upgraded")
            self.assertEqual(effective.kernelctl, "/srv/v13/kernelctl")
            self.assertEqual(effective.socket, "/tmp/kernel-v13.sock")
            self.assertEqual(effective.inbox, historical.inbox)
            self.assertEqual(effective.capabilities, historical.capabilities)
            self.assertEqual(record["owner"], "fleet-endpoints")
            self.assertEqual(record["source"], str(path.resolve()))

            value = json.loads(path.read_text())
            value["nodes"][0]["id"] = "new-node"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "historical catalog"):
                load_fleet_endpoints(path, catalog)

            value["nodes"][0]["id"] = "b200"
            value["nodes"][0]["ssh"] = "host;touch"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "unsafe"):
                load_fleet_endpoints(path, catalog)

            value["nodes"][0]["ssh"] = "b200-upgraded"
            value["nodes"].append(dict(value["nodes"][0]))
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractError, "unique"):
                load_fleet_endpoints(path, catalog)

    def test_snapshot_uses_current_endpoint_but_historical_route_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            _path, endpoints = self.endpoints(root, catalog)
            route = self.route(
                root,
                catalog,
                node_id="b200",
                run_id="old-route-run",
                name="route.json",
            )

            def query(*, node, arguments, **_kwargs):
                self.assertEqual(node.ssh_host, "b200-upgraded")
                self.assertEqual(node.kernelctl, "/srv/v13/kernelctl")
                self.assertEqual(node.socket, "/tmp/kernel-v13.sock")
                self.assertEqual(arguments[-1], "old-route-run")
                return self.response(route, "completed")

            snapshot = fleet_snapshot(
                catalog=catalog,
                route_paths=[route],
                endpoints=endpoints,
                query=query,
            )
            observation = snapshot["observations"][0]
            self.assertEqual(observation["status"], "ok")
            self.assertEqual(observation["endpoint"]["owner"], "fleet-endpoints")
            self.assertEqual(
                observation["endpoint"]["kernelctl"], "/srv/v13/kernelctl"
            )

    def test_endpoint_cancel_preflight_blocks_wrong_state_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            endpoint_path, _endpoints = self.endpoints(root, catalog)
            route = self.route(
                root,
                catalog,
                node_id="b200",
                run_id="cancel-run",
                name="route.json",
            )
            output = root / "cancel-observation.json"
            args = argparse.Namespace(
                command="fleet-cancel",
                catalog=catalog.source_path,
                endpoints=endpoint_path,
                locator=None,
                route=route,
                out=output,
                json=False,
            )
            wrong = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "cancelled": True,
                        "run": {
                            "run_id": "cancel-run",
                            "task_id": "wrong-task",
                            "task_sha256": "1" * 64,
                            "candidate_sha256": "2" * 64,
                            "state": "running",
                        },
                    }
                ),
                stderr="",
            )
            with mock.patch(
                "kernel_infra.fleet.subprocess.run", return_value=wrong
            ) as remote, mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(_fleet_remote_run(args), 1)
            self.assertEqual(remote.call_count, 1)
            command = remote.call_args.args[0][-1]
            self.assertIn("fleet-cancel-checked", command)
            self.assertNotIn(" cancel ", command)
            observation = json.loads(output.read_text())
            self.assertEqual(observation["status"], "unknown")
            self.assertEqual(observation["endpoint"]["owner"], "fleet-endpoints")
            self.assertIn("task_id drift", observation["error"])

    def test_endpoint_map_requires_historical_route_not_bare_locator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            endpoint_path, _endpoints = self.endpoints(root, catalog)
            args = argparse.Namespace(
                command="fleet-status",
                catalog=catalog.source_path,
                endpoints=endpoint_path,
                locator="b200:bare-run",
                route=None,
                out=None,
                json=False,
            )
            with mock.patch(
                "kernel_infra.fleet.subprocess.run"
            ) as remote, mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(_fleet_remote_run(args), 1)
            remote.assert_not_called()

    def test_snapshot_cli_writes_create_only_view_and_signals_all_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.catalog(root)
            route = self.route(
                root,
                catalog,
                node_id="a800",
                run_id="unknown-run",
                name="route.json",
            )
            snapshot = {
                "schema": "kernelinfra.fleet-snapshot.v2",
                "observed_at": "2026-08-25T00:00:00+00:00",
                "catalog": str(catalog.source_path),
                "observations": [],
                "summary": {
                    "total": 1,
                    "ok": 0,
                    "unknown": 1,
                    "terminal": 0,
                    "nonterminal": 0,
                    "states": {},
                },
            }
            output = root / "snapshot.json"
            args = argparse.Namespace(
                catalog=catalog.source_path,
                out=output,
                json=True,
                routes=[route],
            )
            with mock.patch(
                "kernel_infra.cli.build_fleet_snapshot", return_value=snapshot
            ), mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(_fleet_snapshot(args), 1)
            self.assertEqual(json.loads(output.read_text()), snapshot)

            with mock.patch(
                "kernel_infra.cli.build_fleet_snapshot"
            ) as build, mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(_fleet_snapshot(args), 1)
            build.assert_not_called()


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


class FleetArtifactMirrorTests(unittest.TestCase):
    def state(self, run_dir: Path, *, state: str = "completed"):
        return {
            "run_id": "fleet-task-run-123",
            "task_id": "fleet-task",
            "task_sha256": "1" * 64,
            "candidate_sha256": "2" * 64,
            "state": state,
            "terminal_at": "2026-08-25T00:00:01+00:00",
            "run_dir": str(run_dir),
        }

    def catalog_and_route(self, root: Path):
        catalog_path = root / "catalog-source.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "schema": "kernelinfra.fleet.v1",
                    "connect_timeout_s": 2,
                    "command_timeout_s": 5,
                    "nodes": [
                        {
                            "id": "b200",
                            "ssh": "verda-b200x4",
                            "kernelctl": "/srv/kernelctl",
                            "socket": "/tmp/kernel.sock",
                            "inbox": "/srv/inbox",
                            "capabilities": ["b200", "cuda"],
                        }
                    ],
                }
            )
        )
        catalog = load_fleet_catalog(catalog_path)
        content = {
            "schema": "kernelinfra.route-receipt.v1",
            "catalog_sha256": catalog.digest,
            "status": "submitted",
            "selected_node": "b200",
            "bundle_id": "a" * 32,
            "task_id": "fleet-task",
            "task_sha256": "1" * 64,
            "candidate_sha256": "2" * 64,
            "locator": {"node_id": "b200", "run_id": "fleet-task-run-123"},
            "remote": {
                "bundle_id": "a" * 32,
                "bundle_dir": "/srv/inbox/" + "a" * 32,
                "run": {"run_id": "fleet-task-run-123"},
            },
        }
        route = {**content, "route_receipt_sha256": digest_json(content)}
        return catalog, route

    @staticmethod
    def archive(manifest, files):
        buffer = io.BytesIO()
        encoded = (json.dumps(manifest, indent=2) + "\n").encode()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("artifact-manifest.json")
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
            for relative, data in files.items():
                info = tarfile.TarInfo("artifacts/" + relative)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        buffer.seek(0)
        return buffer

    def test_terminal_export_and_mirror_are_exact_and_create_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            (run_dir / "stages" / "judge").mkdir(parents=True)
            (run_dir / "result.json").write_text('{"outcome":"completed"}\n')
            (run_dir / "stages" / "judge" / "stdout.log").write_text("ok\n")
            stream = io.BytesIO()
            exported = write_artifact_export(
                run_state=self.state(run_dir), stream=stream, max_bytes=1024 * 1024
            )
            self.assertIn("artifact_set_sha256", exported)
            self.assertNotIn("manifest_sha256", exported)
            self.assertNotIn("sha256", exported["files"][0])

            catalog, route = self.catalog_and_route(root)
            route_content = dict(route)
            route_content.pop("route_receipt_sha256")
            route_content["remote"]["run"]["run_dir"] = str(run_dir.resolve())
            route = {
                **route_content,
                "route_receipt_sha256": digest_json(route_content),
            }
            destination = root / "mirror"
            stream.seek(0)
            mirror = install_artifact_mirror(
                stream=stream,
                destination=destination,
                catalog=catalog,
                route=route,
                max_bytes=1024 * 1024,
            )
            self.assertEqual(mirror["authority"], "mirror-only")
            self.assertNotIn("mirror_sha256", mirror)
            self.assertNotIn("artifact_set_sha256", mirror)
            self.assertEqual(mirror["validation"], "passed")
            self.assertEqual(
                (destination / "artifacts" / "result.json").read_text(),
                '{"outcome":"completed"}\n',
            )
            self.assertEqual(
                json.loads((destination / "artifact-manifest.json").read_text()),
                exported,
            )
            self.assertTrue((destination / "catalog.json").is_file())
            self.assertTrue((destination / "route.json").is_file())
            self.assertTrue((destination / "endpoint.json").is_file())
            self.assertTrue((destination / "mirror.json").is_file())
            self.assertEqual(mirror["schema"], "kernelinfra.artifact-mirror.v2")
            self.assertEqual(mirror["transport_endpoint"], "endpoint.json")

            second = io.BytesIO()
            write_artifact_export(
                run_state=self.state(run_dir), stream=second, max_bytes=1024 * 1024
            )
            second.seek(0)
            with self.assertRaisesRegex(RuntimeError, "overwrite"):
                install_artifact_mirror(
                    stream=second,
                    destination=destination,
                    catalog=catalog,
                    route=route,
                    max_bytes=1024 * 1024,
                )

    def test_export_rejects_nonterminal_and_symlink_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "result.json").write_text("result")
            with self.assertRaisesRegex(RuntimeError, "terminal"):
                build_artifact_manifest(
                    run_state=self.state(run_dir, state="running"), max_bytes=1024
                )
            outside = root / "outside"
            outside.mkdir()
            (run_dir / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                build_artifact_manifest(
                    run_state=self.state(run_dir), max_bytes=1024
                )

    def test_receive_rejects_traversal_symlink_and_duplicate_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = []
            for name in ("traversal", "symlink", "duplicate"):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    manifest = tarfile.TarInfo("artifact-manifest.json")
                    manifest.size = 2
                    archive.addfile(manifest, io.BytesIO(b"{}"))
                    if name == "traversal":
                        member = tarfile.TarInfo("artifacts/../escape")
                        member.size = 0
                        archive.addfile(member, io.BytesIO())
                    elif name == "symlink":
                        member = tarfile.TarInfo("artifacts/link")
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/etc/passwd"
                        archive.addfile(member)
                    else:
                        for _ in range(2):
                            member = tarfile.TarInfo("artifacts/same")
                            member.size = 0
                            archive.addfile(member, io.BytesIO())
                buffer.seek(0)
                cases.append((name, buffer))
            for name, buffer in cases:
                with self.subTest(name=name):
                    workspace = root / name
                    with self.assertRaisesRegex(
                        RuntimeError, "unsafe|non-file|duplicate"
                    ):
                        receive_artifact_export(
                            stream=buffer, workspace=workspace, max_bytes=1024
                        )
            self.assertFalse((root / "escape").exists())

    def test_receive_rejects_content_drift_and_partial_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            payload = b"a" * 32_000
            (run_dir / "result.bin").write_bytes(payload)
            manifest, _files = build_artifact_manifest(
                run_state=self.state(run_dir), max_bytes=64_000
            )
            drifted = self.archive(manifest, {"result.bin": b"b" * len(payload)})
            with self.assertRaisesRegex(RuntimeError, "content drift"):
                receive_artifact_export(
                    stream=drifted, workspace=root / "drift", max_bytes=64_000
                )

            complete = io.BytesIO()
            write_artifact_export(
                run_state=self.state(run_dir), stream=complete, max_bytes=64_000
            )
            partial = io.BytesIO(complete.getvalue()[:20_000])
            with self.assertRaises(tarfile.ReadError):
                receive_artifact_export(
                    stream=partial, workspace=root / "partial", max_bytes=64_000
                )

    def test_remote_fetch_failure_is_unknown_to_local_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, _route = self.catalog_and_route(root)
            completed = subprocess.CompletedProcess(
                args=[], returncode=255, stdout=b"", stderr=b"ssh timeout"
            )
            archive = root / "partial.tar"
            with mock.patch("kernel_infra.fleet.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "ssh timeout"):
                    fetch_artifact_export(
                        node=catalog.nodes[0],
                        catalog=catalog,
                        run_id="fleet-task-run-123",
                        archive_path=archive,
                        max_bytes=1024,
                    )
            self.assertTrue(archive.exists())
            self.assertEqual(archive.stat().st_size, 0)

    def test_fleet_fetch_unknown_leaves_no_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, route = self.catalog_and_route(root)
            route_path = root / "route.json"
            route_path.write_text(json.dumps(route))
            output = root / "mirror"
            args = argparse.Namespace(
                catalog=catalog.source_path,
                route=route_path,
                out=output,
                max_bytes=1024,
                timeout=1,
                json=False,
            )
            with mock.patch(
                "kernel_infra.cli.fetch_artifact_export",
                side_effect=RuntimeError("ssh timeout"),
            ), mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(_fleet_fetch(args), 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
