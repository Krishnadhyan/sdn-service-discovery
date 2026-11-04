import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integration import start_demo


class StartDemoTests(unittest.TestCase):
    def test_registry_command_uses_h3_namespace_and_existing_registry_entrypoint(self):
        command = start_demo.registry_command(303)
        self.assertEqual(command[:3], [start_demo.MNEXEC, "-a", "303"])
        self.assertEqual(command[3:], [start_demo.sys.executable, str(start_demo.REGISTRY)])

    def test_provider_commands_preserve_each_host_service_address_and_port(self):
        self.assertEqual(len(start_demo.PROVIDERS), 7)
        for spec in start_demo.PROVIDERS:
            host, provider_id, service, ip, port = spec
            command = start_demo.provider_command(100 + int(host[1:]), spec)
            self.assertEqual(command[:3], [start_demo.MNEXEC, "-a", str(100 + int(host[1:]))])
            self.assertEqual(command[command.index("--id") + 1], provider_id)
            self.assertEqual(command[command.index("--service") + 1], service)
            self.assertEqual(command[command.index("--host") + 1], ip)
            self.assertEqual(command[command.index("--port") + 1], str(port))
            self.assertEqual(command[command.index("--registry-host") + 1], "10.0.0.3")
            self.assertEqual(command[command.index("--registry-port") + 1], "5000")
            self.assertEqual(command[command.index("--lifecycle-token-file") + 1], str(start_demo.TOKEN_FILE))

    def test_registry_and_provider_process_detection_is_namespace_specific(self):
        registry = {"pid": 44, "argv": ["python3", str(start_demo.REGISTRY)], "env": [], "netns": 3}
        with patch.object(start_demo, "process_in_netns", return_value=True):
            self.assertIs(start_demo.existing_registry([registry], 123), registry)
        with patch.object(start_demo, "process_in_netns", return_value=False):
            self.assertIsNone(start_demo.existing_registry([registry], 123))

        spec = start_demo.PROVIDERS[0]
        argv = ["python3", str(start_demo.PROVIDER), "--id", spec[1],
                "--service", spec[2], "--host", spec[3], "--port", str(spec[4])]
        process = {"pid": 45, "argv": argv, "env": [], "netns": 4}
        host_pids = {host: 1000 + index for index, host in enumerate(start_demo.HOSTS)}
        with patch.object(start_demo, "process_in_netns", return_value=True):
            self.assertEqual(start_demo.existing_providers([process], host_pids), {spec[1]: process})
        with patch.object(start_demo, "process_in_netns", return_value=False):
            with self.assertRaisesRegex(start_demo.StartupError, "outside its configured"):
                start_demo.existing_providers([process], host_pids)

    def test_startup_verification_requires_every_provider_active_and_heartbeat_seen(self):
        stats = {"providers": {spec[1]: {"status": "ACTIVE", "heartbeat_count": 1}
                               for spec in start_demo.PROVIDERS}}
        self.assertTrue(start_demo.providers_healthy(stats))
        stats["providers"][start_demo.PROVIDERS[-1][1]]["heartbeat_count"] = 0
        self.assertFalse(start_demo.providers_healthy(stats))
        stats["providers"][start_demo.PROVIDERS[-1][1]]["status"] = "INACTIVE"
        self.assertFalse(start_demo.providers_healthy(stats))

    def test_clean_shutdown_terminates_only_launcher_owned_children(self):
        class FakeChild:
            def __init__(self):
                self.terminated = False
                self.returncode = None
            def poll(self): return self.returncode
            def terminate(self):
                self.terminated = True
                self.returncode = 0
            def wait(self, timeout=None): return self.returncode

        launcher = start_demo.DemoLauncher()
        owned, adopted = FakeChild(), FakeChild()
        launcher.children = [owned]
        launcher.shutdown()
        self.assertTrue(owned.terminated)
        self.assertFalse(adopted.terminated)

    def test_second_launcher_cannot_acquire_active_demo_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "demo.lock"
            state_path = Path(directory) / "demo.json"
            with patch.object(start_demo, "LOCK_FILE", lock_path), patch.object(start_demo, "STATE_FILE", state_path):
                first = start_demo.DemoLauncher()
                second = start_demo.DemoLauncher()
                first.acquire_lock()
                try:
                    with self.assertRaisesRegex(start_demo.StartupError, "already active"):
                        second.acquire_lock()
                finally:
                    first.lock_handle.close()
                    if second.lock_handle:
                        second.lock_handle.close()


if __name__ == "__main__":
    unittest.main()
