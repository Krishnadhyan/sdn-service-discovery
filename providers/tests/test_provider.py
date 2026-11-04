import threading
import tempfile
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import provider


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self):
        default_token_patch = patch.object(provider, "DEFAULT_LIFECYCLE_TOKEN_FILE",
                                           Path("/tmp/nonexistent-provider-lifecycle-test-token"))
        default_token_patch.start()
        self.addCleanup(default_token_patch.stop)
        self.instance = provider.Provider("FILE-1", "FILE", "0.0.0.0", 5001, "127.0.0.1", 5000)
        self.instance.lifecycle_token = "test-token-that-is-long-enough-123456"
        # Unit fixtures are not live client traffic and must not enter telemetry.
        event_patcher = patch.object(provider, "emit_event")
        event_patcher.start()
        self.addCleanup(event_patcher.stop)

    def test_tcp_service_response_emits_observed_client_traffic(self):
        class FakeConnection:
            def __init__(self):
                self.sent = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): self.timeout = timeout
            def recv(self, size): return b"PING\n"
            def sendall(self, data): self.sent.append(data)
        conn = FakeConnection()
        with patch.object(provider, "emit_event") as emit:
            self.instance._tcp_client(conn, ("10.0.0.1", 43210))
        emit.assert_called_once_with("FILE-1", "service_traffic_served",
                                     service="FILE", protocol="TCP", ip="0.0.0.0",
                                     port=5001, client_ip="10.0.0.1",
                                     client_port=43210, response_sent=True)
        self.assertIn(b"FILE-1 OK service=FILE", conn.sent[0])

    def test_topology_token_enables_control_without_new_cli_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "service_discovery_lifecycle.token"
            token_file.write_text("topology-token-with-at-least-32-characters")
            with patch.object(provider, "DEFAULT_LIFECYCLE_TOKEN_FILE", token_file):
                launched_like_existing_mininet_command = provider.Provider(
                    "FILE-1", "FILE", "0.0.0.0", 5001, "127.0.0.1", 5000)
        self.assertEqual(launched_like_existing_mininet_command.lifecycle_token,
                         "topology-token-with-at-least-32-characters")

    def test_lifecycle_listener_binds_when_valid_token_is_loaded(self):
        class FakeControlSocket:
            def __init__(self): self.bound = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): self.timeout = timeout
            def bind(self, address): self.bound = address
            def recvfrom(self, size):
                self_provider.stop_event.set()
                raise provider.socket.timeout()

        self_provider = self.instance
        fake = FakeControlSocket()
        with patch.object(provider.socket, "socket", return_value=fake):
            self.instance._lifecycle_control()
        self.assertEqual(fake.bound, ("0.0.0.0", provider.LIFECYCLE_PORT))

    def test_seven_provider_configurations_register_independently(self):
        assignments = [
            ("FILE-1", "FILE", "10.0.0.4", 5001, "TCP"),
            ("FILE-2", "FILE", "10.0.0.5", 5001, "TCP"),
            ("FILE-3", "FILE", "10.0.0.8", 5001, "TCP"),
            ("CHAT-1", "CHAT", "10.0.0.6", 5002, "TCP"),
            ("CHAT-2", "CHAT", "10.0.0.9", 5002, "TCP"),
            ("VIDEO-1", "VIDEO", "10.0.0.7", 5003, "UDP"),
            ("VIDEO-2", "VIDEO", "10.0.0.10", 5003, "UDP"),
        ]
        registered = []
        for provider_id, service, host, port, protocol in assignments:
            instance = provider.Provider(provider_id, service, host, port,
                                         "10.0.0.3", 5000)
            def registry_reply(message, expect_reply=False):
                registered.append(message)
                return f"REGISTER_OK|{provider_id}|{service}"
            with patch.object(instance, "_registry_message", side_effect=registry_reply):
                instance.register()
        self.assertEqual(registered, [
            f"REGISTER|{provider_id}|{service}|{protocol}|{port}"
            for provider_id, service, _, port, protocol in assignments
        ])

    def test_unreadable_automatic_token_does_not_break_provider_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "service_discovery_lifecycle.token"
            token_file.write_text("valid-token-material-is-at-least-32-chars")
            with patch.object(provider, "DEFAULT_LIFECYCLE_TOKEN_FILE", token_file), \
                 patch.object(Path, "read_text", side_effect=PermissionError("permission denied")):
                instance = provider.Provider("FILE-2", "FILE", "0.0.0.0", 5001, "127.0.0.1", 5000)
        self.assertIsNone(instance.lifecycle_token)

    def test_only_project_bridge_with_secret_and_matching_id_can_control(self):
        pause = f"PAUSE_HEARTBEATS|{self.instance.lifecycle_token}|FILE-1".encode()
        self.assertIsNone(self.instance.handle_lifecycle_request(pause, "10.0.0.4"))
        self.assertFalse(self.instance.heartbeats_paused.is_set())
        self.assertIsNone(self.instance.handle_lifecycle_request(
            f"PAUSE_HEARTBEATS|wrong-token|FILE-1".encode(), "10.0.0.254"))
        self.assertIsNone(self.instance.handle_lifecycle_request(
            f"PAUSE_HEARTBEATS|{self.instance.lifecycle_token}|FILE-2".encode(), "10.0.0.254"))
        self.assertFalse(self.instance.heartbeats_paused.is_set())

    def test_heartbeat_transmission_pauses_and_resumes(self):
        sent = []
        lock = threading.Lock()

        def send(message, expect_reply=False):
            with lock:
                sent.append(message)

        self.instance._registry_message = send
        self.instance.stop_event.clear()
        with patch.object(provider, "HEARTBEAT_INTERVAL", 0.02):
            thread = threading.Thread(target=self.instance._heartbeats)
            thread.start()
            deadline = time.monotonic() + 1
            while len(sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            pause = f"PAUSE_HEARTBEATS|{self.instance.lifecycle_token}|FILE-1".encode()
            ack = self.instance.handle_lifecycle_request(pause, provider.LIFECYCLE_SOURCE)
            self.assertEqual(ack, b"LIFECYCLE_OK|FILE-1|PAUSED")
            time.sleep(0.06)
            with lock:
                paused_count = len(sent)
            time.sleep(0.06)
            with lock:
                self.assertEqual(len(sent), paused_count)
            resume = f"RESUME_HEARTBEATS|{self.instance.lifecycle_token}|FILE-1".encode()
            ack = self.instance.handle_lifecycle_request(resume, provider.LIFECYCLE_SOURCE)
            self.assertEqual(ack, b"LIFECYCLE_OK|FILE-1|RESUMED")
            deadline = time.monotonic() + 1
            while len(sent) == paused_count and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreater(len(sent), paused_count)
            self.instance.stop_event.set()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
