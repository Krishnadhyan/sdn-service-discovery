import json
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from frontend import workloads


class FakeProcess:
    active = 0
    maximum = 0
    guard = threading.Lock()
    commands = []

    def __init__(self, args, stdout=None, stderr=None, text=None, bufsize=None):
        self.args = args
        type(self).commands.append(args)
        self.pid = 4321
        self.code = 0
        self.terminated = threading.Event()
        with self.guard:
            type(self).active += 1
            type(self).maximum = max(type(self).maximum, type(self).active)
        host = args[args.index("--client-host") + 1]
        count = int(args[args.index("--requests") + 1])
        execution_mode = args[args.index("--execution-mode") + 1]
        service = args[args.index(str(workloads.CLIENT_SCRIPT)) + 1]
        self.lines = [json.dumps({"request_id": f"{host}-request-{i:06d}", "client_hostname": host,
                                  "client_ip": workloads.CLIENT_IPS[host], "service": service,
                                  "execution_mode": execution_mode,
                                  "request_started_at": "2026-01-01T00:00:00+00:00",
                                  "response_at": "2026-01-01T00:00:00.001+00:00", "latency_ms": 1.0,
                                  "discovery_latency_ms": 0.2 if execution_mode == "dynamic" else None,
                                  "discovery_success": execution_mode == "dynamic",
                                  "discovery_attempted": execution_mode == "dynamic",
                                  "end_to_end_latency_ms": 1.0,
                                  "selected_provider": "FILE-1" if service == "FILE" else "CHAT-1",
                                  "provider_candidates": ["FILE-1"], "success": True,
                                  "provider_attempts": [{"provider_id": "FILE-1", "status": "succeeded"}],
                                  "final_provider": {"id": "FILE-1"}, "provider_response": "FILE-1 OK",
                                  "outcome": "service_succeeded", "error": None}) + "\n" for i in range(1, count + 1)]
        self.stdout = iter(self.lines)
        self.stderr = type("Err", (), {"read": lambda _self: ""})()

    def poll(self): return self.code
    def wait(self, timeout=None):
        time.sleep(0.025)
        with self.guard:
            if type(self).active:
                type(self).active -= 1
        return self.code
    def terminate(self): self.terminated.set(); self.code = -15


