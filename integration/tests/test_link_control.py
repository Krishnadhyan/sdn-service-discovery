import unittest
from unittest.mock import Mock

from integration.topology import LinkControl


class FakeInterface:
    def __init__(self, name):
        self.name = name
        self.up = True
    def isUp(self):
        return self.up
    def __str__(self):
        return self.name


class FakeNet:
    def __init__(self):
        self.s1, self.s2 = object(), object()
        self.link = type("Link", (), {})()
        self.link.intf1, self.link.intf2 = FakeInterface("s1-eth7"), FakeInterface("s2-eth1")
        self.config_calls = []
    def get(self, name):
        if name == "s1": return self.s1
        if name == "s2": return self.s2
        raise KeyError(name)
    def linksBetween(self, a, b):
        return [self.link] if {a, b} == {self.s1, self.s2} else []
    def configLinkStatus(self, a, b, status):
        self.config_calls.append((a, b, status))
        self.link.intf1.up = self.link.intf2.up = status == "up"


class LinkControlTests(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.control = LinkControl(self.net)

    def test_reports_actual_initial_state_and_interfaces(self):
        state = self.control.execute("status")
        self.assertEqual(state["status"], "UP")
        self.assertEqual(state["interfaces"], {"s1": "s1-eth7", "s2": "s2-eth1"})

    def test_link_down_and_up_call_mininet_config_link_status(self):
        down = self.control.execute("down")
        self.assertTrue(down["ok"])
        self.assertEqual(down["status"], "DOWN")
        up = self.control.execute("up")
        self.assertTrue(up["ok"])
        self.assertEqual(up["status"], "UP")
        self.assertEqual(self.net.config_calls, [("s1", "s2", "down"), ("s1", "s2", "up")])

    def test_duplicate_operation_is_idempotent(self):
        self.control.execute("down")
        duplicate = self.control.execute("down")
        self.assertTrue(duplicate["ok"])
        self.assertFalse(duplicate["changed"])
        self.assertEqual(len(self.net.config_calls), 1)

    def test_missing_net_switches_or_link_report_unknown(self):
        self.assertEqual(LinkControl(None).execute("status")["status"], "UNKNOWN")
        self.net.get = Mock(side_effect=KeyError("s1"))
        self.assertEqual(self.control.execute("down")["status"], "UNKNOWN")
        self.net.get = FakeNet().get
        self.net.linksBetween = Mock(return_value=[])
        state = self.control.execute("status")
        self.assertFalse(state["ok"])
        self.assertEqual(state["status"], "UNKNOWN")

    def test_mininet_command_failure_does_not_report_success(self):
        self.net.configLinkStatus = Mock(side_effect=OSError("failed"))
        result = self.control.execute("down")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "UP")
        self.assertIn("command failed", result["error"])

    def test_rejects_arbitrary_operations_and_busy_conflicting_operation(self):
        self.assertFalse(self.control.execute("s1-eth7 down")["ok"])
        self.control.operation_lock.acquire()
        try:
            result = self.control.execute("down")
        finally:
            self.control.operation_lock.release()
        self.assertFalse(result["ok"])
        self.assertIn("already in progress", result["error"])
        self.assertEqual(self.net.config_calls, [])

    def test_partial_interface_state_is_unknown(self):
        self.net.link.intf2.up = False
        self.assertEqual(self.control.execute("status")["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
