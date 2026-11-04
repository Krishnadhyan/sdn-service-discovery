import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider_store import ProviderStore
from registry import Registry
import registry as registry_module


class Clock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = value


class RecordingNotifier:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def notify(self, record, status):
        with self.lock:
            self.events.append((record.provider_id, status))


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def sendto(self, payload, address):
        with self.lock:
            self.sent.append((payload, address))


class RegistryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise fake Registry requests, not live observations.
        patcher = patch.object(registry_module, "emit_event")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_registry(self, store=None):
        registry = Registry.__new__(Registry)
        registry.store = store or ProviderStore()
        registry.notifier = RecordingNotifier()
        registry.sock = FakeSocket()
        registry._transition_lock = threading.Lock()
        return registry

    def test_simultaneous_discover_requests_get_valid_responses(self):
        registry = self.make_registry()
        registry.store.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        count = 32
        barrier = threading.Barrier(count)

        def request(index):
            addr = (f"10.0.0.{index + 1}", 10000 + index)
            barrier.wait()
            registry.handle(b"DISCOVER|FILE", addr)
            return addr

        with ThreadPoolExecutor(max_workers=count) as pool:
            addresses = list(pool.map(request, range(count)))
        sent = registry.sock.sent
        self.assertEqual(len(sent), count)
        self.assertEqual({addr for _, addr in sent}, set(addresses))
        self.assertTrue(all(payload == b"DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001"
                            for payload, _ in sent))

    def test_concurrent_round_robin_distributes_registry_requests(self):
        registry = self.make_registry()
        registry.store.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        registry.store.register("FILE-2", "FILE", "TCP", "10.0.0.5", 5001)
        registry.store.register("FILE-3", "FILE", "TCP", "10.0.0.6", 5001)
        count = 300

        def request(index, barrier):
            barrier.wait()
            registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 10000 + index))

        with patch("registry.config.PROVIDER_SELECTION_POLICY", "ROUND_ROBIN"):
            with ThreadPoolExecutor(max_workers=32) as pool:
                for offset in range(0, count, 32):
                    batch = min(32, count - offset)
                    barrier = threading.Barrier(batch)
                    futures = [pool.submit(request, index, barrier)
                               for index in range(offset, offset + batch)]
                    for future in futures:
                        future.result()

        selected = [payload.decode().split("|")[2].split(",")[0]
                    for payload, _ in registry.sock.sent]
        self.assertEqual(len(selected), count)
        self.assertEqual({provider_id: selected.count(provider_id)
                          for provider_id in ("FILE-1", "FILE-2", "FILE-3")},
                         {"FILE-1": 100, "FILE-2": 100, "FILE-3": 100})
        self.assertEqual(len(registry.store.snapshot()), 3)

    def test_registry_round_robin_rotates_existing_response_format(self):
        registry = self.make_registry()
        for index in range(1, 4):
            registry.store.register(f"FILE-{index}", "FILE", "TCP",
                                    f"10.0.0.{index + 3}", 5001)

        with patch("registry.config.PROVIDER_SELECTION_POLICY", "ROUND_ROBIN"):
            for index in range(3):
                registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 9000 + index))

        self.assertEqual([payload for payload, _ in registry.sock.sent], [
            b"DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001|FILE-3,TCP,10.0.0.6,5001",
            b"DISCOVER_RESPONSE|FILE|FILE-2,TCP,10.0.0.5,5001|FILE-3,TCP,10.0.0.6,5001|FILE-1,TCP,10.0.0.4,5001",
            b"DISCOVER_RESPONSE|FILE|FILE-3,TCP,10.0.0.6,5001|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001",
        ])

    def test_round_robin_selects_multiple_chat_and_video_instances(self):
        registry = self.make_registry()
        registry.store.register("CHAT-1", "CHAT", "TCP", "10.0.0.6", 5002)
        registry.store.register("CHAT-2", "CHAT", "TCP", "10.0.0.9", 5002)
        registry.store.register("VIDEO-1", "VIDEO", "UDP", "10.0.0.7", 5003)
        registry.store.register("VIDEO-2", "VIDEO", "UDP", "10.0.0.10", 5003)

        with patch("registry.config.PROVIDER_SELECTION_POLICY", "ROUND_ROBIN"):
            for service in ("CHAT", "CHAT", "VIDEO", "VIDEO"):
                registry.handle(f"DISCOVER|{service}".encode(), ("10.0.0.1", 9000))

        first_entries = [payload.decode().split("|")[2].split(",")
                         for payload, _ in registry.sock.sent]
        self.assertEqual([entry[0] for entry in first_entries],
                         ["CHAT-1", "CHAT-2", "VIDEO-1", "VIDEO-2"])
        self.assertEqual([entry[1] for entry in first_entries],
                         ["TCP", "TCP", "UDP", "UDP"])

    def test_concurrent_register_heartbeat_and_discovery(self):
        registry = self.make_registry()
        count = 24
        barrier = threading.Barrier(count)

        def lifecycle(index):
            pid = f"FILE-{index}"
            addr = (f"10.0.0.{index + 1}", 50000 + index)
            barrier.wait()
            registry.handle(f"REGISTER|{pid}|FILE|TCP|5001".encode(), addr)
            registry.handle(f"HEARTBEAT|{pid}".encode(), addr)

        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(lifecycle, range(count)))
        registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 9000))
        payload, _ = registry.sock.sent[-1]
        self.assertTrue(payload.startswith(b"DISCOVER_RESPONSE|FILE|"))
        self.assertEqual(len(registry.store.active_for("FILE")), count)
        self.assertEqual(len({p.provider_id for p in registry.store.snapshot()}), count)

    def test_multiple_provider_failures_are_independent_and_skipped_by_policies(self):
        clock = Clock()
        store = ProviderStore(timeout=1, clock=clock)
        store.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        store.register("FILE-2", "FILE", "TCP", "10.0.0.5", 5001)
        store.register("FILE-3", "FILE", "TCP", "10.0.0.8", 5001)
        clock.set(1.5)
        store.heartbeat("FILE-3")
        registry = self.make_registry(store)
        class EventCollector:
            def __init__(self): self.events = []
            def notify(self, record, status): self.events.append((record.provider_id, status))
        events = EventCollector()
        registry.notifier = events
        clock.set(2)
        expired = registry.check_timeouts()
        self.assertEqual([row.provider_id for row in expired], ["FILE-1", "FILE-2"])
        self.assertEqual(events.events, [("FILE-1", "INACTIVE"), ("FILE-2", "INACTIVE")])
        with patch("registry.config.PROVIDER_SELECTION_POLICY", "FIRST_AVAILABLE"):
            registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 1))
        self.assertEqual(registry.sock.sent[-1][0], b"DISCOVER_RESPONSE|FILE|FILE-3,TCP,10.0.0.8,5001")
        with patch("registry.config.PROVIDER_SELECTION_POLICY", "ROUND_ROBIN"):
            registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 2))
        self.assertEqual(registry.sock.sent[-1][0], b"DISCOVER_RESPONSE|FILE|FILE-3,TCP,10.0.0.8,5001")
        registry.handle(b"HEARTBEAT|FILE-2", ("10.0.0.5", 50000))
        self.assertEqual(events.events[-1], ("FILE-2", "ACTIVE"))
        with patch("registry.config.PROVIDER_SELECTION_POLICY", "FIRST_AVAILABLE"):
            registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 3))
        self.assertIn(b"FILE-2,TCP,10.0.0.5,5001", registry.sock.sent[-1][0])
        self.assertNotIn(b"FILE-1,TCP", registry.sock.sent[-1][0])
        with patch("registry.config.PROVIDER_SELECTION_POLICY", "ROUND_ROBIN"):
            registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 4))
        self.assertIn(b"FILE-2,TCP,10.0.0.5,5001", registry.sock.sent[-1][0])
        self.assertNotIn(b"FILE-1,TCP", registry.sock.sent[-1][0])

    def test_discovery_during_timeout_and_recovery_returns_valid_protocol(self):
        clock = Clock()
        store = ProviderStore(timeout=1, clock=clock)
        store.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        registry = self.make_registry(store)
        start = threading.Barrier(9)

        def discover(index):
            start.wait()
            for attempt in range(40):
                registry.handle(b"DISCOVER|FILE", ("10.0.0.1", 10000 + index))

        def toggle_status():
            start.wait()
            for _ in range(20):
                clock.set(2)
                registry.check_timeouts()
                clock.set(3)
                registry.handle(b"HEARTBEAT|FILE-1", ("10.0.0.4", 5001))

        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = [pool.submit(discover, i) for i in range(8)]
            futures.append(pool.submit(toggle_status))
            for future in futures:
                future.result()

        self.assertTrue(registry.sock.sent)
        for payload, addr in registry.sock.sent:
            if addr == ("10.0.0.4", 5001):
                self.assertEqual(payload, b"HEARTBEAT_OK|FILE-1")
            else:
                self.assertIn(payload, (b"DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001",
                                        b"ERROR|SERVICE_UNAVAILABLE|FILE"))

    def test_no_provider_returns_service_unavailable(self):
        registry = self.make_registry()
        registry.handle(b"DISCOVER|VIDEO", ("10.0.0.1", 9000))
        self.assertEqual(registry.sock.sent, [(b"ERROR|SERVICE_UNAVAILABLE|VIDEO",
                                               ("10.0.0.1", 9000))])

    def test_timeout_and_recovery_notifications_remain_ordered(self):
        clock = Clock()
        store = ProviderStore(timeout=1, clock=clock)
        store.register("FILE-1", "FILE", "TCP", "10.0.0.4", 5001)
        registry = self.make_registry(store)
        clock.set(2)
        expired_inside_store = threading.Event()
        release_expire = threading.Event()
        real_expire = store.expire

        def paused_expire():
            changed = real_expire()
            expired_inside_store.set()
            release_expire.wait(2)
            return changed

        with patch.object(store, "expire", side_effect=paused_expire):
            timeout = threading.Thread(target=registry.check_timeouts)
            timeout.start()
            self.assertTrue(expired_inside_store.wait(1))
            recovery = threading.Thread(target=registry.handle,
                                        args=(b"HEARTBEAT|FILE-1", ("10.0.0.4", 5001)))
            recovery.start()
            time.sleep(0.02)
            self.assertTrue(recovery.is_alive(), "recovery must wait for timeout event enqueue")
            release_expire.set()
            timeout.join(1)
            recovery.join(1)
        self.assertFalse(timeout.is_alive())
        self.assertFalse(recovery.is_alive())
        self.assertEqual(registry.notifier.events,
                         [("FILE-1", "INACTIVE"), ("FILE-1", "ACTIVE")])

    def test_bounded_workers_process_requests_concurrently_and_shutdown(self):
        registry = self.make_registry()
        registry._work_queue = __import__("queue").Queue(maxsize=4)
        registry._workers = []
        arrived = 0
        arrived_lock = threading.Lock()
        both_workers_started = threading.Event()
        release = threading.Event()

        def blocking_handle(data, addr):
            nonlocal arrived
            with arrived_lock:
                arrived += 1
                if arrived == 2:
                    both_workers_started.set()
            release.wait(1)

        registry.handle = blocking_handle
        with patch("registry.config.REQUEST_WORKERS", 2):
            registry._start_workers()
            registry._work_queue.put((b"one", ("10.0.0.1", 1)))
            registry._work_queue.put((b"two", ("10.0.0.2", 2)))
            self.assertTrue(both_workers_started.wait(1))
            release.set()
            registry._work_queue.join()
            registry._stop_workers()
        self.assertEqual(registry._workers, [])
        self.assertEqual(registry._work_queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
