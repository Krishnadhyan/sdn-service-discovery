import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REGISTRY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REGISTRY_DIR))
spec = importlib.util.spec_from_file_location("registry_main_under_test", REGISTRY_DIR / "registry.py")
registry_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_main)


class Store:
    def select_for(self, service, policy):
        return [type("Provider", (), {"provider_id": "FILE-1", "protocol": "TCP", "ip": "10.0.0.4", "port": 5001})()]


class FakeSocket:
    def sendto(self, data, addr): self.last = (data, addr)


class DiscoveryAccountingTests(unittest.TestCase):
    def test_only_dedicated_mininet_client_source_is_counted_as_client_workload(self):
        instance = object.__new__(registry_main.Registry)
        instance.store = Store()
        instance.sock = FakeSocket()
        rows = []
        with patch.object(registry_main, "emit_event", side_effect=lambda kind, **fields: rows.append((kind, fields))):
            instance.handle(b"DISCOVER|FILE", ("10.0.0.1", 3333))
            instance.handle(b"DISCOVER|FILE", ("10.0.0.254", 3334))
        discovery = [fields for kind, fields in rows if kind == "discovery"]
        self.assertEqual([row["genuine_client_request"] for row in discovery], [True, False])
        self.assertEqual(instance.sock.last[0].split(b"|")[0], b"DISCOVER_RESPONSE")


if __name__ == "__main__":
    unittest.main()
