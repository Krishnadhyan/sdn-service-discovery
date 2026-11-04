import sys
import io
import json
import queue
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller
import run_controller


class FakeParser:
    def OFPMatch(self, **kwargs):
        return kwargs
    def OFPFlowMod(self, **kwargs):
        return kwargs
    def OFPInstructionActions(self, instruction, actions):
        return (instruction, actions)
    def OFPActionOutput(self, port, max_len=None):
        return ("output", port) if max_len is None else ("output", port, max_len)


class FakeDatapath:
    def __init__(self, datapath_id=2):
        self.id = datapath_id
        self.ofproto_parser = FakeParser()
        self.ofproto = type("OF", (), {"OFPFC_ADD": 0, "OFPFC_DELETE": 3,
                                        "OFPP_ANY": 0xFFFFFFFF, "OFPG_ANY": 0xFFFFFFFF,
                                        "OFPIT_APPLY_ACTIONS": 4, "OFPP_NORMAL": 0xFFFFFFFA,
                                        "OFPP_CONTROLLER": 0xFFFFFFFD,
                                        "OFPCML_NO_BUFFER": 0xFFFF,
                                        "OFPPS_LINK_DOWN": 1, "OFPPR_ADD": 0,
                                        "OFPPR_DELETE": 1, "OFPPR_MODIFY": 2})()
        self.messages = []
        self.fail_sends = 0
    def send_msg(self, message):
        if self.fail_sends:
            self.fail_sends -= 1
            raise OSError("simulated datapath send failure")
        self.messages.append(message)


