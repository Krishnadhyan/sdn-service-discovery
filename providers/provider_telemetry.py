"""Bounded per-provider JSONL log for actual service responses."""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(os.getenv("PROVIDER_EVENT_LOG_DIR", "/tmp/service_discovery_provider_events"))
MAX_BYTES = int(os.getenv("PROVIDER_EVENT_LOG_MAX_BYTES", "262144"))
_lock = threading.Lock()


def emit(provider_id, kind, **fields):
    event = {
        "id": uuid.uuid4().hex,
        "session_id": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "source": "provider",
        "kind": kind,
        "provider_id": provider_id,
        **fields,
    }
    try:
        path = LOG_DIR / f"{provider_id}.jsonl"
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            if path.exists() and path.stat().st_size + len(line) > MAX_BYTES:
                content = path.read_bytes()[-max(1024, MAX_BYTES // 2):]
                boundary = content.find(b"\n")
                path.write_bytes(content[boundary + 1:] if boundary >= 0 else b"")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except (OSError, ValueError):
        # Observability must not affect service replies or provider lifecycle.
        pass
    return event
