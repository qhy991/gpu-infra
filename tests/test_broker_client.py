import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from kernel_infra.broker import cancel_broker_job


class BrokerClientTests(unittest.TestCase):
    def exchange(self, response, action):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "b.sock"
            requests = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen()
                listener.settimeout(10)
                def serve():
                    connection, _ = listener.accept()
                    with connection, connection.makefile("rwb") as stream:
                        requests.append(json.loads(stream.readline()))
                        stream.write((json.dumps(response) + "\n").encode())
                        stream.flush()
                thread = threading.Thread(target=serve)
                thread.start()
                try:
                    result = action(path)
                finally:
                    thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                return result, requests

    def test_cancel_requires_typed_acknowledgment(self):
        for ok in (True, False):
            result, requests = self.exchange(
                {"type": "cancelled", "ok": ok},
                lambda path: cancel_broker_job(path, "job"),
            )
            self.assertIs(result, ok)
            self.assertEqual(requests, [{"op": "cancel", "job_id": "job"}])
        for response in ({"ok": True}, {"type": "cancelled", "ok": "false"}, []):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.exchange(response, lambda path: cancel_broker_job(path, "job"))

    def test_direct_diagnosis_runs_cli_without_daemon_or_mutation(self):
        snapshot = {
            "version": 2, "broker_version": "0.7.0", "instance_id": "fixture",
            "gpu_observed_at": "2026-10-03T07:00:00+00:00",
            "gpu_observation_age_seconds": 0.1,
            "probe_error": None, "gpus": [{"gpu_id": 0, "state": "exclusive"}],
            "running": [], "queue": [{"job_id": "job", "wait_seconds": 600}],
            "recent": [],
        }
        result, requests = self.exchange(
            {"snapshot": snapshot},
            lambda path: subprocess.run(
                [sys.executable, "-m", "kernel_infra", "diagnose",
                 "--broker-socket", str(path), "--json"],
                capture_output=True, text=True, timeout=15, env=os.environ.copy(),
            ),
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["scope"], "broker")
        self.assertEqual(value["runs"], [])
        self.assertEqual(value["verdict"], "attention")
        self.assertEqual(requests, [{"op": "status"}])


if __name__ == "__main__":
    unittest.main()