class ControllerFlowLifecycleTests(unittest.TestCase):
    def setUp(self):
        # Mock datapaths are test fixtures; never write them into the live event log.
        patcher = patch.object(controller, "emit_event")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_drop_rule_submission_emits_observed_controller_event(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath()
        app.datapaths = {2: datapath}
        app.provider_status = {}
        payload = {"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
                   "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
        with patch.object(controller, "emit_event") as emit:
            app.handle_provider_status(payload)
        emit.assert_called_once_with("provider_drop_flow_mod_submitted", switch="s2",
                                     provider_id="FILE-1", ip="10.0.0.4",
                                     protocol="TCP", port=5001, priority=100)

    def test_openflow_port_status_is_recorded_from_switch_event(self):
        from types import SimpleNamespace
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath(datapath_id=1)
        desc = SimpleNamespace(name=b"s1-eth7\x00", state=datapath.ofproto.OFPPS_LINK_DOWN,
                               port_no=7)
        event = SimpleNamespace(msg=SimpleNamespace(datapath=datapath, desc=desc,
                                                    reason=datapath.ofproto.OFPPR_MODIFY))
        with patch.object(controller, "emit_event") as emit:
            controller.ServiceDiscoveryController.port_status_handler(app, event)
        emit.assert_called_once_with("openflow_port_status", switch="s1", datapath_id=1,
                                     port_no=7, port_name="s1-eth7", status="DOWN", reason="MODIFY")

    def test_launcher_resolves_and_loads_controller_next_to_script(self):
        expected = (Path(__file__).resolve().parents[1] / "controller.py").resolve()
        original = sys.modules.get("controller")

        class FakeAppManager:
            def load_apps(self, apps):
                self.apps = apps
                module = types.ModuleType("controller")
                module.__file__ = str(expected)
                sys.modules["controller"] = module

        manager = FakeAppManager()
        try:
            with patch("pathlib.Path.cwd", return_value=Path("/wrong/launch/directory")):
                loaded = run_controller.load_controller_application(manager)
            self.assertEqual(loaded, expected)
            self.assertEqual(manager.apps, [str(expected)])
            self.assertEqual(Path(sys.modules["controller"].__file__).resolve(), expected)
        finally:
            if original is not None:
                sys.modules["controller"] = original
            else:
                sys.modules.pop("controller", None)

    def test_registry_http_event_is_validated_and_queued(self):
        payload = {"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
                   "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
        body = json.dumps(payload).encode()
        handler = object.__new__(controller.ProviderStatusHandler)
        handler.path = "/provider-status"
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        response = {}
        handler.send_response = lambda status: response.update(status=status)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.send_error = lambda status, message: response.update(status=status, error=message)
        events = queue.Queue()
        with patch.object(controller, "status_queue", events):
            handler.do_POST()
        self.assertEqual(response["status"], 200)
        self.assertEqual(events.get_nowait(), payload)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {"message": "Status queued"})

    def test_baseline_flow_is_submitted_to_both_switches(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        app.datapaths = {}
        app.provider_status = {}
        for switch_id in (1, 2):
            datapath = FakeDatapath(switch_id)
            ev = types.SimpleNamespace(msg=types.SimpleNamespace(datapath=datapath))
            controller.ServiceDiscoveryController.switch_features_handler(app, ev)
            self.assertEqual(app.datapaths[switch_id], datapath)
            self.assertEqual(len(datapath.messages), 3)
            table_miss, ipv4_baseline, arp_baseline = datapath.messages
            self.assertEqual(table_miss["priority"], 0)
            self.assertEqual(table_miss["match"], {})
            self.assertEqual(table_miss["instructions"], [(datapath.ofproto.OFPIT_APPLY_ACTIONS,
                [("output", datapath.ofproto.OFPP_CONTROLLER, datapath.ofproto.OFPCML_NO_BUFFER)])])
            self.assertEqual(ipv4_baseline["priority"], 1)
            self.assertEqual(ipv4_baseline["match"], {"eth_type": 0x0800})
            self.assertEqual(arp_baseline["priority"], 1)
            self.assertEqual(arp_baseline["match"], {"eth_type": 0x0806})
            for flow in (ipv4_baseline, arp_baseline):
                self.assertEqual(flow["instructions"], [(datapath.ofproto.OFPIT_APPLY_ACTIONS,
                    [("output", datapath.ofproto.OFPP_NORMAL)])])

    def test_duplicate_events_are_idempotent_and_healthy_provider_match_is_separate(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath()
        app.datapaths = {2: datapath}
        app.provider_status = {}
        failed = {"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
                  "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
        healthy = {"provider_id": "FILE-2", "service": "FILE", "status": "ACTIVE",
                   "ip": "10.0.0.5", "port": 5001, "protocol": "TCP"}
        app.handle_provider_status(failed)
        app.handle_provider_status(failed)
        self.assertEqual(len(datapath.messages), 1)
        app.handle_provider_status(healthy)
        add, remove = datapath.messages
        self.assertEqual(add["command"], datapath.ofproto.OFPFC_ADD)
        self.assertEqual(add["match"]["ipv4_dst"], "10.0.0.4")
        self.assertEqual(add["match"]["tcp_dst"], 5001)
        self.assertEqual(add["instructions"], [])  # Empty actions means drop.
        self.assertEqual(remove["command"], datapath.ofproto.OFPFC_DELETE)
        self.assertEqual(remove["match"]["ipv4_dst"], "10.0.0.5")
        self.assertEqual(len(datapath.messages), 2)

    def test_multiple_failed_providers_install_only_their_own_matches(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath()
        app.datapaths = {2: datapath}
        app.provider_status = {}
        for pid, ip in (("FILE-1", "10.0.0.4"), ("FILE-2", "10.0.0.5")):
            app.handle_provider_status({"provider_id": pid, "service": "FILE", "status": "INACTIVE",
                                        "ip": ip, "port": 5001, "protocol": "TCP"})
        self.assertEqual([message["match"]["ipv4_dst"] for message in datapath.messages],
                         ["10.0.0.4", "10.0.0.5"])
        self.assertTrue(all(message["instructions"] == [] for message in datapath.messages))

    def test_status_queue_retries_transient_flow_submission_failure(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        app.logger = Mock()
        event = {"provider_id": "FILE-1", "status": "INACTIVE"}
        app.handle_provider_status = Mock(side_effect=[OSError("temporary"), None])
        with patch.object(controller.hub, "sleep") as sleep:
            app._process_status_with_retry(event)
        self.assertEqual(app.handle_provider_status.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_failed_datapath_send_remains_retryable(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath()
        datapath.fail_sends = 1
        app.datapaths = {2: datapath}
        app.provider_status = {}
        payload = {"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
                   "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
        with self.assertRaises(OSError):
            app.handle_provider_status(payload)
        self.assertNotIn("FILE-1", app.provider_status)
        app.handle_provider_status(payload)
        self.assertEqual(len(datapath.messages), 1)
        self.assertEqual(app.provider_status["FILE-1"], payload)

    def test_file_and_chat_inactive_rules_match_tcp_destination(self):
        for provider_id, service, ip, port in (("FILE-1", "FILE", "10.0.0.4", 5001),
                                                ("CHAT-1", "CHAT", "10.0.0.6", 5002)):
            with self.subTest(provider=provider_id):
                app = object.__new__(controller.ServiceDiscoveryController)
                datapath = FakeDatapath()
                app.datapaths = {2: datapath}
                app.provider_status = {}
                app.handle_provider_status({"provider_id": provider_id, "service": service,
                    "status": "INACTIVE", "ip": ip, "port": port, "protocol": "TCP"})
                flow = datapath.messages[0]
                self.assertEqual(flow["priority"], 100)
                self.assertGreater(flow["priority"], 1)
                self.assertEqual(flow["command"], datapath.ofproto.OFPFC_ADD)
                self.assertEqual(flow["match"], {"eth_type": 0x0800, "ipv4_dst": ip,
                                                 "ip_proto": 6, "tcp_dst": port})
                self.assertEqual(flow["instructions"], [])

    def test_video_drop_uses_udp_destination_match(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath()
        app.datapaths = {2: datapath}
        app.provider_status = {}
        app.handle_provider_status({"provider_id": "VIDEO-1", "service": "VIDEO", "status": "INACTIVE",
                                    "ip": "10.0.0.7", "port": 5003, "protocol": "UDP"})
        self.assertEqual(datapath.messages[0]["match"], {"eth_type": 0x0800, "ipv4_dst": "10.0.0.7",
                                                         "ip_proto": 17, "udp_dst": 5003})

    def test_switch_reconnect_restores_baseline_and_inactive_provider_drop(self):
        from types import SimpleNamespace
        app = object.__new__(controller.ServiceDiscoveryController)
        app.datapaths = {}
        app.provider_status = {"FILE-1": {"provider_id": "FILE-1", "service": "FILE",
            "status": "INACTIVE", "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}}
        first_connection = FakeDatapath()
        controller.ServiceDiscoveryController.switch_features_handler(
            app, SimpleNamespace(msg=SimpleNamespace(datapath=first_connection)))
        datapath = FakeDatapath()
        controller.ServiceDiscoveryController.switch_features_handler(
            app, SimpleNamespace(msg=SimpleNamespace(datapath=datapath)))
        self.assertEqual(app.datapaths[2], datapath)
        self.assertEqual(len(first_connection.messages), 4)
        self.assertEqual(len(datapath.messages), 4)
        table_miss, ipv4_baseline, arp_baseline, drop = datapath.messages
        self.assertEqual(table_miss["priority"], 0)
        self.assertEqual(table_miss["instructions"][0][1][0][1], datapath.ofproto.OFPP_CONTROLLER)
        self.assertEqual(ipv4_baseline["priority"], 1)
        self.assertEqual(ipv4_baseline["instructions"][0][1][0][1], datapath.ofproto.OFPP_NORMAL)
        self.assertEqual(arp_baseline["priority"], 1)
        self.assertEqual(arp_baseline["instructions"][0][1][0][1], datapath.ofproto.OFPP_NORMAL)
        self.assertEqual(drop["priority"], 100)
        self.assertGreater(drop["priority"], ipv4_baseline["priority"])
        self.assertEqual(drop["match"]["ipv4_dst"], "10.0.0.4")

    def test_inactive_adds_and_active_removes_only_provider_drop(self):
        app = object.__new__(controller.ServiceDiscoveryController)
        datapath = FakeDatapath()
        app.datapaths = {2: datapath}
        app.provider_status = {}
        payload = {"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
                   "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
        app.handle_provider_status(payload)
        added = datapath.messages[-1]
        self.assertEqual(added["command"], datapath.ofproto.OFPFC_ADD)
        self.assertEqual(added["priority"], 100)
        self.assertEqual(added["match"], {"eth_type": 0x0800, "ipv4_dst": "10.0.0.4",
                                          "ip_proto": 6, "tcp_dst": 5001})
        payload = dict(payload, status="ACTIVE")
        app.handle_provider_status(payload)
        removed = datapath.messages[-1]
        self.assertEqual(removed["command"], datapath.ofproto.OFPFC_DELETE)
        self.assertEqual(removed["priority"], 100)
        self.assertEqual(removed["match"], added["match"])
        self.assertEqual(app.provider_status["FILE-1"]["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
