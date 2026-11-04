import json
import logging
import queue
import threading
import time
import urllib.request

import config
from registry_telemetry import emit as emit_event

log = logging.getLogger("registry.controller")


class ControllerNotifier:
    """Sends events from a background thread so HTTP problems never block UDP."""

    def __init__(self, url=config.CONTROLLER_URL, timeout=config.CONTROLLER_TIMEOUT,
                 retry_initial_delay=1.0, retry_max_delay=30.0):
        self.url = url
        self.timeout = timeout
        self.retry_initial_delay = retry_initial_delay
        self.retry_max_delay = retry_max_delay
        self._q = queue.Queue()
        threading.Thread(target=self._worker, name="registry-controller-notifier", daemon=True).start()

    def notify(self, rec, status):
        self._q.put({
            "provider_id": rec.provider_id,
            "service": rec.service,
            "status": status,
            "ip": rec.ip,
            "port": rec.port,
            "protocol": rec.protocol,
        })

    def _worker(self):
        while True:
            event = self._q.get()
            try:
                # Retry the head event until acknowledged before sending later
                # transitions, preserving ACTIVE/INACTIVE ordering.
                self._send_with_retry(event)
            finally:
                self._q.task_done()

    def _send_with_retry(self, event, sleep=time.sleep):
        delay = self.retry_initial_delay
        while not self._send(event):
            sleep(delay)
            delay = min(self.retry_max_delay, max(self.retry_initial_delay, delay * 2))

    def _send(self, event):
        emit_event("controller_notification_attempted", provider_id=event["provider_id"],
                   service=event["service"], status=event["status"],
                   ip=event["ip"], protocol=event["protocol"], port=event["port"])
        body = json.dumps(event).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if not 200 <= resp.status < 300:
                    raise RuntimeError("controller returned HTTP %s" % resp.status)
                log.info("Controller event sent: %s %s -> HTTP %s",
                         event["provider_id"], event["status"], resp.status)
                emit_event("controller_notification_acknowledged", provider_id=event["provider_id"],
                           service=event["service"], status=event["status"],
                           ip=event["ip"], protocol=event["protocol"], port=event["port"],
                           http_status=resp.status)
                return True
        except Exception as e:      # connection refused, timeout, HTTP 4xx/5xx...
            emit_event("controller_notification_failed", provider_id=event["provider_id"],
                       service=event["service"], status=event["status"], error=str(e))
            log.error("Controller notification FAILED for %s %s: %s; retrying",
                      event["provider_id"], event["status"], e)
            return False
