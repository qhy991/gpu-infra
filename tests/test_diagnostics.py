import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kernel_infra import cli
from kernel_infra.diagnostics import build_diagnosis


OBSERVED_AT = "2026-08-27T08:00:00+00:00"


def run_state(**changes):
    value = {
        "run_id": "run-1",
        "task_id": "task",
        "state": "running",
        "stage_id": "benchmark",
        "stage_kind": "benchmark",
        "stage_index": 0,
        "broker_job_id": "job-1",
        "gpu_ids": [0],
        "accepted_at": "2026-08-27T07:00:00+00:00",
        "updated_at": "2026-08-27T07:00:00+00:00",
    }
    value.update(changes)
    return value


def run_request(*, execution="broker", queue_timeout=900, run_timeout=120):
    resources = None
    if execution == "broker":
        resources = {
            "mode": "exclusive",
            "gpu_count": 1,
            "estimate_s": None,
            "queue_timeout_s": queue_timeout,
            "run_timeout_s": run_timeout,
        }
    return {
        "run_id": "run-1",
        "stages": [
            {
                "id": "benchmark",
                "kind": "benchmark",
                "execution": execution,
                "resources": resources,
            }
        ],
    }


def broker_snapshot(*, running=None, queue=None, probe_error=None):
    return {
        "version": 2,
        "broker_version": "0.6.0",
        "instance_id": "broker",
        "updated_at": OBSERVED_AT,
        "probe_error": probe_error,
        "shared_capacity": 2,
        "gpus": [{"gpu_id": 0, "state": "exclusive"}],
        "running": running or [],
        "queue": queue or [],
        "recent": [],
    }


def diagnose(state, *, request=None, broker=None, broker_error=None):
    return build_diagnosis(
        observed_at=OBSERVED_AT,
        attention_after_s=300,
        run_states=[state],
        run_requests={"run-1": request or run_request()},
        request_errors={},
        broker=broker,
        broker_error=broker_error,
        services=[],
    )


class DiagnosisTests(unittest.TestCase):
    def test_missing_broker_list_is_unknown(self):
        for field in ("gpus", "running", "queue"):
            snapshot = broker_snapshot()
            del snapshot[field]
            with self.subTest(field=field):
                self.assertEqual(diagnose(run_state(), broker=snapshot)["verdict"], "unknown")

    def test_malformed_live_timing_is_unknown(self):
        for duration in (float("nan"), float("inf"), -1, True, None):
            with self.subTest(duration=duration):
                value = diagnose(run_state(), broker=broker_snapshot(running=[{
                    "job_id": "job-1", "gpu_ids": [0], "run_seconds": duration,
                }]))
                self.assertEqual(value["verdict"], "unknown")

    def test_invalid_attention_threshold_is_rejected(self):
        for value in ("nan", "inf", "-1"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                cli._parser().parse_args(["diagnose", "--attention-after", value])

    def test_direct_failure_remains_machine_readable_unknown(self):
        args = cli._parser().parse_args([
            "diagnose", "--broker-socket", "/absent.sock", "--json",
        ])
        output = StringIO()
        with mock.patch("kernel_infra.cli.query_broker", side_effect=RuntimeError("offline")), redirect_stdout(output):
            self.assertEqual(cli._diagnose(args), 1)
        import json
        self.assertEqual(json.loads(output.getvalue())["verdict"], "unknown")

    def test_live_broker_job_is_running_despite_old_run_update(self):
        value = diagnose(
            run_state(),
            broker=broker_snapshot(
                running=[
                    {
                        "job_id": "job-1",
                        "state": "running",
                        "gpu_ids": [0],
                        "run_seconds": 60,
                    }
                ]
            ),
        )
        self.assertEqual(value["verdict"], "ok")
        self.assertEqual(value["runs"][0]["diagnosis"], "running")

    def test_long_queue_wait_needs_attention_without_being_called_stalled(self):
        value = diagnose(
            run_state(state="queued", gpu_ids=[]),
            broker=broker_snapshot(
                queue=[
                    {
                        "job_id": "job-1",
                        "state": "queued",
                        "gpu_ids": [],
                        "wait_seconds": 600,
                        "position": 2,
                        "eta_seconds": None,
                    }
                ]
            ),
        )
        self.assertEqual(value["verdict"], "attention")
        self.assertEqual(value["runs"][0]["diagnosis"], "long_wait")

    def test_live_job_beyond_declared_timeout_is_suspected_stall(self):
        value = diagnose(
            run_state(),
            broker=broker_snapshot(
                running=[
                    {
                        "job_id": "job-1",
                        "state": "running",
                        "gpu_ids": [0],
                        "run_seconds": 130,
                    }
                ]
            ),
        )
        self.assertEqual(value["verdict"], "attention")
        self.assertEqual(value["runs"][0]["diagnosis"], "suspected_stall")

    def test_broker_failure_is_unknown_not_idle_or_stalled(self):
        value = diagnose(
            run_state(),
            broker=None,
            broker_error="RuntimeError: cannot query broker",
        )
        self.assertEqual(value["verdict"], "unknown")
        self.assertEqual(value["broker"]["observation"], "unknown")
        self.assertEqual(value["runs"][0]["diagnosis"], "unknown")

    def test_local_stage_age_does_not_prove_stall(self):
        value = diagnose(
            run_state(broker_job_id=None, gpu_ids=[]),
            request=run_request(execution="local"),
            broker=broker_snapshot(),
        )
        self.assertEqual(value["verdict"], "ok")
        self.assertEqual(value["runs"][0]["diagnosis"], "running")

    def test_cli_exit_three_makes_attention_scriptable(self):
        args = SimpleNamespace(
            socket=Path("/tmp/kernel.sock"),
            run_id="run-1",
            attention_after=300,
            json=True,
        )
        request = mock.Mock(
            return_value={
                "diagnosis": {
                    "schema": "kernelinfra.diagnosis.v1",
                    "verdict": "attention",
                }
            }
        )
        with mock.patch("kernel_infra.cli._request", request), redirect_stdout(
            StringIO()
        ):
            exit_code = cli._diagnose(args)
        self.assertEqual(exit_code, 3)
        request.assert_called_once_with(
            args.socket,
            {
                "op": "diagnose",
                "run_id": "run-1",
                "attention_after_s": 300,
            },
            timeout_s=cli.DIAGNOSE_REQUEST_TIMEOUT_S,
        )


if __name__ == "__main__":
    unittest.main()
