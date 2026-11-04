import csv
import json
import tempfile
import unittest
from pathlib import Path

from benchmark import analyze_results as analyzer


def make_record(run_id, client, request, policy, success=True, latency=None, provider="FILE-1"):
    return {"run_id": run_id, "client_id": f"client-{client:06d}",
            "request_id": f"client-{client:06d}-request-{request:06d}",
            "requested_service": "FILE", "selection_policy": policy,
            "request_start_timestamp": "2026-01-01T00:00:00+00:00",
            "response_timestamp": "2026-01-01T00:00:00.001+00:00" if latency is not None else None,
            "discovery_latency_ms": latency, "selected_provider_id": provider if success else None,
            "success": success, "failure_reason": None if success else "registry_timeout", "worker_pid": 1000}


def fixture_run(run_id="run-a", policy="FIRST_AVAILABLE", clients=1, duration=2.0, records=None, status="completed"):
    if records is None:
        records = [make_record(run_id, 1, 1, policy, True, 1.0, "FILE-1"),
                   make_record(run_id, 1, 2, policy, True, 2.0, "FILE-2"),
                   make_record(run_id, 1, 3, policy, True, 3.0, "FILE-1"),
                   make_record(run_id, 1, 4, policy, False, None)]
    return {"records": records, "configuration": {"clients": clients, "requests_per_client": 4,
            "max_concurrency": 1, "service": "FILE", "policy": policy, "registry_host": "10.0.0.3",
            "registry_port": 5000, "provider_ids": ["FILE-1", "FILE-2"]},
            "metadata": {"elapsed_seconds": duration}, "status": status,
            "aggregate_counts": {"total_requests": 4}, "source": run_id + ".json", "format": "json"}


class AnalysisTests(unittest.TestCase):
    def test_known_metrics_and_failed_latency_exclusion(self):
        result = analyzer.analyze_run(fixture_run())
        self.assertEqual(result["total_logical_requests"], 4)
        self.assertEqual(result["completed_requests"], 4)
        self.assertEqual(result["successful_discoveries"], 3)
        self.assertEqual(result["failed_discoveries"], 1)
        self.assertEqual(result["success_rate_percent"], 75.0)
        self.assertEqual(result["latency_ms"]["average_ms"], 2.0)
        self.assertEqual(result["latency_ms"]["median_ms"], 2.0)
        self.assertEqual(result["latency_ms"]["p95_ms"], 3.0)
        self.assertEqual(result["latency_ms"]["minimum_ms"], 1.0)
        self.assertEqual(result["latency_ms"]["maximum_ms"], 3.0)
        self.assertEqual(result["throughput_requests_per_second"], 2.0)
        self.assertEqual(result["provider_distribution"]["FILE-1"]["requests"], 2)
        self.assertEqual(result["provider_distribution"]["FILE-1"]["selection_percentage"], 200 / 3)

    def test_missing_latency_and_interrupted_run(self):
        records = [make_record("run-a", 1, 1, "FIRST_AVAILABLE", True, None)]
        run = fixture_run(records=records, status="cancelled")
        result = analyzer.analyze_run(run)
        self.assertIsNone(result["latency_ms"]["average_ms"])
        self.assertEqual(result["pending_or_incomplete_requests"], 3)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual(result["throughput_requests_per_second"], 0.5)

    def test_csv_json_parsing_and_consistency(self):
        run = fixture_run()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, json_path = Path(tmp) / "x.csv", Path(tmp) / "x.json"
            fields = list(run["records"][0])
            with csv_path.open("w", newline="", encoding="utf8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(run["records"])
            payload = {"configuration": run["configuration"], "run_metadata": run["metadata"],
                       "execution_status": run["status"], "aggregate_counts": run["aggregate_counts"],
                       "request_results": run["records"]}
            json_path.write_text(json.dumps(payload), encoding="utf8")
            from_csv, from_json = analyzer.load_run(csv_path), analyzer.load_run(json_path)
            self.assertEqual(analyzer.consistency_check(from_csv, from_json)["record_count"], 4)
            self.assertIsNone(analyzer.analyze_run(from_csv)["throughput_requests_per_second"])
            self.assertEqual(analyzer.analyze_run(from_json)["throughput_requests_per_second"], 2)

    def test_reject_malformed_and_duplicate_records(self):
        with self.assertRaises(analyzer.AnalysisError):
            analyzer._normalize_records([{"run_id": "x"}], "test")
        duplicate = make_record("r", 1, 1, "FIRST_AVAILABLE", True, 1)
        with self.assertRaisesRegex(analyzer.AnalysisError, "duplicate"):
            analyzer._normalize_records([duplicate, duplicate], "test")

    def test_policy_compatibility_validation(self):
        first = fixture_run("first", "FIRST_AVAILABLE")
        rr = fixture_run("rr", "ROUND_ROBIN")
        self.assertTrue(analyzer.compare_runs([first, rr])[0]["compatible"])
        rr["configuration"]["max_concurrency"] = 2
        comparison = analyzer.compare_runs([first, rr])[0]
        self.assertFalse(comparison["compatible"])
        self.assertEqual(comparison["mismatches"][0]["field"], "max_concurrency")
        rr["configuration"]["max_concurrency"] = 1
        del rr["configuration"]["registry_port"]
        self.assertFalse(analyzer.compare_runs([first, rr])[0]["compatible"])

    def test_graph_generation_outputs_png_from_fixture_only(self):
        run = fixture_run()
        analysis = analyzer.analyze_run(run)
        rr = analyzer.analyze_run(fixture_run("rr", "ROUND_ROBIN"))
        comparisons = analyzer.compare_runs([run, fixture_run("rr", "ROUND_ROBIN")])
        with tempfile.TemporaryDirectory() as tmp:
            outputs = analyzer.generate_graphs([analysis, rr], tmp, comparisons)
            self.assertEqual(len(outputs), 6)
            for filename in outputs:
                self.assertEqual(Path(filename).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            policy_graph = Path(tmp) / "policy_latency_comparison.png"
            self.assertTrue(policy_graph.is_file())
        # Fixtures are deterministic test data and are not live network measurements.

    def test_paired_files_export_one_run_with_traceable_metadata(self):
        run = fixture_run()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, json_path = Path(tmp) / "same.csv", Path(tmp) / "same.json"
            fields = list(run["records"][0])
            with csv_path.open("w", newline="", encoding="utf8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(run["records"])
            payload = {"configuration": run["configuration"], "run_metadata": run["metadata"],
                       "execution_status": run["status"], "aggregate_counts": run["aggregate_counts"],
                       "request_results": run["records"]}
            json_path.write_text(json.dumps(payload), encoding="utf8")
            result = analyzer.analyze_files([str(csv_path), str(json_path)], str(Path(tmp) / "out"))
            self.assertEqual(len(result["report"]["runs"]), 1)
            self.assertEqual(len(result["report"]["source_files"]), 2)
            self.assertTrue(Path(result["output_dir"], "summary.csv").is_file())
            self.assertTrue(Path(result["output_dir"], "analysis.json").is_file())
            self.assertEqual(len(result["graphs"]), 6)


if __name__ == "__main__":
    unittest.main()
