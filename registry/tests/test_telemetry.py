import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registry_telemetry as telemetry


class RegistryTelemetryTests(unittest.TestCase):
    def test_session_stats_count_discovery_and_keep_latest_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(telemetry, "EVENT_LOG", Path(tmp) / "events.jsonl"), \
             patch.object(telemetry, "STATS_FILE", Path(tmp) / "stats.json"):
            telemetry.emit("registry_started", host="0.0.0.0", port=5000,
                           heartbeat_timeout_seconds=30, heartbeat_interval_seconds=2,
                           selection_policy="ROUND_ROBIN")
            telemetry.emit("heartbeat", provider_id="FILE-1", service="FILE",
                           status="ACTIVE", recovered=False)
            telemetry.emit("discovery", service="FILE", client_ip="10.0.0.1",
                           policy="ROUND_ROBIN", selected_provider="FILE-2",
                           provider_ids=["FILE-2", "FILE-3"], success=True,
                           genuine_client_request=True)
            telemetry.emit("discovery", service="FILE", client_ip="10.0.0.2",
                           policy="ROUND_ROBIN", selected_provider="FILE-3",
                           provider_ids=["FILE-3", "FILE-2"], success=True,
                           genuine_client_request=True)
            telemetry.emit("discovery", service="FILE", client_ip="10.0.0.254",
                           policy="ROUND_ROBIN", selected_provider="FILE-2",
                           provider_ids=["FILE-2"], success=True,
                           genuine_client_request=False)
            before_refresh = json.loads((Path(tmp) / "stats.json").read_text())["discovery_requests"]
            for _ in range(10):
                telemetry.touch_liveness()
            stats = json.loads((Path(tmp) / "stats.json").read_text())
            rows = [json.loads(line) for line in (Path(tmp) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(stats["selection_policy"], "ROUND_ROBIN")
        self.assertEqual(stats["discovery_requests"], 2)
        self.assertEqual(stats["selection_counts"], {"FILE-2": 1, "FILE-3": 1})
        self.assertEqual(stats["providers"]["FILE-1"]["heartbeat_count"], 1)
        self.assertEqual(stats["providers"]["FILE-1"]["last_heartbeat"], rows[1]["timestamp"])
        self.assertTrue(all(row["session_id"] == telemetry.SESSION_ID for row in rows))
        self.assertEqual(before_refresh, stats["discovery_requests"])
        self.assertEqual(stats["providers"]["FILE-1"]["heartbeat_count"], 1)


if __name__ == "__main__":
    unittest.main()
