#!/usr/bin/env python3
"""Small real-state dashboard bridge for the Service Discovery System.

Run in the Ubuntu root namespace while the Mininet topology, registry,
providers, and controller are running. Uses only the Python standard library.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import sys
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from workloads import manager as workload_manager

REGISTRY_HOST = os.getenv("REGISTRY_HOST", "10.0.0.3")
REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "5000"))
REGISTRY_LOG = Path(os.getenv("REGISTRY_LOG", "/tmp/registry.log"))
CONTROLLER_LOG = Path(os.getenv("CONTROLLER_LOG", "/tmp/controller.log"))
ACTIVITY_LOG = Path(os.getenv("DASHBOARD_ACTIVITY_LOG", "/tmp/dashboard_activity.jsonl"))
REGISTRY_EVENT_LOG = Path(os.getenv("REGISTRY_EVENT_LOG", "/tmp/registry_events.jsonl"))
REGISTRY_STATS_FILE = Path(os.getenv("REGISTRY_STATS_FILE", "/tmp/registry_stats.json"))
SDN_COMPARISON_FILE = Path(os.getenv("SDN_COMPARISON_FILE", "/tmp/sdn_failure_comparison.json"))
CONTROLLER_EVENT_LOG = Path(os.getenv("CONTROLLER_EVENT_LOG", "/tmp/controller_events.jsonl"))
PROVIDER_EVENT_LOG_DIR = Path(os.getenv("PROVIDER_EVENT_LOG_DIR", "/tmp/service_discovery_provider_events"))
DASHBOARD_EVENT_LOG = Path(os.getenv("DASHBOARD_EVENT_LOG", "/tmp/dashboard_observed_events.jsonl"))
_DASHBOARD_SESSION_ID = uuid.uuid4().hex
LIFECYCLE_TOKEN_FILE = Path(os.getenv("LIFECYCLE_TOKEN_FILE", "/tmp/service_discovery_lifecycle.token"))
LINK_CONTROL_SOCKET = Path(os.getenv("MININET_LINK_CONTROL_SOCKET", "/tmp/service_discovery_mininet.sock"))
LIFECYCLE_PORT = 55000
_activity_lock = threading.Lock()
_lifecycle_lock = threading.Lock()
_lifecycle_requests = {}
_flow_observation_lock = threading.Lock()
_last_provider_drop_state = {}
OVS_OFCTL = os.getenv("OVS_OFCTL", "ovs-ofctl")
KNOWN = {
    "FILE-1": {"service": "FILE", "ip": "10.0.0.4", "protocol": "TCP", "port": 5001, "host": "h4"},
    "FILE-2": {"service": "FILE", "ip": "10.0.0.5", "protocol": "TCP", "port": 5001, "host": "h5"},
    "FILE-3": {"service": "FILE", "ip": "10.0.0.8", "protocol": "TCP", "port": 5001, "host": "h8"},
    "CHAT-1": {"service": "CHAT", "ip": "10.0.0.6", "protocol": "TCP", "port": 5002, "host": "h6"},
    "CHAT-2": {"service": "CHAT", "ip": "10.0.0.9", "protocol": "TCP", "port": 5002, "host": "h9"},
    "VIDEO-1": {"service": "VIDEO", "ip": "10.0.0.7", "protocol": "UDP", "port": 5003, "host": "h7"},
    "VIDEO-2": {"service": "VIDEO", "ip": "10.0.0.10", "protocol": "UDP", "port": 5003, "host": "h10"},
}


def discover(service, timeout=1.5):
    if service not in {"FILE", "CHAT", "VIDEO"}:
        raise ValueError("Unsupported service")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        started = time.perf_counter()
        sock.sendto(f"DISCOVER|{service}".encode(), (REGISTRY_HOST, REGISTRY_PORT))
        try:
            client_ip = sock.getsockname()[0]
        except (AttributeError, OSError):
            client_ip = None
        data, _ = sock.recvfrom(8192)
        latency_ms = (time.perf_counter() - started) * 1000
    raw = data.decode("utf-8", "replace").strip()
    fields = raw.split("|")
    if fields[:2] == ["ERROR", "SERVICE_UNAVAILABLE"]:
        return {"service": service, "raw": raw, "providers": [], "discovery_latency_ms": latency_ms,
                "client_ip": client_ip}
    if len(fields) < 2 or fields[0] != "DISCOVER_RESPONSE" or fields[1] != service:
        raise RuntimeError(f"Unexpected registry response: {raw}")
    providers = []
    for entry in fields[2:]:
        if not entry:
            continue
        bits = entry.split(",")
        if len(bits) != 4:
            raise RuntimeError(f"Malformed provider entry: {entry}")
        pid, protocol, ip, port = bits
        providers.append({"id": pid, "service": service, "protocol": protocol.upper(), "ip": ip, "port": int(port)})
    return {"service": service, "raw": raw, "providers": providers,
            "discovery_latency_ms": latency_ms, "client_ip": client_ip}


def communicate(provider, request, timeout=2.0):
    proto = provider["protocol"].upper()
    if proto == "TCP":
        with socket.create_connection((provider["ip"], int(provider["port"])), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((request.strip() + "\n").encode())
            response = sock.recv(4096).decode("utf-8", "replace").strip()
    elif proto == "UDP":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(request.strip().encode(), (provider["ip"], int(provider["port"])))
            response, _ = sock.recvfrom(4096)
            response = response.decode("utf-8", "replace").strip()
    else:
        raise RuntimeError(f"Unsupported provider protocol: {proto}")
    if not response:
        raise RuntimeError("Provider returned an empty response")
    return response


def request_provider_lifecycle(provider_id, operation, timeout=1.5):
    """Send one fixed, authenticated heartbeat control to a known provider."""
    if not isinstance(provider_id, str) or provider_id not in KNOWN:
        raise ValueError("Unknown provider")
    if not isinstance(operation, str) or operation not in {"offline", "online"}:
        raise ValueError("Unsupported lifecycle operation")
    try:
        token = LIFECYCLE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Provider lifecycle controls are unavailable; start the Mininet topology first") from exc
    if len(token) < 32:
        raise RuntimeError("Provider lifecycle token is invalid")
    verb = "PAUSE_HEARTBEATS" if operation == "offline" else "RESUME_HEARTBEATS"
    request = f"{verb}|{token}|{provider_id}".encode()
    target = (KNOWN[provider_id]["ip"], LIFECYCLE_PORT)
    expected = f"LIFECYCLE_OK|{provider_id}|{'PAUSED' if operation == 'offline' else 'RESUMED'}"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(request, target)
        try:
            data, source = sock.recvfrom(512)
        except socket.timeout as exc:
            raise RuntimeError(
                f"No heartbeat-control acknowledgement from {provider_id} at {target[0]}:{target[1]}. "
                "Restart that provider after topology setup so it loads the lifecycle token and starts its control listener."
            ) from exc
    if source[0] != target[0] or data.decode("utf-8", "replace").strip() != expected:
        raise RuntimeError("Provider returned an invalid lifecycle acknowledgement")
    return {"provider": provider_id, "operation": operation, "acknowledged": True}


def request_mininet_link(operation, timeout=5.0):
    """Ask the topology process to inspect/change its existing s1-s2 link."""
    if operation not in {"status", "down", "up"}:
        raise ValueError("Unsupported s1-s2 link operation")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(LINK_CONTROL_SOCKET))
        sock.sendall(json.dumps({"operation": operation}).encode() + b"\n")
        chunks = bytearray()
        while len(chunks) <= 8192:
            part = sock.recv(8192 - len(chunks) + 1)
            if not part:
                break
            chunks.extend(part)
            if b"\n" in chunks:
                break
    if len(chunks) > 8192 or b"\n" not in chunks:
        raise RuntimeError("Mininet link controller returned an invalid response")
    result = json.loads(bytes(chunks).split(b"\n", 1)[0])
    if not isinstance(result, dict) or result.get("status") not in {"UP", "DOWN", "UNKNOWN"}:
        raise RuntimeError("Mininet link controller returned an invalid state")
    return result


def registry_provider_status(provider_id):
    """Return cached Registry status telemetry; never issue a DISCOVER probe."""
    try:
        stats = json.loads(REGISTRY_STATS_FILE.read_text(encoding="utf-8"))
        item = stats.get("providers", {}).get(provider_id, {})
        return item.get("status") if stats.get("session_id") else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def get_status():
    telemetry = get_telemetry()
    registry_stats = telemetry.get("registry")
    registry_document = None
    try:
        registry_document = json.loads(REGISTRY_STATS_FILE.read_text(encoding="utf-8"))
        if registry_document.get("counter_scope") != "mininet_client_sources_v1":
            registry_document = None
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    last_seen = registry_document.get("last_seen_at") if registry_document else None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_seen)).total_seconds() if last_seen else None
    except (TypeError, ValueError):
        age = None
    registry_ok = bool(registry_stats and age is not None and age <= 4.0)
    registry_error = None if registry_ok else "Registry liveness telemetry unavailable or stale"
    services = {service: {"service": service, "providers": []} for service in ("FILE", "CHAT", "VIDEO")}
    stats_providers = registry_document.get("providers", {}) if registry_document else {}
    for pid, meta in KNOWN.items():
        if stats_providers.get(pid, {}).get("status") == "ACTIVE":
            services[meta["service"]]["providers"].append({"id": pid, **meta})
    providers = []
    flow_check = None
    for pid, meta in KNOWN.items():
        provider_stats = stats_providers.get(pid, {})
        active = provider_stats.get("status") == "ACTIVE"
        status = provider_stats.get("status", "UNKNOWN") if registry_ok else "UNKNOWN"
        reason = provider_stats.get("reason") or ("Registry status telemetry" if registry_ok else "Registry telemetry unavailable")
        lifecycle_status = "UNKNOWN"
        if registry_ok:
            active = status == "ACTIVE"
            with _lifecycle_lock:
                requested = _lifecycle_requests.get(pid)
                if requested == "offline" and not active:
                    _lifecycle_requests.pop(pid, None)
                    requested = None
                elif requested == "online" and active:
                    if flow_check is None:
                        flow_check = get_flows()
                    if flow_check["ok"] and not provider_drop_present(flow_check["output"], meta):
                        _lifecycle_requests.pop(pid, None)
                        requested = None
            lifecycle_status = ("DETECTING FAILURE" if requested == "offline" else
                                "RECOVERING" if requested == "online" else
                                "ONLINE" if active else "OFFLINE")
        else:
            with _lifecycle_lock:
                requested = _lifecycle_requests.get(pid)
            if requested:
                lifecycle_status = "DETECTING FAILURE" if requested == "offline" else "RECOVERING"
        providers.append({"id": pid, **meta, "status": status, "lifecycle_status": lifecycle_status, "reason": reason})
    return {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "registry": {"ok": registry_ok, "host": REGISTRY_HOST, "port": REGISTRY_PORT, "error": registry_error,
                         "selection_policy": registry_stats.get("selection_policy") if registry_stats else None},
            "services": services if registry_ok else {}, "providers": providers}


def get_flows():
    return get_switch_flows("s2")


def get_switch_flows(switch):
    if switch not in {"s1", "s2"}:
        raise ValueError("Unknown switch")
    cmd = [OVS_OFCTL, "-O", "OpenFlow13", "dump-flows", switch]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=4, check=False)
        return {"ok": proc.returncode == 0, "switch": switch, "command": " ".join(cmd),
                "output": (proc.stdout + proc.stderr).strip(), "returncode": proc.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "switch": switch, "command": " ".join(cmd),
                "output": str(exc), "returncode": None}


def get_switch_status():
    cmd = ["ovs-vsctl", "show"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=4, check=False)
        if proc.returncode:
            return {"ok": False, "switches": {}, "error": (proc.stdout + proc.stderr).strip()}
        switches = {}
        current = None
        for line in proc.stdout.splitlines():
            bridge = re.search(r'Bridge\s+"?(s[12])"?\s*$', line.strip())
            if bridge:
                current = bridge.group(1)
                switches[current] = {"connected": None, "target": None}
                continue
            if current:
                target = re.search(r'Controller\s+"([^"]+)"', line.strip())
                if target:
                    switches[current]["target"] = target.group(1)
                connected = re.search(r"is_connected\s*:\s*(true|false)", line.strip(), re.I)
                if connected:
                    switches[current]["connected"] = connected.group(1).lower() == "true"
        return {"ok": True, "switches": switches, "error": None}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "switches": {}, "error": str(exc)}


def provider_drop_present(flow_output, provider):
    """Match the controller's exact priority-100 provider drop entry."""
    ip_proto = "6" if provider["protocol"] == "TCP" else "17"
    for line in flow_output.splitlines():
        if "priority=100" not in line or "actions=drop" not in line:
            continue
        if not any(field in line for field in (f"nw_dst={provider['ip']}", f"ipv4_dst={provider['ip']}")):
            continue
        if f"nw_proto={ip_proto}" not in line and f"{provider['protocol'].lower()}_dst={provider['port']}" not in line:
            continue
        if not any(field in line for field in (f"tp_dst={provider['port']}", f"{provider['protocol'].lower()}_dst={provider['port']}")):
            continue
        return True
    return False


def get_events(limit=40):
    controller_lines = []
    try:
        if CONTROLLER_LOG.exists():
            controller_lines = [line for line in CONTROLLER_LOG.read_text(errors="replace").splitlines()
                                if re.search(r"Handling provider|Installed drop rule|Removed drop rule|s2 is not connected", line, re.I)][-limit:]
    except OSError:
        controller_lines = []
    if not REGISTRY_LOG.exists():
        return {"ok": False, "path": str(REGISTRY_LOG), "lines": [], "controller_lines": controller_lines,
                "error": "Registry log not found. Set REGISTRY_LOG to the real log path."}
    try:
        lines = REGISTRY_LOG.read_text(errors="replace").splitlines()
        # Filter the complete log before limiting results; routine discovery
        # messages should not hide older provider lifecycle events.
        interesting = [
            line for line in lines
            if re.search(r"REGISTER|ACTIVE|INACTIVE|timed out|Controller (?:event sent|notification FAILED)", line, re.I)
        ]
        return {"ok": True, "path": str(REGISTRY_LOG), "lines": interesting[-limit:],
                "controller_lines": controller_lines, "error": None}
    except OSError as exc:
        return {"ok": False, "path": str(REGISTRY_LOG), "lines": [], "controller_lines": controller_lines, "error": str(exc)}


