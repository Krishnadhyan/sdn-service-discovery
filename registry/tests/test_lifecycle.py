import sys
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from provider_store import ProviderStore
from registry import Registry
import registry as registry_module
import controller_client
from controller_client import ControllerNotifier


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


class Notifier:
    def __init__(self):
        self.events = []
    def notify(self, record, status):
        self.events.append((record.provider_id, status))


class FakeSocket:
    def __init__(self):
        self.sent = []
    def sendto(self, data, address):
        self.sent.append((data, address))


class RegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = ProviderStore(timeout=6, clock=self.clock)
        self.store.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        self.registry = Registry.__new__(Registry)
        self.registry.store = self.store
        self.registry.notifier = Notifier()
        self.registry.sock = FakeSocket()
        self.registry._transition_lock = threading.Lock()
        # Mock Registry activity must not enter the dashboard's real telemetry.
        for module, name in ((registry_module, "emit_event"), (controller_client, "emit_event")):
            patcher = patch.object(module, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_first_registration_notifies_active_and_duplicate_does_not(self):
        self.registry.handle(b"REGISTER|FILE-NEW|FILE|TCP|5002", ("10.0.0.8", 4321))
        self.assertEqual(self.registry.notifier.events, [("FILE-NEW", "ACTIVE")])
        self.registry.handle(b"REGISTER|FILE-NEW|FILE|TCP|5002", ("10.0.0.8", 4321))
        self.assertEqual(self.registry.notifier.events, [("FILE-NEW", "ACTIVE")])

    def test_registry_telemetry_records_heartbeat_transitions_and_selection(self):
        with patch.object(registry_module, "emit_event") as emit:
            self.registry.handle(b"HEARTBEAT|FILE-1", ("10.0.0.4", 1234))
            self.clock.value = 7
            self.registry.check_timeouts()
            self.registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 9999))
            self.registry.handle(b"HEARTBEAT|FILE-1", ("10.0.0.4", 1234))
        events = [call.args[0] for call in emit.call_args_list]
        self.assertIn("heartbeat", events)
        self.assertIn("provider_status", events)
        self.assertIn("discovery", events)
        discovery = next(call for call in emit.call_args_list if call.args[0] == "discovery")
        self.assertEqual(discovery.kwargs["client_ip"], "10.0.0.1")
        self.assertEqual(discovery.kwargs["selected_provider"], None)
        recovery = [call for call in emit.call_args_list
                    if call.args[0] == "provider_status" and call.kwargs["status"] == "ACTIVE"]
        self.assertEqual(recovery[-1].kwargs["reason"], "heartbeat_recovery")
    def test_timeout_notifies_controller_and_discovery_excludes_provider(self):
        self.clock.value = 7
        expired = self.registry.check_timeouts()
        self.assertEqual([record.provider_id for record in expired], ["FILE-1"])
        self.assertEqual(self.registry.notifier.events, [("FILE-1", "INACTIVE")])
        self.assertEqual(self.store.active_for("FILE"), [])
        self.assertEqual(self.registry.check_timeouts(), [])
        self.assertEqual(len(self.registry.notifier.events), 1)

    def test_heartbeat_recovers_and_notifies_controller_once(self):
        self.clock.value = 7
        self.registry.check_timeouts()
        self.registry.handle(b"HEARTBEAT|FILE-1", ("10.0.0.4", 1234))
        self.assertEqual(self.registry.notifier.events, [("FILE-1", "INACTIVE"), ("FILE-1", "ACTIVE")])
        self.assertEqual([record.provider_id for record in self.store.active_for("FILE")], ["FILE-1"])
        self.registry.handle(b"HEARTBEAT|FILE-1", ("10.0.0.4", 1234))
        self.assertEqual(len(self.registry.notifier.events), 2)

    def test_recovery_restores_discovery_response(self):
        self.clock.value = 7
        self.registry.check_timeouts()
        self.registry.handle(b"HEARTBEAT|FILE-1", ("10.0.0.4", 1234))
        self.registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 9999))
        data, target = self.registry.sock.sent[-1]
        self.assertEqual(target, ("10.0.0.1", 9999))
        self.assertEqual(data, b"DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001")

    def test_controller_notification_retries_after_unavailable_then_resumes(self):
        notifier = object.__new__(ControllerNotifier)
        notifier.retry_initial_delay = 0.25
        notifier.retry_max_delay = 1.0
        event = {"provider_id": "FILE-1", "status": "INACTIVE"}
        attempts = []
        delays = []
        def send(_event):
            attempts.append(_event)
            return len(attempts) >= 2
        notifier._send = send
        notifier._send_with_retry(event, sleep=delays.append)
        self.assertEqual(attempts, [event, event])
        self.assertEqual(delays, [0.25])

    def test_controller_notifier_posts_real_status_payload(self):
        notifier = object.__new__(ControllerNotifier)
        notifier.url = "http://controller.test/provider-status"
        notifier.timeout = 2
        event = {"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
                 "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch("controller_client.urllib.request.urlopen", return_value=Response()) as send:
            ControllerNotifier._send(notifier, event)
        request = send.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.full_url, "http://controller.test/provider-status")
        self.assertEqual(json.loads(request.data), event)


if __name__ == "__main__":
    unittest.main()
