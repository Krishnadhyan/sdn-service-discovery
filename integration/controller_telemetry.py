"""Best-effort bounded event log for observed controller and OpenFlow changes."""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

EVENT_LOG = Path(os.getenv("CONTROLLER_EVENT_LOG", "/tmp/controller_events.jsonl"))
MAX_BYTES = int(os.getenv("CONTROLLER_EVENT_LOG_MAX_BYTES", "1048576"))
_lock = threading.Lock()
SESSION_ID = uuid.uuid4().hex


def emit(kind, **fields):
    event = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "source": "controller",
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
                EVENT_LOG.write_bytes(retained[boundary + 1:] if boundary >= 0 else b"")
            with EVENT_LOG.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except (OSError, ValueError):
        pass
    return event
