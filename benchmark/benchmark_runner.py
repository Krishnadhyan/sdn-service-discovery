#!/usr/bin/env python3
"""Run bounded, process-based UDP service-discovery benchmarks."""
import argparse
import csv
import json
import multiprocessing as mp
import os
import queue
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ("FILE", "CHAT", "VIDEO")
POLICIES = ("FIRST_AVAILABLE", "ROUND_ROBIN")


def parse_response(raw, requested_service):
    """Parse the existing client-compatible UDP discovery response."""
    try:
        text = raw.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError) as exc:
        raise ValueError("malformed_response: response is not valid UTF-8") from exc
    fields = text.split("|")
    if fields[:2] == ["ERROR", "SERVICE_UNAVAILABLE"]:
        return None
    if len(fields) < 2 or fields[0] != "DISCOVER_RESPONSE" or fields[1] != requested_service:
        raise ValueError("malformed_response: unexpected registry response")
    if len(fields) == 2 or all(not entry for entry in fields[2:]):
        return None
    providers = []
    for entry in fields[2:]:
        if not entry:
            continue
        bits = entry.split(",")
        if len(bits) != 4 or not bits[0] or not bits[1] or not bits[2]:
            raise ValueError("malformed_response: malformed provider entry")
        try:
            port = int(bits[3])
        except ValueError as exc:
            raise ValueError("malformed_response: provider port is not an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("malformed_response: provider port out of range")
        providers.append({"id": bits[0], "protocol": bits[1], "ip": bits[2], "port": port})
    return providers or None


def _base_record(run_id, client_id, request_num, service, policy, worker_pid=None):
    return {
        "run_id": run_id,
        "client_id": client_id,
        "request_id": f"{client_id}-request-{request_num:06d}",
        "requested_service": service,
        "selection_policy": policy,
        "request_start_timestamp": None,
        "response_timestamp": None,
        "discovery_latency_ms": None,
        "selected_provider_id": None,
        "success": False,
        "failure_reason": None,
        "worker_pid": worker_pid,
    }


def _failure_record(run_id, client_id, request_num, service, policy, reason, worker_pid=None):
    result = _base_record(run_id, client_id, request_num, service, policy, worker_pid)
    result["failure_reason"] = reason
    return result


def _discover_one(run_id, client_id, request_num, service, policy, host, port, timeout):
    pid = os.getpid()
    result = _base_record(run_id, client_id, request_num, service, policy, pid)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            start = time.perf_counter_ns()
            result["request_start_timestamp"] = datetime.now(timezone.utc).isoformat()
            sock.sendto(f"DISCOVER|{service}".encode("utf-8"), (host, port))
            raw, _ = sock.recvfrom(8192)
            end = time.perf_counter_ns()
            result["response_timestamp"] = datetime.now(timezone.utc).isoformat()
        result["discovery_latency_ms"] = (end - start) / 1_000_000
        providers = parse_response(raw, service)
        if providers is None:
            result["failure_reason"] = "no_available_provider"
        else:
            result["success"] = True
            result["selected_provider_id"] = providers[0]["id"]
    except socket.timeout:
        result["failure_reason"] = "registry_timeout"
    except ValueError as exc:
        result["failure_reason"] = str(exc).split(":", 1)[0]
    except OSError as exc:
        result["failure_reason"] = f"socket_error: {exc}"
    except Exception as exc:  # Capture unexpected per-request failures in output.
        result["failure_reason"] = f"unexpected_error: {type(exc).__name__}: {exc}"
    return result


def _worker(client_num, requests, run_id, service, policy, host, port, timeout, result_queue):
    client_id = f"client-{client_num:06d}"
    for request_num in range(1, requests + 1):
        result = _discover_one(run_id, client_id, request_num, service, policy, host, port, timeout)
        result_queue.put(result)


def _write_outputs(output_dir, run_id, config, metadata, records, aggregates, status):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"benchmark_{run_id}"
    payload = {"configuration": config, "run_metadata": metadata, "aggregate_counts": aggregates,
               "execution_status": status, "request_results": records}
    json_path = directory / f"{stem}.json"
    csv_path = directory / f"{stem}.csv"
    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    fields = list(records[0]) if records else list(_base_record(run_id, "", 1, config["service"], config["policy"]))
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    return str(csv_path), str(json_path)


def _aggregates(records, requested):
    successes = sum(bool(item["success"]) for item in records)
    return {"total_requests": requested, "completed_requests": len(records), "successful_requests": successes,
            "failed_requests": len(records) - successes, "pending_requests": max(0, requested - len(records))}


def run_benchmark(clients=1, requests=10, max_concurrency=1, service="FILE", policy="FIRST_AVAILABLE",
                  timeout=2.0, registry_host="10.0.0.3", registry_port=5000, output_dir="benchmark-results",
                  worker_target=None, progress=True):
    """Run clients with at most max_concurrency active child processes."""
    if clients < 1 or requests < 1 or max_concurrency < 1:
        raise ValueError("clients, requests, and max_concurrency must be >= 1")
    if timeout <= 0 or not 1 <= registry_port <= 65535:
        raise ValueError("timeout must be > 0 and registry_port must be 1..65535")
    if service not in SERVICES or policy not in POLICIES:
        raise ValueError("unsupported service or policy")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    started = time.monotonic()
    requested = clients * requests
    context = mp.get_context("spawn")
    result_queue = context.Queue(maxsize=max(2, max_concurrency * 2))
    target = worker_target or _worker
    active = {}
    next_client = 1
    records_by_id = {}
    interrupted = False
    last_display = 0.0
    try:
        while next_client <= clients or active:
            while next_client <= clients and len(active) < max_concurrency:
                process = context.Process(target=target, args=(next_client, requests, run_id, service, policy,
                                      registry_host, registry_port, timeout, result_queue),
                                      name=f"discovery-{next_client}")
                process.start()
                active[next_client] = process
                next_client += 1
            try:
                item = result_queue.get(timeout=0.1)
                records_by_id[item["request_id"]] = item
            except queue.Empty:
                pass
            for client_num, process in list(active.items()):
                if process.is_alive():
                    continue
                process.join()
                del active[client_num]
                client_id = f"client-{client_num:06d}"
                for req_num in range(1, requests + 1):
                    request_id = f"{client_id}-request-{req_num:06d}"
                    if request_id not in records_by_id:
                        records_by_id[request_id] = _failure_record(
                            run_id, client_id, req_num, service, policy,
                            f"worker_process_error: exited with code {process.exitcode}", process.pid)
            now = time.monotonic()
            if progress and now - last_display >= 0.5:
                count = len(records_by_id)
                ok = sum(bool(r["success"]) for r in records_by_id.values())
                elapsed = now - started
                active_n = sum(p.is_alive() for p in active.values())
                print(f"\rRequests {count}/{requested} | success {ok} | failed {count-ok} | pending {requested-count} | "
                      f"workers {active_n}/{max_concurrency} | elapsed {elapsed:.1f}s | throughput {count/elapsed if elapsed else 0:.2f}/s",
                      end="", flush=True)
                last_display = now
    except KeyboardInterrupt:
        interrupted = True
        for process in active.values():
            if process.is_alive():
                process.terminate()
        for process in active.values():
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join()
        for client_num in range(next_client, clients + 1):
            for req_num in range(1, requests + 1):
                key = f"client-{client_num:06d}-request-{req_num:06d}"
                records_by_id.setdefault(key, _failure_record(run_id, f"client-{client_num:06d}", req_num,
                    service, policy, "worker_process_error: cancelled before start"))
        for client_num in list(active):
            for req_num in range(1, requests + 1):
                key = f"client-{client_num:06d}-request-{req_num:06d}"
                records_by_id.setdefault(key, _failure_record(run_id, f"client-{client_num:06d}", req_num,
                    service, policy, "worker_process_error: cancelled by user"))
    finally:
        result_queue.close()
        result_queue.join_thread()
    records = sorted(records_by_id.values(), key=lambda r: (r["client_id"], r["request_id"]))
    counts = _aggregates(records, requested)
    status = "cancelled" if interrupted else "completed"
    metadata = {"started_at": datetime.fromtimestamp(time.time() - (time.monotonic() - started), timezone.utc).isoformat(),
                "ended_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": time.monotonic() - started}
    config = {"clients": clients, "requests_per_client": requests, "max_concurrency": max_concurrency,
              "service": service, "policy": policy, "timeout_seconds": timeout, "registry_host": registry_host,
              "registry_port": registry_port, "output_dir": str(output_dir)}
    if not isinstance(config["registry_host"], (str, int, float, bool, type(None))):
        config["registry_host"] = "<controlled test worker argument>"
    csv_path, json_path = _write_outputs(output_dir, run_id, config, metadata, records, counts, status)
    if progress:
        print(f"\nRun {status}: {counts['successful_requests']} successful, {counts['failed_requests']} failed.\nCSV: {csv_path}\nJSON: {json_path}")
    return {"run_id": run_id, "records": records, "aggregates": counts, "status": status,
            "csv_path": csv_path, "json_path": json_path, "config": config}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=1, help="independent client processes (default: 1)")
    parser.add_argument("--requests", type=int, default=10, help="requests per client (default: 10)")
    parser.add_argument("--max-concurrency", type=int, default=2, help="maximum active processes (default: 2)")
    parser.add_argument("--service", choices=SERVICES, default="FILE")
    parser.add_argument("--policy", choices=POLICIES, default="FIRST_AVAILABLE", help="recorded desired registry policy")
    parser.add_argument("--timeout", type=float, default=2.0, help="per-request UDP timeout in seconds")
    parser.add_argument("--registry-host", default="10.0.0.3")
    parser.add_argument("--registry-port", type=int, default=5000)
    parser.add_argument("--output-dir", default="benchmark-results")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        run_benchmark(**vars(args))
    except ValueError as exc:
        build_parser().error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
