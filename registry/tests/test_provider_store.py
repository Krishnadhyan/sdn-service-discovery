import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from provider_store import ProviderStore

class Clock:
    t = 0.0
    def __call__(self): return self.t

class T(unittest.TestCase):
    def setUp(self):
        self.c = Clock(); self.s = ProviderStore(timeout=6, clock=self.c)
        self.s.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        self.s.register("FILE-2", "FILE", "TCP", "10.0.0.5", 5001)

    def test_discover_both(self):
        self.assertEqual([p.provider_id for p in self.s.active_for("FILE")], ["FILE-1", "FILE-2"])

    def test_round_robin_cycles_three_healthy_providers(self):
        self.s.register("FILE-3", "FILE", "TCP", "10.0.0.6", 5001)
        selected = [self.s.select_for("FILE", "ROUND_ROBIN")[0].provider_id
                    for _ in range(6)]
        self.assertEqual(selected, ["FILE-1", "FILE-2", "FILE-3"] * 2)

    def test_round_robin_counters_are_independent_per_service(self):
        self.s.register("CHAT-1", "CHAT", "TCP", "10.0.0.6", 5002)
        self.s.register("CHAT-2", "CHAT", "TCP", "10.0.0.7", 5002)
        self.s.register("VIDEO-1", "VIDEO", "UDP", "10.0.0.8", 5003)
        self.s.register("VIDEO-2", "VIDEO", "UDP", "10.0.0.9", 5003)
        self.assertEqual(self.s.select_for("CHAT", "ROUND_ROBIN")[0].provider_id,
                         "CHAT-1")
        self.assertEqual(self.s.select_for("VIDEO", "ROUND_ROBIN")[0].provider_id,
                         "VIDEO-1")
        self.assertEqual(self.s.select_for("CHAT", "ROUND_ROBIN")[0].provider_id,
                         "CHAT-2")
        self.assertEqual(self.s.select_for("VIDEO", "ROUND_ROBIN")[0].provider_id,
                         "VIDEO-2")

    def test_first_available_keeps_sorted_order_and_does_not_advance_counter(self):
        self.s.register("FILE-3", "FILE", "TCP", "10.0.0.6", 5001)
        for _ in range(4):
            self.assertEqual([p.provider_id for p in self.s.select_for("FILE")],
                             ["FILE-1", "FILE-2", "FILE-3"])
        self.assertEqual(self.s.select_for("FILE", "ROUND_ROBIN")[0].provider_id,
                         "FILE-1")

    def test_round_robin_skips_inactive_and_expired_providers(self):
        self.s.register("FILE-3", "FILE", "TCP", "10.0.0.6", 5001)
        self.c.t = 7
        self.s.heartbeat("FILE-1")
        self.s.heartbeat("FILE-3")
        self.s.expire()
        selected = [self.s.select_for("FILE", "ROUND_ROBIN")
                    for _ in range(4)]
        self.assertEqual([[p.provider_id for p in row] for row in selected],
                         [["FILE-1", "FILE-3"], ["FILE-3", "FILE-1"]] * 2)
        self.assertNotIn("FILE-2", [p.provider_id for row in selected for p in row])

    def test_new_provider_and_recovered_provider_are_eligible(self):
        self.s.register("FILE-3", "FILE", "TCP", "10.0.0.6", 5001)
        before_failure = [self.s.select_for("FILE", "ROUND_ROBIN")[0].provider_id
                         for _ in range(3)]
        self.assertEqual(before_failure, ["FILE-1", "FILE-2", "FILE-3"])

        self.c.t = 7
        self.s.heartbeat("FILE-1")
        self.s.heartbeat("FILE-3")
        self.s.expire()
        self.assertNotIn("FILE-2", [p.provider_id
                                    for p in self.s.select_for("FILE", "ROUND_ROBIN")])

        self.assertTrue(self.s.heartbeat("FILE-2")[1])
        eligible = [self.s.select_for("FILE", "ROUND_ROBIN")[0].provider_id
                    for _ in range(3)]
        self.assertCountEqual(eligible, ["FILE-1", "FILE-2", "FILE-3"])

    def test_expiry_once_and_excluded(self):
        self.c.t = 7
        self.s.heartbeat("FILE-2")
        self.assertEqual([r.provider_id for r in self.s.expire()], ["FILE-1"])
        self.assertEqual(self.s.expire(), [])          # no repeated event
        self.assertEqual([p.provider_id for p in self.s.active_for("FILE")], ["FILE-2"])

    def test_recovery(self):
        self.c.t = 7; self.s.expire()
        found, became, _ = self.s.heartbeat("FILE-1")
        self.assertTrue(found and became)
        _, became2, _ = self.s.heartbeat("FILE-1")
        self.assertFalse(became2)                       # no duplicate ACTIVE

    def test_unknown_heartbeat(self):
        self.assertFalse(self.s.heartbeat("NOPE")[0])

unittest.main() if __name__ == "__main__" else None
