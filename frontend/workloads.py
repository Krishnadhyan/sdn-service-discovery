"""Bounded orchestration of real client processes inside Mininet namespaces."""
import json
import csv
import math
import statistics
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SCRIPT = ROOT / "client" / "client.py"
CLIENT_IPS = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h11": "10.0.0.11", "h12": "10.0.0.12", "h13": "10.0.0.13"}
MNEXEC = os.getenv("MNEXEC", "/usr/bin/mnexec")
MAX_PROVIDERS_PER_SERVICE = 3  # FILE has the largest configured provider pool.
STATIC_FILE_PROVIDER = {"id": "FILE-1", "protocol": "TCP", "ip": "10.0.0.4", "port": 5001}


def find_mininet_host_pids():
    found = {host: [] for host in CLIENT_IPS}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [x.decode(errors="replace") for x in (entry / "cmdline").read_bytes().split(b"\0") if x]
        except OSError:
            continue
        for host in found:
            if f"mininet:{host}" in argv:
                found[host].append(int(entry.name))
    return {host: pids[0] for host, pids in found.items() if len(pids) == 1}


class WorkloadManager:
    """One active bounded workload; one existing client process per selected host."""
    def __init__(self, pid_finder=find_mininet_host_pids, popen=subprocess.Popen,
                 results_dir=None):
        self.pid_finder, self.popen = pid_finder, popen
        self.results_dir = Path(results_dir) if results_dir else None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.active = None
        self.processes = []
        self.history = []

    def start(self, payload):
        clients = payload.get("clients")
        service = str(payload.get("service", "")).upper()
        requests = payload.get("requests_per_client")
        concurrency = payload.get("concurrency_per_client", 1)
        mode = payload.get("mode", "concurrent")
        execution_mode = payload.get("execution_mode", "dynamic")
        timeout = payload.get("timeout", 2.0)
        experiment = payload.get("experiment")
        if not isinstance(clients, list) or not clients or len(set(clients)) != len(clients) or any(c not in CLIENT_IPS for c in clients):
            raise ValueError("Select one or more distinct configured Mininet clients")
        if service not in {"FILE", "CHAT", "VIDEO"}:
            raise ValueError("Service must be FILE, CHAT or VIDEO")
        if execution_mode not in {"dynamic", "static"}:
            raise ValueError("Execution mode must be dynamic or static")
        if execution_mode == "static" and service != "FILE":
            raise ValueError("Static execution is currently supported only for FILE")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 30:
            raise ValueError("Timeout must be between 0.1 and 30 seconds")
        if type(requests) is not int or not 1 <= requests <= 500:
            raise ValueError("Requests per client must be between 1 and 500")
        if type(concurrency) is not int or not 1 <= concurrency <= 10:
            raise ValueError("Concurrency per client must be between 1 and 10")
        if mode not in {"sequential", "concurrent"}:
            raise ValueError("Mode must be sequential or concurrent")
        if mode == "sequential":
            concurrency = 1
        pids = self.pid_finder()
        missing = [host for host in clients if host not in pids]
        if missing:
            raise RuntimeError("Mininet client namespaces not found (start topology): " + ", ".join(missing))
        with self.lock:
            if self.active and self.active["status"] in {"starting", "running", "stopping"}:
                raise RuntimeError("A workload is already active")
            if self.active and self.active["status"] in {"completed", "failed", "stopped"}:
                old = self.active
                distribution = {}
                for row in old["records"]:
                    provider = row.get("selected_provider")
                    if provider:
                        distribution[provider] = distribution.get(provider, 0) + 1
                self.history.append({k: old.get(k) for k in (
                    "run_id", "status", "clients", "service", "requests_per_client",
                    "concurrency_per_client", "mode", "execution_mode", "timeout",
                    "experiment",
                    "total_requests", "completed", "successful", "failed",
                    "success_rate_percent", "successful_requests_per_second",
                    "discovery_success_rate_percent", "successful_discoveries_per_second",
                    "mean_discovery_latency_ms", "p95_discovery_latency_ms", "mean_end_to_end_latency_ms",
                    "elapsed_seconds", "result_file", "static_provider")})
                self.history[-1]["selection_policy"] = old.get("selection_policy")
                self.history[-1]["provider_distribution"] = distribution
                self.history = self.history[-10:]
            run_id = uuid.uuid4().hex
            self.stop_event = threading.Event()
            self.processes = []
            self.active = {"run_id": run_id, "status": "starting", "clients": clients,
                           "service": service, "requests_per_client": requests,
                           "concurrency_per_client": concurrency, "mode": mode,
                           "execution_mode": execution_mode, "timeout": float(timeout),
                           "experiment": experiment if isinstance(experiment, str) and experiment in {"A", "B", "C"} else None,
                           "registry_destination": {"ip": "10.0.0.3", "port": 5000},
                           "static_provider": dict(STATIC_FILE_PROVIDER) if execution_mode == "static" else None,
                           "selection_policy": payload.get("selection_policy") if payload.get("selection_policy") in {"FIRST_AVAILABLE", "ROUND_ROBIN"} else None,
                           "total_requests": len(clients) * requests, "completed": 0,
                           "successful": 0, "failed": 0, "pending": len(clients) * requests,
                           "records": [], "started_at": time.time(), "started_monotonic": time.monotonic(), "error": None}
            thread = threading.Thread(target=self._run, args=(dict(self.active), pids), daemon=True,
                                      name=f"dashboard-workload-{run_id[:8]}")
            self.active["thread"] = thread
            thread.start()
            return self.snapshot()

    def run_one_shot(self, payload):
        """Run one client operation in its existing Mininet namespace."""
        host = payload.get("client")
        service = str(payload.get("service", "")).upper()
        operation = payload.get("operation", "discover")
        request = payload.get("request", "PING")
        timeout = payload.get("timeout", 2.0)
        if not isinstance(host, str) or host not in CLIENT_IPS:
            raise ValueError("Select a configured Mininet client")
        if service not in {"FILE", "CHAT", "VIDEO"}:
            raise ValueError("Service must be FILE, CHAT or VIDEO")
        if not isinstance(operation, str) or operation not in {"discover", "service"}:
            raise ValueError("Operation must be discover or service")
        if not isinstance(request, str) or len(request) > 512:
            raise ValueError("Request must be a string of 512 characters or fewer")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 30:
            raise ValueError("Timeout must be between 0.1 and 30 seconds")

        pids = self.pid_finder()
        if host not in pids:
            raise RuntimeError(f"Mininet client namespace not found: {host}")
        args = [MNEXEC, "-a", str(pids[host]), sys.executable, str(CLIENT_SCRIPT), service,
                "--registry-host", "10.0.0.3", "--registry-port", "5000",
                "--timeout", str(timeout), "--one-shot-operation", operation,
                "--client-host", host, "--request", request]
        process = None
        try:
            process = self.popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1)
            try:
                # Leave room for the Registry timeout plus one bounded client
                # attempt per returned provider, including the largest pool.
                process_timeout = ((1 + MAX_PROVIDERS_PER_SERVICE) * float(timeout) + 2)
                stdout, stderr = process.communicate(timeout=process_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                return {"client": host, "client_ip": CLIENT_IPS[host], "service": service,
                        "registry_destination": {"ip": "10.0.0.3", "port": 5000},
                        "raw_registry_response": None, "providers": [], "selected_provider": None,
                        "discovery_success": False, "discovery_latency_ms": None,
                        "provider_connection_success": None, "provider_response": None,
                        "operation_status": "client_process_timeout",
                        "error": (stderr or "Client operation exceeded its execution deadline").strip()}
            rows = [line for line in stdout.splitlines() if line.strip()]
            if len(rows) != 1:
                raise RuntimeError((stderr or "Client returned no single structured result").strip())
            result = json.loads(rows[0])
            if not isinstance(result, dict) or result.get("client") != host or result.get("service") != service:
                raise RuntimeError("Client returned a result that did not match the request")
            if process.returncode not in {0, 2}:
                raise RuntimeError((stderr or f"Client process exited {process.returncode}").strip())
            # An unbound UDP socket may still report the wildcard address in
            # some Mininet environments; the selected host has a fixed lab IP.
            client_ip = result.get("client_ip")
            result["client_ip"] = CLIENT_IPS[host] if client_ip in {None, "", "0.0.0.0"} else client_ip
            result["client_process_exit_code"] = process.returncode
            return result
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return {"client": host, "client_ip": CLIENT_IPS[host], "service": service,
                    "registry_destination": {"ip": "10.0.0.3", "port": 5000},
                    "raw_registry_response": None, "providers": [], "selected_provider": None,
                    "discovery_success": False, "discovery_latency_ms": None,
                    "provider_connection_success": None, "provider_response": None,
                    "operation_status": "client_execution_error", "error": str(exc)}

    def _record(self, row):
        row["run_id"] = self.active["run_id"]
        row["kind"] = "workload_request"
        row["timestamp"] = row.get("response_at") or row.get("request_started_at") or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        if row.get("client_ip") in {None, "", "0.0.0.0"}:
            row["client_ip"] = CLIENT_IPS.get(row.get("client_hostname"))
        with self.lock:
            self.active["records"].append(row)
            self.active["completed"] += 1
            if row.get("success"):
                self.active["successful"] += 1
            else:
                self.active["failed"] += 1
            self.active["pending"] = self.active["total_requests"] - self.active["completed"]

    def _run_host(self, cfg, host, pid, prefix):
        args = [MNEXEC, "-a", str(pid), sys.executable, str(CLIENT_SCRIPT), cfg["service"],
                "--registry-host", "10.0.0.3", "--registry-port", "5000", "--timeout", str(cfg["timeout"]),
                "--service-workload", "--execution-mode", cfg["execution_mode"],
                "--requests", str(cfg["requests_per_client"]),
                "--concurrency", str(cfg["concurrency_per_client"]),
                "--client-host", host, "--static-provider-id", STATIC_FILE_PROVIDER["id"],
                "--static-provider-ip", STATIC_FILE_PROVIDER["ip"],
                "--static-provider-port", str(STATIC_FILE_PROVIDER["port"])]
        observed = set()
        expected_ids = {f"{host}-request-{index:06d}" for index in range(1, cfg["requests_per_client"] + 1)}
        process = None
        try:
            process = self.popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            with self.lock:
                self.processes.append(process)
                self.active["status"] = "running"
            for line in process.stdout:
                if self.stop_event.is_set():
                    break
                try:
                    row = json.loads(line)
                    request_id = row.get("request_id")
                    if (request_id in expected_ids and request_id not in observed
                            and row.get("client_hostname") == host
                            and row.get("service") == cfg["service"]):
                        observed.add(request_id)
                        self._record(row)
                except (ValueError, TypeError):
                    continue
            if self.stop_event.is_set() and process.poll() is None:
                process.terminate()
            stderr = process.stderr.read() if process.stderr else ""
            code = process.wait()
            if code and not self.stop_event.is_set():
                with self.lock:
                    self.active["error"] = (stderr.strip() or f"{host} client process exited {code}")[:1000]
        except Exception as exc:
            with self.lock:
                self.active["error"] = str(exc)[:1000]
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except Exception:
                    pass
        finally:
            # Preserve exact accounting on process failure/cancellation. Missing
            # results are explicit process outcomes, never successful discoveries.
            for index in range(1, cfg["requests_per_client"] + 1):
                request_id = f"{host}-request-{index:06d}"
                if request_id not in observed:
                    self._record({"request_id": request_id, "client_hostname": host,
                                  "client_ip": CLIENT_IPS[host], "service": cfg["service"],
                                  "request_started_at": None, "response_at": None,
                                  "latency_ms": None, "discovery_latency_ms": None,
                                  "end_to_end_latency_ms": None, "provider_attempts": [],
                                  "final_provider": None, "provider_response": None,
                                  "execution_mode": cfg["execution_mode"], "selected_provider": None,
                                  "provider_candidates": [], "success": False,
                                  "outcome": "worker_process_error" if not self.stop_event.is_set() else "cancelled",
                                  "error": self.active.get("error") or "Worker stopped before a result was recorded",
                                  "pid": getattr(process, "pid", None) if process else None})

    def _run(self, cfg, pids):
        try:
            started_hosts = set()
            if cfg["mode"] == "concurrent":
                workers = []
                for host in cfg["clients"]:
                    if self.stop_event.is_set():
                        break
                    started_hosts.add(host)
                    prefix = cfg["run_id"] + "-" + host
                    worker = threading.Thread(target=self._run_host, args=(cfg, host, pids[host], prefix), daemon=True)
                    workers.append(worker)
                    worker.start()
                for worker in workers:
                    worker.join()
            else:
                for host in cfg["clients"]:
                    if self.stop_event.is_set():
                        break
                    started_hosts.add(host)
                    self._run_host(cfg, host, pids[host], cfg["run_id"] + "-" + host)
            for host in cfg["clients"]:
                if host in started_hosts:
                    continue
                for index in range(1, cfg["requests_per_client"] + 1):
                    self._record({"request_id": f"{host}-request-{index:06d}",
                                  "client_hostname": host, "client_ip": CLIENT_IPS[host],
                                  "service": cfg["service"], "request_started_at": None,
                                  "response_at": None, "latency_ms": None,
                                  "discovery_latency_ms": None, "end_to_end_latency_ms": None,
                                  "provider_attempts": [], "final_provider": None,
                                  "provider_response": None, "execution_mode": cfg["execution_mode"],
                                  "selected_provider": None, "provider_candidates": [],
                                  "success": False, "outcome": "cancelled",
                                  "error": "Cancelled before host worker was started", "pid": None})
            with self.lock:
                self.active["status"] = "stopped" if self.stop_event.is_set() else ("failed" if self.active["error"] else "completed")
            self._finish_metrics_and_persist()
        except Exception as exc:
            with self.lock:
                self.active["error"] = str(exc)
                self.active["status"] = "failed"

    def stop(self, run_id=None):
        with self.lock:
            if not self.active or (run_id and run_id != self.active["run_id"]):
                raise ValueError("No matching workload is active")
            if self.active["status"] not in {"starting", "running", "stopping"}:
                return self.snapshot()
            self.active["status"] = "stopping"
            self.stop_event.set()
            for proc in self.processes:
                if proc.poll() is None:
                    proc.terminate()
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            if not self.active:
                return {"status": "idle", "records": []}
            result = {k: v for k, v in self.active.items() if k != "thread"}
            result["elapsed_seconds"] = result.get("elapsed_seconds") or max(0, time.time() - result["started_at"])
            result["provider_distribution"] = {}
            for row in result.get("records", []):
                provider = row.get("selected_provider")
                if provider:
                    result["provider_distribution"][provider] = result["provider_distribution"].get(provider, 0) + 1
            result.update(self._metrics(result))
            result["history"] = list(self.history)
            return result

    @staticmethod
    def _metrics(run):
        records = run.get("records", [])
        successful = [row for row in records if row.get("success") is True]
        dynamic_attempts = [row for row in records
                            if row.get("execution_mode", run.get("execution_mode")) == "dynamic"
                            and row.get("discovery_attempted") is True]
        discovered = [row for row in dynamic_attempts if row.get("discovery_success") is True]
        discovery = [float(row["discovery_latency_ms"]) for row in discovered
                     if row.get("discovery_latency_ms") is not None]
        end_to_end = [float(row["end_to_end_latency_ms"]) for row in records
                      if row.get("end_to_end_latency_ms") is not None]
        elapsed = float(run.get("elapsed_seconds") or 0)
        completed = int(run.get("completed") or 0)
        return {
            "success_rate_percent": 100.0 * len(successful) / completed if completed else None,
            "successful_requests_per_second": len(successful) / elapsed if elapsed > 0 else None,
            "mean_discovery_latency_ms": statistics.mean(discovery) if discovery else None,
            "p95_discovery_latency_ms": (sorted(discovery)[math.ceil(0.95 * len(discovery)) - 1]
                                         if discovery else None),
            "discovery_success_rate_percent": (100.0 * len(discovered) / len(dynamic_attempts)
                                               if dynamic_attempts else None),
            "successful_discoveries_per_second": len(discovered) / elapsed
                                                 if dynamic_attempts and elapsed > 0 else None,
            "mean_end_to_end_latency_ms": statistics.mean(end_to_end) if end_to_end else None,
        }

    def _finish_metrics_and_persist(self):
        if not self.active:
            return
        with self.lock:
            self.active["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self.active["elapsed_seconds"] = max(0.0, time.monotonic() - self.active["started_monotonic"])
            self.active.update(self._metrics(self.active))
            if self.results_dir is None:
                return
            run = {key: value for key, value in self.active.items()
                   if key not in {"thread", "started_monotonic"}}
            config = {key: self.active.get(key) for key in (
                "clients", "service", "requests_per_client", "concurrency_per_client",
                "mode", "execution_mode", "timeout", "experiment", "selection_policy")}
            config["registry_destination"] = self.active.get("registry_destination")
            config["static_provider"] = self.active.get("static_provider")
        self.results_dir.mkdir(parents=True, exist_ok=True)
        stem = f"workload_{run['run_id']}"
        json_path = self.results_dir / f"{stem}.json"
        csv_path = self.results_dir / f"{stem}.csv"
        with json_path.open("w", encoding="utf-8") as stream:
            json.dump({"configuration": config, "summary": self._metrics(run),
                       "run": run}, stream, indent=2)
            stream.write("\n")
        records = run.get("records", [])
        fields = sorted({field for row in records for field in row})
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or ["request_id"])
            writer.writeheader()
            for row in records:
                writer.writerow({key: json.dumps(value, separators=(",", ":"))
                                 if isinstance(value, (list, dict)) else value
                                 for key, value in row.items()})
        with self.lock:
            if self.active and self.active["run_id"] == run["run_id"]:
                self.active["result_file"] = str(json_path)

    def shutdown(self, timeout=5):
        """Stop only this manager's children and join its coordinator thread."""
        with self.lock:
            thread = self.active.get("thread") if self.active else None
            running = bool(self.active and self.active["status"] in {"starting", "running", "stopping"})
            if running:
                self.active["status"] = "stopping"
                self.stop_event.set()
            processes = list(self.processes)
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except (OSError, AttributeError):
                pass
        if thread:
            thread.join(timeout)
        for process in processes:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired, AttributeError):
                pass
        if thread and thread.is_alive():
            thread.join(2)


manager = WorkloadManager(results_dir=ROOT / "benchmark-results")
