#!/usr/bin/env python3
"""Discover a service through T1, then talk directly to a returned provider."""
import argparse
import concurrent.futures
import datetime
import json
import socket
import time
import uuid

class DiscoveryError(RuntimeError): pass
class ProviderTimeout(TimeoutError): pass


def discover(registry_host, registry_port, service, timeout):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)

        start_time = time.perf_counter()
        sock.sendto(f'DISCOVER|{service}'.encode(), (registry_host, registry_port))
        raw, _ = sock.recvfrom(8192)
        end_time = time.perf_counter()

        latency_ms = (end_time - start_time) * 1000
        print(f'Discovery latency ({service}): {latency_ms:.3f} ms')

    return parse_discovery_response(raw.decode('utf-8').strip(), service)


def parse_discovery_response(text, service):
    fields = text.split('|')
    if fields[:2] == ['ERROR', 'SERVICE_UNAVAILABLE']:
        return []
    if len(fields) < 2 or fields[0] != 'DISCOVER_RESPONSE' or fields[1] != service:
        raise DiscoveryError(f'unexpected registry response: {text!r}')
    providers = []
    for item in fields[2:]:
        if not item: continue
        bits = item.split(',')
        if len(bits) != 4: raise DiscoveryError(f'malformed provider entry: {item!r}')
        pid, proto, ip, port = bits
        providers.append({'id': pid, 'protocol': proto, 'ip': ip, 'port': int(port)})
    return providers


def discovery_attempt(registry_host, registry_port, service, timeout, request_id, client_host):
    started_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    sent_monotonic = None
    sent_at = None
    client_ip = None
    response_at = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(f"DISCOVER|{service}".encode(), (registry_host, registry_port))
            sent_monotonic = time.perf_counter_ns()
            sent_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
            client_ip = sock.getsockname()[0]
            raw, _ = sock.recvfrom(8192)
        elapsed = (time.perf_counter_ns() - sent_monotonic) / 1_000_000
        response_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
        providers = parse_discovery_response(raw.decode("utf-8", "replace").strip(), service)
        return {"request_id": request_id, "client_hostname": client_host, "client_ip": client_ip,
                "service": service, "request_started_at": started_utc, "request_sent_at": sent_at, "response_at": response_at,
                "latency_ms": round(elapsed, 3), "selected_provider": providers[0]["id"] if providers else None,
                "provider_candidates": [p["id"] for p in providers], "success": True,
                "outcome": "success" if providers else "no_available_provider", "error": None,
                "pid": __import__("os").getpid(), "thread": __import__("threading").current_thread().name}
    except socket.timeout as exc:
        return {"request_id": request_id, "client_hostname": client_host, "client_ip": client_ip,
                "service": service, "request_started_at": started_utc, "request_sent_at": sent_at, "response_at": None,
                "latency_ms": None, "selected_provider": None, "provider_candidates": [],
                "success": False, "outcome": "registry_timeout", "error": str(exc),
                "pid": __import__("os").getpid(), "thread": __import__("threading").current_thread().name}
    except (OSError, DiscoveryError, UnicodeError, ValueError) as exc:
        elapsed = (time.perf_counter_ns() - sent_monotonic) / 1_000_000 if sent_monotonic else None
        outcome = "socket_error" if isinstance(exc, OSError) else "malformed_response"
        return {"request_id": request_id, "client_hostname": client_host, "client_ip": client_ip,
                "service": service, "request_started_at": started_utc, "request_sent_at": sent_at, "response_at": response_at,
                "latency_ms": round(elapsed, 3) if elapsed is not None else None, "selected_provider": None, "provider_candidates": [],
                "success": False, "outcome": outcome, "error": str(exc),
                "pid": __import__("os").getpid(), "thread": __import__("threading").current_thread().name}