def _read_json_events(path, max_bytes=2_500_000):
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [], str(exc)
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        data = data[data.find(b"\n") + 1:]
    events = []
    for line in data.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("timestamp") and item.get("kind"):
                events.append(item)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return events, None


def get_telemetry():
    registry_events, registry_error = _read_json_events(REGISTRY_EVENT_LOG)
    controller_events, controller_error = _read_json_events(CONTROLLER_EVENT_LOG)
    dashboard_events, dashboard_error = _read_json_events(DASHBOARD_EVENT_LOG)
    provider_events_by_id = {}
    provider_event_errors = {}
    for pid in KNOWN:
        rows, error = _read_json_events(PROVIDER_EVENT_LOG_DIR / f"{pid}.jsonl")
        starts = [event for event in rows if event.get("kind") == "provider_started"]
        if starts:
            provider_session = starts[-1].get("session_id")
            rows = [event for event in rows if event.get("session_id") == provider_session]
        elif rows:
            provider_session = rows[-1].get("session_id")
            rows = [event for event in rows if event.get("session_id") == provider_session]
        provider_events_by_id[pid] = rows
        provider_event_errors[pid] = error
    try:
        registry_stats = json.loads(REGISTRY_STATS_FILE.read_text(encoding="utf-8"))
        if (not isinstance(registry_stats, dict) or not registry_stats.get("session_id")
                or registry_stats.get("counter_scope") != "mininet_client_sources_v1"):
            registry_stats = None
    except (OSError, json.JSONDecodeError):
        registry_stats = None

    def current_session(events, startup_kind):
        startups = [event for event in events if event.get("kind") == startup_kind]
        if not startups:
            if events and events[-1].get("session_id"):
                session = events[-1]["session_id"]
                return [event for event in events if event.get("session_id") == session], None
            return events, None
        session = startups[-1].get("session_id")
        return ([event for event in events if event.get("session_id") == session], startups[-1])

    if registry_stats:
        registry_events = [event for event in registry_events
                           if event.get("session_id") == registry_stats.get("session_id")]
        registry_events = [event for event in registry_events
                           if event.get("kind") not in {"discovery", "discovery_response_sent"}
                           or event.get("genuine_client_request")]
        registry_start = registry_stats
    else:
        registry_events, registry_start = current_session(registry_events, "registry_started")
    controller_events, controller_start = current_session(controller_events, "controller_started")
    dashboard_events = [event for event in dashboard_events
                        if event.get("session_id") == _DASHBOARD_SESSION_ID]
    providers = {}
    for pid in KNOWN:
        providers[pid] = {"last_heartbeat": None, "heartbeat_count": None,
                          "heartbeat_history": [], "status_event": None,
                          "controller_events": [],
                          "service_events": provider_events_by_id[pid],
                          "service_events_available": provider_event_errors[pid] is None}
    discoveries = []
    for event in registry_events:
        pid = event.get("provider_id")
        if pid in providers and event.get("kind") == "heartbeat":
            data = providers[pid]
            data["last_heartbeat"] = event["timestamp"]
            data["heartbeat_history"].append(event)
            data["heartbeat_history"] = data["heartbeat_history"][-30:]
        elif pid in providers and event.get("kind") == "provider_status":
            providers[pid]["status_event"] = event
        if event.get("kind") == "discovery":
            discoveries.append(event)
    for event in controller_events:
        pid = event.get("provider_id")
        if pid in providers:
            providers[pid]["controller_events"].append(event)

    if registry_start:
        for pid, stats in registry_start.get("providers", {}).items():
            if pid in providers:
                providers[pid]["last_heartbeat"] = stats.get("last_heartbeat")
                providers[pid]["heartbeat_count"] = stats.get("heartbeat_count", 0)
                if stats.get("status"):
                    providers[pid]["status_event"] = {
                        "timestamp": stats.get("status_timestamp"),
                        "kind": "provider_status", "provider_id": pid,
                        "status": stats["status"], "reason": stats.get("reason"),
                    }

    selection_counts = {pid: 0 for pid in KNOWN}
    if registry_start:
        selection_counts.update(registry_start.get("selection_counts", {}))
    all_provider_events = [event for rows in provider_events_by_id.values() for event in rows]
    event_list = sorted(registry_events + controller_events + dashboard_events + all_provider_events,
                        key=lambda event: event.get("timestamp", ""))
    return {
        "availability": {
            "registry_events": registry_error is None and registry_start is not None,
            "registry_stats": registry_stats is not None,
            "controller_events": controller_error is None,
            "dashboard_events": dashboard_error is None,
            "provider_events": {pid: provider_event_errors[pid] is None for pid in KNOWN},
            "registry_error": registry_error,
            "controller_error": controller_error,
        },
        "registry": ({
            "selection_policy": registry_start.get("selection_policy"),
            "heartbeat_timeout_seconds": registry_start.get("heartbeat_timeout_seconds"),
            "heartbeat_interval_seconds": registry_start.get("heartbeat_interval_seconds"),
            "host": registry_start.get("host"), "port": registry_start.get("port"),
            "discovery_requests": registry_start.get("discovery_requests", len(discoveries)),
            "selection_counts": selection_counts,
        } if registry_start else None),
        "providers": providers if registry_start else None,
        "discovery_events": discoveries[-250:] if registry_start else None,
        "events": event_list[-250:],
        "controller": ({
            "openflow_version": (controller_start or {}).get("openflow_version"),
            "http_port": (controller_start or {}).get("http_port"),
        } if controller_start or controller_events else None),
    }


def get_controller_status():
    switches = get_switch_status()
    flows = {switch: get_switch_flows(switch) for switch in ("s1", "s2")}
    rules = {}
    for pid, provider in KNOWN.items():
        flow_dump = flows["s2"]
        rules[pid] = {
            "available": flow_dump["ok"],
            "drop_present": provider_drop_present(flow_dump["output"], provider) if flow_dump["ok"] else None,
            "priority": 100 if flow_dump["ok"] and provider_drop_present(flow_dump["output"], provider) else None,
            "switch": "s2",
        }
    _observe_provider_rule_changes(rules)
    return {"switches": switches, "flows": flows, "provider_rules": rules}


def get_sdn_comparison():
    try:
        result = json.loads(SDN_COMPARISON_FILE.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("comparison file must contain a JSON object")
        return {"ok": True, "comparison": result}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "comparison": None, "error": str(exc)}


def _observe_provider_rule_changes(rules):
    """Record only provider-drop state changes seen in successful live OVS dumps."""
    try:
        DASHBOARD_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        DASHBOARD_EVENT_LOG.touch(exist_ok=True)
    except OSError:
        return
    with _flow_observation_lock:
        for pid, rule in rules.items():
            if not rule["available"]:
                continue
            present = bool(rule["drop_present"])
            previous = _last_provider_drop_state.get(pid)
            _last_provider_drop_state[pid] = present
            # The first dump establishes an observed baseline; it is not a
            # transition and must not be presented as an install/removal event.
            if previous is None or previous == present:
                continue
            event = {
                "id": uuid.uuid4().hex,
                "session_id": _DASHBOARD_SESSION_ID,
                "source": "ovs_observer",
                "kind": "ovs_provider_drop_present_observed" if present else "ovs_provider_drop_absent_observed",
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "provider_id": pid, "ip": KNOWN[pid]["ip"],
                "protocol": KNOWN[pid]["protocol"], "port": KNOWN[pid]["port"],
                "switch": "s2", "priority": 100,
            }
            try:
                line = json.dumps(event, separators=(",", ":")) + "\n"
                if DASHBOARD_EVENT_LOG.stat().st_size + len(line) > 524288:
                    content = DASHBOARD_EVENT_LOG.read_bytes()[-262144:]
                    boundary = content.find(b"\n")
                    DASHBOARD_EVENT_LOG.write_bytes(content[boundary + 1:] if boundary >= 0 else b"")
                with DASHBOARD_EVENT_LOG.open("a", encoding="utf-8") as stream:
                    stream.write(line)
            except OSError:
                continue


