import logging
import queue
import socket
import threading

import config
from config import MININET_CLIENT_IPS
import protocol
from controller_client import ControllerNotifier
from protocol import ProtocolError
from provider_store import ProviderStore
from registry_telemetry import emit as emit_event, touch_liveness

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("registry")


class Registry:
    def __init__(self):
        self.store = ProviderStore()
        self.notifier = ControllerNotifier()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((config.REGISTRY_HOST, config.REGISTRY_PORT))
        self.sock.settimeout(1.0)       # lets Ctrl+C shut down cleanly
        self.running = True
        self._stop_event = threading.Event()
        self._transition_lock = threading.Lock()
        self._work_queue = queue.Queue(maxsize=max(1, config.REQUEST_QUEUE_SIZE))
        self._workers = []
        self._timeout_thread = None

    def handle(self, data, addr):
        try:
            kind, f = protocol.parse(data)
        except ProtocolError as e:
            log.warning("Ignoring bad message from %s: %s", addr, e)
            return

        if kind == "REGISTER":
            ip = addr[0]                      # provider IP = UDP source IP
            with self._transition_lock:
                rec, became_active = self.store.register(
                    f["provider_id"], f["service"], f["protocol"], ip, f["port"])
                if became_active:
                    emit_event("provider_status", provider_id=rec.provider_id, service=rec.service,
                               status="ACTIVE", ip=rec.ip, protocol=rec.protocol, port=rec.port,
                               transition=True, reason="registration")
                    self.notifier.notify(rec, "ACTIVE")
            log.info("REGISTER %s %s %s %s:%d", rec.provider_id, rec.service,
                     rec.protocol, rec.ip, rec.port)
            self.sock.sendto(protocol.fmt_register_ok(rec.provider_id, rec.service), addr)
            if became_active:
                log.info("%s status changed to ACTIVE", rec.provider_id)

        elif kind == "HEARTBEAT":
            with self._transition_lock:
                found, became_active, rec = self.store.heartbeat(f["provider_id"])
                if found:
                    emit_event("heartbeat", provider_id=rec.provider_id, service=rec.service,
                               ip=rec.ip, status="ACTIVE", recovered=became_active)
                    if became_active:
                        emit_event("provider_status", provider_id=rec.provider_id, service=rec.service,
                                   status="ACTIVE", ip=rec.ip, protocol=rec.protocol, port=rec.port,
                                   transition=True, reason="heartbeat_recovery")
                        self.notifier.notify(rec, "ACTIVE")
            if not found:
                log.warning("Heartbeat from unknown provider %s ignored", f["provider_id"])
                self.sock.sendto(f"ERROR|UNKNOWN_PROVIDER|{f['provider_id']}".encode(), addr)
            else:
                self.sock.sendto(f"HEARTBEAT_OK|{rec.provider_id}".encode(), addr)
                if became_active:
                    log.info("%s recovered -> ACTIVE", rec.provider_id)
                else:
                    log.debug("heartbeat %s", rec.provider_id)

        elif kind == "DISCOVER":
            svc = f["service"]
            providers = self.store.select_for(
                svc, config.PROVIDER_SELECTION_POLICY)
            selected = providers[0].provider_id if providers else None
            is_client_request = addr[0] in MININET_CLIENT_IPS
            emit_event("discovery", service=svc, client_ip=addr[0],
                       policy=config.PROVIDER_SELECTION_POLICY,
                       provider_ids=[provider.provider_id for provider in providers],
                       selected_provider=selected, success=bool(providers),
                       genuine_client_request=is_client_request)
            if providers:
                reply = protocol.fmt_discover_response(svc, providers)
                log.info("DISCOVER %s from %s -> %d provider(s)", svc, addr[0], len(providers))
            else:
                reply = protocol.fmt_unavailable(svc)
                log.info("DISCOVER %s from %s -> SERVICE_UNAVAILABLE", svc, addr[0])
            self.sock.sendto(reply, addr)
            emit_event("discovery_response_sent", service=svc, client_ip=addr[0],
                       policy=config.PROVIDER_SELECTION_POLICY, selected_provider=selected,
                       provider_ids=[provider.provider_id for provider in providers],
                       success=bool(providers), genuine_client_request=is_client_request)

    def timeout_loop(self):
        while not self._stop_event.is_set():
            self.check_timeouts()
            touch_liveness()
            self._stop_event.wait(config.CHECK_INTERVAL)

    def check_timeouts(self):
        """Apply Registry timeout transitions and queue their controller events."""
        # Keep transition and enqueue order atomic with register/heartbeat. The
        # notifier only queues here; its network I/O runs on its own worker.
        with self._transition_lock:
            changed = self.store.expire()
            for rec in changed:
                emit_event("provider_status", provider_id=rec.provider_id, service=rec.service,
                           status="INACTIVE", ip=rec.ip, protocol=rec.protocol, port=rec.port,
                           transition=True, reason="heartbeat_timeout")
                self.notifier.notify(rec, "INACTIVE")
        for rec in changed:
            log.warning("%s timed out -> INACTIVE", rec.provider_id)
        return changed

    def _request_worker(self):
        while True:
            item = self._work_queue.get()
            try:
                if item is None:
                    return
                data, addr = item
                try:
                    self.handle(data, addr)
                except Exception:
                    log.exception("Error handling message from %s", addr)
            finally:
                self._work_queue.task_done()

    def _start_workers(self):
        count = max(1, config.REQUEST_WORKERS)
        self._workers = [threading.Thread(target=self._request_worker,
                                          name=f"registry-worker-{i + 1}")
                         for i in range(count)]
        for worker in self._workers:
            worker.start()

    def _stop_workers(self):
        # Sentinels follow queued requests, so shutdown drains accepted work.
        for _ in self._workers:
            self._work_queue.put(None)
        self._work_queue.join()
        for worker in self._workers:
            worker.join()
        self._workers.clear()

    def serve(self):
        self._start_workers()
        self._timeout_thread = threading.Thread(target=self.timeout_loop,
                                                name="registry-timeout")
        self._timeout_thread.start()
        log.info("Registry listening on UDP %s:%d (timeout=%ss, controller=%s)",
                 config.REGISTRY_HOST, config.REGISTRY_PORT,
                 config.HEARTBEAT_TIMEOUT, config.CONTROLLER_URL)
        emit_event("registry_started", host=config.REGISTRY_HOST, port=config.REGISTRY_PORT,
                   heartbeat_timeout_seconds=config.HEARTBEAT_TIMEOUT,
                   heartbeat_interval_seconds=config.HEARTBEAT_INTERVAL,
                   selection_policy=config.PROVIDER_SELECTION_POLICY)
        try:
            while self.running:
                try:
                    data, addr = self.sock.recvfrom(2048)
                except socket.timeout:
                    continue
                try:
                    self._work_queue.put_nowait((data, addr))
                except queue.Full:
                    log.error("Request queue full; dropping %s from %s", data[:80], addr)
                    try:
                        self.sock.sendto(b"ERROR|SERVER_BUSY", addr)
                    except OSError:
                        log.exception("Could not report busy Registry queue to %s", addr)
        except KeyboardInterrupt:
            log.info("Shutting down")
        finally:
            self.running = False
            self._stop_event.set()
            if self._timeout_thread:
                self._timeout_thread.join()
            self._stop_workers()
            self.sock.close()


if __name__ == "__main__":
    Registry().serve()
