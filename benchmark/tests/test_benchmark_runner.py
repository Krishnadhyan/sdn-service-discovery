import csv
import json
import multiprocessing as mp
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark import benchmark_runner as runner


def _bounded_worker(client_num, requests, run_id, service, policy, tracker, port, timeout, result_queue):
    current, peak, lock = tracker
    with lock:
        current.value += 1
        peak.value = max(peak.value, current.value)
    import time
    time.sleep(0.05)
    for num in range(1, requests + 1):
        item = runner._base_record(run_id, f"client-{client_num:06d}", num, service, policy)
        item["success"] = True
        item["selected_provider_id"] = "FILE-1"
        result_queue.put(item)
    with lock:
        current.value -= 1


def _crash_worker(*args):
    raise RuntimeError("intentional test worker failure")


class BenchmarkTests(unittest.TestCase):
    def test_response_classification(self):
        self.assertEqual(runner.parse_response(b"DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001", "FILE")[0]["id"], "FILE-1")
        self.assertIsNone(runner.parse_response(b"ERROR|SERVICE_UNAVAILABLE|FILE", "FILE"))
        with self.assertRaises(ValueError):
            runner.parse_response(b"wat", "FILE")

    def test_timeout_and_socket_error_classification(self):
        with patch.object(runner.socket, "socket", side_effect=socket.timeout()):
            self.assertEqual(runner._discover_one("r", "c", 1, "FILE", "FIRST_AVAILABLE", "h", 5000, 0.1)["failure_reason"], "registry_timeout")
        with patch.object(runner.socket, "socket", side_effect=OSError("offline")):
            self.assertTrue(runner._discover_one("r", "c", 1, "FILE", "FIRST_AVAILABLE", "h", 5000, 0.1)["failure_reason"].startswith("socket_error"))

    def test_worker_process_failure_accounting_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.run_benchmark(clients=1, requests=2, max_concurrency=1, output_dir=tmp,
                                          worker_target=_crash_worker, progress=False)
            self.assertEqual(result["aggregates"]["completed_requests"], 2)
            self.assertTrue(all("worker_process_error" in x["failure_reason"] for x in result["records"]))
            with open(result["json_path"], encoding="utf8") as stream:
                payload = json.load(stream)
            with open(result["csv_path"], newline="", encoding="utf8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(payload["request_results"]), len(rows))
            self.assertEqual([r["request_id"] for r in payload["request_results"]], [r["request_id"] for r in rows])

    def test_unique_ids_and_bounded_process_concurrency(self):
        ctx = mp.get_context("spawn")
        current, peak, lock = ctx.Value("i", 0), ctx.Value("i", 0), ctx.Lock()
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.run_benchmark(clients=10, requests=2, max_concurrency=3, output_dir=tmp,
                                          registry_host=(current, peak, lock), worker_target=_bounded_worker, progress=False)
        self.assertEqual(result["aggregates"]["total_requests"], 20)
        self.assertEqual(len({r["client_id"] for r in result["records"]}), 10)
        self.assertEqual(len({r["request_id"] for r in result["records"]}), 20)
        self.assertLessEqual(peak.value, 3)
        self.assertEqual(len({r["request_id"] for r in result["records"]}), len(result["records"]))

    def test_graceful_cancellation_writes_pending_request_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("multiprocessing.context.SpawnProcess.start", side_effect=KeyboardInterrupt):
                result = runner.run_benchmark(clients=2, requests=3, max_concurrency=1,
                                              output_dir=tmp, progress=False)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["aggregates"]["completed_requests"], 6)
        self.assertTrue(all("cancelled" in row["failure_reason"] for row in result["records"]))

    def test_service_and_policy_validation(self):
        with self.assertRaises(ValueError):
            runner.run_benchmark(service="INVALID", progress=False)
        with self.assertRaises(ValueError):
            runner.run_benchmark(policy="INVALID", progress=False)


if __name__ == "__main__":
    unittest.main()
