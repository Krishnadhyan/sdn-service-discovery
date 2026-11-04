import contextlib
import io
import json
import unittest
from unittest.mock import patch

from client import client


class FakeSocket:
    def __init__(self, *args): self.sent = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, value): self.timeout = value
    def sendto(self, payload, address): self.sent = (payload, address)
    def getsockname(self): return ("10.0.0.1", 34222)
    def recvfrom(self, size):
        return (b"DISCOVER_RESPONSE|FILE|FILE-2,TCP,10.0.0.5,5001|FILE-3,TCP,10.0.0.8,5001", ("10.0.0.3", 5000))


class ClientDiscoveryWorkloadTests(unittest.TestCase):
    def test_attempt_uses_existing_wire_format_and_actual_first_registry_candidate(self):
        fake = FakeSocket()
        with patch.object(client.socket, "socket", return_value=fake):
            row = client.discovery_attempt("10.0.0.3", 5000, "FILE", 1, "run-h1-000001", "h1")
        self.assertEqual(fake.sent, (b"DISCOVER|FILE", ("10.0.0.3", 5000)))
        self.assertEqual(row["client_ip"], "10.0.0.1")
        self.assertEqual(row["selected_provider"], "FILE-2")
        self.assertEqual(row["provider_candidates"], ["FILE-2", "FILE-3"])
        self.assertTrue(row["success"])
        self.assertIsNotNone(row["latency_ms"])

    def test_bounded_batch_records_unique_local_request_ids(self):
        def result(_host, _port, service, _timeout, request_id, client_host):
            return {"request_id": request_id, "client_hostname": client_host, "service": service,
                    "success": True, "selected_provider": "FILE-1"}
        output = io.StringIO()
        with patch.object(client, "discovery_attempt", side_effect=result), contextlib.redirect_stdout(output):
            client.run_discovery_batch("FILE", "10.0.0.3", 5000, 1, 7, 2, "test-run", "h2")
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 7)
        self.assertEqual(len({row["request_id"] for row in rows}), 7)
        self.assertTrue(all(row["client_hostname"] == "h2" for row in rows))

    def test_empty_provider_list_is_successful_discovery_without_service(self):
        fake = FakeSocket()
        fake.recvfrom = lambda size: (b"ERROR|SERVICE_UNAVAILABLE|CHAT", ("10.0.0.3", 5000))
        with patch.object(client.socket, "socket", return_value=fake):
            row = client.discovery_attempt("10.0.0.3", 5000, "CHAT", 1, "run-h2-000001", "h2")
        self.assertTrue(row["success"])
        self.assertEqual(row["outcome"], "no_available_provider")
        self.assertIsNone(row["selected_provider"])


if __name__ == "__main__":
    unittest.main()
