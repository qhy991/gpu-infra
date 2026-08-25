import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kernel_infra import service_attestation
from kernel_infra.adapters import fibserve


SNAPSHOT = {
    "broker_version": "0.5.3",
    "instance_id": "node-pid1-start1",
    "_kernelinfra_peer_pid": 123,
    "_kernelinfra_peer_uid": 1000,
    "_kernelinfra_peer_gid": 1000,
    "running": [
        {
            "job_id": "gpuq-service",
            "label": "fibserve-test",
            "owner": "judge",
            "mode": "exclusive",
            "gpu_ids": [1],
            "gpu_count": 1,
            "submitted_at": "2026-08-25T00:00:00+00:00",
            "started_at": "2026-08-25T00:00:01+00:00",
        }
    ],
}
ROOT = {
    "name": "FIBServe",
    "version": "0.1",
    "commit": "491860bd3e587eda230ab426b086f31b2da4aa12",
}
HEALTH = {"status": "ok", "queue_size": 0, "workers": [{"healthy": True}]}
PEER = {"pid": 123, "uid": 1000, "gid": 1000}
ADMISSION_CONTENT = {
    "schema": service_attestation.BROKER_ADMISSION_SCHEMA,
    "job_id": "gpuq-service",
    "broker_version": "0.5.3",
    "broker_instance_id": "node-pid1-start1",
    "submitted_at": "2026-08-25T00:00:00+00:00",
    "started_at": "2026-08-25T00:00:01+00:00",
    "owner": "judge",
    "label": "fibserve-test",
    "mode": "exclusive",
    "gpu_count": 1,
    "gpu_ids": [1],
    "cwd": "/srv/fibserve",
    "argv_count": 2,
    "argv_sha256": "a" * 64,
    "env_keys": ["SERVICE_PORT"],
    "env_sha256": "b" * 64,
    "launch_spec_sha256": "c" * 64,
    "resolved_executable": "/srv/fibserve/start.sh",
    "executable_sha256": "d" * 64,
    "effective_env_sha256": "e" * 64,
}
ADMISSION = {
    **ADMISSION_CONTENT,
    "receipt_sha256": service_attestation._digest_json(ADMISSION_CONTENT),
}
SERVICE_IDENTITY = (
    "PTXBench@491860bd3e587eda230ab426b086f31b2da4aa12"
    f"+admission@sha256:{ADMISSION['launch_spec_sha256']}"
    f"+executable@sha256:{ADMISSION['executable_sha256']}"
)