def run_discovery_batch(service, registry_host, registry_port, timeout, requests, concurrency,
                        request_id_prefix, client_host):
    """Emit one JSONL result per actual UDP discovery performed by this host process."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        pending = set()
        next_index = 1
        while next_index <= requests or pending:
            while next_index <= requests and len(pending) < concurrency:
                pending.add(pool.submit(discovery_attempt, registry_host, registry_port, service, timeout,
                                        f"{request_id_prefix}-{next_index:06d}", client_host))
                next_index += 1
            done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                print(json.dumps(future.result(), separators=(",", ":")), flush=True)
    return 0

def communicate(provider, service, request, timeout):
    proto = provider['protocol'].upper()
    if proto == 'TCP':
        deadline = time.perf_counter() + timeout
        try:
            connection = socket.create_connection((provider['ip'], provider['port']), timeout=timeout)
        except socket.timeout as exc:
            raise ProviderTimeout('connection timeout') from exc
        with connection as sock:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise ProviderTimeout('provider response timeout')
            sock.settimeout(remaining)
            try:
                sock.sendall((request + '\n').encode())
                response = sock.recv(4096).decode('utf-8', errors='replace').strip()
            except socket.timeout as exc:
                raise ProviderTimeout('provider response timeout') from exc
    elif proto == 'UDP':
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(request.encode(), (provider['ip'], provider['port']))
            try:
                response, _ = sock.recvfrom(4096)
            except socket.timeout as exc:
                raise ProviderTimeout('provider response timeout') from exc
            response = response.decode('utf-8', errors='replace').strip()
    else: raise DiscoveryError(f'unsupported protocol from registry: {proto}')
    if not response: raise OSError('provider returned an empty response')
    return response


def run_one_shot(service, registry_host, registry_port, request, timeout, operation, client_host):
    """Perform one real discovery, optionally followed by provider service use."""
    started = time.perf_counter()
    discovery_started = None
    base = {"client": client_host, "client_ip": None, "service": service,
            "registry_destination": {"ip": registry_host, "port": registry_port},
            "raw_registry_response": None, "providers": [], "selected_provider": None,
            "provider_attempts": [], "final_provider": None,
            "discovery_attempted": False, "discovery_success": False, "discovery_latency_ms": None,
            "operation_latency_ms": None,
            "provider_connection_success": None, "provider_response": None,
            "provider_error": None, "operation_status": "discovery_failed"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            discovery_started = time.perf_counter()
            sock.sendto(f"DISCOVER|{service}".encode(), (registry_host, registry_port))
            base["discovery_attempted"] = True
            base["client_ip"] = sock.getsockname()[0]
            raw, _ = sock.recvfrom(8192)
        base["discovery_latency_ms"] = round((time.perf_counter() - discovery_started) * 1000, 3)
        raw_text = raw.decode("utf-8", "replace").strip()
        base["raw_registry_response"] = raw_text
        providers = parse_discovery_response(raw_text, service)
        base["providers"] = providers
        base["selected_provider"] = providers[0] if providers else None
        # A valid empty Registry response is still a successful discovery.
        # Service availability is a separate outcome.
        base["discovery_success"] = True
        if not providers:
            base["operation_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            base["operation_status"] = "no_available_provider"
            return base
        if operation == "discover":
            base["operation_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            base["operation_status"] = "discovery_succeeded"
            return base
        provider_errors = []
        for provider in providers:
            attempt = {"provider_id": provider["id"], "endpoint": provider,
                       "status": "attempting", "error": None}
            base["provider_attempts"].append(attempt)
            try:
                response = communicate(provider, service, request, timeout)
            except (OSError, socket.timeout, DiscoveryError) as exc:
                attempt["status"] = "timeout" if isinstance(exc, TimeoutError) else "failed"
                attempt["error"] = str(exc)
                provider_errors.append(f"{provider['id']}: {exc}")
                continue
            attempt["status"] = "succeeded"
            attempt["response"] = response
            base["provider_connection_success"] = True
            base["final_provider"] = provider
            base["provider_response"] = response
            base["operation_status"] = "service_succeeded"
            break
        else:
            base["provider_connection_success"] = False
            base["provider_error"] = "; ".join(provider_errors)
            base["operation_status"] = "service_failed"
        base["operation_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return base
    except socket.timeout as exc:
        base["discovery_latency_ms"] = round((time.perf_counter() - discovery_started) * 1000, 3) if discovery_started is not None else None
        base["operation_latency_ms"] = base["discovery_latency_ms"]
        base["error"] = str(exc)
        base["operation_status"] = "registry_timeout"
        return base
    except (OSError, DiscoveryError, UnicodeError, ValueError) as exc:
        base["discovery_latency_ms"] = round((time.perf_counter() - discovery_started) * 1000, 3) if discovery_started is not None else None
        base["operation_latency_ms"] = base["discovery_latency_ms"]
        base["error"] = str(exc)
        base["operation_status"] = "discovery_error"
        return base


def run_one_shot_cli(service, registry_host, registry_port, request, timeout, operation, client_host):
    result = run_one_shot(service, registry_host, registry_port, request, timeout, operation, client_host)
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0 if result["operation_status"] in {"discovery_succeeded", "service_succeeded"} else 2


def _service_workload_request(index, service, request, timeout, mode, registry_host,
                              registry_port, client_host, static_provider):
    started = time.perf_counter()
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    request_id = f"{client_host}-request-{index:06d}"
    if mode == "dynamic":
        result = run_one_shot(service, registry_host, registry_port, request, timeout,
                              "service", client_host)
        attempts = result["provider_attempts"]
        providers = result.get("providers", [])
        final_provider = result["final_provider"]
        provider_response = result["provider_response"]
        discovery_latency = result["discovery_latency_ms"]
        discovery_attempted = result["discovery_attempted"]
        discovery_success = result["discovery_success"]
        success = result["operation_status"] == "service_succeeded"
        error = result.get("provider_error") or result.get("error")
        if not error and not success:
            error = result.get("operation_status")
        client_ip = result.get("client_ip")
    else:
        attempts = [{"provider_id": static_provider["id"], "endpoint": static_provider,
                     "status": "attempting", "error": None}]
        providers = [static_provider]
        final_provider = None
        provider_response = None
        discovery_latency = None
        discovery_attempted = False
        discovery_success = None
        error = None
        try:
            provider_response = communicate(static_provider, service, request, timeout)
            attempts[0].update(status="succeeded", response=provider_response)
            final_provider = static_provider
            success = True
        except (OSError, socket.timeout, DiscoveryError) as exc:
            attempts[0].update(status="timeout" if isinstance(exc, TimeoutError) else "failed",
                               error=str(exc))
            error = str(exc)
            success = False
        client_ip = None
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    provider_id = final_provider["id"] if final_provider else None
    return {"request_id": request_id, "client_hostname": client_host, "client_ip": client_ip,
            "service": service, "execution_mode": mode, "request": request,
            "request_started_at": timestamp,
            "response_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
            "discovery_attempted": discovery_attempted, "discovery_success": discovery_success,
            "discovery_latency_ms": discovery_latency,
            "end_to_end_latency_ms": elapsed_ms, "latency_ms": discovery_latency,
            "providers": providers, "provider_candidates": [p["id"] for p in providers],
            "provider_attempts": attempts, "selected_provider": provider_id,
            "final_provider": final_provider, "provider_response": provider_response,
            "success": success,
            "outcome": "service_succeeded" if success else ("service_failed" if discovery_success is not False else "discovery_failed"),
            "error": error}


def run_service_workload(service, registry_host, registry_port, request, timeout,
                         requests, concurrency, execution_mode, client_host,
                         static_provider):
    if service not in {"FILE", "CHAT", "VIDEO"}:
        raise ValueError("unsupported service")
    if execution_mode == "static" and service != "FILE":
        raise ValueError("static execution is currently supported only for FILE")
    if execution_mode not in {"dynamic", "static"}:
        raise ValueError("execution mode must be dynamic or static")
    if not 1 <= requests <= 500 or not 1 <= concurrency <= 10:
        raise ValueError("requests must be 1..500 and concurrency must be 1..10")
    endpoint = {"id": "FILE-1", "protocol": "TCP", "ip": "10.0.0.4", "port": 5001}
    if static_provider:
        endpoint.update(static_provider)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_service_workload_request, index, service, request, timeout,
                               execution_mode, registry_host, registry_port, client_host, endpoint)
                   for index in range(1, requests + 1)]
        for future in concurrent.futures.as_completed(futures):
            print(json.dumps(future.result(), separators=(",", ":")), flush=True)
    return 0

def run(service, registry_host, registry_port, request, timeout, attempts, retry_delay, preferred):
    last_error = 'no active provider was returned by registry'
    for attempt in range(1, attempts + 1):
        try:
            providers = discover(registry_host, registry_port, service, timeout)
            print(f'Discovery {service}: ' + (', '.join(f"{p['id']} ({p['protocol']} {p['ip']}:{p['port']})" for p in providers) if providers else 'unavailable'), flush=True)
            if preferred:
                providers.sort(key=lambda p: (p['id'] != preferred, p['id']))
            for provider in providers:
                try:
                    response = communicate(provider, service, request, timeout)
                    print(f"Provider response from {provider['id']}: {response}")
                    return 0
                except (OSError, socket.timeout) as exc:
                    last_error = f"{provider['id']} failed: {exc}"
                    print(last_error, flush=True)
        except (OSError, socket.timeout, DiscoveryError) as exc:
            last_error = f'discovery failed: {exc}'
            print(last_error, flush=True)
        if attempt < attempts:
            time.sleep(retry_delay)
    print(f'SERVICE_UNAVAILABLE|{service}: {last_error}')
    return 2

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('service', choices=['FILE', 'CHAT', 'VIDEO'])
    p.add_argument('--registry-host', default='10.0.0.3')
    p.add_argument('--registry-port', type=int, default=5000)
    p.add_argument('--request', default='PING')
    p.add_argument('--timeout', type=float, default=2.0)
    p.add_argument('--attempts', type=int, default=3)
    p.add_argument('--retry-delay', type=float, default=1.0)
    p.add_argument('--prefer', help='provider ID to try first, e.g. FILE-1')
    p.add_argument('--discover-only', action='store_true', help='Send a bounded discovery-only JSONL workload; do not contact providers')
    p.add_argument('--requests', type=int, default=1, help='Requests in discovery-only mode (1..500)')
    p.add_argument('--concurrency', type=int, default=1, help='Concurrent requests in discovery-only mode (1..10)')
    p.add_argument('--request-id-prefix', default=None, help='Unique workload prefix for result IDs')
    p.add_argument('--client-host', default='mininet-client', help='Mininet hostname recorded in JSONL results')
    p.add_argument('--one-shot-operation', choices=['discover', 'service'],
                   help='Perform one structured discovery, optionally followed by service communication')
    p.add_argument('--service-workload', action='store_true',
                   help='Run repeated real service requests and emit one JSON record per request')
    p.add_argument('--execution-mode', choices=['dynamic', 'static'], default='dynamic')
    p.add_argument('--static-provider-id', default='FILE-1')
    p.add_argument('--static-provider-ip', default='10.0.0.4')
    p.add_argument('--static-provider-port', type=int, default=5001)
    a = p.parse_args()
    if a.one_shot_operation:
        if a.timeout <= 0:
            p.error('timeout must be >0')
        raise SystemExit(run_one_shot_cli(a.service, a.registry_host, a.registry_port,
                                          a.request, a.timeout, a.one_shot_operation, a.client_host))
    if a.service_workload:
        if a.timeout <= 0 or a.requests < 1 or a.concurrency < 1:
            p.error('service workload requires timeout > 0, requests >= 1 and concurrency >= 1')
        endpoint = {'id': a.static_provider_id, 'protocol': 'TCP',
                    'ip': a.static_provider_ip, 'port': a.static_provider_port}
        raise SystemExit(run_service_workload(a.service, a.registry_host, a.registry_port,
                    a.request, a.timeout, a.requests, a.concurrency, a.execution_mode,
                    a.client_host, endpoint))
    if a.discover_only:
        if not 1 <= a.requests <= 500 or not 1 <= a.concurrency <= 10 or a.timeout <= 0:
            p.error('discovery-only mode requires requests 1..500, concurrency 1..10 and timeout >0')
        prefix = a.request_id_prefix or uuid.uuid4().hex
        raise SystemExit(run_discovery_batch(a.service, a.registry_host, a.registry_port,
                                             a.timeout, a.requests, a.concurrency, prefix, a.client_host))
    if a.attempts < 1 or a.timeout <= 0 or a.retry_delay < 0: p.error('attempts must be >=1, timeout >0, retry-delay >=0')
    raise SystemExit(run(a.service, a.registry_host, a.registry_port, a.request, a.timeout, a.attempts, a.retry_delay, a.prefer))

if __name__ == '__main__': main()
