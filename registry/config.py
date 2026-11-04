import os

REGISTRY_HOST = os.getenv("REGISTRY_HOST", "0.0.0.0")
REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "5000"))

HEARTBEAT_INTERVAL = 2      # providers send every 2 s (agreed with Teammate 3)
HEARTBEAT_TIMEOUT = float(os.getenv("HEARTBEAT_TIMEOUT", "30"))  # inactive after 30 s by default
CHECK_INTERVAL = 1          # how often the timeout checker runs

# Registry UDP work is concurrent but bounded to cap threads and queued packets.
REQUEST_WORKERS = int(os.getenv("REGISTRY_WORKERS", "8"))
REQUEST_QUEUE_SIZE = int(os.getenv("REGISTRY_QUEUE_SIZE", "64"))

PROVIDER_SELECTION_POLICY = os.getenv(
    "PROVIDER_SELECTION_POLICY", "FIRST_AVAILABLE").strip().upper()
if PROVIDER_SELECTION_POLICY not in {"FIRST_AVAILABLE", "ROUND_ROBIN"}:
    raise ValueError(
        "PROVIDER_SELECTION_POLICY must be FIRST_AVAILABLE or ROUND_ROBIN"
    )

# MUST be agreed with Teammate 2 (see Step 6)
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://127.0.0.1:8080/provider-status")
CONTROLLER_TIMEOUT = 2      # seconds, so a dead controller can't stall us

VALID_SERVICES = {"FILE", "CHAT", "VIDEO"}
VALID_PROTOCOLS = {"TCP", "UDP"}

# Only packets sourced by these dedicated Mininet clients count as client
# discovery workload. Dashboard root-bridge requests and Registry telemetry
# reads are not part of this counter.
MININET_CLIENTS = {
    "h1": "10.0.0.1",
    "h2": "10.0.0.2",
    "h11": "10.0.0.11",
    "h12": "10.0.0.12",
    "h13": "10.0.0.13",
}
MININET_CLIENT_IPS = frozenset(MININET_CLIENTS.values())