def record_activity(service, request, provider, success, response=None, error=None,
                    client_ip=None):
    """Append one real manual dashboard transaction result to JSONL history."""
    event = {"id": uuid.uuid4().hex, "kind": "service_traffic",
             "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             "service": service, "request": request, "provider": provider,
             "success": bool(success), "response": response, "error": error,
             "client_ip": client_ip, "provider_ip": KNOWN.get(provider, {}).get("ip")}
    with _activity_lock:
        ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACTIVITY_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def record_lifecycle_activity(provider, operation):
    event = {"id": uuid.uuid4().hex, "kind": "lifecycle_request",
             "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             "provider": provider, "operation": operation,
             "message": "Heartbeat pause requested" if operation == "offline" else "Heartbeat resume requested"}
    with _activity_lock:
        ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACTIVITY_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def get_activities(limit=40):
    try:
        if not ACTIVITY_LOG.exists():
            return {"ok": True, "path": str(ACTIVITY_LOG), "events": [], "error": None}
        events = []
        for line in ACTIVITY_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"ok": True, "path": str(ACTIVITY_LOG), "events": events[-limit:], "error": None}
    except OSError as exc:
        return {"ok": False, "path": str(ACTIVITY_LOG), "events": [], "error": str(exc)}


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Service Discovery · Control Room</title>
<style>
:root{--bg:#0b1020;--panel:#121a2d;--panel2:#172239;--line:#293750;--text:#edf3ff;--muted:#91a2be;--cyan:#6ce5d2;--blue:#80aaff;--green:#50d6a0;--red:#ff7d8b;--amber:#ffce75;--shadow:0 18px 55px #0003}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 20% -10%,#1d3658 0,transparent 40%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}button,input,select{font:inherit}button{border:1px solid #3a5071;background:#223452;color:var(--text);padding:10px 13px;border-radius:10px;cursor:pointer;transition:.15s}button:hover{border-color:var(--cyan);transform:translateY(-1px)}button.primary{background:var(--cyan);border-color:var(--cyan);color:#07171a;font-weight:750}button:disabled{opacity:.5;cursor:wait}.shell{max-width:1440px;margin:auto;padding:26px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:24px}.brand{display:flex;align-items:center;gap:13px}.logo{width:44px;height:44px;border-radius:13px;background:linear-gradient(135deg,var(--cyan),var(--blue));display:grid;place-items:center;color:#091626;font-size:22px;font-weight:900}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--cyan);font-weight:800}.top h1{font-size:24px;margin:2px 0 0;letter-spacing:-.04em}.top p{margin:0;color:var(--muted);font-size:13px}.actions{display:flex;gap:9px;flex-wrap:wrap}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:16px}.stat,.panel{border:1px solid var(--line);border-radius:17px;background:linear-gradient(145deg,#141e32,#10182a);box-shadow:var(--shadow)}.stat{padding:17px 18px}.stat-head{display:flex;justify-content:space-between;align-items:center;color:var(--muted);font-size:12px}.stat-value{font-size:28px;font-weight:780;letter-spacing:-.05em;margin-top:7px}.stat-foot{font-size:12px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--green);box-shadow:0 0 12px currentColor;margin-right:6px}.dot.bad{background:var(--red)}.layout{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(360px,.85fr);gap:16px}.panel{padding:20px;min-width:0}.panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:17px}.panel h2{font-size:16px;margin:0;letter-spacing:-.02em}.sub{font-size:12px;color:var(--muted);margin-top:4px}.tag{border:1px solid var(--line);border-radius:999px;padding:4px 9px;font-size:11px;color:var(--muted);white-space:nowrap}.providers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.provider{padding:14px;border:1px solid var(--line);background:#10182a;border-radius:13px}.provider-controls{display:flex;gap:7px;margin-top:12px;flex-wrap:wrap}.provider-controls button{padding:7px 9px;font-size:11px}.provider-top{display:flex;align-items:center;justify-content:space-between;gap:8px}.pid{font-weight:800}.pill{font-size:10px;font-weight:800;letter-spacing:.06em;border-radius:999px;padding:4px 8px}.ACTIVE,.ONLINE{color:#8ff0c4;background:#143a34}.INACTIVE,.OFFLINE{color:#ff9ca7;background:#3b202c}.UNKNOWN{color:#ffcf83;background:#40331c}.DETECTING_FAILURE{color:#ffcf83;background:#40331c}.RECOVERING{color:#80aaff;background:#1e2e4b}.meta{font-size:12px;color:var(--muted);margin-top:7px}.service-badge{display:inline-block;margin-top:11px;color:var(--blue);font-size:10px;letter-spacing:.1em;font-weight:800}.service-list{display:grid;gap:10px}.service-row{border:1px solid var(--line);border-radius:12px;padding:13px;display:flex;justify-content:space-between;gap:12px;align-items:center}.service-name{font-weight:750}.service-details{font-size:12px;color:var(--muted)}.service-row button{padding:8px 11px;font-size:12px}.comm-form{display:grid;grid-template-columns:130px minmax(0,1fr) auto;gap:9px;margin-top:15px}.comm-form input,.comm-form select{min-width:0;background:#0d1526;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px}.latency{margin:0 0 14px;padding:12px 14px;border:1px solid #286258;background:linear-gradient(110deg,#12332f,#101c2b);border-radius:12px;display:flex;align-items:center;justify-content:space-between;gap:12px}.latency-label{font-size:11px;color:var(--cyan);font-weight:800;letter-spacing:.12em;text-transform:uppercase}.latency-value{font-size:24px;font-weight:800;letter-spacing:-.04em}.latency.pending .latency-value{font-size:13px;color:var(--muted);font-weight:500;letter-spacing:0}.result{margin-top:12px;background:#0a1323;border:1px solid var(--line);border-radius:11px;padding:12px;font:12px/1.6 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;overflow-wrap:anywhere;min-height:44px;color:#c7f7e8}.result.muted{color:var(--muted)}.timeline{display:grid;gap:10px;max-height:300px;overflow:auto}.event{display:grid;grid-template-columns:10px 1fr;gap:10px;font-size:12px}.event .rail{position:relative;display:flex;justify-content:center}.event .rail:after{content:"";position:absolute;top:12px;bottom:-12px;width:1px;background:var(--line)}.event:last-child .rail:after{display:none}.event .rail i{display:block;width:7px;height:7px;background:var(--cyan);border-radius:50%;margin-top:5px;z-index:1}.event code{color:#c5d3ea;overflow-wrap:anywhere}.empty{color:var(--muted);font-size:12px;padding:15px 0}.wide{grid-column:1/-1}.flowbox{background:#0a1323;border:1px solid var(--line);border-radius:11px;padding:13px;overflow:auto;max-height:260px;white-space:pre;font:12px/1.6 ui-monospace,SFMono-Regular,monospace;color:#d2e3ff}.foot{margin-top:18px;display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:11px}.notice{padding:10px 12px;border:1px solid #66502b;background:#2b2418;color:#f4d49b;border-radius:10px;font-size:12px;margin-bottom:15px}.loading{opacity:.7}@media(max-width:950px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.shell{padding:14px}.top{align-items:flex-start;flex-direction:column}.providers{grid-template-columns:1fr}.comm-form{grid-template-columns:1fr}.stats{gap:8px}.stat{padding:12px}.stat-value{font-size:23px}.panel{padding:14px}}
</style>
<style>
.topology-panel{margin-bottom:16px;overflow:hidden}.topology-wrap{overflow:auto;background:radial-gradient(ellipse at 52% 45%,#172740 0,transparent 72%),#0b1424;border:1px solid var(--line);border-radius:13px}.topology{display:block;width:100%;min-width:900px;height:auto}.net-link{stroke:#405676;stroke-width:3;fill:none}.net-link.inter{stroke:var(--cyan);stroke-width:4}.control-link{stroke:#7788a5;stroke-width:1.5;stroke-dasharray:5 6;fill:none}.topo-node rect{fill:#15233a;stroke:#617797;stroke-width:1.5;rx:10}.topo-node.switch rect{fill:#16354a;stroke:var(--cyan);stroke-width:2}.topo-node.registry rect{fill:#233151;stroke:var(--blue)}.topo-node.controller rect{fill:#272b45;stroke:#9e91ff;stroke-dasharray:5 4}.topo-node text{fill:var(--text);font:700 14px Inter,system-ui,sans-serif}.topo-node .small{fill:#9eafca;font-size:10px;font-weight:500}.topo-node.provider-node{cursor:pointer}.topo-node.provider-node.active rect{stroke:var(--green)}.topo-node.provider-node.inactive rect{stroke:var(--red);fill:#351f2b}.topo-node.provider-node.unknown rect{stroke:var(--amber)}.topo-node.selected rect{stroke:#fff;stroke-width:3}.topo-label{fill:#8295b1;font:10px ui-monospace,monospace}.packet{pointer-events:none}.heartbeat-packet{fill:var(--cyan);filter:drop-shadow(0 0 5px var(--cyan))}.discovery-packet,.request-packet{fill:var(--blue);filter:drop-shadow(0 0 5px var(--blue))}.response-packet{fill:var(--green);filter:drop-shadow(0 0 5px var(--green))}.service-packet{fill:var(--amber);filter:drop-shadow(0 0 5px var(--amber))}.status-packet{fill:#c2a9ff;filter:drop-shadow(0 0 5px #c2a9ff)}.topology-legend{display:flex;gap:15px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-top:12px}.legend-item{display:flex;align-items:center;gap:6px}.legend-line{width:22px;height:0;border-top:2px solid var(--blue)}.legend-line.heartbeat{border-color:var(--cyan);border-top-style:dashed}.legend-line.service{border-color:var(--amber)}.legend-line.status{border-color:#c2a9ff;border-top-style:dashed}.telemetry-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.metric-row{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:8px 0;font-size:12px}.metric-row span:last-child{color:var(--cyan);font-variant-numeric:tabular-nums}.selection-bars{display:grid;gap:7px}.selection-bar{display:grid;grid-template-columns:70px 1fr 35px;align-items:center;gap:8px;font-size:11px;color:var(--muted)}.selection-bar-track{height:7px;background:#26354c;border-radius:8px;overflow:hidden}.selection-bar-fill{height:100%;background:linear-gradient(90deg,#55cbb8,#80aaff)}.detail-box{background:#0b1424;border:1px solid var(--line);border-radius:12px;padding:13px;min-height:130px}.detail-box h3{margin:0 0 8px;font-size:14px}.detail-history{max-height:190px;overflow:auto;display:grid;gap:7px}.mini-event{border-left:2px solid var(--cyan);padding-left:9px;font-size:11px;color:var(--muted)}.mini-event.failure{border-color:var(--red)}.source-badge{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--cyan);border:1px solid #36586b;border-radius:999px;padding:3px 7px}.flow-provider{border-bottom:1px solid var(--line);padding:8px 0;font:11px/1.5 ui-monospace,monospace}.flow-provider:last-child{border:0}.unavailable{color:var(--amber)}.event-type{display:inline-block;color:var(--cyan);font-size:9px;font-weight:800;letter-spacing:.08em;margin-right:6px}.topology-caption{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:0 0 12px}
</style><style>
.latency-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.latency-grid>.latency{min-width:0}@media(max-width:600px){.latency-grid{grid-template-columns:1fr}}
.provider .hb-widget{display:flex;align-items:center;gap:9px;margin-top:12px;font-size:11px;color:var(--muted)}.provider .hb-ring{display:block;width:38px;height:38px;min-width:38px;max-width:38px;flex:0 0 38px;overflow:visible;transform:rotate(-90deg)}.provider .hb-track,.provider .hb-progress{fill:none;stroke-width:3}.provider .hb-track{stroke:#293750}.provider .hb-progress{stroke:var(--green);stroke-linecap:round;transition:stroke-dashoffset .4s,stroke .4s}.provider .hb-center{transform:rotate(90deg);transform-origin:18px 18px;fill:var(--text);font:8px ui-monospace,monospace;text-anchor:middle}
html,body{height:100%;overflow:hidden}.shell{width:100%;max-width:none;height:100dvh;padding:0;display:grid;grid-template-columns:minmax(0,1fr)}.workspace{width:100%;height:100dvh;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0;align-items:stretch}.topology-column{position:relative;top:auto;min-width:0;height:100dvh;overflow:hidden}.topology-panel{height:100%;min-height:0;margin:0;border-radius:0;padding:16px;display:flex;flex-direction:column}.topology-wrap{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden}.topology{display:block;width:100%;height:100%;min-width:0;object-fit:contain}.topology-legend{flex:none}.right-scroll{height:100dvh;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:18px 20px 12px}.right-column{min-width:0}.right-scroll>.top{margin-bottom:12px}.right-scroll>.stats{margin:0 0 16px;grid-template-columns:repeat(5,minmax(0,1fr))}.tabbar{position:sticky;top:-18px;z-index:5;padding:12px 0;background:var(--bg);border-bottom:1px solid var(--line)}.topology-column .panel{margin-top:0}@media(max-width:900px){html,body{height:auto;overflow:auto}.shell{height:auto;min-height:100dvh;display:block;padding:12px}.workspace{height:auto;min-height:0;display:flex;flex-direction:column;gap:12px}.topology-column{height:65vh;min-height:420px;position:relative}.topology-panel{border-radius:16px}.right-scroll{height:auto;overflow:visible;padding:0}.right-scroll>.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.tabbar{position:static;padding:8px 0}.topology{min-width:0}}
</style><style>.manual-form{grid-template-columns:repeat(5,minmax(0,1fr));align-items:end}.manual-form label{display:grid;gap:5px;color:var(--muted);font-size:12px}.workload-form{grid-template-columns:repeat(3,minmax(0,1fr));align-items:end}.workload-form label{display:grid;gap:5px;color:var(--muted);font-size:12px}.workload-form fieldset{grid-column:1/-1;border:1px solid var(--line);border-radius:10px;display:flex;gap:14px;flex-wrap:wrap}.workload-form fieldset label{display:flex;align-items:center;gap:5px}.request-packet{fill:var(--blue);filter:drop-shadow(0 0 5px var(--blue))}.response-packet{fill:#b39bff;filter:drop-shadow(0 0 5px #b39bff)}.manual-highlight rect{stroke:var(--cyan)!important;stroke-width:3!important;filter:drop-shadow(0 0 7px var(--cyan))}.manual-path{stroke:var(--cyan)!important;stroke-width:5!important;filter:drop-shadow(0 0 5px var(--cyan))}.manual-service-path{stroke:var(--amber)!important;stroke-width:5!important;filter:drop-shadow(0 0 5px var(--amber))}@media(max-width:600px){.manual-form,.workload-form{grid-template-columns:1fr}}</style></head><body><main class="shell">
<header class="top"><div class="brand"><div class="logo">⇄</div><div><div class="eyebrow">T3 · Live integration</div><h1>Service Discovery Control Room</h1><p>Registry-backed discovery · direct provider communication · real OpenFlow evidence</p></div></div><div class="actions"><button id="refresh">↻ Refresh state</button><button id="flowsBtn">Inspect flow table</button></div></header>
<div id="notice" class="notice">Live values are queried from the Registry and Open vSwitch. A missing provider is marked inactive only when the Registry responds successfully.</div>
<section class="panel topology-panel">
<div class="topology-caption"><div><h2>Network topology</h2><div class="sub">Mininet host inventory and switch links · provider state from Registry replies</div></div><span id="telemetrySource" class="source-badge">Awaiting telemetry</span></div>
<div class="topology-wrap"><svg id="topology" class="topology" viewBox="0 0 1200 550" role="img" aria-label="Service discovery Mininet topology">
<path class="net-link client-link" data-client-link="h1" d="M110 77 L385 265"/><path class="net-link client-link" data-client-link="h2" d="M110 142 L385 265"/><path class="net-link client-link" data-client-link="h11" d="M110 207 L385 265"/><path class="net-link client-link" data-client-link="h12" d="M110 272 L385 265"/><path class="net-link client-link" data-client-link="h13" d="M110 337 L385 265"/>
<path class="net-link registry-link" d="M450 300 L450 455"/><path class="net-link inter-link inter" d="M515 265 L715 265"/><path class="net-link" d="M515 265 L590 450"/>
<path class="net-link provider-link" data-provider-link="FILE-1" d="M845 265 L1000 65"/><path class="net-link provider-link" data-provider-link="FILE-2" d="M845 265 L1000 130"/><path class="net-link provider-link" data-provider-link="CHAT-1" d="M845 265 L1000 260"/><path class="net-link provider-link" data-provider-link="VIDEO-1" d="M845 265 L1000 390"/><path class="net-link provider-link" data-provider-link="FILE-3" d="M845 265 L1000 195"/><path class="net-link provider-link" data-provider-link="CHAT-2" d="M845 265 L1000 325"/><path class="net-link provider-link" data-provider-link="VIDEO-2" d="M845 265 L1000 455"/>
<path class="control-link" d="M660 105 L470 230 M720 105 L790 230"/>
<g class="topo-node client" data-client="h1"><rect x="30" y="58" width="80" height="38"/><text x="70" y="75" text-anchor="middle">h1</text><text class="small" x="70" y="89" text-anchor="middle">10.0.0.1</text></g>
<g class="topo-node client" data-client="h2"><rect x="30" y="123" width="80" height="38"/><text x="70" y="140" text-anchor="middle">h2</text><text class="small" x="70" y="154" text-anchor="middle">10.0.0.2</text></g>
<g class="topo-node client" data-client="h11"><rect x="30" y="188" width="80" height="38"/><text x="70" y="205" text-anchor="middle">h11</text><text class="small" x="70" y="219" text-anchor="middle">10.0.0.11</text></g>
<g class="topo-node client" data-client="h12"><rect x="30" y="253" width="80" height="38"/><text x="70" y="270" text-anchor="middle">h12</text><text class="small" x="70" y="284" text-anchor="middle">10.0.0.12</text></g>
<g class="topo-node client" data-client="h13"><rect x="30" y="318" width="80" height="38"/><text x="70" y="335" text-anchor="middle">h13</text><text class="small" x="70" y="349" text-anchor="middle">10.0.0.13</text></g>
<g class="topo-node switch"><rect x="385" y="230" width="130" height="70"/><text x="450" y="259" text-anchor="middle">s1</text><text class="small" x="450" y="278" text-anchor="middle">OpenFlow 1.3</text></g>
<g class="topo-node switch"><rect x="715" y="230" width="130" height="70"/><text x="780" y="259" text-anchor="middle">s2</text><text class="small" x="780" y="278" text-anchor="middle">OpenFlow 1.3</text></g>
<g class="topo-node registry" data-registry-ip="10.0.0.3"><rect x="385" y="455" width="130" height="55"/><text x="450" y="479" text-anchor="middle">h3 · Registry</text><text class="small" x="450" y="497" text-anchor="middle">10.0.0.3 · UDP 5000</text></g>
<g class="topo-node controller"><rect x="600" y="45" width="120" height="60"/><text x="660" y="70" text-anchor="middle">SDN Controller</text><text class="small" x="660" y="89" text-anchor="middle">127.0.0.1:6653</text></g>
<g class="topo-node"><rect x="550" y="450" width="140" height="55"/><text x="620" y="474" text-anchor="middle">VM API bridge</text><text class="small" x="620" y="492" text-anchor="middle">10.0.0.254</text></g>
<g class="topo-node provider-node unknown" data-provider="FILE-1" transform="translate(1000 46)"><rect width="160" height="38"/><text x="10" y="16">h4 · FILE-1</text><text class="small" x="10" y="31">10.0.0.4 · TCP/5001</text></g>
<g class="topo-node provider-node unknown" data-provider="FILE-2" transform="translate(1000 111)"><rect width="160" height="38"/><text x="10" y="16">h5 · FILE-2</text><text class="small" x="10" y="31">10.0.0.5 · TCP/5001</text></g>
<g class="topo-node provider-node unknown" data-provider="CHAT-1" transform="translate(1000 241)"><rect width="160" height="38"/><text x="10" y="16">h6 · CHAT-1</text><text class="small" x="10" y="31">10.0.0.6 · TCP/5002</text></g>
<g class="topo-node provider-node unknown" data-provider="VIDEO-1" transform="translate(1000 371)"><rect width="160" height="38"/><text x="10" y="16">h7 · VIDEO-1</text><text class="small" x="10" y="31">10.0.0.7 · UDP/5003</text></g>
<g class="topo-node provider-node unknown" data-provider="FILE-3" transform="translate(1000 176)"><rect width="160" height="38"/><text x="10" y="16">h8 · FILE-3</text><text class="small" x="10" y="31">10.0.0.8 · TCP/5001</text></g>
<g class="topo-node provider-node unknown" data-provider="CHAT-2" transform="translate(1000 306)"><rect width="160" height="38"/><text x="10" y="16">h9 · CHAT-2</text><text class="small" x="10" y="31">10.0.0.9 · TCP/5002</text></g>
<g class="topo-node provider-node unknown" data-provider="VIDEO-2" transform="translate(1000 436)"><rect width="160" height="38"/><text x="10" y="16">h10 · VIDEO-2</text><text class="small" x="10" y="31">10.0.0.10 · UDP/5003</text></g>
<text class="topo-label service-group-label" x="1000" y="38">FILE SERVICES</text><text class="topo-label service-group-label" x="1000" y="233">CHAT SERVICES</text><text class="topo-label service-group-label" x="1000" y="363">VIDEO SERVICES</text><g id="packets"></g><text class="topo-label" x="560" y="250">s1 ↔ s2</text><text class="topo-label" x="30" y="380">CLIENTS</text><text class="topo-label" x="1000" y="505">PROVIDER HOSTS</text></svg></div>
<div class="topology-legend"><span class="legend-item"><i class="legend-line heartbeat"></i>Heartbeat provider → Registry</span><span class="legend-item"><i class="legend-line"></i>Discovery request / response</span><span class="legend-item"><i class="legend-line service"></i>Observed service traffic</span><span class="legend-item"><i class="legend-line status"></i>Registry → controller status</span><span class="legend-item">Moving markers appear only for newly observed events</span></div></section>
<section class="stats"><div class="stat"><div class="stat-head">REGISTRY <span id="regDot" class="dot bad"></span></div><div id="regValue" class="stat-value">Checking…</div><div id="regFoot" class="stat-foot">UDP discovery endpoint</div></div><div class="stat"><div class="stat-head">DISCOVERY REQUESTS</div><div id="discoveryCount" class="stat-value">—</div><div class="stat-foot">Requests from h1, h2, h11, h12 or h13</div></div><div class="stat"><div class="stat-head">ACTIVE / INACTIVE</div><div id="activeValue" class="stat-value">—</div><div id="inactiveValue" class="stat-foot">Awaiting Registry</div></div><div class="stat"><div class="stat-head">SELECTION POLICY</div><div id="policyValue" class="stat-value" style="font-size:18px">—</div><div class="stat-foot">Registry process configuration</div></div><div class="stat"><div class="stat-head">OPENFLOW S1 / S2 <span id="flowDot" class="dot"></span></div><div id="flowValue" class="stat-value">Not checked</div><div class="stat-foot">Live OVS state</div></div></section>
<div class="layout">
<section class="panel"><div class="panel-head"><div><h2>Provider detail</h2><div class="sub">Select a provider in the topology or inventory</div></div><span id="selectedProviderTag" class="tag">No selection</span></div><div id="providerDetail" class="detail-box"><div class="empty">Provider telemetry has not been loaded.</div></div></section>
<section class="panel"><div class="panel-head"><div><h2>Registry selection distribution</h2><div class="sub">First provider in each Registry response from a Mininet client, current session</div></div><span id="selectionPolicyTag" class="tag">Unavailable</span></div><div id="selectionDistribution" class="selection-bars"><div class="empty">Registry telemetry unavailable.</div></div></section>
<section class="panel"><div class="panel-head"><div><h2>Controller and switches</h2><div class="sub">OVS connection state and live flow table queries</div></div><button id="controllerBtn">Refresh controller</button></div><div id="switchStatus"><div class="empty">Controller state not queried.</div></div><div id="providerRules" class="flowbox" style="margin-top:12px">Provider drop rules are read from s2.</div></section>
<section class="panel"><div class="panel-head"><div><h2>Provider inventory</h2><div class="sub">Seven configured endpoints · state derived from Registry discovery</div></div><span class="tag" id="updatedAt">Waiting for data</span></div><div id="providers" class="providers"><div class="empty">Connecting to Registry…</div></div></section>
<section class="panel"><div class="panel-head"><div><h2>Discover a service</h2><div class="sub">One client operation through the Mininet namespace and Registry</div></div><span class="tag">UDP :5000</span></div><div class="latency-grid"><div id="discoveryLatency" class="latency pending"><div><div class="latency-label">Discovery latency</div><div id="latencyValue" class="latency-value">Waiting for Registry reply</div></div><span class="tag">Registry round trip</span></div><div id="operationLatency" class="latency pending"><div><div class="latency-label">End-to-end latency</div><div id="operationLatencyValue" class="latency-value">Waiting for operation</div></div><span id="operationLatencyScope" class="tag">Discovery + provider response</span></div></div><div class="service-list" id="services"><div class="empty">Registry service state is shown here.</div></div><div class="comm-form manual-form"><label>Client<select id="manualClient"><option value="h1">h1 · 10.0.0.1</option><option value="h2">h2 · 10.0.0.2</option><option value="h11">h11 · 10.0.0.11</option><option value="h12">h12 · 10.0.0.12</option><option value="h13">h13 · 10.0.0.13</option></select></label><label>Service<select id="service"><option>FILE</option><option>CHAT</option><option>VIDEO</option></select></label><label>Request<input id="request" value="PING" maxlength="512" aria-label="Request payload"></label><button id="discoverOnly">Discover Only</button><button id="send" class="primary">Discover &amp; Send</button></div><pre id="commResult" class="result muted">Choose a client and service, then run a manual operation.</pre></section>
<section class="panel"><div class="panel-head"><div><h2>Mininet client workload</h2><div class="sub">Real service requests originate in selected client namespaces · static mode is direct FILE-1</div></div><span class="tag" id="workloadPolicy">Policy unavailable</span></div><div class="comm-form workload-form"><fieldset id="workloadClients"><legend>Clients</legend><label><input type="checkbox" value="h1"> h1 · 10.0.0.1</label><label><input type="checkbox" value="h2"> h2 · 10.0.0.2</label><label><input type="checkbox" value="h11"> h11 · 10.0.0.11</label><label><input type="checkbox" value="h12"> h12 · 10.0.0.12</label><label><input type="checkbox" value="h13"> h13 · 10.0.0.13</label></fieldset><label>Experiment preset<select id="workloadPreset"><option value="A">A · 1 client × 20</option><option value="B">B · 3 clients × 20</option><option value="C">C · 5 clients × 20</option></select></label><label>Service<select id="workloadService"><option>FILE</option><option>CHAT</option><option>VIDEO</option></select></label><label>Requests / client<input id="workloadRequests" type="number" min="1" max="500" value="50"></label><label>Concurrency / client<input id="workloadConcurrency" type="number" min="1" max="10" value="1"></label><label>Timeout (seconds)<input id="workloadTimeout" type="number" min="0.1" max="30" step="0.1" value="2"></label><label>Execution<select id="workloadMode"><option value="concurrent">Concurrent clients</option><option value="sequential">Sequential clients</option></select></label><label>Discovery mode<select id="workloadExecutionMode"><option value="dynamic">Dynamic · Registry</option><option value="static">Static · direct FILE-1</option></select></label><button id="workloadStart" class="primary">Start workload</button><button id="workloadStop" disabled>Stop workload</button></div><div id="workloadProgress" class="result muted">Choose static or dynamic mode and run a preset on the same clients for a comparison.</div><div class="flowbox" style="margin-top:10px;max-height:260px;overflow:auto" id="workloadResults">No workload results recorded.</div></section>
<section class="panel"><div class="panel-head"><div><h2>Failure & recovery events</h2><div class="sub">Registry lifecycle and controller outcomes, plus manual dashboard transactions</div></div><button id="eventsBtn">Refresh events</button></div><div id="events" class="timeline"><div class="empty">Loading events…</div></div></section>
<section class="panel" id="networkFault"><div class="panel-head"><div><h2>Network Fault Injection</h2><div class="sub">Partition and restore the existing Mininet inter-switch link.</div></div><span class="tag">Target: s1 ↔ s2</span></div><div class="metric-row"><span>Current link status</span><strong id="networkLinkStatus">UNKNOWN</strong></div><div id="networkLinkInterfaces" class="sub">Interface names unavailable</div><div class="actions" style="margin-top:12px"><button id="linkDownButton">Take Link Down</button><button id="linkUpButton">Restore Link</button></div><div id="networkLinkMessage" class="result muted" aria-live="polite">Checking the actual Mininet link state…</div></section>
<section class="panel"><div class="panel-head"><div><h2>OpenFlow evidence</h2><div class="sub">Live output from <code>ovs-ofctl dump-flows s2</code>; not simulated</div></div><span id="flowStatus" class="tag">Not queried</span></div><div id="flows" class="flowbox">Press “Inspect flow table” to query the current switch state.</div></section></div>
<footer class="foot"><span>Service Discovery System · T1 Registry / T2 SDN / T3 Applications</span><span>Live state refresh: 5s · observed event polling: 1.5s</span></footer></main>
<script>
// Legacy /api/events remains available for clients using the text-log endpoint.
const $=id=>document.getElementById(id), esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
let busy=false, telemetryBusy=false, liveStatus=null, telemetry=null, controllerState=null, selectedProvider=null, initialized=false, activitiesInitialized=false, packetQueue=Promise.resolve();
const seenEvents=new Set(), hostByIp={'10.0.0.1':'h1','10.0.0.2':'h2','10.0.0.11':'h11','10.0.0.12':'h12','10.0.0.13':'h13'};
const providerY={'FILE-1':65,'FILE-2':130,'FILE-3':195,'CHAT-1':260,'CHAT-2':325,'VIDEO-1':390,'VIDEO-2':455};
let lastHeartbeatAnimationAt=0;
const failureDemo={provider:'FILE-1',pauseRequested:false,recoveryRequested:false,failoverConfirmed:false,beforeFlows:null};
const providerServices={'FILE-1':'FILE','FILE-2':'FILE','FILE-3':'FILE','CHAT-1':'CHAT','CHAT-2':'CHAT','VIDEO-1':'VIDEO','VIDEO-2':'VIDEO'};
async function api(path,opts={}){const r=await fetch(path,opts);const j=await r.json();if(!r.ok){const e=new Error(j.error||`HTTP ${r.status}`);e.payload=j;throw e}return j}
function setupDashboardLayout(){const main=document.querySelector('main.shell'),header=main.querySelector('.top'),notice=$('notice'),stats=document.querySelector('.stats'),topology=document.querySelector('.topology-panel'),oldLayout=document.querySelector('.layout'),footer=main.querySelector('.foot'),panels=[...document.querySelector('.layout').children];const workspace=document.createElement('div');workspace.className='workspace';const left=document.createElement('aside');left.className='topology-column';const scroll=document.createElement('div');scroll.className='right-scroll';const right=document.createElement('section');right.className='right-column';workspace.append(left,scroll);left.append(topology);const nav=document.createElement('nav');nav.className='tabbar';nav.setAttribute('aria-label','Dashboard sections');const tabDefs=[['live','Live'],['try','Try it'],['network','Network evidence'],['performance','Performance']];const panes={};for(const [id,label] of tabDefs){const button=document.createElement('button');button.className='tab-button'+(id==='live'?' active':'');button.dataset.tab=id;button.textContent=label;button.setAttribute('role','tab');button.setAttribute('aria-selected',id==='live'?'true':'false');nav.append(button);const pane=document.createElement('div');pane.className='tab-pane';pane.dataset.pane=id;pane.hidden=id!=='live';panes[id]=pane;right.append(pane)}right.insertBefore(nav,right.firstChild);for(const panel of panels){let target='live';if(panel.querySelector('#providers')||panel.querySelector('#discoverOnly'))target='try';if(panel.querySelector('#providerRules')||panel.querySelector('#flows')||panel.querySelector('#networkFault'))target='network';if(panel.querySelector('#workloadStart'))target='performance';panes[target].append(panel)}oldLayout.remove();scroll.append(header,notice,stats,right,footer);main.replaceChildren(workspace);nav.addEventListener('click',e=>{const button=e.target.closest('[data-tab]');if(!button)return;nav.querySelectorAll('[data-tab]').forEach(x=>{const active=x===button;x.classList.toggle('active',active);x.setAttribute('aria-selected',active?'true':'false')});Object.entries(panes).forEach(([id,pane])=>pane.hidden=id!==button.dataset.tab)});
const demo=document.createElement('section');demo.className='panel failure-demo';demo.innerHTML=`<div class="panel-head"><div><h2>Guided failure demo</h2><div class="sub">Each step advances from Registry status and live s2 flow evidence.</div></div><span class="tag" id="demoProviderState">Waiting for live state</span></div><div class="demo-controls"><label>Provider <select id="demoProvider"><option>FILE-1</option><option>FILE-2</option><option>FILE-3</option><option>CHAT-1</option><option>CHAT-2</option><option>VIDEO-1</option><option>VIDEO-2</option></select></label><button id="demoPause">Pause heartbeats</button><button id="demoFailover" disabled>Test failover</button><button id="demoResume" disabled>Resume heartbeats</button></div><div id="demoCountdownLabel" class="sub">Choose a provider, then pause its heartbeats.</div><div class="countdown-track"><div id="demoCountdown" class="countdown-fill"></div></div><div id="demoSteps" class="demo-steps"></div><div class="sub" id="demoExplanation">The provider service stays running. The demo checks whether a direct endpoint is blocked after the controller installs its drop rule.</div>`;panes.try.prepend(demo);
const diff=document.createElement('section');diff.className='panel';diff.innerHTML='<div class="panel-head"><div><h2>Before / after flow diff</h2><div class="sub">Compared from the captured s2 dump to the current live dump.</div></div><span class="tag" id="flowDiffStatus">No baseline captured</span></div><div id="flowDiff" class="flow-diff"><div class="empty">Pause a provider in the guided demo to capture the before state.</div></div>';panes.network.prepend(diff);
const perf=document.createElement('section');perf.className='panel';perf.innerHTML='<div class="panel-head"><div><h2>Workload performance</h2><div class="sub">Per-request service results from actual Mininet client namespaces.</div></div><span class="tag" id="perfPolicy">Policy unavailable</span></div><div id="perfSummary" class="perf-summary"></div><h3>Recent discovery latency</h3><div id="latencyBars" class="latency-bars"><div class="empty">Run a dynamic FILE workload to collect discovery latency samples.</div></div><h3 style="margin-top:18px">Static versus dynamic FILE comparison</h3><div id="workloadComparison" class="result muted">Run the same preset in both modes to compare results.</div><div id="comparisonCharts"></div>';panes.performance.prepend(perf);
const demoProvider=$('demoProvider');demoProvider.value=failureDemo.provider;demoProvider.onchange=()=>{failureDemo.provider=demoProvider.value;failureDemo.pauseRequested=false;failureDemo.recoveryRequested=false;failureDemo.failoverConfirmed=false;failureDemo.beforeFlows=null;renderFailureDemo()};
$('demoPause').onclick=async()=>{const pid=demoProvider.value;failureDemo.provider=pid;failureDemo.pauseRequested=false;failureDemo.recoveryRequested=false;failureDemo.failoverConfirmed=false;try{const c=await api('/api/controller');failureDemo.beforeFlows=c.flows?.s2?.output||null;controllerState=c}catch(_){failureDemo.beforeFlows=controllerState?.flows?.s2?.output||null}renderFailureDemo();await changeProviderLifecycle(pid,'offline')};
$('demoFailover').onclick=async()=>{const pid=demoProvider.value;$('service').value=providerServices[pid];await runManualOperation('service')};
$('demoResume').onclick=async()=>{const pid=demoProvider.value;await changeProviderLifecycle(pid,'online')};
}
function showDiscoveryLatency(ms){$('discoveryLatency').classList.toggle('pending',ms==null);$('latencyValue').textContent=ms==null?'Waiting for Registry reply':`${Number(ms).toFixed(3)} ms`}
function showOperationLatency(ms,operation){$('operationLatency').classList.toggle('pending',ms==null);$('operationLatencyScope').textContent=operation==='discover'?'Registry response only':'Discovery + provider response';$('operationLatencyValue').textContent=ms==null?'Waiting for operation':`${Number(ms).toFixed(3)} ms`}
function updateHeartbeatRings(){const timeout=telemetry?.registry?.heartbeat_timeout_seconds;document.querySelectorAll('[data-hb-ring]').forEach(ring=>{const pid=ring.dataset.hbRing,hb=telemetry?.providers?.[pid]?.last_heartbeat,text=document.querySelector(`[data-hb-text="${pid}"]`);if(!timeout||!hb){if(text)text.textContent='—';return}const left=Math.max(0,timeout-(Date.now()-Date.parse(hb))/1000),ratio=Math.max(0,Math.min(1,left/timeout));ring.style.strokeDashoffset=String(94.25*(1-ratio));ring.style.stroke=left<5?'var(--red)':left<timeout*.25?'var(--amber)':'var(--green)';if(text)text.textContent=String(Math.ceil(left))});renderFailureDemo()}
function updateFlowDiff(){const box=$('flowDiff'),tag=$('flowDiffStatus');if(!box||!tag)return;const current=controllerState?.flows?.s2?.output;if(!failureDemo.beforeFlows||!current){tag.textContent='No live comparison';return}const before=new Set(failureDemo.beforeFlows.split('\n').map(x=>x.trim()).filter(Boolean)),after=new Set(current.split('\n').map(x=>x.trim()).filter(Boolean));const added=[...after].filter(x=>!before.has(x)),removed=[...before].filter(x=>!after.has(x));const relevant=[...added.map(x=>({kind:'added',text:x})),...removed.map(x=>({kind:'removed',text:x}))];tag.textContent=`${added.length} added · ${removed.length} removed`;box.innerHTML=relevant.length?relevant.map(x=>`<div class="flow-diff-line ${x.kind}">${x.kind==='added'?'+':'−'} ${esc(x.text)}</div>`).join(''):'<div class="empty">No flow changes since the baseline capture.</div>'}
function renderFailureDemo(){const stepBox=$('demoSteps');if(!stepBox)return;const pid=$('demoProvider')?.value||failureDemo.provider;failureDemo.provider=pid;const provider=liveStatus?.providers?.find(x=>x.id===pid),rule=controllerState?.provider_rules?.[pid],heartbeat=telemetry?.providers?.[pid]?.last_heartbeat,timeout=telemetry?.registry?.heartbeat_timeout_seconds;const left=heartbeat&&timeout?Math.max(0,timeout-(Date.now()-Date.parse(heartbeat))/1000):null;const paused=failureDemo.pauseRequested||provider?.lifecycle_status==='DETECTING FAILURE'||provider?.lifecycle_status==='OFFLINE';const inactive=provider?.status==='INACTIVE';const dropped=rule?.available&&rule.drop_present===true;const done=[paused,inactive,dropped,failureDemo.failoverConfirmed,failureDemo.recoveryRequested&&provider?.status==='ACTIVE'&&rule?.available&&!rule.drop_present];const labels=['Pause provider','Detecting · timeout','Drop rule installed','Failover confirmed','Recovered'];const current=done.findIndex(x=>!x);stepBox.innerHTML=labels.map((label,i)=>`<div class="demo-step ${done[i]?'done':i===current?'current':''}">${done[i]?'✓ ':''}${label}</div>`).join('');const label=$('demoCountdownLabel'),fill=$('demoCountdown'),state=$('demoProviderState');if(state)state.textContent=provider?`${pid} · ${provider.lifecycle_status}`:'Provider state unavailable';if(label)label.textContent=left==null?'Heartbeat countdown unavailable':inactive?'Registry marked provider INACTIVE':paused?`${Math.ceil(left)}s until Registry timeout`:`Next heartbeat timeout in ${Math.ceil(left)}s`;if(fill)fill.style.width=left==null?'0%':`${Math.min(100,Math.max(0,(timeout-left)/timeout*100))}%`;const pause=$('demoPause'),failover=$('demoFailover'),resume=$('demoResume');if(pause)pause.disabled=!provider||!liveStatus?.registry?.ok||provider.status!=='ACTIVE'||paused;if(failover)failover.disabled=!dropped;if(resume)resume.disabled=!provider||(!inactive&&!failureDemo.recoveryRequested);updateFlowDiff()}
function renderPerformance(w){const summary=$('perfSummary'),bars=$('latencyBars');if(!summary||!bars)return;const records=(w.records||[]).filter(r=>r.execution_mode==='dynamic'&&r.discovery_success===true&&r.discovery_latency_ms!=null&&Number.isFinite(Number(r.discovery_latency_ms))).slice(-30),latencies=records.map(r=>Number(r.discovery_latency_ms));$('perfPolicy').textContent=w.selection_policy||liveStatus?.registry?.selection_policy||'Policy unavailable';const fmt=(v,suffix=' ms')=>v==null?'Unavailable':`${Number(v).toFixed(2)}${suffix}`,discoveryUnavailable=w.execution_mode==='static'?'Not applicable (static)':null,card=([label,value])=>`<div class="perf-card" style="padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)"><span style="display:block;color:var(--muted);font-size:12px">${label}</span><strong>${value}</strong></div>`,group=title=>`<h3 class="perf-group-title" style="grid-column:1/-1;margin:8px 0 0">${title}</h3>`;summary.style.cssText='display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px';summary.innerHTML=group('Service Discovery Performance')+[ ['Mean discovery latency',discoveryUnavailable||fmt(w.mean_discovery_latency_ms)],['P95 discovery latency',discoveryUnavailable||fmt(w.p95_discovery_latency_ms)],['Discovery success rate',discoveryUnavailable||fmt(w.discovery_success_rate_percent,'%')],['Successful discovery requests / second',discoveryUnavailable||fmt(w.successful_discoveries_per_second,' req/s')] ].map(card).join('')+group('Service Communication Performance')+[['Service success rate',fmt(w.success_rate_percent,'%')],['Successful service requests / second',fmt(w.successful_requests_per_second,' req/s')],['Mean end-to-end latency',fmt(w.mean_end_to_end_latency_ms)]].map(card).join('');if(!latencies.length){bars.innerHTML='<div class="empty">Only successful dynamic Registry responses contribute discovery latency samples. Static mode does not contact the Registry.</div>'}else{const max=Math.max(...latencies,1);bars.innerHTML=latencies.map((value,i)=>`<div class="latency-bar" title="${esc(records[i].request_id||'request')} · ${value.toFixed(3)} ms" data-ms="${value.toFixed(3)}" style="height:${Math.max(4,value/max*100)}%"></div>`).join('')}renderWorkloadComparison(w)}
function renderWorkloadComparison(w){const table=$('workloadComparison'),charts=$('comparisonCharts');if(!table||!charts)return;const runs=[...(w.history||[]),w].filter(r=>r.status==='completed'&&r.service==='FILE'),cell=(v,suffix='')=>v==null?'—':Number(v).toFixed(2)+suffix,metricRows=runs.map(r=>{const exp=r.experiment||'Custom',disc=r.execution_mode==='static'?'Not applicable':cell(r.mean_discovery_latency_ms,' ms'),p95=r.execution_mode==='static'?'Not applicable':cell(r.p95_discovery_latency_ms,' ms'),dsr=r.execution_mode==='static'?'Not applicable':cell(r.discovery_success_rate_percent,'%'),dps=r.execution_mode==='static'?'Not applicable':cell(r.successful_discoveries_per_second,' req/s');return `<tr><td>${esc(exp)}</td><td>${esc((r.clients||[]).length+' clients × '+r.requests_per_client)}</td><td>${esc(r.execution_mode||'dynamic')}</td><td>${r.successful??0}/${r.total_requests??0}</td><td>${cell(r.success_rate_percent,'%')}</td><td>${cell(r.successful_requests_per_second,' req/s')}</td><td>${cell(r.mean_end_to_end_latency_ms,' ms')}</td><td>${disc}</td><td>${p95}</td><td>${dsr}</td><td>${dps}</td></tr>`}).join('');table.innerHTML=runs.length?`FILE workload results · dynamic contacts the Registry before the provider; static contacts FILE-1 directly and has no discovery metrics.<div style="overflow:auto"><table><thead><tr><th>Experiment</th><th>Configuration</th><th>Mode</th><th>Service success</th><th>Service success rate</th><th>Successful service req/s</th><th>Mean end-to-end</th><th>Mean discovery</th><th>P95 discovery</th><th>Discovery success rate</th><th>Discovery req/s</th></tr></thead><tbody>${metricRows}</tbody></table></div><br>Raw per-request records and configuration are saved under benchmark-results/.`:'Run FILE experiments A, B and C in dynamic mode; repeat in static mode for a direct-provider comparison. Raw requests and configuration are saved under benchmark-results/.';const same=(a,b)=>JSON.stringify([[...(a.clients||[])].sort(),a.requests_per_client,a.concurrency_per_client,a.mode,a.timeout,a.experiment])===JSON.stringify([[...(b.clients||[])].sort(),b.requests_per_client,b.concurrency_per_client,b.mode,b.timeout,b.experiment]);let pair=null;for(let i=runs.length-1;i>=0&&!pair;i--)for(let j=i-1;j>=0;j--)if(runs[i].execution_mode!==runs[j].execution_mode&&same(runs[i],runs[j])){pair={dynamic:runs[i].execution_mode==='dynamic'?runs[i]:runs[j],static:runs[i].execution_mode==='static'?runs[i]:runs[j]};break}if(!pair){charts.innerHTML='';return}const d=pair.dynamic,s=pair.static,metrics=[['Mean end-to-end latency (ms)',d.mean_end_to_end_latency_ms,s.mean_end_to_end_latency_ms],['Successful service requests / second',d.successful_requests_per_second,s.successful_requests_per_second],['Service success rate (%)',d.success_rate_percent,s.success_rate_percent]],maxes=metrics.map(x=>Math.max(Number(x[1])||0,Number(x[2])||0,1));charts.innerHTML=`<h4>Matched static versus dynamic · ${esc(d.experiment||'custom')} (${d.clients.length} clients, ${d.requests_per_client} requests/client)</h4>`+metrics.map(([label,dv,sv],i)=>`<div style="margin:12px 0"><strong>${label}</strong>${[['Dynamic',dv,'#6ce5d2'],['Static',sv,'#80aaff']].map(([name,val,color])=>`<div style="display:flex;align-items:center;gap:8px;margin:4px 0"><span style="width:58px">${name}</span><div style="height:12px;background:${color};width:${Math.max(1,(Number(val)||0)/maxes[i]*65)}%;border-radius:6px"></div><span>${cell(val)}</span></div>`).join('')}</div>`).join('')}
let networkLinkBusy=false,networkLinkStatus='UNKNOWN';
function renderNetworkLinkStatus(data,message){const interfaces=$('networkLinkInterfaces');if(interfaces&&data?.interfaces)interfaces.textContent=`Observed ports: ${data.interfaces.s1||'s1 unknown'} ↔ ${data.interfaces.s2||'s2 unknown'}`;networkLinkStatus=['UP','DOWN','UNKNOWN'].includes(data?.status)?data.status:'UNKNOWN';const state=$('networkLinkStatus');if(state){state.textContent=networkLinkStatus;state.className='tag '+(networkLinkStatus==='UP'?'ONLINE':networkLinkStatus==='DOWN'?'OFFLINE':'')}const down=$('linkDownButton'),up=$('linkUpButton');if(down)down.disabled=networkLinkBusy||networkLinkStatus==='DOWN';if(up)up.disabled=networkLinkBusy||networkLinkStatus==='UP';if(message!==undefined){$('networkLinkMessage').textContent=message;$('networkLinkMessage').className='result '+(data?.ok?'':'unavailable')}}
async function refreshNetworkLink(){try{const result=await api('/api/network-link');renderNetworkLinkStatus(result)}catch(error){renderNetworkLinkStatus(error.payload||{status:'UNKNOWN'},`Link state unavailable: ${error.message}`)}}
async function operateNetworkLink(operation){if(networkLinkBusy)return;networkLinkBusy=true;const down=$('linkDownButton'),up=$('linkUpButton');if(down)down.textContent=operation==='down'?'Taking Link Down…':'Take Link Down';if(up)up.textContent=operation==='up'?'Restoring Link…':'Restore Link';renderNetworkLinkStatus({status:networkLinkStatus},operation==='down'?'Taking s1 ↔ s2 down…':'Restoring s1 ↔ s2…');try{const result=await api('/api/network-link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation})});renderNetworkLinkStatus(result,result.message||`Link is ${result.status}`)}catch(error){const actual=error.payload||{status:'UNKNOWN'};renderNetworkLinkStatus(actual,`Could not ${operation==='down'?'take the link down':'restore the link'}: ${error.message}`)}finally{networkLinkBusy=false;if(down)down.textContent='Take Link Down';if(up)up.textContent='Restore Link';const current=networkLinkStatus;renderNetworkLinkStatus({status:current,ok:true});}}
function pill(s){const cls=s.replaceAll(' ','_');return `<span class="pill ${cls}">${esc(s)}</span>`}
function renderProviders(d){liveStatus=d;$('updatedAt').textContent='Updated '+new Date(d.timestamp).toLocaleTimeString();$('regDot').className='dot '+(d.registry.ok?'':'bad');$('regValue').textContent=d.registry.ok?'Connected':'Unavailable';$('regFoot').textContent=d.registry.ok?`${d.registry.host}:${d.registry.port} · UDP`:(d.registry.error||'No response');const active=d.registry.ok?d.providers.filter(p=>p.status==='ACTIVE').length:null;const inactive=d.registry.ok?d.providers.filter(p=>p.status==='INACTIVE').length:null;$('activeValue').textContent=active===null?'—':String(active).padStart(2,'0')+' / 07';$('inactiveValue').textContent=inactive===null?'Inactive count unavailable':`${inactive} inactive · live Registry reply`;$('providers').innerHTML=d.providers.map(p=>{const state=p.lifecycle_status||'UNKNOWN',offlineEnabled=state==='ONLINE',onlineEnabled=state==='OFFLINE';return `<article class="provider" data-provider-card="${p.id}" tabindex="0"><div class="provider-top"><span class="pid">${esc(p.id)}</span>${pill(state)}</div><div class="meta">${esc(p.host)} · ${esc(p.ip)}</div><div class="meta">${esc(p.protocol)} / ${esc(p.port)}</div><span class="service-badge">${esc(p.service)}</span><div class="hb-widget"><svg class="hb-ring" viewBox="0 0 36 36" aria-label="Heartbeat timeout countdown"><circle class="hb-track" cx="18" cy="18" r="15"></circle><circle class="hb-progress" data-hb-ring="${p.id}" cx="18" cy="18" r="15" stroke-dasharray="94.25" stroke-dashoffset="0"></circle><text class="hb-center" data-hb-text="${p.id}" x="18" y="21">—</text></svg><span>heartbeat timeout countdown</span></div><div class="provider-controls"><button data-provider="${p.id}" data-operation="offline" ${offlineEnabled?'':'disabled'}>Take offline</button><button data-provider="${p.id}" data-operation="online" ${onlineEnabled?'':'disabled'}>Bring online</button></div></article>`}).join('');document.querySelectorAll('[data-provider-card]').forEach(card=>{card.onclick=e=>{if(!e.target.closest('button'))selectProvider(card.dataset.providerCard)};card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectProvider(card.dataset.providerCard)}}});document.querySelectorAll('.provider-controls button:not(:disabled)').forEach(b=>b.onclick=()=>changeProviderLifecycle(b.dataset.provider,b.dataset.operation));document.querySelectorAll('.provider-node').forEach(node=>{const p=d.providers.find(x=>x.id===node.dataset.provider);node.classList.remove('active','inactive','unknown');node.classList.add(!p||p.status==='UNKNOWN'?'unknown':p.status==='ACTIVE'?'active':'inactive');node.onclick=()=>selectProvider(node.dataset.provider)});$('services').innerHTML=['FILE','CHAT','VIDEO'].map(s=>{const x=d.services?.[s],ps=x?.providers||[];return `<div class="service-row"><div><div class="service-name">${s}</div><div class="service-details">${d.registry.ok?(ps.length?ps.map(p=>`${esc(p.id)} · ${esc(p.protocol)} ${esc(p.ip)}:${esc(p.port)}`).join('<br>'):'No active provider returned'):'Registry data unavailable'}</div></div><button data-service="${s}" class="discoverOne">Inspect</button></div>`}).join('');document.querySelectorAll('.discoverOne').forEach(b=>b.onclick=()=>{$('service').value=b.dataset.service;runManualOperation('discover')});if(!selectedProvider)selectProvider(d.providers[0]?.id)}
function relativeAge(iso){if(!iso)return 'Unavailable';const seconds=Math.max(0,(Date.now()-Date.parse(iso))/1000);return seconds<2?'just now':seconds<60?`${seconds.toFixed(1)}s ago`:`${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s ago`}
function selectProvider(pid){if(!pid)return;selectedProvider=pid;document.querySelectorAll('.provider-node').forEach(n=>n.classList.toggle('selected',n.dataset.provider===pid));document.querySelectorAll('[data-provider-card]').forEach(n=>n.style.outline=n.dataset.providerCard===pid?'1px solid #6ce5d2':'');$('selectedProviderTag').textContent=pid;renderProviderDetail()}
function renderProviderDetail(){if(!selectedProvider||!liveStatus)return;const p=liveStatus.providers.find(x=>x.id===selectedProvider),t=telemetry?.providers?.[selectedProvider],rule=controllerState?.provider_rules?.[selectedProvider],timeout=telemetry?.registry?.heartbeat_timeout_seconds;const hb=t?.last_heartbeat,history=(telemetry?.events||[]).filter(e=>e.provider_id===selectedProvider&&(e.source==='registry'||e.kind?.startsWith('provider_drop'))).slice(-30).reverse(),overdue=hb&&timeout&&((Date.now()-Date.parse(hb))/1000)>timeout;const heartbeatState=!telemetry?.availability?.registry_events?'Unavailable':!hb?'No heartbeat observed':overdue?(p?.status==='INACTIVE'?'Registry marked INACTIVE after heartbeat timeout':p?.status==='ACTIVE'?'Heartbeat overdue; Registry still reports ACTIVE':'Heartbeat overdue; Registry state unavailable'):'Recent';$('providerDetail').innerHTML=p?`<h3>${esc(p.id)} · ${pill(p.status)}</h3><div class="metric-row"><span>Endpoint</span><span>${esc(p.ip)} · ${esc(p.protocol)}/${esc(p.port)}</span></div><div class="metric-row"><span>Service / host</span><span>${esc(p.service)} · ${esc(p.host)}</span></div><div class="metric-row"><span>Heartbeat health</span><span>${esc(heartbeatState)}</span></div><div class="metric-row"><span>Last heartbeat</span><span>${esc(relativeAge(hb))}</span></div><div class="metric-row"><span>Heartbeat packets observed</span><span>${t?.heartbeat_count??'Unavailable'}${timeout? ` · timeout ${timeout}s`:''}</span></div><div class="metric-row"><span>s2 priority-100 drop</span><span>${rule?.available?(rule.drop_present?'PRESENT':'ABSENT'):'Unavailable'}</span></div><h3 style="margin-top:12px">Heartbeat and failure / recovery history</h3><div class="detail-history">${history.length?history.map(e=>`<div class="mini-event ${e.status==='INACTIVE'||e.kind==='provider_drop_installed'?'failure':''}"><strong>${esc(e.timestamp)}</strong> · ${esc(e.kind)} ${esc(e.status||'')} ${esc(e.reason||'')}</div>`).join(''):'<div class="empty">No retained heartbeat or transition events.</div>'}</div>`: '<div class="empty">Provider state unavailable.</div>'}
function renderTelemetry(t){telemetry=t;const r=t.registry;const eventsOk=t.availability.registry_events,statsOk=t.availability.registry_stats;$('telemetrySource').textContent=eventsOk?'LIVE EVENT STREAM':'EVENT STREAM UNAVAILABLE';$('telemetrySource').classList.toggle('unavailable',!eventsOk);$('discoveryCount').textContent=statsOk&&r?String(r.discovery_requests):'Unavailable';$('policyValue').textContent=r?.selection_policy||'Unavailable';$('selectionPolicyTag').textContent=r?.selection_policy||'Unavailable';const counts=r?.selection_counts||{};const max=Math.max(1,...Object.values(counts));$('selectionDistribution').innerHTML=statsOk?Object.keys(counts).map(pid=>`<div class="selection-bar"><span>${esc(pid)}</span><div class="selection-bar-track"><div class="selection-bar-fill" style="width:${Math.min(100,(counts[pid]||0)/max*100)}%"></div></div><span>${counts[pid]||0}</span></div>`).join(''):'<div class="empty unavailable">Registry statistics unavailable; no distribution inferred.</div>';renderProviderDetail();animateNewEvents(t.events||[]);updateHeartbeatRings()}
function renderController(c){controllerState=c;const states=c.switches?.switches||{};$('switchStatus').innerHTML=c.switches?.ok?['s1','s2'].map(s=>{const x=states[s];return `<div class="metric-row"><span>${s} · ${esc(x?.target||'controller target unavailable')}</span><span class="${x?.connected===true?'':'unavailable'}">${x?.connected===true?'CONNECTED':x?.connected===false?'DISCONNECTED':'UNKNOWN'}</span></div>`}).join(''):`<div class="empty unavailable">Switch connection state unavailable: ${esc(c.switches?.error||'no OVS data')}</div>`;const rules=c.provider_rules||{};$('providerRules').innerHTML=['s1','s2'].map(s=>`<strong>${s} · live flow dump</strong><pre>${esc(c.flows?.[s]?.ok?c.flows[s].output:c.flows?.[s]?.output||'Unavailable')}</pre>`).join('')+'<strong>Priority-100 provider drops on s2</strong>'+Object.keys(rules).map(pid=>`<div class="flow-provider">${esc(pid)} · ${rules[pid].available?(rules[pid].drop_present?'PRESENT · priority 100':'absent'):'Unavailable'}</div>`).join('');const any=Object.values(c.flows||{}).some(f=>f.ok);$('flowValue').textContent=any?'Queried':'Unavailable';$('flowDot').className='dot '+(any?'':'bad');renderProviderDetail();updateFlowDiff();renderFailureDemo()}
const renderTelemetryBase=renderTelemetry;
renderTelemetry=function(t){renderTelemetryBase(t);const counts=t.registry?.selection_counts||{},total=Object.values(counts).reduce((a,b)=>a+b,0),max=Math.max(1,...Object.values(counts));if(t.availability.registry_events){$('selectionDistribution').innerHTML=Object.keys(counts).map(pid=>'<div class="selection-bar"><span>'+esc(pid)+'</span><div class="selection-bar-track"><div class="selection-bar-fill" style="width:'+Math.min(100,(counts[pid]||0)/max*100)+'%"></div></div><span>'+counts[pid]+' · '+(total?((counts[pid]||0)*100/total).toFixed(1):'0.0')+'%</span></div>').join('')}};
function eventLabel(e){const labels={heartbeat:'HEARTBEAT RECEIVED',provider_status:'REGISTRY STATUS',discovery:'DISCOVERY REQUEST',discovery_response_sent:'DISCOVERY RESPONSE SENT',controller_notification_sent:'STATUS NOTIFICATION SENT',controller_notification_failed:'STATUS NOTIFICATION FAILED',status_notification_received:'CONTROLLER RECEIVED STATUS',provider_drop_installed:'DROP FLOW MOD SUBMITTED',provider_drop_removed:'DROP FLOW REMOVAL SUBMITTED',switch_connected:'SWITCH CONNECTED',switch_disconnected:'SWITCH DISCONNECTED',baseline_rule_installed:'BASELINE FLOW MOD SUBMITTED',service_traffic:'SERVICE TRAFFIC',lifecycle_request:'LIFECYCLE REQUEST',openflow_port_status:'OPENFLOW PORT STATUS'};return labels[e.kind]||String(e.kind||e.source||'EVENT').toUpperCase()}
function eventText(e){if(e.kind==='heartbeat')return `${e.provider_id} heartbeat received at Registry`;if(e.kind==='provider_status')return `${e.provider_id} → ${e.status} (${e.reason||'status transition'})`;if(e.kind==='discovery')return `${e.client_ip||'unknown client'} requested ${e.service}; Registry candidate ${e.selected_provider||'none'} (${e.policy})`;if(e.kind==='discovery_response_sent')return `Registry response for ${e.service} sent to ${e.client_ip||'unknown client'}; selected ${e.selected_provider||'none'}`;if(e.kind==='controller_notification_sent'||e.kind==='controller_notification_failed')return `${e.provider_id} ${e.status} · ${e.http_status?'HTTP '+e.http_status:e.error||''}`;if(e.kind==='status_notification_received')return `Controller accepted ${e.provider_id} ${e.status} notification`;if(e.kind?.startsWith('provider_drop'))return `${e.provider_id} ${e.ip} ${e.protocol}/${e.port} priority ${e.priority} on ${e.switch}`;if(e.kind==='switch_connected'||e.kind==='switch_disconnected')return `${e.switch} datapath ${e.datapath_id}`;if(e.kind==='openflow_port_status')return `${e.switch} ${e.port_name||('port '+e.port_no)} ${e.status} (reason ${e.reason})`;if(e.kind==='baseline_rule_installed')return `${e.switch} priority ${e.priority} ${JSON.stringify(e.match)} → ${e.action}`;if(e.kind==='service_traffic')return `${e.client_ip||'source unknown'} → ${e.provider||'provider unknown'} ${e.success?'response observed':'attempt failed'}`;return JSON.stringify(e)}
function renderEvents(t,a){const rows=[...(t.events||[])];if(a.ok)rows.push(...a.events);rows.sort((x,y)=>String(y.timestamp).localeCompare(String(x.timestamp)));const unavailable=[];if(!t.availability.registry_events)unavailable.push('Registry heartbeat/discovery event source unavailable');if(!t.availability.controller_events)unavailable.push('Controller event log unavailable');$('events').innerHTML=(unavailable.length?`<div class="empty unavailable">${unavailable.map(esc).join(' · ')}</div>`:'')+(rows.length?rows.slice(0,120).map(e=>`<div class="event"><span class="rail"><i></i></span><div><strong>${esc(e.timestamp)} <span class="event-type">${esc(eventLabel(e))}</span></strong> <code>${esc(eventText(e))}</code></div></div>`).join(''):'<div class="empty">No observed events in the retained event history.</div>')}
function packet(kind,path){const animation=packetQueue.then(()=>new Promise(resolve=>{const svg=$('topology'),group=$('packets'),circle=document.createElementNS('http://www.w3.org/2000/svg','circle'),route=document.createElementNS('http://www.w3.org/2000/svg','path');route.setAttribute('d',path);route.setAttribute('visibility','hidden');svg.appendChild(route);circle.setAttribute('r','6');circle.setAttribute('class','packet '+kind);group.appendChild(circle);let length;try{length=route.getTotalLength()}catch(_){route.remove();circle.remove();resolve();return}if(!Number.isFinite(length)||length<=0){route.remove();circle.remove();resolve();return}const duration=1250,start=performance.now();let finished=false;const finish=()=>{if(finished)return;finished=true;route.remove();circle.remove();resolve()};const frame=now=>{if(finished)return;const progress=Math.min(1,(now-start)/duration);try{const point=route.getPointAtLength(length*progress);circle.setAttribute('cx',point.x);circle.setAttribute('cy',point.y)}catch(_){finish();return}if(progress<1)requestAnimationFrame(frame);else finish()};requestAnimationFrame(frame)}));packetQueue=animation.catch(()=>{});return animation}
function animateEvent(e){const pid=e.provider_id||e.provider,py=providerY[pid],kind=e.kind;if(kind==='heartbeat'&&py){const now=Date.now();if(now-lastHeartbeatAnimationAt<5000)return;lastHeartbeatAnimationAt=now;return packet('heartbeat-packet',`M1000 ${py} L845 265 L715 265 L515 265 L450 300 L450 455`)}if(kind==='discovery'||kind==='discovery_response_sent')return;if((kind==='controller_notification_sent'||kind==='status_notification_received')&&py)return packet('status-packet','M450 455 L450 350 L660 105');if(kind?.startsWith('provider_drop')&&py)return packet('status-packet','M660 105 L780 230');if(kind==='service_traffic'&&e.provider_ip&&py){const source=e.client_ip==='10.0.0.254'?'M620 478 L590 450 L515 265 L715 265':e.client_ip&&hostByIp[e.client_ip]?'M110 207 L385 265 L515 265 L715 265':'M620 478 L590 450 L515 265 L715 265';return packet('service-packet',`${source} L845 265 L1000 ${py}`)}}
function animateNewEvents(events){const incoming=events.filter(e=>e.id&&!seenEvents.has(e.id));if(!initialized){events.forEach(e=>e.id&&seenEvents.add(e.id));initialized=true;return}incoming.forEach(e=>{seenEvents.add(e.id);animateEvent(e)});if(seenEvents.size>3000)seenEvents.clear()}
async function refresh(){if(busy)return;busy=true;$('refresh').disabled=true;try{const d=await api('/api/status');renderProviders(d)}catch(e){$('regValue').textContent='Unavailable';$('regDot').className='dot bad';$('regFoot').textContent=e.message;$('providers').innerHTML='<div class="empty unavailable">Live Registry state unavailable; provider state is unknown.</div>';document.querySelectorAll('.provider-node').forEach(n=>{n.classList.remove('active','inactive');n.classList.add('unknown')});$('activeValue').textContent='—';$('inactiveValue').textContent='Provider counts unavailable';if(liveStatus){liveStatus={...liveStatus,registry:{...liveStatus.registry,ok:false,error:e.message},providers:liveStatus.providers.map(p=>({...p,status:'UNKNOWN',lifecycle_status:'UNKNOWN'})),services:{}};renderProviderDetail()}}finally{busy=false;$('refresh').disabled=false}}
async function loadTelemetry(){if(telemetryBusy)return;telemetryBusy=true;try{const [t,c]=await Promise.all([api('/api/telemetry'),api('/api/controller')]);renderTelemetry(t);renderController(c);const a=await api('/api/activities');renderEvents(t,a);if(!activitiesInitialized){a.events.forEach(e=>e.id&&seenEvents.add(e.id));activitiesInitialized=true}else animateNewEvents(a.events.filter(e=>e.kind==='service_traffic'))}catch(e){$('telemetrySource').textContent='TELEMETRY UNAVAILABLE';$('telemetrySource').classList.add('unavailable')}finally{telemetryBusy=false}}
function clearManualHighlight(){document.querySelectorAll('.manual-highlight,.manual-path,.manual-service-path').forEach(node=>node.classList.remove('manual-highlight','manual-path','manual-service-path'))}
async function animateManualRequest(host){const client=[...document.querySelectorAll('.topo-node.client')].find(node=>node.dataset.client===host);const y={h1:77,h2:142,h11:207,h12:272,h13:337}[host];if(!client||!y)return;client.classList.add('manual-highlight');document.querySelector(`.client-link[data-client-link="${host}"]`)?.classList.add('manual-path');await packet('discovery-packet',`M110 ${y} L385 265 L450 300 L450 455`)}
async function animateManualResponse(result,operation,host){if(result.raw_registry_response==null)return;const registry=document.querySelector('.topo-node.registry');const y={h1:77,h2:142,h11:207,h12:272,h13:337}[host];if(!registry||!y)return;registry.classList.add('manual-highlight');document.querySelector('.registry-link')?.classList.add('manual-path');await packet('discovery-packet',`M450 455 L450 300 L385 265 L110 ${y}`);const provider=result.final_provider;if(operation==='service'&&result.provider_connection_success===true&&provider){const pid=provider.id,node=[...document.querySelectorAll('.provider-node')].find(item=>item.dataset.provider===pid);if(node){node.classList.add('manual-highlight');document.querySelector('.inter-link')?.classList.add('manual-service-path');document.querySelector(`.provider-link[data-provider-link="${pid}"]`)?.classList.add('manual-service-path');await packet('service-packet',`M110 ${y} L385 265 L515 265 L715 265 L845 265 L1000 ${providerY[pid]}`)}}}
function renderManualResult(result,operation){const isDiscovery=operation==='discover';showDiscoveryLatency(result.discovery_latency_ms);showOperationLatency(result.operation_latency_ms,operation);const providers=Array.isArray(result.providers)?result.providers:[],attempts=Array.isArray(result.provider_attempts)?result.provider_attempts:[],providerLabel=p=>`${p.id} · ${p.protocol} ${p.ip}:${p.port}`,registryList=providers.length?providers.map((p,i)=>`${i+1}. ${providerLabel(p)}`).join('\n'):'None';let attemptList;if(isDiscovery)attemptList='Not attempted (Discover Only)';else if(!attempts.length)attemptList='No provider attempts recorded';else attemptList=attempts.map((a,i)=>{if(a.status==='succeeded')return `${i+1}. ${a.provider_id}: Succeeded`;if(a.status==='timeout'){const reason=String(a.error||'').toLowerCase().includes('connection timeout')?'connection timeout':'response timeout';return `${i+1}. ${a.provider_id}: Failed - ${reason}`}return `${i+1}. ${a.provider_id}: Failed${a.error?' - '+a.error:''}`}).join('\n');const finalProvider=result.final_provider?providerLabel(result.final_provider):'None';const lines=[`Discovery: ${result.discovery_success===true?'Succeeded':result.discovery_success===false?'Failed':'Unavailable'}`,`Providers returned by Registry (in order):\n${registryList}`,`Provider attempts:\n${attemptList}`,`Final provider: ${finalProvider}`,`Provider response: ${result.provider_response??'None'}`,`Overall operation: ${result.operation_status||'Unavailable'}`,`Discovery latency: ${result.discovery_latency_ms==null?'Unavailable':`${result.discovery_latency_ms} ms`}`,`End-to-end latency: ${result.operation_latency_ms==null?'Unavailable':`${result.operation_latency_ms} ms`}`,`Selected client: ${result.client||'Unavailable'}`,`Client source IP: ${result.client_ip||'Unavailable'}`,`Registry destination: ${result.registry_destination?`${result.registry_destination.ip}:${result.registry_destination.port}`:'Unavailable'}`,`Raw Registry response: ${result.raw_registry_response??'Unavailable'}`];if(result.error)lines.push(`Operation error: ${result.error}`);if(!result.provider_connection_success&&result.provider_error)lines.push(`Provider errors: ${result.provider_error}`);$('commResult').textContent=lines.join('\n');$('commResult').className=`result ${result.operation_status==='service_succeeded'||result.operation_status==='discovery_succeeded'?'':'unavailable'}`;}
async function runManualOperation(operation){const payload={client:$('manualClient').value,service:$('service').value,operation,request:$('request').value||'PING',timeout:2};showDiscoveryLatency(null);showOperationLatency(null,operation);clearManualHighlight();$('commResult').textContent='Running operation in selected Mininet client…';$('commResult').className='result muted';$('discoverOnly').disabled=true;$('send').disabled=true;try{const requestAnimation=animateManualRequest(payload.client);const resultPromise=api('/api/client-operation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const [result]=await Promise.all([resultPromise,requestAnimation]);await animateManualResponse(result,operation,payload.client);renderManualResult(result,operation);if(operation==='service'&&failureDemo.pauseRequested&&result.discovery_success&&result.provider_connection_success===true&&result.final_provider?.id!==failureDemo.provider)failureDemo.failoverConfirmed=true;renderFailureDemo();await refresh();await loadTelemetry()}catch(e){$('commResult').textContent=`Operation request failed: ${e.message}`;$('commResult').className='result unavailable';clearManualHighlight()}finally{$('discoverOnly').disabled=false;$('send').disabled=false}}
async function loadFlows(){try{const c=await api('/api/controller');renderController(c);const f=c.flows?.s2;$('flows').textContent=f?.output||'Switch flow data unavailable';$('flowStatus').textContent=f?.ok?'Live s2 flow output':'Query unavailable'}catch(e){$('flows').textContent=e.message;$('flowStatus').textContent='Query failed'}}
async function loadEvents(){await loadTelemetry()}
let workloadSeen=new Set(),workloadInitialized=false;
function renderWorkload(w){const policy=w.selection_policy||(liveStatus?.registry?.ok?liveStatus.registry.selection_policy:null);$('workloadPolicy').textContent=policy||'Policy unavailable';const running=['starting','running','stopping'].includes(w.status);$('workloadStart').disabled=running;$('workloadStop').disabled=!running;const total=w.total_requests||0,done=w.completed||0;const distribution=Object.entries(w.provider_distribution||{}).map(([p,n])=>p+': '+n).join(', ')||'No successful provider responses yet';$('workloadProgress').textContent=`${w.status||'idle'} · ${w.execution_mode||'dynamic'} · ${done}/${total} completed · ${w.successful||0} service responses · ${w.failed||0} failed · ${w.pending||0} pending · ${w.elapsed_seconds?Number(w.elapsed_seconds).toFixed(2)+'s':''} · provider ${distribution}${w.successful_requests_per_second==null?'':` · ${Number(w.successful_requests_per_second).toFixed(2)} successful req/s`}${w.error?' · '+w.error:''}`;const rows=w.records||[];if(!workloadInitialized){rows.forEach(r=>workloadSeen.add(r.request_id));workloadInitialized=true}document.querySelectorAll('.provider-node').forEach(n=>n.classList.remove('request-selected'));const lastSelected=[...rows].reverse().find(r=>r.selected_provider);if(lastSelected)document.querySelector(`.provider-node[data-provider="${lastSelected.selected_provider}"]`)?.classList.add('request-selected');let content=rows.length?rows.slice(-100).reverse().map(r=>{const attempts=(r.provider_attempts||[]).map(a=>`${a.provider_id}:${a.status}${a.error?' ('+a.error+')':''}`).join(' → ')||'no provider attempts';return `<div class="metric-row"><span>${esc(r.request_id)} · ${esc(r.client_hostname)} (${esc(r.client_ip||'IP unavailable')}) · ${esc(r.execution_mode||w.execution_mode||'dynamic')}</span><span>${esc(r.outcome)} · ${esc(r.final_provider?.id||r.selected_provider||'no provider')} · discovery ${r.discovery_latency_ms==null?'—':esc(r.discovery_latency_ms+' ms')} · end-to-end ${r.end_to_end_latency_ms==null?'—':esc(r.end_to_end_latency_ms+' ms')}<br>${esc(attempts)}${r.provider_response?' · '+esc(r.provider_response):''}</span></div>`}).join(''):'No workload request results recorded.';$('workloadResults').innerHTML=content;if(rows.length>100)$('workloadResults').insertAdjacentHTML('beforeend',`<div class="empty">Showing latest 100 of ${rows.length} recorded request results.</div>`);for(const r of rows){if(workloadSeen.has(r.request_id))continue;workloadSeen.add(r.request_id);const clientY={h1:77,h2:142,h11:207,h12:272,h13:337}[r.client_hostname];if(clientY){packet('request-packet',`M110 ${clientY} L385 265 L450 300 L450 455`);if(r.discovery_success)packet('response-packet',`M450 455 L450 300 L385 265 L110 ${clientY}`);const provider=r.final_provider;if(r.success&&provider){const node=[...document.querySelectorAll('.provider-node')].find(item=>item.dataset.provider===provider.id);node?.classList.add('request-selected');packet('service-packet',`M110 ${clientY} L385 265 L515 265 L715 265 L845 265 L1000 ${providerY[provider.id]}`)}}}renderPerformance(w)}
async function loadWorkload(){try{renderWorkload(await api('/api/workloads'))}catch(e){$('workloadProgress').textContent='Workload status unavailable: '+e.message}}
async function startWorkload(){const clients=[...document.querySelectorAll('#workloadClients input:checked')].map(x=>x.value);try{workloadSeen=new Set();workloadInitialized=true;const policy=liveStatus?.registry?.ok?liveStatus.registry.selection_policy:null;const w=await api('/api/workloads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({clients,service:$('workloadService').value,requests_per_client:Number($('workloadRequests').value),concurrency_per_client:Number($('workloadConcurrency').value),timeout:Number($('workloadTimeout').value),execution_mode:$('workloadExecutionMode').value,experiment:$('workloadPreset').value,mode:$('workloadMode').value,selection_policy:policy})});renderWorkload(w)}catch(e){$('workloadProgress').textContent=e.message}}
function applyWorkloadPreset(){const preset=$('workloadPreset').value,count=preset==='A'?1:preset==='B'?3:5,requests=20;document.querySelectorAll('#workloadClients input').forEach((box,index)=>box.checked=index<count);$('workloadRequests').value=String(requests);$('workloadService').value='FILE'}
async function stopWorkload(){try{renderWorkload(await api('/api/workloads/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}))}catch(e){$('workloadProgress').textContent=e.message}}
function updateWorkloadMode(){const sequential=$('workloadMode').value==='sequential';$('workloadConcurrency').disabled=sequential;if(sequential)$('workloadConcurrency').value='1'}
async function changeProviderLifecycle(provider,operation){if(operation==='offline'&&$('demoProvider')?.value===provider)failureDemo.beforeFlows=controllerState?.flows?.s2?.output||failureDemo.beforeFlows;renderFailureDemo();$('notice').textContent=operation==='offline'?`Requesting heartbeat pause for ${provider}…`:`Requesting heartbeat resume for ${provider}…`;try{const r=await api('/api/provider-lifecycle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider,operation})});if($('demoProvider')?.value===provider){if(operation==='offline')failureDemo.pauseRequested=true;if(operation==='online')failureDemo.recoveryRequested=true}renderFailureDemo();$('notice').textContent=r.message;await refresh();await loadTelemetry()}catch(e){$('notice').textContent=e.message;await loadTelemetry()}}
const telemetryRenderWithLive=renderTelemetry;
renderTelemetry=function(t){telemetryRenderWithLive(t);if(!liveStatus?.registry?.ok&&t.registry){$('telemetrySource').textContent='RETAINED HISTORY · REGISTRY UNAVAILABLE';$('telemetrySource').classList.add('unavailable');$('selectionPolicyTag').textContent=(t.registry.selection_policy||'Unknown')+' · last session';$('discoveryCount').title='Count from the last Registry process session; Registry is not currently responding.'}};
const providerDetailWithLive=renderProviderDetail;
renderProviderDetail=function(){providerDetailWithLive();if(!liveStatus?.registry?.ok&&selectedProvider&&telemetry?.providers){const note=document.createElement('div');note.className='empty unavailable';note.textContent='Heartbeat and Registry status are retained history; Registry is not currently responding.';$('providerDetail').appendChild(note)}};
const providerHistoryBase=renderProviderDetail;
renderProviderDetail=function(){providerHistoryBase();const p=telemetry?.providers?.[selectedProvider];if(!p)return;const events=[...(p.heartbeat_history||[]),...(p.controller_events||[]),...(telemetry.events||[]).filter(e=>e.provider_id===selectedProvider&&e.kind==='provider_status')];const unique=[...new Map(events.map(e=>[(e.id||e.kind+e.timestamp),e])).values()].sort((a,b)=>String(b.timestamp).localeCompare(String(a.timestamp))).slice(0,60);const list=document.querySelector('#providerDetail .detail-history');if(list)list.innerHTML=unique.length?unique.map(e=>'<div class="mini-event '+(e.status==='INACTIVE'||e.kind==='provider_drop_flow_mod_submitted'?'failure':'')+'"><strong>'+esc(e.timestamp)+'</strong> · '+esc(e.kind)+' '+esc(e.status||'')+' '+esc(e.reason||'')+'</div>').join(''):'<div class="empty">No retained heartbeat or transition events.</div>'};
const providerTrafficHistoryBase=renderProviderDetail;
renderProviderDetail=function(){providerTrafficHistoryBase();const p=telemetry?.providers?.[selectedProvider];if(!p)return;const events=[...(p.heartbeat_history||[]),...(p.controller_events||[]),...(p.service_events||[]).filter(e=>e.kind==='service_traffic_served'),...(telemetry.events||[]).filter(e=>e.provider_id===selectedProvider&&(e.kind==='provider_status'||e.kind.startsWith('ovs_provider_drop')))];const unique=[...new Map(events.map(e=>[(e.id||e.kind+e.timestamp),e])).values()].sort((a,b)=>String(b.timestamp).localeCompare(String(a.timestamp))).slice(0,60);const list=document.querySelector('#providerDetail .detail-history');if(list)list.innerHTML=unique.length?unique.map(e=>'<div class="mini-event '+(e.status==='INACTIVE'||e.kind==='provider_drop_flow_mod_submitted'||e.kind==='ovs_provider_drop_present_observed'?'failure':'')+'"><strong>'+esc(e.timestamp)+'</strong> · '+esc(e.kind)+' '+esc(e.status||e.client_ip||'')+' '+esc(e.reason||'')+'</div>').join(''):'<div class="empty">No retained heartbeat, provider traffic or transition events.</div>';if(!p.service_events_available){const note=document.createElement('div');note.className='empty unavailable';note.textContent='Provider service traffic history unavailable; restart this provider with the current code to enable event capture.';$('providerDetail').appendChild(note)}};
const animateEventBase=animateEvent;
animateEvent=function(e){if(e.kind==='service_traffic')return;if(e.kind==='service_traffic_served')return;if(e.kind==='ovs_provider_drop_present_observed'||e.kind==='ovs_provider_drop_absent_observed'){const py=providerY[e.provider_id];if(py)packet('status-packet','M660 105 L780 230');return}animateEventBase(e)};
const eventTextOriginal=eventText;
eventText=function(e){if(e.kind==='ovs_provider_drop_present_observed')return e.provider_id+' priority-100 drop observed in live s2 flow dump';if(e.kind==='ovs_provider_drop_absent_observed')return e.provider_id+' priority-100 drop no longer present in live s2 flow dump';if(e.kind==='controller_notification_attempted')return e.provider_id+' '+e.status+' notification attempted to controller';if(e.kind==='controller_notification_acknowledged')return e.provider_id+' '+e.status+' notification acknowledged by controller';return eventTextOriginal(e)};
const renderEventsOriginal=renderEvents;
renderEvents=function(t,a){renderEventsOriginal(t,a);if(!t.availability.dashboard_events){const note=document.createElement('div');note.className='empty unavailable';note.textContent='Live OVS rule transition history unavailable; current rule state is still shown only if flow queries succeed.';$('events').prepend(note)}};
const eventLabelCurrent=eventLabel;
eventLabel=function(e){const labels={service_traffic_served:'SERVICE RESPONSE OBSERVED',provider_started:'PROVIDER PROCESS STARTED',controller_notification_attempted:'STATUS NOTIFICATION ATTEMPT',controller_notification_acknowledged:'STATUS NOTIFICATION ACK',ovs_provider_drop_present_observed:'LIVE DROP RULE APPEARED',ovs_provider_drop_absent_observed:'LIVE DROP RULE REMOVED',provider_drop_flow_mod_submitted:'DROP FLOW MOD SUBMITTED',provider_drop_removal_submitted:'DROP REMOVAL MOD SUBMITTED'};return labels[e.kind]||eventLabelCurrent(e)};
const eventTextCurrent=eventText;
eventText=function(e){if(e.kind==='service_traffic_served')return e.client_ip+' → '+e.provider_id+' '+e.protocol+'/'+e.port+' response sent';return eventTextCurrent(e)};
const renderEventsCurrent=renderEvents;
renderEvents=function(t,a){renderEventsCurrent(t,a);if(!t.availability.dashboard_events){const note=document.createElement('div');note.className='empty unavailable';note.textContent='Live OVS rule transition history unavailable; current state requires a successful flow query.';$('events').prepend(note)}};
const eventLabelForWorkload=eventLabel;
eventLabel=function(e){return e.kind==='workload_request'?'CLIENT DISCOVERY RESULT':eventLabelForWorkload(e)};
const eventTextForWorkload=eventText;
eventText=function(e){if(e.kind==='workload_request')return `${e.request_id} · ${e.client_hostname} (${e.client_ip||'IP unavailable'}) requested ${e.service} · ${e.success?'selected '+(e.selected_provider||'provider unavailable'):'failed: '+e.outcome} · ${e.latency_ms==null?'response time unavailable':e.latency_ms+' ms'}`;return eventTextForWorkload(e)};
// Dashboard status and telemetry calls are read-only; workload controls run client.py via mnexec.
setupDashboardLayout();$('refresh').onclick=refresh;$('flowsBtn').onclick=()=>{document.querySelector('[data-tab="network"]')?.click();loadFlows()};$('controllerBtn').onclick=loadTelemetry;$('eventsBtn').onclick=loadEvents;$('discoverOnly').onclick=()=>runManualOperation('discover');$('send').onclick=()=>runManualOperation('service');$('workloadStart').onclick=startWorkload;$('workloadStop').onclick=stopWorkload;$('workloadMode').onchange=updateWorkloadMode;$('workloadPreset').onchange=applyWorkloadPreset;applyWorkloadPreset();updateWorkloadMode();refresh();loadTelemetry();loadWorkload();refreshNetworkLink();$('linkDownButton').onclick=()=>operateNetworkLink('down');$('linkUpButton').onclick=()=>operateNetworkLink('up');setInterval(refreshNetworkLink,5000);setInterval(refresh,5000);setInterval(loadTelemetry,1500);setInterval(loadWorkload,1000);setInterval(updateHeartbeatRings,1000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "ServiceDiscoveryDashboard/1.0"

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.path == "/api/status":
            self.send_json(get_status())
        elif path.path == "/api/discover":
            from urllib.parse import parse_qs
            service = parse_qs(path.query).get("service", ["FILE"])[0].upper()
            try:
                self.send_json(discover(service))
            except (OSError, RuntimeError, ValueError) as exc:
                self.send_json({"error": str(exc)}, 502)
        elif path.path == "/api/flows":
            self.send_json(get_flows())
        elif path.path == "/api/events":
            self.send_json(get_events())
        elif path.path == "/api/telemetry":
            self.send_json(get_telemetry())
        elif path.path == "/api/controller":
            self.send_json(get_controller_status())
        elif path.path == "/api/network-link":
            try:
                result = request_mininet_link("status")
                self.send_json(result, 200 if result.get("ok") else 503)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "target": "s1 ↔ s2", "status": "UNKNOWN",
                                "error": f"Cannot query the running Mininet topology: {exc}"}, 503)
        elif path.path == "/api/activities":
            activities = get_activities()
            workload = workload_manager.snapshot()
            activities["events"] = sorted(activities.get("events", []) + workload.get("records", []),
                                          key=lambda item: item.get("request_started_at") or item.get("timestamp") or "")[-250:]
            self.send_json(activities)
        elif path.path == "/api/workloads":
            self.send_json(workload_manager.snapshot())
        elif path.path == "/api/sdn-comparison":
            self.send_json(get_sdn_comparison())
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/client-operation":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 8192:
                    raise ValueError("Request body size is invalid")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object")
                result = workload_manager.run_one_shot(payload)
                self.send_json(result)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, 503)
            return
        if route in {"/api/workloads", "/api/workloads/stop"}:
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json({"error": "Workload controls are restricted to the dashboard host"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 8192:
                    raise ValueError("Request body size is invalid")
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = workload_manager.stop(payload.get("run_id")) if route.endswith("/stop") else workload_manager.start(payload)
                self.send_json(result, 202 if route == "/api/workloads" else 200)
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if route == "/api/provider-lifecycle":
            self.post_provider_lifecycle()
            return
        if route == "/api/network-link":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json({"ok": False, "error": "Network fault controls are restricted to the dashboard host"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1024:
                    raise ValueError("Request body size is invalid")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or payload.get("operation") not in {"down", "up"}:
                    raise ValueError("Operation must be down or up")
                result = request_mininet_link(payload["operation"])
                status = 200 if result.get("ok") else (409 if "already in progress" in result.get("error", "") else 503)
                self.send_json(result, status)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "target": "s1 ↔ s2", "status": "UNKNOWN", "error": str(exc)}, 400)
            except (OSError, RuntimeError) as exc:
                self.send_json({"ok": False, "target": "s1 ↔ s2", "status": "UNKNOWN",
                                "error": f"Mininet link operation failed: {exc}"}, 503)
            return
        if route != "/api/communicate":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 8192:
                raise ValueError("Request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            service = str(payload.get("service", "")).upper()
            request = str(payload.get("request", "PING"))
            if service not in {"FILE", "CHAT", "VIDEO"}:
                raise ValueError("Unsupported service")
            if len(request) > 512:
                raise ValueError("Request must be 512 characters or fewer")
            result = discover(service)
            if not result["providers"]:
                error = f"No active provider for {service}"
                record_activity(service, request, None, False, error=error,
                                client_ip=result.get("client_ip"))
                self.send_json({"error": error, "discovery": result, "discovery_latency_ms": result["discovery_latency_ms"]}, 409)
                return
            # Use actual provider entries returned by the registry; FILE uses first
            # sorted result unless direct communication fails.
            errors = []
            selected_provider = None
            for provider in result["providers"]:
                selected_provider = provider["id"]
                try:
                    response = communicate(provider, request)
                    record_activity(service, request, provider["id"], True, response=response,
                                    client_ip=result.get("client_ip"))
                    self.send_json({"service": service, "provider": provider["id"], "endpoint": provider, "request": request, "response": response, "discovery_raw": result["raw"], "discovery_latency_ms": result["discovery_latency_ms"]})
                    return
                except (OSError, RuntimeError, socket.timeout) as exc:
                    errors.append(f"{provider['id']}: {exc}")
            error = "All discovered providers failed communication"
            record_activity(service, request, selected_provider, False,
                            error=f"{error}: {'; '.join(errors)}",
                            client_ip=result.get("client_ip"))
            self.send_json({"error": error, "details": errors, "discovery_raw": result["raw"], "discovery_latency_ms": result["discovery_latency_ms"]}, 502)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except (OSError, RuntimeError, socket.timeout) as exc:
            if "service" in locals() and service in {"FILE", "CHAT", "VIDEO"} and "request" in locals():
                record_activity(service, request, None, False, error=str(exc))
            self.send_json({"error": str(exc)}, 502)

    def post_provider_lifecycle(self):
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_json({"error": "Provider controls are restricted to the dashboard host"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024:
                raise ValueError("Request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request must be a JSON object")
            provider_id = payload.get("provider")
            operation = payload.get("operation")
            if not isinstance(provider_id, str) or provider_id not in KNOWN:
                raise ValueError("Unknown provider")
            if not isinstance(operation, str) or operation not in {"offline", "online"}:
                raise ValueError("Operation must be offline or online")
            with _lifecycle_lock:
                requested = _lifecycle_requests.get(provider_id)
                if requested == operation:
                    self.send_json({"error": f"{provider_id} is already waiting for {operation} confirmation"}, 409)
                    return
                provider_status = registry_provider_status(provider_id)
                active = provider_status == "ACTIVE"
                if provider_status is None:
                    self.send_json({"error": "Registry status telemetry is unavailable"}, 503)
                    return
                if operation == "offline" and not active and requested != "online":
                    self.send_json({"error": f"{provider_id} is already inactive in the Registry"}, 409)
                    return
                if operation == "online" and active and requested != "offline":
                    self.send_json({"error": f"{provider_id} is already active in the Registry"}, 409)
                    return
                request_provider_lifecycle(provider_id, operation)
                _lifecycle_requests[provider_id] = operation
            record_lifecycle_activity(provider_id, operation)
            message = (f"Heartbeat pause acknowledged for {provider_id}; waiting for Registry timeout" if operation == "offline"
                       else f"Heartbeat resume acknowledged for {provider_id}; waiting for Registry recovery")
            self.send_json({"provider": provider_id, "operation": operation, "state": "requested", "message": message}, 202)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except (OSError, RuntimeError, socket.timeout) as exc:
            self.send_json({"error": str(exc)}, 502)

    def log_message(self, fmt, *args):
        print(f"[dashboard] {self.address_string()} {fmt % args}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard listening on http://{args.host}:{args.port}", flush=True)
    print(f"Registry target: {REGISTRY_HOST}:{REGISTRY_PORT}/UDP; registry log: {REGISTRY_LOG}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard shutting down", flush=True)
    finally:
        workload_manager.shutdown()
        server.server_close()

if __name__ == "__main__":
    main()
