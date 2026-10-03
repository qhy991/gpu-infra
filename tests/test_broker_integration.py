"""Exercise the pinned broker protocol with CPU work and no device access."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from kernel_infra.broker import query_broker
from kernel_infra.diagnostics import build_diagnosis
from kernel_infra.service_attestation import (
    _digest_json, query_broker_admission, validate_broker_admission_receipt,
)
from kernel_infra.services import validate_managed_broker
from kernel_infra.store import utc_now

BROKER_SRC = Path(__file__).resolve().parents[1] / "agent-gpu-broker" / "src"


@unittest.skipUnless(BROKER_SRC.is_dir(), "initialize the pinned broker submodule")
class BrokerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_scoped_receipt_and_freshness_cross_real_socket(self):
        sys.path.insert(0, str(BROKER_SRC))
        try:
            from agent_gpu_broker.broker import GpuBroker, JobSpec
            from agent_gpu_broker.server import BrokerServer
        finally:
            sys.path.remove(str(BROKER_SRC))
        class Inventory:
            async def gpu_ids(self): return [0, 1]
            async def compute_pids(self): return {0: [], 1: []}
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            broker = GpuBroker(state_dir=root / "state", lock_dir=root / "locks",
                               inventory=Inventory(), poll_interval_s=.01,
                               terminate_grace_s=.05)
            path = root / "b.sock"
            server = BrokerServer(broker, path)
            await server.start()
            try:
                job = broker.submit(JobSpec(
                    argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                    cwd=str(root), owner="test", label="scoped-integration",
                    mode="exclusive", gpu_count=1, estimate_s=None,
                    run_timeout_s=60, queue_timeout_s=5, allowed_gpu_ids=(1,),
                ))
                while (await asyncio.wait_for(job.events.get(), 5))["type"] != "started":
                    pass
                receipt, _ = await asyncio.to_thread(query_broker_admission, path, job.job_id)
                self.assertEqual(receipt["allowed_gpu_ids"], [1])
                self.assertEqual(receipt["gpu_ids"], [1])
                snapshot = await asyncio.to_thread(query_broker, path)
                validate_managed_broker(snapshot)
                result = build_diagnosis(observed_at=utc_now(), attention_after_s=300,
                    run_states=[], run_requests={}, request_errors={}, services=[],
                    broker=snapshot, broker_error=None)
                self.assertEqual(result["verdict"], "ok")
                self.assertEqual(result["broker"]["running"][0]["allowed_gpu_ids"], [1])
                for scope in ([0], [1, 1], [], [True], "1"):
                    changed = {**receipt, "allowed_gpu_ids": scope}
                    changed.pop("receipt_sha256")
                    changed["receipt_sha256"] = _digest_json(changed)
                    with self.subTest(scope=scope), self.assertRaisesRegex(RuntimeError, "GPU scope"):
                        validate_broker_admission_receipt(changed)
            finally:
                await server.close()
            self.assertEqual(broker.snapshot()["running"], [])
            self.assertEqual(broker.snapshot()["queue"], [])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