class WorkloadManagerTests(unittest.TestCase):
    def setUp(self):
        FakeProcess.active = FakeProcess.maximum = 0
        FakeProcess.commands = []
        self.manager = workloads.WorkloadManager(
            pid_finder=lambda: {"h1": 1001, "h2": 1002, "h11": 1011}, popen=FakeProcess)

    def _wait(self):
        self.manager.active["thread"].join(timeout=3)
        self.assertFalse(self.manager.active["thread"].is_alive())

    def test_actual_namespace_commands_account_all_results_and_bound_processes(self):
        started = self.manager.start({"clients": ["h1", "h2", "h11"], "service": "FILE",
                                      "requests_per_client": 4, "concurrency_per_client": 2,
                                      "mode": "concurrent"})
        self.assertEqual(started["total_requests"], 12)
        self._wait()
        result = self.manager.snapshot()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], 12)
        self.assertEqual(result["successful"], 12)
        self.assertEqual(len({r["request_id"] for r in result["records"]}), 12)
        self.assertEqual({r["client_hostname"] for r in result["records"]}, {"h1", "h2", "h11"})
        self.assertLessEqual(FakeProcess.maximum, 3)
        self.assertTrue(all(r["selected_provider"] == "FILE-1" for r in result["records"]))
        self.assertEqual(result["success_rate_percent"], 100.0)
        self.assertIsNotNone(result["mean_discovery_latency_ms"])
        self.assertIsNotNone(result["p95_discovery_latency_ms"])
        self.assertEqual(result["discovery_success_rate_percent"], 100.0)
        self.assertGreater(result["successful_discoveries_per_second"], 0)
        self.assertEqual(result["mean_end_to_end_latency_ms"], 1.0)

    def test_static_file_workload_uses_fixed_endpoint_and_success_metrics(self):
        self.manager.start({"clients": ["h1"], "service": "FILE", "execution_mode": "static",
                            "requests_per_client": 2, "concurrency_per_client": 1})
        self._wait()
        args = FakeProcess.commands[0]
        result = self.manager.snapshot()
        self.assertEqual(args[args.index("--execution-mode") + 1], "static")
        self.assertEqual(args[args.index("--static-provider-ip") + 1], "10.0.0.4")
        self.assertEqual(args[args.index("--static-provider-port") + 1], "5001")
        self.assertEqual(result["success_rate_percent"], 100.0)
        self.assertGreater(result["successful_requests_per_second"], 0)
        self.assertIsNone(result["mean_discovery_latency_ms"])
        self.assertIsNone(result["p95_discovery_latency_ms"])
        self.assertIsNone(result["discovery_success_rate_percent"])
        self.assertIsNone(result["successful_discoveries_per_second"])
        self.assertEqual(result["mean_end_to_end_latency_ms"], 1.0)

    def test_static_mode_rejects_other_service_types(self):
        with self.assertRaisesRegex(ValueError, "only for FILE"):
            self.manager.start({"clients": ["h1"], "service": "VIDEO", "execution_mode": "static",
                                "requests_per_client": 1})

    def test_success_throughput_and_rate_exclude_failed_service_requests(self):
        metrics = workloads.WorkloadManager._metrics({"completed": 2, "elapsed_seconds": 4,
            "execution_mode": "dynamic",
            "records": [{"success": True, "execution_mode": "dynamic", "discovery_attempted": True, "discovery_success": True, "discovery_latency_ms": 2.0, "end_to_end_latency_ms": 8.0},
                        {"success": False, "execution_mode": "dynamic", "discovery_attempted": True, "discovery_success": True, "discovery_latency_ms": 1.0, "end_to_end_latency_ms": 20.0}]})
        self.assertEqual(metrics["success_rate_percent"], 50.0)
        self.assertEqual(metrics["successful_requests_per_second"], 0.25)
        self.assertEqual(metrics["mean_discovery_latency_ms"], 1.5)
        self.assertEqual(metrics["p95_discovery_latency_ms"], 2.0)
        self.assertEqual(metrics["discovery_success_rate_percent"], 100.0)
        self.assertEqual(metrics["successful_discoveries_per_second"], 0.5)
        self.assertEqual(metrics["mean_end_to_end_latency_ms"], 14.0)

    def test_discovery_metrics_exclude_invalid_registry_responses_and_keep_service_failure_separate(self):
        metrics = workloads.WorkloadManager._metrics({"completed": 2, "elapsed_seconds": 2,
            "execution_mode": "dynamic", "records": [
                {"success": False, "discovery_attempted": True, "discovery_success": True, "execution_mode": "dynamic",
                 "discovery_latency_ms": 4.0, "end_to_end_latency_ms": 50.0},
                {"success": False, "discovery_attempted": True, "discovery_success": False, "execution_mode": "dynamic",
                 "discovery_latency_ms": 30.0, "end_to_end_latency_ms": 30.0}]})
        self.assertEqual(metrics["discovery_success_rate_percent"], 50.0)
        self.assertEqual(metrics["mean_discovery_latency_ms"], 4.0)
        self.assertEqual(metrics["p95_discovery_latency_ms"], 4.0)
        self.assertEqual(metrics["successful_discoveries_per_second"], 0.5)
        self.assertEqual(metrics["success_rate_percent"], 0.0)

    def test_completed_raw_workload_is_saved_as_json_and_csv(self):
        with tempfile.TemporaryDirectory() as output_dir:
            manager = workloads.WorkloadManager(pid_finder=lambda: {"h1": 1001},
                                                 popen=FakeProcess, results_dir=output_dir)
            manager.start({"clients": ["h1"], "service": "FILE", "requests_per_client": 1,
                           "concurrency_per_client": 1, "execution_mode": "dynamic"})
            manager.active["thread"].join(timeout=3)
            result = manager.snapshot()
            json_path = Path(result["result_file"])
            csv_path = json_path.with_suffix(".csv")
            self.assertTrue(json_path.is_file())
            self.assertTrue(csv_path.is_file())
            saved = json.loads(json_path.read_text())
            self.assertEqual(saved["configuration"]["execution_mode"], "dynamic")
            self.assertEqual(saved["configuration"]["registry_destination"], {"ip": "10.0.0.3", "port": 5000})
            self.assertEqual(saved["run"]["records"][0]["outcome"], "service_succeeded")

    def test_sequential_mode_starts_only_one_host_at_a_time(self):
        self.manager.start({"clients": ["h1", "h2"], "service": "CHAT",
                            "requests_per_client": 2, "concurrency_per_client": 7,
                            "mode": "sequential"})
        self._wait()
        result = self.manager.snapshot()
        self.assertEqual(result["concurrency_per_client"], 1)
        self.assertEqual(FakeProcess.maximum, 1)
        self.assertTrue(all(r["service"] == "CHAT" and r["selected_provider"] == "CHAT-1"
                            for r in result["records"]))

    def test_worker_command_uses_mnexec_and_existing_client_script(self):
        self.manager.start({"clients": ["h1"], "service": "VIDEO",
                            "requests_per_client": 1, "concurrency_per_client": 1,
                            "mode": "concurrent"})
        self._wait()
        args = FakeProcess.commands[0]
        self.assertEqual(args[:3], [workloads.MNEXEC, "-a", "1001"])
        self.assertIn(str(workloads.CLIENT_SCRIPT), args)
        self.assertIn("--service-workload", args)
        self.assertEqual(args[args.index("--execution-mode") + 1], "dynamic")
        self.assertEqual(args[args.index("--client-host") + 1], "h1")

    def test_invalid_or_missing_mininet_clients_are_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.start({"clients": ["h3"], "service": "FILE", "requests_per_client": 1})
        manager = workloads.WorkloadManager(pid_finder=lambda: {}, popen=FakeProcess)
        with self.assertRaisesRegex(RuntimeError, "namespaces not found"):
            manager.start({"clients": ["h1"], "service": "FILE", "requests_per_client": 1})

    def test_one_shot_rejects_invalid_client_and_executes_valid_host_with_mnexec(self):
        with self.assertRaises(ValueError):
            self.manager.run_one_shot({"client": "h3", "service": "FILE"})
        payload = {"client": "h1", "service": "FILE", "operation": "service"}
        expected = {"client": "h1", "service": "FILE", "operation_status": "service_succeeded",
                    "discovery_success": True, "client_ip": "10.0.0.1", "providers": [],
                    "provider_attempts": [{"provider_id": "FILE-2", "status": "succeeded"}],
                    "final_provider": {"id": "FILE-2"}, "provider_response": "FILE-2 OK"}
        class OneShotProcess:
            returncode = 0
            communicate_timeout = None
            def __init__(self, args, **kwargs):
                self.args = args
                self.stdout = self.stderr = None
                self.code = 0
                FakeProcess.commands.append(args)
            def communicate(self, timeout=None):
                type(self).communicate_timeout = timeout
                return json.dumps(expected), ""
            def poll(self): return self.code
            def terminate(self): self.code = -15
            def kill(self): self.code = -9
            def wait(self, timeout=None): return self.code
        self.manager.popen = OneShotProcess
        result = self.manager.run_one_shot(payload)
        args = FakeProcess.commands[-1]
        self.assertEqual(args[:3], [workloads.MNEXEC, "-a", "1001"])
        self.assertEqual(args[args.index("--one-shot-operation") + 1], "service")
        self.assertEqual(args[args.index("--registry-host") + 1], "10.0.0.3")
        self.assertEqual(args[args.index("--registry-port") + 1], "5000")
        self.assertEqual(result["operation_status"], "service_succeeded")
        self.assertEqual(result["final_provider"]["id"], "FILE-2")
        self.assertEqual(OneShotProcess.communicate_timeout,
                         (1 + workloads.MAX_PROVIDERS_PER_SERVICE) * 2 + 2)

    def test_one_shot_execution_timeout_terminates_only_child(self):
        class TimeoutProcess:
            def __init__(self, *args, **kwargs): self.code = None; self.terminated = False; self.killed = False; self.calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls <= 2: raise workloads.subprocess.TimeoutExpired("mnexec", timeout)
                return "", ""
            def poll(self): return self.code
            def terminate(self): self.terminated = True; self.code = -15
            def kill(self): self.killed = True; self.code = -9
        process = TimeoutProcess()
        self.manager.popen = lambda *args, **kwargs: process
        result = self.manager.run_one_shot({"client": "h1", "service": "FILE", "timeout": 0.1})
        self.assertEqual(result["operation_status"], "client_process_timeout")
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_stop_terminates_managed_worker_and_accounts_cancelled_requests(self):
        class BlockingProcess:
            def __init__(self, *args, **kwargs):
                self.pid = 9876
                self.ended = threading.Event()
                self.stdout = self
                self.stderr = type("Err", (), {"read": lambda _self: ""})()
            def __iter__(self): return self
            def __next__(self):
                if self.ended.wait(2): raise StopIteration
                raise StopIteration
            def poll(self): return -15 if self.ended.is_set() else None
            def terminate(self): self.ended.set()
            def wait(self, timeout=None): return -15
        manager = workloads.WorkloadManager(pid_finder=lambda: {"h1": 1001}, popen=BlockingProcess)
        manager.start({"clients": ["h1"], "service": "FILE", "requests_per_client": 3,
                       "concurrency_per_client": 1, "mode": "concurrent"})
        deadline = time.time() + 1
        while not manager.processes and time.time() < deadline:
            time.sleep(.005)
        manager.stop()
        manager.active["thread"].join(timeout=2)
        result = manager.snapshot()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["pending"], 0)
        self.assertTrue(all(row["outcome"] == "cancelled" for row in result["records"]))

    def test_worker_start_crash_is_an_explicit_failure_for_every_request(self):
        def crash(*args, **kwargs):
            raise OSError("mnexec unavailable")
        manager = workloads.WorkloadManager(pid_finder=lambda: {"h1": 1001}, popen=crash)
        manager.start({"clients": ["h1"], "service": "FILE", "requests_per_client": 2,
                       "concurrency_per_client": 1, "mode": "concurrent"})
        manager.active["thread"].join(timeout=2)
        result = manager.snapshot()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed"], 2)
        self.assertTrue(all(row["outcome"] == "worker_process_error" for row in result["records"]))
        self.assertTrue(all("mnexec unavailable" in row["error"] for row in result["records"]))


if __name__ == "__main__":
    unittest.main()
