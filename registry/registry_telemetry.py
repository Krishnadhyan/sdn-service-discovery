"""Small bounded JSONL event sink for live dashboard observability."""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

EVENT_LOG = Path(os.getenv("REGISTRY_EVENT_LOG", "/tmp/registry_events.jsonl"))
STATS_FILE = Path(os.getenv("REGISTRY_STATS_FILE", "/tmp/registry_stats.json"))
MAX_BYTES = int(os.getenv("REGISTRY_EVENT_LOG_MAX_BYTES", "2097152"))
_lock = threading.Lock()
SESSION_ID = uuid.uuid4().hex


def emit(kind, **fields):
    """Append a timestamped observed event without affecting Registry behavior."""
    event = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "source": "registry",
        "session_id": SESSION_ID,
        "kind": kind,
        **fields,
    }
    try:
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if EVENT_LOG.exists() and EVENT_LOG.stat().st_size + len(line) > MAX_BYTES:
                content = EVENT_LOG.read_bytes()
                retained = content[-max(1024, MAX_BYTES // 2):]
                boundary = retained.find(b"\n")
                retained = retained[boundary + 1:] if boundary >= 0 else b""
                EVENT_LOG.write_bytes(retained)
            with EVENT_LOG.open("a", encoding="utf-8") as stream:
                stream.write(line)
            _update_stats(event)
    except (OSError, ValueError):
        # Telemetry is deliberately best-effort and cannot interrupt discovery.
        pass
    return event


def touch_liveness():
    """Refresh Registry liveness without generating a discovery packet/event."""
    try:
        with _lock:
            stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            if stats.get("session_id") != SESSION_ID:
                return False
            stats["last_seen_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            temporary = STATS_FILE.with_name(STATS_FILE.name + f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(stats, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, STATS_FILE)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _update_stats(event):
    """Maintain exact current-process counters separately from bounded history."""
    try:
        if event["kind"] == "registry_started":
            stats = {
                "session_id": SESSION_ID, "started_at": event["timestamp"],
                "host": event.get("host"), "port": event.get("port"),
                "selection_policy": event.get("selection_policy"),
                "heartbeat_timeout_seconds": event.get("heartbeat_timeout_seconds"),
                "heartbeat_interval_seconds": event.get("heartbeat_interval_seconds"),
                "last_seen_at": event["timestamp"],
                "counter_scope": "mininet_client_sources_v1",
                "discovery_requests": 0, "selection_counts": {},
                "providers": {},
            }
        else:
            try:
                stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stats = {}
            if stats.get("session_id") != SESSION_ID:
                stats = {
                    "session_id": SESSION_ID, "started_at": event["timestamp"],
                    "selection_policy": None, "heartbeat_timeout_seconds": None,
                    "heartbeat_interval_seconds": None, "discovery_requests": 0,
                    "selection_counts": {}, "providers": {},
                    "counter_scope": "mininet_client_sources_v1",
                }
        if event["kind"] == "discovery":
            if event.get("genuine_client_request"):
                stats["discovery_requests"] = stats.get("discovery_requests", 0) + 1
                selected = event.get("selected_provider")
                if selected:
                    counts = stats.setdefault("selection_counts", {})
                    counts[selected] = counts.get(selected, 0) + 1
        elif event["kind"] == "heartbeat":
            providers = stats.setdefault("providers", {})
            item = providers.setdefault(event["provider_id"], {})
            item["last_heartbeat"] = event["timestamp"]
            item["heartbeat_count"] = item.get("heartbeat_count", 0) + 1
        elif event["kind"] == "provider_status":
            providers = stats.setdefault("providers", {})
            item = providers.setdefault(event["provider_id"], {})
            item.update(status=event["status"], status_timestamp=event["timestamp"],
                        service=event.get("service"), ip=event.get("ip"),
                        protocol=event.get("protocol"), port=event.get("port"))
        temporary = STATS_FILE.with_name(STATS_FILE.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(stats, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, STATS_FILE)
    except (OSError, ValueError, TypeError):
        pass