class ServiceAttestationTests(unittest.TestCase):
    def test_build_and_live_verify_exclusive_deployment(self):
        def request(url, timeout=10.0):
            del timeout
            return HEALTH if url.endswith("/health") else ROOT

        with mock.patch.object(
            service_attestation, "query_broker", return_value=SNAPSHOT
        ), mock.patch.object(
            service_attestation,
            "query_broker_admission",
            return_value=(ADMISSION, PEER),
        ), mock.patch.object(
            service_attestation,
            "load_broker_admission_receipt",
            return_value=ADMISSION,
        ), mock.patch.object(
            service_attestation,
            "_git_source_identity",
            return_value={
                "source_root": "/srv/fibserve",
                "source_commit": "491860bd3e587eda230ab426b086f31b2da4aa12",
                "source_tree": "tree-1",
                "source_dirty": False,
            },
        ), mock.patch.object(service_attestation, "request_json", side_effect=request):
            receipt = service_attestation.build_service_receipt(
                broker_socket=Path("/tmp/gpuq.sock"),
                broker_job_id="gpuq-service",
                broker_admission_receipt=Path("/tmp/admission.json"),
                service_url="http://127.0.0.1:10000",
                service_identity=SERVICE_IDENTITY,
                source_root=Path("/srv/fibserve"),
            )
            verified = service_attestation.verify_service_receipt(receipt)
        self.assertEqual(receipt["broker_gpu_ids"], [1])
        self.assertEqual(verified["job"]["mode"], "exclusive")

    def test_free_floating_or_shared_service_is_rejected(self):
        absent = {**SNAPSHOT, "running": []}
        with mock.patch.object(
            service_attestation, "query_broker", return_value=absent
        ):
            with self.assertRaisesRegex(RuntimeError, "not uniquely running"):
                service_attestation.build_service_receipt(
                    broker_socket=Path("/tmp/gpuq.sock"),
                    broker_job_id="gpuq-service",
                    broker_admission_receipt=Path("/tmp/admission.json"),
                    service_url="http://127.0.0.1:10000",
                    service_identity="PTXBench@commit",
                    source_root=Path("/srv/fibserve"),
                )
        shared = {
            **SNAPSHOT,
            "running": [{**SNAPSHOT["running"][0], "mode": "shared"}],
        }
        with mock.patch.object(
            service_attestation, "query_broker", return_value=shared
        ):
            with self.assertRaisesRegex(RuntimeError, "exclusive"):
                service_attestation.build_service_receipt(
                    broker_socket=Path("/tmp/gpuq.sock"),
                    broker_job_id="gpuq-service",
                    broker_admission_receipt=Path("/tmp/admission.json"),
                    service_url="http://127.0.0.1:10000",
                    service_identity="PTXBench@commit",
                    source_root=Path("/srv/fibserve"),
                )

    def test_receipt_round_trip_and_fibserve_result_custody(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.json"
            receipt = {
                "schema": service_attestation.SCHEMA,
                "attested_at": "2026-08-25T00:00:00+00:00",
                "service_url": "http://127.0.0.1:10000",
                "service_identity": "PTXBench@commit",
                "service_root": {"commit": "commit"},
                "service_health": HEALTH,
                "broker_admission_receipt": ADMISSION,
                "source_root": "/srv/fibserve",
                "source_commit": "commit",
                "source_tree": "tree",
                "source_dirty": False,
                "broker_socket": "/tmp/gpuq.sock",
                "broker_version": "0.5.3",
                "broker_instance_id": "instance",
                "broker_peer_pid": 123,
                "broker_peer_uid": 1000,
                "broker_peer_gid": 1000,
                "broker_job_id": "gpuq-service",
                "broker_job_submitted_at": "2026-08-25T00:00:00+00:00",
                "broker_job_started_at": "2026-08-25T00:00:01+00:00",
                "broker_label": "fibserve",
                "broker_owner": "judge",
                "broker_mode": "exclusive",
                "broker_gpu_ids": [1],
            }
            service_attestation.atomic_json(path, receipt)
            loaded = service_attestation.load_service_receipt(path)
            response = {
                "status": "completed",
                "traces": [
                    {
                        "workload": {"uuid": "w0"},
                        "evaluation": {
                            "status": "PASSED",
                            "performance": {
                                "latency_ms": 1.0,
                                "reference_latency_ms": 2.0,
                            },
                        },
                    }
                ],
            }
            result = fibserve._translate(
                response,
                [{"path": "kernel.cu", "content": "source"}],
                "fib-task",
                loaded,
            )
        self.assertEqual(result["validity"], "valid")
        self.assertEqual(result["artifacts"]["broker_job_id"], "gpuq-service")
        self.assertIn("deployment_receipt_sha256", result["fingerprints"])
        self.assertEqual(
            result["fingerprints"]["broker_launch_spec_sha256"],
            ADMISSION["launch_spec_sha256"],
        )
        self.assertEqual(
            result["fingerprints"]["broker_executable_sha256"],
            ADMISSION["executable_sha256"],
        )

    def test_only_loopback_service_urls_are_allowed(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            service_attestation._normalize_loopback_url("http://example.com:10000")

    def test_fibserve_task_identity_binds_exact_deployment_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = {
                "service_identity": "FIBServe@commit+admission@sha256:a"
            }
            marker = "deployment-receipt@sha256:" + fibserve._json_sha256(
                deployment
            )
            task = root / "task.json"
            task.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "id": "service",
                                "judge": {
                                    "identity": deployment["service_identity"]
                                    + "+"
                                    + marker
                                },
                            }
                        ]
                    }
                )
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "KERNELINFRA_TASK": str(task),
                    "KERNELINFRA_STAGE_ID": "service",
                },
                clear=False,
            ):
                fibserve._require_task_identity(deployment)
                value = json.loads(task.read_text())
                value["stages"][0]["judge"]["identity"] = deployment[
                    "service_identity"
                ]
                task.write_text(json.dumps(value))
                with self.assertRaisesRegex(RuntimeError, "deployment receipt"):
                    fibserve._require_task_identity(deployment)

    def test_saved_admission_must_match_live_broker_and_service_identity(self):
        source = {
            "source_root": "/srv/fibserve",
            "source_commit": "491860bd3e587eda230ab426b086f31b2da4aa12",
            "source_tree": "tree-1",
            "source_dirty": False,
        }
        changed = {**ADMISSION, "label": "tampered"}
        with mock.patch.object(
            service_attestation, "query_broker", return_value=SNAPSHOT
        ), mock.patch.object(
            service_attestation,
            "query_broker_admission",
            return_value=(ADMISSION, PEER),
        ), mock.patch.object(
            service_attestation,
            "load_broker_admission_receipt",
            return_value=changed,
        ):
            with self.assertRaisesRegex(RuntimeError, "differs from live broker"):
                service_attestation.build_service_receipt(
                    broker_socket=Path("/tmp/gpuq.sock"),
                    broker_job_id="gpuq-service",
                    broker_admission_receipt=Path("/tmp/admission.json"),
                    service_url="http://127.0.0.1:10000",
                    service_identity=SERVICE_IDENTITY,
                    source_root=Path("/srv/fibserve"),
                )

        with mock.patch.object(
            service_attestation, "query_broker", return_value=SNAPSHOT
        ), mock.patch.object(
            service_attestation,
            "query_broker_admission",
            return_value=(ADMISSION, PEER),
        ), mock.patch.object(
            service_attestation,
            "load_broker_admission_receipt",
            return_value=ADMISSION,
        ), mock.patch.object(
            service_attestation, "_git_source_identity", return_value=source
        ):
            with self.assertRaisesRegex(RuntimeError, "does not bind"):
                service_attestation.build_service_receipt(
                    broker_socket=Path("/tmp/gpuq.sock"),
                    broker_job_id="gpuq-service",
                    broker_admission_receipt=Path("/tmp/admission.json"),
                    service_url="http://127.0.0.1:10000",
                    service_identity=(
                        "PTXBench@491860bd3e587eda230ab426b086f31b2da4aa12"
                    ),
                    source_root=Path("/srv/fibserve"),
                )

    def test_source_checkout_must_stay_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "service.py").write_text("value = 1\n")
            subprocess.run(["git", "-C", str(root), "add", "service.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Kernel Infra",
                    "-c",
                    "user.email=kernel-infra@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            identity = service_attestation._git_source_identity(root)
            self.assertFalse(identity["source_dirty"])
            (root / "service.py").write_text("value = 2\n")
            with self.assertRaisesRegex(RuntimeError, "source checkout is dirty"):
                service_attestation._git_source_identity(root)

    def test_broker_peer_change_is_rejected(self):
        source = {
            "source_root": "/srv/fibserve",
            "source_commit": "491860bd3e587eda230ab426b086f31b2da4aa12",
            "source_tree": "tree-1",
            "source_dirty": False,
        }

        def request(url, timeout=10.0):
            del timeout
            return HEALTH if url.endswith("/health") else ROOT

        changed = {**SNAPSHOT, "_kernelinfra_peer_pid": 456}
        with mock.patch.object(
            service_attestation, "query_broker", side_effect=[SNAPSHOT, changed]
        ), mock.patch.object(
            service_attestation,
            "query_broker_admission",
            return_value=(ADMISSION, PEER),
        ), mock.patch.object(
            service_attestation,
            "load_broker_admission_receipt",
            return_value=ADMISSION,
        ), mock.patch.object(
            service_attestation, "_git_source_identity", return_value=source
        ), mock.patch.object(service_attestation, "request_json", side_effect=request):
            receipt = service_attestation.build_service_receipt(
                broker_socket=Path("/tmp/gpuq.sock"),
                broker_job_id="gpuq-service",
                broker_admission_receipt=Path("/tmp/admission.json"),
                service_url="http://127.0.0.1:10000",
                service_identity=SERVICE_IDENTITY,
                source_root=Path("/srv/fibserve"),
            )
            with self.assertRaisesRegex(RuntimeError, "broker peer changed"):
                service_attestation.verify_service_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
