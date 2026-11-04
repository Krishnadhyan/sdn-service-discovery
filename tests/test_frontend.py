import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from email.message import Message
from unittest.mock import patch

APP_PATH = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
spec = importlib.util.spec_from_file_location("dashboard_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class FrontendBridgeTests(unittest.TestCase):
    def test_network_fault_ui_has_separate_link_controls_and_observed_state(self):
        self.assertIn('id="networkFault"', app.PAGE)
        self.assertIn("Network Fault Injection", app.PAGE)
        self.assertIn("Take Link Down", app.PAGE)
        self.assertIn("Restore Link", app.PAGE)
        self.assertIn("networkLinkBusy||networkLinkStatus==='DOWN'", app.PAGE)
        self.assertIn("networkLinkBusy||networkLinkStatus==='UP'", app.PAGE)
        self.assertIn("Taking Link Down…", app.PAGE)
        self.assertIn("Restoring Link…", app.PAGE)
        self.assertIn("Could not ${operation==='down'", app.PAGE)
        self.assertIn("refreshNetworkLink()", app.PAGE)

    def test_network_link_api_rejects_remote_and_arbitrary_operations(self):
        for remote, payload, expected in (
            ("192.0.2.5", {"operation": "down"}, 403),
            ("127.0.0.1", {"operation": "sh down"}, 400),
        ):
            body = json.dumps(payload).encode()
            handler = object.__new__(app.Handler)
            handler.path = "/api/network-link"
            handler.client_address = (remote, 1234)
            handler.headers = Message()
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = io.BytesIO(body)
            captured = {}
            handler.send_json = lambda value, status=200: captured.update(payload=value, status=status)
            with patch.object(app, "request_mininet_link") as operation:
                handler.do_POST()
            self.assertEqual(captured["status"], expected)
            operation.assert_not_called()

    def test_network_link_api_returns_topology_result(self):
        body = json.dumps({"operation": "down"}).encode()
        handler = object.__new__(app.Handler)
        handler.path = "/api/network-link"
        handler.client_address = ("127.0.0.1", 1234)
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        captured = {}
        handler.send_json = lambda value, status=200: captured.update(payload=value, status=status)
        result = {"ok": True, "status": "DOWN", "operation": "down"}
        with patch.object(app, "request_mininet_link", return_value=result) as operation:
            handler.do_POST()
        operation.assert_called_once_with("down")
        self.assertEqual(captured, {"payload": result, "status": 200})

    def test_performance_workload_ui_exposes_static_dynamic_presets(self):
        self.assertIn('id="workloadPreset"', app.PAGE)
        self.assertIn('value="A">A · 1 client × 20</option>', app.PAGE)
        self.assertIn('value="B">B · 3 clients × 20</option>', app.PAGE)
        self.assertIn('value="C">C · 5 clients × 20</option>', app.PAGE)
        self.assertIn('id="workloadExecutionMode"', app.PAGE)
        self.assertIn('value="static">Static · direct FILE-1', app.PAGE)

    def test_known_provider_inventory_matches_seven_instance_topology(self):
        expected = {
            "FILE-1": {"service": "FILE", "ip": "10.0.0.4", "protocol": "TCP", "port": 5001, "host": "h4"},
            "FILE-2": {"service": "FILE", "ip": "10.0.0.5", "protocol": "TCP", "port": 5001, "host": "h5"},
            "FILE-3": {"service": "FILE", "ip": "10.0.0.8", "protocol": "TCP", "port": 5001, "host": "h8"},
            "CHAT-1": {"service": "CHAT", "ip": "10.0.0.6", "protocol": "TCP", "port": 5002, "host": "h6"},
            "CHAT-2": {"service": "CHAT", "ip": "10.0.0.9", "protocol": "TCP", "port": 5002, "host": "h9"},
            "VIDEO-1": {"service": "VIDEO", "ip": "10.0.0.7", "protocol": "UDP", "port": 5003, "host": "h7"},
            "VIDEO-2": {"service": "VIDEO", "ip": "10.0.0.10", "protocol": "UDP", "port": 5003, "host": "h10"},
        }
        self.assertEqual(app.KNOWN, expected)

    def test_parse_real_discovery_response(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def sendto(self, data, target):
                self.data, self.target = data, target
            def recvfrom(self, size):
                return (b"DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001", ("10.0.0.3", 5000))
        with patch.object(app.socket, "socket", return_value=FakeSocket()), \
             patch.object(app.time, "perf_counter", side_effect=[10.0, 10.025]):
            result = app.discover("FILE")
        self.assertEqual(result["raw"].split("|")[0], "DISCOVER_RESPONSE")
        self.assertEqual([p["id"] for p in result["providers"]], ["FILE-1", "FILE-2"])
        self.assertEqual(result["providers"][0]["ip"], "10.0.0.4")
        self.assertAlmostEqual(result["discovery_latency_ms"], 25.0)

    def test_unavailable_response_is_empty_provider_list(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def sendto(self, data, target): pass
            def recvfrom(self, size):
                return (b"ERROR|SERVICE_UNAVAILABLE|CHAT", ("10.0.0.3", 5000))
        with patch.object(app.socket, "socket", return_value=FakeSocket()), \
             patch.object(app.time, "perf_counter", side_effect=[2.0, 2.012]):
            result = app.discover("CHAT")
        self.assertEqual(result["providers"], [])
        self.assertIn("SERVICE_UNAVAILABLE", result["raw"])
        self.assertAlmostEqual(result["discovery_latency_ms"], 12.0)

    def test_registry_timeout_has_no_latency_measurement(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def sendto(self, data, target): pass
            def recvfrom(self, size): raise TimeoutError("timed out")
        with patch.object(app.socket, "socket", return_value=FakeSocket()), \
             patch.object(app.time, "perf_counter", side_effect=[3.0]):
            with self.assertRaises(TimeoutError):
                app.discover("FILE")

    def test_registry_error_has_no_latency_measurement(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def sendto(self, data, target): raise OSError("network unreachable")
        with patch.object(app.socket, "socket", return_value=FakeSocket()), \
             patch.object(app.time, "perf_counter", side_effect=[3.0]):
            with self.assertRaisesRegex(OSError, "network unreachable"):
                app.discover("FILE")

    def test_status_is_unknown_if_registry_is_unreachable(self):
        with patch.object(app, "discover", side_effect=OSError("no route")):
            result = app.get_status()
        self.assertFalse(result["registry"]["ok"])
        self.assertTrue(all(p["status"] == "UNKNOWN" for p in result["providers"]))

    def test_repeated_dashboard_refreshes_do_not_send_discovery_or_change_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stats = {"session_id": "test-session", "counter_scope": "mininet_client_sources_v1", "started_at": app.datetime.now(app.timezone.utc).isoformat(),
                     "last_seen_at": app.datetime.now(app.timezone.utc).isoformat(),
                     "discovery_requests": 17, "selection_policy": "ROUND_ROBIN", "selection_counts": {},
                     "providers": {}}
            stats_path = root / "stats.json"
            stats_path.write_text(json.dumps(stats))
            with patch.object(app, "REGISTRY_STATS_FILE", stats_path), \
                 patch.object(app, "REGISTRY_EVENT_LOG", root / "registry.jsonl"), \
                 patch.object(app, "CONTROLLER_EVENT_LOG", root / "controller.jsonl"), \
                 patch.object(app, "DASHBOARD_EVENT_LOG", root / "dashboard.jsonl"), \
                 patch.object(app, "PROVIDER_EVENT_LOG_DIR", root / "providers"), \
                 patch.object(app, "discover", side_effect=AssertionError("status refresh sent DISCOVER")):
                for _ in range(10):
                    app.get_status()
                    app.get_telemetry()
            self.assertEqual(json.loads(stats_path.read_text())["discovery_requests"], 17)

    def test_registry_events_exclude_discovery_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "registry.log"
            controller_log = Path(tmp) / "controller.log"
            log.write_text("DISCOVER FILE from 127.0.0.1 -> 2 provider(s)\n"
                           "REGISTER FILE-1 FILE TCP 10.0.0.4:5001\n"
                           "FILE-1 timed out -> INACTIVE\n"
                           "Controller event sent: FILE-1 INACTIVE -> HTTP 200\n"
                           "Controller notification FAILED for FILE-2 ACTIVE: refused\n")
            controller_log.write_text("Installed drop rule for FILE-1 on switch 2\nheartbeat polling\n")
            with patch.object(app, "REGISTRY_LOG", log), patch.object(app, "CONTROLLER_LOG", controller_log):
                result = app.get_events()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["lines"]), 4)
        self.assertFalse(any("DISCOVER" in line for line in result["lines"]))
        self.assertEqual(result["controller_lines"], ["Installed drop rule for FILE-1 on switch 2"])

    def _post_transaction(self, discovery_result, communications):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(app, "ACTIVITY_LOG", Path(temp.name) / "activity.jsonl"), \
             patch.object(app, "discover", return_value=discovery_result), \
             patch.object(app, "communicate", side_effect=communications):
            body = json.dumps({"service": "FILE", "request": "GET file.txt"}).encode()
            handler = object.__new__(app.Handler)
            handler.path = "/api/communicate"
            handler.headers = Message()
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = io.BytesIO(body)
            captured = {}
            handler.send_json = lambda payload, status=200: captured.update(status=status, payload=payload)
            handler.do_POST()
            status, payload = captured["status"], captured["payload"]
            events = app.get_activities()["events"]
        return status, payload, events

    def test_manual_transaction_success_is_recorded(self):
        result = {"raw": "DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001", "discovery_latency_ms": 4.5,
                  "providers": [{"id": "FILE-1", "service": "FILE", "protocol": "TCP", "ip": "10.0.0.4", "port": 5001}]}
        status, payload, events = self._post_transaction(result, lambda *args: "FILE-1 OK")
        self.assertEqual(status, 200)
        self.assertEqual(payload["response"], "FILE-1 OK")
        self.assertEqual(payload["discovery_latency_ms"], 4.5)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["service"], "FILE")
        self.assertEqual(events[0]["request"], "GET file.txt")
        self.assertEqual(events[0]["provider"], "FILE-1")
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["response"], "FILE-1 OK")
        self.assertTrue(events[0]["timestamp"])

    def test_client_operation_endpoint_delegates_without_bridge_network_io(self):
        payload = {"client": "h1", "service": "FILE", "operation": "discover"}
        body = json.dumps(payload).encode()
        handler = object.__new__(app.Handler)
        handler.path = "/api/client-operation"
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        captured = {}
        handler.send_json = lambda result, status=200: captured.update(result=result, status=status)
        expected = {"client": "h1", "raw_registry_response": "DISCOVER_RESPONSE|FILE|"}
        with patch.object(app.workload_manager, "run_one_shot", return_value=expected) as run, \
             patch.object(app.socket, "socket", side_effect=AssertionError("bridge opened a discovery socket")), \
             patch.object(app, "communicate", side_effect=AssertionError("bridge contacted a provider")):
            handler.do_POST()
        run.assert_called_once_with(payload)
        self.assertEqual(captured, {"result": expected, "status": 200})

    def test_manual_transaction_failure_is_recorded_with_error(self):
        result = {"raw": "DISCOVER_RESPONSE|FILE|FILE-2,TCP,10.0.0.5,5001", "discovery_latency_ms": 7.25,
                  "providers": [{"id": "FILE-2", "service": "FILE", "protocol": "TCP", "ip": "10.0.0.5", "port": 5001}]}
        status, payload, events = self._post_transaction(result, OSError("connection refused"))
        self.assertEqual(status, 502)
        self.assertIn("connection refused", payload["details"][0])
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["success"])
        self.assertEqual(events[0]["provider"], "FILE-2")
        self.assertIn("connection refused", events[0]["error"])

    def test_empty_manual_discovery_returns_registry_latency(self):
        result = {"raw": "ERROR|SERVICE_UNAVAILABLE|FILE", "providers": [], "discovery_latency_ms": 9.0}
        status, payload, events = self._post_transaction(result, lambda *args: self.fail("no provider should be contacted"))
        self.assertEqual(status, 409)
        self.assertEqual(payload["discovery_latency_ms"], 9.0)
        self.assertEqual(payload["discovery"]["providers"], [])
        self.assertFalse(events[0]["success"])

    def _post_lifecycle(self, payload, remote="127.0.0.1"):
        body = json.dumps(payload).encode()
        handler = object.__new__(app.Handler)
        handler.path = "/api/provider-lifecycle"
        handler.client_address = (remote, 12345)
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        captured = {}
        handler.send_json = lambda value, status=200: captured.update(status=status, payload=value)
        handler.post_provider_lifecycle()
        return captured

    def test_lifecycle_rejects_unknown_provider_and_operation(self):
        for request in ({"provider": "FILE-999", "operation": "offline"},
                        {"provider": "FILE-1", "operation": "run shell"}):
            with patch.object(app, "request_provider_lifecycle") as control:
                result = self._post_lifecycle(request)
            self.assertEqual(result["status"], 400)
            control.assert_not_called()

    def test_lifecycle_request_is_acknowledged_but_waits_for_registry(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(app, "ACTIVITY_LOG", Path(temp.name) / "activity.jsonl"), \
             patch.object(app, "_lifecycle_requests", {}), \
             patch.object(app, "registry_provider_status", return_value="ACTIVE"), \
             patch.object(app, "request_provider_lifecycle", return_value={"acknowledged": True}) as control:
            result = self._post_lifecycle({"provider": "FILE-1", "operation": "offline"})
            self.assertEqual(result["status"], 202)
            self.assertIn("waiting for Registry timeout", result["payload"]["message"])
            repeated = self._post_lifecycle({"provider": "FILE-1", "operation": "offline"})
            self.assertEqual(repeated["status"], 409)
            # An explicit opposite request is allowed to cancel a pending pause.
            resumed = self._post_lifecycle({"provider": "FILE-1", "operation": "online"})
            self.assertEqual(resumed["status"], 202)
            control.assert_has_calls([
                unittest.mock.call("FILE-1", "offline"),
                unittest.mock.call("FILE-1", "online"),
            ])
            activity = app.get_activities()["events"]
            self.assertEqual([event["operation"] for event in activity], ["offline", "online"])

    def test_lifecycle_rejects_remote_dashboard_client(self):
        with patch.object(app, "request_provider_lifecycle") as control:
            result = self._post_lifecycle({"provider": "FILE-1", "operation": "offline"}, "192.0.2.10")
        self.assertEqual(result["status"], 403)
        control.assert_not_called()

    def test_lifecycle_status_is_registry_derived_with_pending_transition(self):
        stats = {"session_id": "test", "counter_scope": "mininet_client_sources_v1", "last_seen_at": app.datetime.now(app.timezone.utc).isoformat(),
                 "selection_policy": "FIRST_AVAILABLE", "providers": {
                     "FILE-1": {"status": "ACTIVE"}, "FILE-2": {"status": "ACTIVE"},
                     "CHAT-1": {"status": "ACTIVE"}, "VIDEO-1": {"status": "ACTIVE"}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            path.write_text(json.dumps(stats))
            with patch.object(app, "REGISTRY_STATS_FILE", path), \
             patch.object(app, "_lifecycle_requests", {"FILE-1": "offline", "FILE-2": "online"}), \
             patch.object(app, "get_flows", return_value={"ok": True, "output": ""}):
                status = app.get_status()
        states = {p["id"]: p["lifecycle_status"] for p in status["providers"]}
        self.assertEqual(states["FILE-1"], "DETECTING FAILURE")
        self.assertEqual(states["FILE-2"], "ONLINE")
        self.assertEqual(states["CHAT-1"], "ONLINE")

    def test_recovery_waits_until_provider_drop_is_absent(self):
        drop = "priority=100,ip,nw_dst=10.0.0.4,nw_proto=6,tp_dst=5001 actions=drop"
        stats = {"session_id": "test", "counter_scope": "mininet_client_sources_v1", "last_seen_at": app.datetime.now(app.timezone.utc).isoformat(),
                 "selection_policy": "FIRST_AVAILABLE", "providers": {"FILE-1": {"status": "ACTIVE"}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            path.write_text(json.dumps(stats))
            with patch.object(app, "REGISTRY_STATS_FILE", path), \
             patch.object(app, "_lifecycle_requests", {"FILE-1": "online"}), \
             patch.object(app, "get_flows", return_value={"ok": True, "output": drop}):
                status = app.get_status()
        self.assertEqual(next(p for p in status["providers"] if p["id"] == "FILE-1")["lifecycle_status"], "RECOVERING")

    def test_provider_drop_match_uses_known_endpoint_and_protocol(self):
        provider = app.KNOWN["FILE-1"]
        drop = "priority=100,ip,nw_dst=10.0.0.4,nw_proto=6,tp_dst=5001 actions=drop"
        self.assertTrue(app.provider_drop_present(drop, provider))
        self.assertFalse(app.provider_drop_present(drop.replace("5001", "5002"), provider))

    def test_lifecycle_helper_rejects_arbitrary_provider_or_command(self):
        with patch.object(app.socket, "socket") as make_socket:
            for provider, operation in (("/bin/sh", "offline"), ("FILE-1", "exec rm -rf /")):
                with self.assertRaises(ValueError):
                    app.request_provider_lifecycle(provider, operation)
            make_socket.assert_not_called()

    def test_lifecycle_helper_targets_fixed_provider_and_uses_shared_secret(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def sendto(self, data, target): self.data, self.target = data, target
            def recvfrom(self, size): return b"LIFECYCLE_OK|FILE-2|PAUSED", ("10.0.0.5", app.LIFECYCLE_PORT)
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("a" * 40)
            fake = FakeSocket()
            with patch.object(app, "LIFECYCLE_TOKEN_FILE", token_file), \
                 patch.object(app.socket, "socket", return_value=fake):
                result = app.request_provider_lifecycle("FILE-2", "offline")
        self.assertEqual(result["acknowledged"], True)
        self.assertEqual(fake.target, ("10.0.0.5", app.LIFECYCLE_PORT))
        self.assertEqual(fake.data, f"PAUSE_HEARTBEATS|{'a' * 40}|FILE-2".encode())

    def test_missing_provider_listener_returns_actionable_timeout(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): self.timeout = timeout
            def sendto(self, data, target): self.target = target
            def recvfrom(self, size): raise app.socket.timeout("timed out")
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("a" * 40)
            with patch.object(app, "LIFECYCLE_TOKEN_FILE", token_file), \
                 patch.object(app.socket, "socket", return_value=FakeSocket()):
                with self.assertRaisesRegex(RuntimeError, "Restart that provider"):
                    app.request_provider_lifecycle("FILE-1", "offline", timeout=0.01)

    def test_events_refresh_sources_are_both_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "registry.log"
            activity = Path(tmp) / "activity.jsonl"
            log.write_text("REGISTER FILE-1 FILE TCP 10.0.0.4:5001\n")
            activity.write_text(json.dumps({"timestamp": "2026-01-01T00:00:00+00:00", "service": "FILE",
                                            "request": "PING", "provider": "FILE-1", "success": True,
                                            "response": "OK", "error": None}) + "\n")
            with patch.object(app, "REGISTRY_LOG", log), patch.object(app, "ACTIVITY_LOG", activity):
                self.assertEqual(len(app.get_events()["lines"]), 1)
                self.assertEqual(len(app.get_activities()["events"]), 1)
                self.assertIn("/api/events", app.PAGE)
                self.assertIn("/api/activities", app.PAGE)

    def test_telemetry_aggregates_real_event_records_and_current_session_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            r_session, c_session = "registry-run", "controller-run"
            r_events = [
                {"id": "r0", "session_id": r_session, "source": "registry",
                 "kind": "registry_started", "timestamp": "2026-01-01T00:00:00.000Z",
                 "selection_policy": "ROUND_ROBIN"},
                {"id": "r1", "session_id": r_session, "source": "registry",
                 "kind": "heartbeat", "timestamp": "2026-01-01T00:00:02.000Z",
                 "provider_id": "FILE-1"},
                {"id": "r2", "session_id": r_session, "source": "registry",
                 "kind": "provider_status", "timestamp": "2026-01-01T00:00:40.000Z",
                 "provider_id": "FILE-1", "status": "INACTIVE"},
                {"id": "old", "session_id": "old-run", "source": "registry",
                 "kind": "discovery", "timestamp": "2025-01-01T00:00:00.000Z",
                 "selected_provider": "FILE-3"},
            ]
            c_events = [
                {"id": "c0", "session_id": c_session, "source": "controller",
                 "kind": "controller_started", "timestamp": "2026-01-01T00:00:00.000Z"},
                {"id": "c1", "session_id": c_session, "source": "controller",
                 "kind": "provider_drop_installed", "timestamp": "2026-01-01T00:00:41.000Z",
                 "provider_id": "FILE-1", "priority": 100},
            ]
            stats = {
                "session_id": r_session, "counter_scope": "mininet_client_sources_v1", "selection_policy": "ROUND_ROBIN",
                "heartbeat_timeout_seconds": 30, "heartbeat_interval_seconds": 2,
                "discovery_requests": 12, "selection_counts": {"FILE-1": 7, "FILE-2": 5},
                "providers": {"FILE-1": {"last_heartbeat": "2026-01-01T00:00:02.000Z",
                                           "heartbeat_count": 1, "status": "INACTIVE"}},
            }
            (tmp / "registry.jsonl").write_text("".join(json.dumps(e) + "\n" for e in r_events))
            (tmp / "controller.jsonl").write_text("".join(json.dumps(e) + "\n" for e in c_events))
            (tmp / "stats.json").write_text(json.dumps(stats))
            provider_dir = tmp / "providers"
            provider_dir.mkdir()
            provider_rows = [
                {"id": "p0", "session_id": 77, "source": "provider", "kind": "provider_started",
                 "timestamp": "2026-01-01T00:00:00.500Z", "provider_id": "FILE-1"},
                {"id": "p1", "session_id": 77, "source": "provider", "kind": "service_traffic_served",
                 "timestamp": "2026-01-01T00:00:42.000Z", "provider_id": "FILE-1",
                 "client_ip": "10.0.0.1", "protocol": "TCP"},
            ]
            (provider_dir / "FILE-1.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in provider_rows))
            with patch.object(app, "REGISTRY_EVENT_LOG", tmp / "registry.jsonl"), \
                 patch.object(app, "CONTROLLER_EVENT_LOG", tmp / "controller.jsonl"), \
                 patch.object(app, "REGISTRY_STATS_FILE", tmp / "stats.json"), \
                 patch.object(app, "PROVIDER_EVENT_LOG_DIR", provider_dir):
                result = app.get_telemetry()
        self.assertTrue(result["availability"]["registry_events"])
        self.assertTrue(result["availability"]["controller_events"])
        self.assertEqual(result["registry"]["selection_policy"], "ROUND_ROBIN")
        self.assertEqual(result["registry"]["discovery_requests"], 12)
        self.assertEqual(result["registry"]["selection_counts"]["FILE-1"], 7)
        self.assertEqual(result["providers"]["FILE-1"]["last_heartbeat"], "2026-01-01T00:00:02.000Z")
        service_rows = result["providers"]["FILE-1"]["service_events"]
        served = next(e for e in service_rows if e["kind"] == "service_traffic_served")
        self.assertEqual(served["client_ip"], "10.0.0.1")
        self.assertEqual(result["providers"]["FILE-1"]["service_events_available"], True)
        self.assertEqual([e["id"] for e in result["events"]], ["r0", "c0", "p0", "r1", "r2", "c1", "p1"])

    def test_missing_telemetry_is_unavailable_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "REGISTRY_EVENT_LOG", Path(tmp) / "missing-registry.jsonl"), \
                 patch.object(app, "CONTROLLER_EVENT_LOG", Path(tmp) / "missing-controller.jsonl"), \
                 patch.object(app, "REGISTRY_STATS_FILE", Path(tmp) / "missing-stats.json"):
                result = app.get_telemetry()
        self.assertFalse(result["availability"]["registry_events"])
        self.assertFalse(result["availability"]["controller_events"])
        self.assertIsNone(result["registry"])
        self.assertIsNone(result["providers"])

    def test_legacy_unscoped_counter_is_unavailable_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stats = root / "stats.json"
            stats.write_text(json.dumps({"session_id": "legacy", "discovery_requests": 826,
                                         "selection_counts": {"FILE-1": 826}}))
            with patch.object(app, "REGISTRY_STATS_FILE", stats), \
                 patch.object(app, "REGISTRY_EVENT_LOG", root / "events.jsonl"), \
                 patch.object(app, "CONTROLLER_EVENT_LOG", root / "controller.jsonl"), \
                 patch.object(app, "DASHBOARD_EVENT_LOG", root / "dashboard.jsonl"), \
                 patch.object(app, "PROVIDER_EVENT_LOG_DIR", root / "providers"):
                result = app.get_telemetry()
                status = app.get_status()
        self.assertFalse(result["availability"]["registry_stats"])
        self.assertFalse(status["registry"]["ok"])
        self.assertEqual(status["providers"][0]["status"], "UNKNOWN")

    def test_controller_status_includes_both_switch_flows_and_provider_drop_state(self):
        drop = "priority=100,ip,nw_dst=10.0.0.4,nw_proto=6,tp_dst=5001 actions=drop"
        with patch.object(app, "get_switch_status", return_value={
                "ok": True, "switches": {"s1": {"connected": True}, "s2": {"connected": True}}}), \
             patch.object(app, "_observe_provider_rule_changes"), \
             patch.object(app, "get_switch_flows", side_effect=lambda switch: {
                 "ok": True, "switch": switch, "output": drop if switch == "s2" else "priority=1 actions=NORMAL"}):
            result = app.get_controller_status()
        self.assertTrue(result["switches"]["switches"]["s1"]["connected"])
        self.assertTrue(result["switches"]["switches"]["s2"]["connected"])
        self.assertTrue(result["provider_rules"]["FILE-1"]["drop_present"])
        self.assertEqual(result["provider_rules"]["FILE-1"]["priority"], 100)
        self.assertFalse(result["provider_rules"]["FILE-2"]["drop_present"])

    def test_ovs_show_parser_reads_real_controller_connection_fields(self):
        output = '''Bridge s1
    Controller "tcp:127.0.0.1:6653"
        is_connected: true
Bridge s2
    Controller "tcp:127.0.0.1:6653"
        is_connected: false
'''
        class Result:
            returncode = 0
            stdout = output
            stderr = ""
        with patch.object(app.subprocess, "run", return_value=Result()):
            result = app.get_switch_status()
        self.assertTrue(result["switches"]["s1"]["connected"])
        self.assertFalse(result["switches"]["s2"]["connected"])
        self.assertEqual(result["switches"]["s1"]["target"], "tcp:127.0.0.1:6653")

    def test_ovs_rule_history_records_only_observed_state_changes(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "DASHBOARD_EVENT_LOG", Path(tmp) / "observed.jsonl"), \
             patch.object(app, "_last_provider_drop_state", {}):
            absent = {"FILE-1": {"available": True, "drop_present": False}}
            present = {"FILE-1": {"available": True, "drop_present": True}}
            app._observe_provider_rule_changes(absent)  # initial state, not an install event
            app._observe_provider_rule_changes(absent)
            self.assertEqual((Path(tmp) / "observed.jsonl").read_text(), "")
            app._observe_provider_rule_changes(present)
            event = json.loads((Path(tmp) / "observed.jsonl").read_text().strip())
        self.assertEqual(event["kind"], "ovs_provider_drop_present_observed")
        self.assertEqual(event["provider_id"], "FILE-1")
        self.assertEqual(event["priority"], 100)
        self.assertEqual(event["source"], "ovs_observer")

    def test_topology_diagram_uses_project_host_inventory_and_event_gated_animation(self):
        for host in ("h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8", "h9", "h10", "h11", "h12", "h13"):
            self.assertIn(host, app.PAGE)
        self.assertIn("10.0.0.10", app.PAGE)
        self.assertIn("Heartbeat packets observed", app.PAGE)
        self.assertIn("animateNewEvents", app.PAGE)
        self.assertIn("heartbeat-packet", app.PAGE)

    def test_manual_operation_ui_uses_client_operation_for_both_modes(self):
        for host in ("h1", "h2", "h11", "h12", "h13"):
            self.assertIn(f'<option value="{host}">', app.PAGE)
        self.assertIn('id="discoverOnly"', app.PAGE)
        self.assertIn("Discover Only", app.PAGE)
        self.assertIn("Discover &amp; Send", app.PAGE)
        manual = app.PAGE.split("async function runManualOperation(operation)", 1)[1].split("async function loadFlows", 1)[0]
        self.assertIn("client:$('manualClient').value", manual)
        self.assertIn("api('/api/client-operation'", manual)
        self.assertNotIn("/api/communicate", manual)
        self.assertNotIn("/api/discover", manual)
        self.assertIn("runManualOperation('discover')", app.PAGE)
        self.assertIn("runManualOperation('service')", app.PAGE)

    def test_manual_operation_displays_backend_results_and_failure_states(self):
        renderer = app.PAGE.split("function renderManualResult(result,operation)", 1)[1].split("async function runManualOperation", 1)[0]
        for field in ("client", "client_ip", "registry_destination", "discovery_success",
                      "discovery_latency_ms", "operation_latency_ms", "providers",
                      "provider_attempts", "final_provider", "provider_response",
                      "raw_registry_response", "provider_error", "operation_status"):
            self.assertIn(field, renderer)
        self.assertIn("Providers returned by Registry (in order)", renderer)
        self.assertIn("Provider attempts", renderer)
        self.assertIn("response timeout", renderer)
        self.assertIn("Final provider", renderer)

    def test_manual_topology_highlights_the_actual_successful_fallback_provider(self):
        highlighter = app.PAGE.split("async function animateManualResponse(result,operation,host)", 1)[1].split("function renderManualResult", 1)[0]
        self.assertIn("result.final_provider", highlighter)
        self.assertIn("result.provider_connection_success===true", highlighter)
        self.assertIn('data-provider-link="${pid}"', highlighter)
        self.assertIn('data-provider-link="FILE-1"', app.PAGE)
        self.assertIn("data-registry-ip=\"10.0.0.3\"", app.PAGE)

    def test_manual_animation_orders_request_registry_and_response_before_result_display(self):
        operation = app.PAGE.split("async function runManualOperation(operation)", 1)[1].split("async function loadFlows", 1)[0]
        self.assertLess(operation.index("animateManualRequest(payload.client)"),
                        operation.index("api('/api/client-operation'"))
        self.assertLess(operation.index("await animateManualResponse(result,operation,payload.client)"),
                        operation.index("renderManualResult(result,operation)"))
        response = app.PAGE.split("async function animateManualResponse(result,operation,host)", 1)[1].split("function renderManualResult", 1)[0]
        self.assertIn("result.raw_registry_response==null", response)
        self.assertIn("result.final_provider", response)

    def test_manual_discovery_failures_clear_manual_topology_highlights(self):
        operation = app.PAGE.split("async function runManualOperation(operation)", 1)[1].split("async function loadFlows", 1)[0]
        self.assertIn("clearManualHighlight()", operation)
        self.assertIn("renderManualResult(result,operation)", operation)
        self.assertIn("Operation request failed:", operation)
        self.assertIn("finally", operation)
        self.assertIn("/api/workloads", app.PAGE)
        self.assertIn("workloadStart", app.PAGE)


if __name__ == "__main__":
    unittest.main()
