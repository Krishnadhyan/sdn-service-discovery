import threading
import time
from dataclasses import dataclass, replace

import config


@dataclass
class Provider:
    provider_id: str
    service: str
    protocol: str
    ip: str
    port: int
    status: str = "ACTIVE"
    last_heartbeat: float = 0.0


class ProviderStore:
    def __init__(self, timeout=config.HEARTBEAT_TIMEOUT, clock=time.monotonic):
        self._providers = {}          # key = provider_id
        self._lock = threading.Lock()
        self._timeout = timeout
        self._clock = clock
        self._selection_counters = {}  # next round-robin index, keyed by service

    def register(self, pid, service, protocol, ip, port):
        """Return (record_copy, status_changed).

        New registrations and transitions from INACTIVE are status changes;
        duplicate registrations while ACTIVE do not emit duplicate events.
        """
        with self._lock:
            old = self._providers.get(pid)
            status_changed = old is None or old.status == "INACTIVE"
            rec = Provider(pid, service, protocol, ip, port, "ACTIVE", self._clock())
            self._providers[pid] = rec
            return replace(rec), status_changed

    def heartbeat(self, pid):
        """Returns (found, became_active, record_copy_or_None)."""
        with self._lock:
            rec = self._providers.get(pid)
            if rec is None:
                return False, False, None
            became_active = rec.status == "INACTIVE"
            rec.status = "ACTIVE"
            rec.last_heartbeat = self._clock()
            return True, became_active, replace(rec)

    def expire(self):
        """Mark timed-out ACTIVE providers INACTIVE. Returns list of copies
        that changed (each provider appears once per transition)."""
        changed = []
        now = self._clock()
        with self._lock:
            for rec in self._providers.values():
                if rec.status == "ACTIVE" and now - rec.last_heartbeat > self._timeout:
                    rec.status = "INACTIVE"
                    changed.append(replace(rec))
        return changed

    def active_for(self, service):
        with self._lock:
            now = self._clock()
            result = [replace(r) for r in self._providers.values()
                      if r.service == service and r.status == "ACTIVE"
                      and now - r.last_heartbeat <= self._timeout]
        return sorted(result, key=lambda r: r.provider_id)   # FILE-1 before FILE-2

    def select_for(self, service, policy="FIRST_AVAILABLE"):
        """Return healthy providers in selection order, atomically advancing RR.

        The complete provider list is retained for compatibility and client
        failover. Under ROUND_ROBIN, its first entry is the registry's choice.
        """
        policy = policy.upper()
        if policy not in {"FIRST_AVAILABLE", "ROUND_ROBIN"}:
            raise ValueError("policy must be FIRST_AVAILABLE or ROUND_ROBIN")

        with self._lock:
            now = self._clock()
            providers = sorted(
                (replace(r) for r in self._providers.values()
                 if r.service == service and r.status == "ACTIVE"
                 and now - r.last_heartbeat <= self._timeout),
                key=lambda r: r.provider_id,
            )
            if policy == "FIRST_AVAILABLE" or not providers:
                return providers

            counter = self._selection_counters.get(service, 0)
            self._selection_counters[service] = counter + 1
            index = counter % len(providers)
            return providers[index:] + providers[:index]

    def snapshot(self):
        with self._lock:
            return [replace(r) for r in self._providers.values()]

