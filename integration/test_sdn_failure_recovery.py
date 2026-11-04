#!/usr/bin/env python3
"""Run an end-to-end FILE provider failure/recovery check in Mininet.

Run as root with: sudo python3 integration/test_sdn_failure_recovery.py
The test adopts a complete running topology when present. Otherwise it starts
and fully cleans up a topology and controller of its own.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integration"
REGISTRY = ROOT / "registry" / "registry.py"
PROVIDER = ROOT / "providers" / "provider.py"
CLIENT = ROOT / "client" / "client.py"
CONTROLLER = INTEGRATION / "run_controller.py"
MNEXEC = "/usr/bin/mnexec"
TOKEN_FILE = Path("/tmp/service_discovery_lifecycle.token")
COMPARISON_FILE = Path(os.getenv("SDN_COMPARISON_FILE", "/tmp/sdn_failure_comparison.json"))
REGISTRY_HOST = "10.0.0.3"
REGISTRY_PORT = 5000
CONTROLLER_HTTP = "http://10.0.0.254:8080/provider-status"
TEST_HEARTBEAT_TIMEOUT = 30
VIDEO_MEASUREMENTS_FILE = Path(os.getenv(
    "VIDEO_FAILURE_RECOVERY_MEASUREMENTS_FILE", "/tmp/video_failure_recovery_measurements.json"))
REGISTRY_STATS_FILE = Path(os.getenv("REGISTRY_STATS_FILE", "/tmp/registry_stats.json"))

# host, provider ID, service, IP, protocol, port
PROVIDERS = (
    ("h4", "FILE-1", "FILE", "10.0.0.4", "TCP", 5001),
    ("h5", "FILE-2", "FILE", "10.0.0.5", "TCP", 5001),
    ("h8", "FILE-3", "FILE", "10.0.0.8", "TCP", 5001),
    ("h6", "CHAT-1", "CHAT", "10.0.0.6", "TCP", 5002),
    ("h9", "CHAT-2", "CHAT", "10.0.0.9", "TCP", 5002),
    ("h7", "VIDEO-1", "VIDEO", "10.0.0.7", "UDP", 5003),
    ("h10", "VIDEO-2", "VIDEO", "10.0.0.10", "UDP", 5003),
)


class IntegrationFailure(RuntimeError):
    pass


def report(message):
    print(message, flush=True)


def pass_step(message):
    report(f"PASS: {message}")


def fail_step(message):
    report(f"FAIL: {message}")
    raise IntegrationFailure(message)


def read_processes():
    """Return (pid, argv, cwd) for readable processes without shell matching."""
    found = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [part.decode(errors="replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
            cwd = os.readlink(entry / "cwd")
        except (OSError, PermissionError):
            continue
        found.append((int(entry.name), argv, cwd))
    return found


def mininet_host_pids(processes=None):
    processes = processes if processes is not None else read_processes()
    result = {}
    for pid, argv, _cwd in processes:
        for name in (f"h{i}" for i in range(1, 14)):
            if f"mininet:{name}" in argv:
                result.setdefault(name, []).append(pid)
    return result


def existing_provider_pids(processes=None):
    processes = processes if processes is not None else read_processes()
    result = {spec[1]: [] for spec in PROVIDERS}
    for pid, argv, _cwd in processes:
        if not any(arg.endswith("providers/provider.py") for arg in argv):
            continue
        for index, arg in enumerate(argv[:-1]):
            if arg == "--id" and argv[index + 1] in result:
                result[argv[index + 1]].append(pid)
    return result


def tcp_open(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def command(args, timeout=5, check=True):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise IntegrationFailure(
            f"command failed ({result.returncode}): {' '.join(map(str, args))}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def discover_in_host(host_pid, service, timeout=2.0):
    code = (
        "import socket,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
        "s.settimeout(float(sys.argv[3])); s.sendto(('DISCOVER|'+sys.argv[1]).encode(), "
        "(sys.argv[2],5000)); print(s.recvfrom(8192)[0].decode().strip())"
    )
    result = command([MNEXEC, "-a", str(host_pid), sys.executable, "-c", code,
                      service, REGISTRY_HOST, str(timeout)], timeout=timeout + 2, check=False)
    if result.returncode:
        raise TimeoutError(result.stderr.strip() or result.stdout.strip() or "Registry did not reply")
    return result.stdout.strip()


def provider_ids(response, service):
    fields = response.split("|")
    if fields[:2] == ["ERROR", "SERVICE_UNAVAILABLE"]:
        return []
    if len(fields) < 2 or fields[0] != "DISCOVER_RESPONSE" or fields[1] != service:
        raise IntegrationFailure(f"unexpected {service} discovery response: {response!r}")
    ids = []
    for entry in fields[2:]:
        if not entry:
            continue
        pieces = entry.split(",")
        if len(pieces) != 4:
            raise IntegrationFailure(f"malformed provider entry: {entry!r}")
        ids.append(pieces[0])
    return ids


def wait_for(predicate, timeout, description, interval=0.25):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, TimeoutError, IntegrationFailure) as exc:
            last_error = exc
        time.sleep(interval)
    suffix = f" (last observation: {last_error})" if last_error else ""
    raise IntegrationFailure(f"timed out waiting for {description}{suffix}")


def run_host_python(host_pid, code, args=(), timeout=4, check=False):
    return command([MNEXEC, "-a", str(host_pid), sys.executable, "-c", code, *args],
                   timeout=timeout, check=check)


def service_probe(host_pid, provider_id, ip, protocol, port, timeout=1.2):
    code = r'''import socket,sys
provider,ip,proto,port,limit=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),float(sys.argv[5])
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM if proto == "TCP" else socket.SOCK_DGRAM)
s.settimeout(limit)
try:
    if proto == "TCP":
        s.connect((ip,port)); s.sendall(b"PING\n"); data=s.recv(2048)
    else:
        s.sendto(b"PING",(ip,port)); data,_=s.recvfrom(2048)
    text=data.decode(errors="replace")
    if provider not in text: raise RuntimeError("wrong provider response: "+text)
    print("OK:"+text.strip())
except Exception as exc:
    print("ERR:"+type(exc).__name__+":"+str(exc))
    raise SystemExit(2)
finally:
    s.close()
'''
    result = run_host_python(host_pid, code,
        (provider_id, ip, protocol, str(port), str(timeout)), timeout=timeout + 2)
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def timed_service_probe(host_pid, provider_id, ip, protocol, port, timeout=1.2):
    """Measure the actual service exchange inside the client namespace."""
    code = r'''import json,socket,sys,time
provider,ip,proto,port,limit=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4]),float(sys.argv[5])
started=time.perf_counter()
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM if proto == "TCP" else socket.SOCK_DGRAM)
s.settimeout(limit)
try:
    if proto == "TCP":
        s.connect((ip,port)); s.sendall(b"PING\n"); data=s.recv(2048)
    else:
        s.sendto(b"PING",(ip,port)); data,_=s.recvfrom(2048)
    text=data.decode(errors="replace")
    if provider not in text: raise RuntimeError("wrong provider response: "+text)
    result={"ok":True,"elapsed_ms":(time.perf_counter()-started)*1000,"detail":text.strip()}
except Exception as exc:
    result={"ok":False,"elapsed_ms":(time.perf_counter()-started)*1000,"detail":type(exc).__name__+":"+str(exc)}
finally:
    s.close()
print(json.dumps(result))
'''
    result = run_host_python(host_pid, code,
        (provider_id, ip, protocol, str(port), str(timeout)), timeout=timeout + 2)
    if result.returncode:
        raise IntegrationFailure(result.stderr.strip() or result.stdout.strip() or
                                 "service probe process failed")
    try:
        payload = json.loads(result.stdout)
        return bool(payload["ok"]), payload["detail"], float(payload["elapsed_ms"])
    except (ValueError, KeyError, TypeError) as exc:
        raise IntegrationFailure(f"invalid timed probe output: {result.stdout!r}") from exc


def client_request(host_pid, service="FILE"):
    result = command([MNEXEC, "-a", str(host_pid), sys.executable, str(CLIENT), service,
                      "--request", "PING", "--timeout", "1", "--attempts", "1",
                      "--registry-host", REGISTRY_HOST, "--registry-port", str(REGISTRY_PORT)],
                     timeout=8, check=False)
    return result.returncode, result.stdout + result.stderr


def dump_flows(switch):
    return command(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch], timeout=5).stdout


def has_provider_drop(flows, ip, protocol, port):
    for line in flows.splitlines():
        lowered = line.lower().replace(" ", "")
        if "priority=100" not in lowered or "actions=drop" not in lowered:
            continue
        ip_match = f"nw_dst={ip}" in lowered or f"ipv4_dst={ip}" in lowered
        proto_match = ("tcp" in lowered or "nw_proto=6" in lowered) if protocol == "TCP" else (
            "udp" in lowered or "nw_proto=17" in lowered)
        if ip_match and proto_match and f"tp_dst={port}" in lowered:
            return True
    return False


def lifecycle_request(ip, token, provider_id, action, timeout=2):
    payload = f"{action}|{token}|{provider_id}".encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.bind(("10.0.0.254", 0))
        sock.sendto(payload, (ip, 55000))
        response, _ = sock.recvfrom(512)
    expected_state = "PAUSED" if action == "PAUSE_HEARTBEATS" else "RESUMED"
    expected = f"LIFECYCLE_OK|{provider_id}|{expected_state}"
    if response.decode().strip() != expected:
        raise IntegrationFailure(f"unexpected lifecycle acknowledgement: {response!r}")
    return True


def tail(path, limit=35):
    try:
        return "".join(path.read_text(errors="replace").splitlines(keepends=True)[-limit:])
    except OSError:
        return ""


class Runner:
    def __init__(self):
        self.host_pids = {}
        self.owned_topology = False
        self.net = None
        self.controller_proc = None
        self.registry_proc = None
        self.provider_procs = {}
        self.borrowed_provider_ids = set()
        self.log_files = []
        self.token = None
        self.created_token = False
        self.run_dir = Path(tempfile.mkdtemp(prefix="sdn-failure-recovery-"))
        self.failed_provider_paused = False
        self.video_provider_paused = False
        self.no_drop_baseline_ms = None
        self.sdn_block_timeout_ms = None
        self.video_measurements = {}
        self.observed_heartbeat_timeout = None
        self.registry_owned = False
        # Adopted Registries may use the documented 30 second timeout.
        self.heartbeat_wait = 40

    def _open_log(self, name):
        handle = (self.run_dir / name).open("w", encoding="utf-8")
        self.log_files.append(handle)
        return handle

    def _save_sdn_comparison(self):
        data = {
            "provider_id": "FILE-1",
            "endpoint": "10.0.0.4:5001/TCP",
            "no_drop_baseline_ms": self.no_drop_baseline_ms,
            "sdn_block_timeout_ms": self.sdn_block_timeout_ms,
            "socket_timeout_ms": 1200,
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        temporary = COMPARISON_FILE.with_suffix(COMPARISON_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, COMPARISON_FILE)

    def _existing_lab(self):
        # The runner adopts a complete user-owned lab. It refuses partial state.
        s1 = subprocess.run(["ovs-vsctl", "br-exists", "s1"], capture_output=True).returncode == 0
        s2 = subprocess.run(["ovs-vsctl", "br-exists", "s2"], capture_output=True).returncode == 0
        anchors = mininet_host_pids()
        has_anchors = all(name in anchors and len(anchors[name]) == 1
                          for name in (f"h{i}" for i in range(1, 14)))
        if s1 and s2 and has_anchors:
            self.host_pids = {name: pids[0] for name, pids in anchors.items()}
            self.owned_topology = False
            pass_step("adopted the existing complete Mininet topology")
            return True
        if s1 or s2 or anchors:
            raise IntegrationFailure(
                "found partial/ambiguous Mininet state; refusing to alter it. "
                "Close that topology cleanly, then rerun this test."
            )
        return False

    def _start_owned_topology(self):
        from mininet.cli import CLI  # Import dependencies only after root check.
        from mininet.link import TCLink
        from mininet.net import Mininet
        from mininet.node import OVSSwitch, RemoteController
        import topology

        self.net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
        self.net.addController("c0", controller=RemoteController,
                               ip=topology.CONTROLLER_IP, port=topology.CONTROLLER_PORT)
        s1 = self.net.addSwitch("s1", dpid=topology.S1_DPID, protocols=topology.OPENFLOW_PROTOCOL)
        s2 = self.net.addSwitch("s2", dpid=topology.S2_DPID, protocols=topology.OPENFLOW_PROTOCOL)
        hosts = {name: self.net.addHost(name, ip=ip) for name, ip in topology.HOST_SPECS}
        for host in topology.S1_HOSTS:
            self.net.addLink(hosts[host], s1)
        for host in topology.S2_HOSTS:
            self.net.addLink(hosts[host], s2)
        self.net.addLink(s1, s2)
        self.net.build()
        self.net.start()
        topology.add_root_api_link("s1")
        self.host_pids = {name: node.pid for name, node in hosts.items()}
        self.owned_topology = True
        pass_step("started the project 13-host Mininet topology")

    def _ensure_token(self):
        if TOKEN_FILE.is_file():
            mode = TOKEN_FILE.stat().st_mode & 0o777
            value = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(value) >= 32 and mode & 0o077 == 0:
                self.token = value
                return
            if not self.owned_topology:
                raise IntegrationFailure("existing lifecycle token is invalid or not root-only")
        if not self.owned_topology:
            raise IntegrationFailure("running topology has no valid lifecycle token")
        import topology
        topology.create_lifecycle_token()
        self.created_token = True
        self.token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    def _ensure_controller(self):
        openflow_up = tcp_open("127.0.0.1", 6653)
        api_up = tcp_open("127.0.0.1", 8080)
        if openflow_up and api_up:
            pass_step("reusing the running OpenFlow controller and status API")
            return
        if openflow_up or api_up:
            raise IntegrationFailure("only one controller port (6653/8080) is available; refusing duplicate startup")
        self.controller_proc = subprocess.Popen(
            [sys.executable, str(CONTROLLER)], cwd=ROOT,
            stdout=self._open_log("controller.log"), stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            wait_for(lambda: self.controller_proc.poll() is None and
                     tcp_open("127.0.0.1", 6653) and tcp_open("127.0.0.1", 8080),
                     20, "controller OpenFlow and HTTP listeners")
        except IntegrationFailure as exc:
            raise IntegrationFailure(f"controller failed to start: {exc}\n{tail(self.run_dir / 'controller.log')}")
        pass_step("started the project controller from integration/run_controller.py")

    def _verify_hosts(self):
        expected = {f"h{i}": f"10.0.0.{i}/24" for i in range(1, 14)}
        for host, expected_ip in expected.items():
            result = command([MNEXEC, "-a", str(self.host_pids[host]), "ip", "-4", "-o", "addr", "show"],
                             timeout=3, check=False)
            if result.returncode or expected_ip.split("/")[0] not in result.stdout:
                raise IntegrationFailure(f"{host} namespace does not have expected IP {expected_ip}: {result.stdout}")
        if not Path("/sys/class/net/sdn-root").exists():
            raise IntegrationFailure("root API bridge interface sdn-root is missing")
        pass_step("verified 13 host namespaces and the root API bridge")

    def _registry_alive(self):
        try:
            discover_in_host(self.host_pids["h1"], "FILE", timeout=0.7)
            return True
        except (TimeoutError, IntegrationFailure, subprocess.TimeoutExpired):
            return False

    def _ensure_registry(self):
        if self._registry_alive():
            pass_step("reusing the Registry already answering UDP discovery")
            return
        env = {
            **os.environ,
            "REGISTRY_HOST": "0.0.0.0",
            "REGISTRY_PORT": str(REGISTRY_PORT),
            "HEARTBEAT_TIMEOUT": str(TEST_HEARTBEAT_TIMEOUT),
            "CONTROLLER_URL": CONTROLLER_HTTP,
            "PROVIDER_SELECTION_POLICY": "FIRST_AVAILABLE",
            "PYTHONUNBUFFERED": "1",
        }
        self.registry_proc = subprocess.Popen(
            [MNEXEC, "-a", str(self.host_pids["h3"]), sys.executable, str(REGISTRY)],
            cwd=ROOT, env=env, stdout=self._open_log("registry.log"), stderr=subprocess.STDOUT,
        )
        self.registry_owned = True
        self.heartbeat_wait = TEST_HEARTBEAT_TIMEOUT + 4
        try:
            wait_for(lambda: self.registry_proc.poll() is None and self._registry_alive(),
                     10, "Registry UDP discovery response")
        except IntegrationFailure as exc:
            raise IntegrationFailure(f"Registry failed to start: {exc}\n{tail(self.run_dir / 'registry.log')}")
        pass_step(f"started Registry on UDP {REGISTRY_PORT} with {TEST_HEARTBEAT_TIMEOUT}s test timeout")

    def _start_or_reuse_providers(self):
        all_pids = existing_provider_pids()
        duplicates = {pid: values for pid, values in all_pids.items() if len(values) > 1}
        if duplicates:
            raise IntegrationFailure(f"duplicate provider processes detected; refusing to add more: {duplicates}")
        for host, provider_id, service, ip, protocol, port in PROVIDERS:
            existing = all_pids[provider_id]
            if existing:
                self.borrowed_provider_ids.add(provider_id)
                continue
            args = [sys.executable, str(PROVIDER), "--id", provider_id, "--service", service,
                    "--host", ip, "--port", str(port), "--registry-host", REGISTRY_HOST,
                    "--registry-port", str(REGISTRY_PORT), "--lifecycle-token-file", str(TOKEN_FILE)]
            log = self._open_log(f"{provider_id.lower()}.log")
            process = subprocess.Popen([MNEXEC, "-a", str(self.host_pids[host]), *args],
                cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"}, stdout=log, stderr=subprocess.STDOUT)
            self.provider_procs[provider_id] = process
        pass_step(f"started {len(self.provider_procs)} missing providers; reused {len(self.borrowed_provider_ids)} existing providers")
        for host, provider_id, service, ip, protocol, port in PROVIDERS:
            wait_for(lambda spec=(host, provider_id, ip, protocol, port):
                     service_probe(self.host_pids["h1"], spec[1], spec[2], spec[3], spec[4], timeout=0.7)[0],
                     12, f"{provider_id} {protocol}/{port} listener")
        pass_step("all seven provider service sockets are reachable")

    def _wait_all_registered(self):
        expected = {
            "FILE": {"FILE-1", "FILE-2", "FILE-3"},
            "CHAT": {"CHAT-1", "CHAT-2"},
            "VIDEO": {"VIDEO-1", "VIDEO-2"},
        }
        for service, ids in expected.items():
            wait_for(lambda s=service, required=ids:
                     required.issubset(set(provider_ids(discover_in_host(self.host_pids["h1"], s), s))),
                     12, f"all {service} providers registered")
        pass_step("Registry discovery confirms all seven provider registrations")

    def _verify_baseline(self):
        for switch in ("s1", "s2"):
            flows = dump_flows(switch)
            if "priority=0" not in flows or "CONTROLLER" not in flows:
                raise IntegrationFailure(f"{switch} has no controller table miss:\n{flows}")
            if "priority=1" not in flows or "NORMAL" not in flows:
                raise IntegrationFailure(f"{switch} has no NORMAL baseline:\n{flows}")
        pass_step("both switches have controller table-miss and NORMAL baseline flows")

    def _verify_initial_traffic(self):
        success, output, error = service_probe(self.host_pids["h1"], "FILE-1", "10.0.0.4", "TCP", 5001)
        if not success:
            raise IntegrationFailure(f"initial FILE-1 service request failed: {output} {error}")
        return_code, client_output = client_request(self.host_pids["h1"])
        if return_code:
            raise IntegrationFailure(f"client discovery/service request failed:\n{client_output}")
        pass_step("client-to-Registry discovery and client-to-provider TCP request work")

    def _pause_file1(self):
        lifecycle_request("10.0.0.4", self.token, "FILE-1", "PAUSE_HEARTBEATS")
        self.failed_provider_paused = True
        pass_step("FILE-1 acknowledged authenticated heartbeat pause; service process remains running")
        flows = dump_flows("s2")
        if has_provider_drop(flows, "10.0.0.4", "TCP", 5001):
            raise IntegrationFailure("FILE-1 already has a drop rule; cannot measure the no-drop baseline")
        success, output, elapsed_ms = timed_service_probe(
            self.host_pids["h1"], "FILE-1", "10.0.0.4", "TCP", 5001, timeout=1.2)
        if not success:
            raise IntegrationFailure(f"direct FILE-1 baseline failed before SDN block: {output}")
        self.no_drop_baseline_ms = elapsed_ms
        self.sdn_block_timeout_ms = None
        self._save_sdn_comparison()
        pass_step(f"direct cached-address FILE-1 baseline succeeded with no drop rule in {elapsed_ms:.2f} ms")
        wait_for(lambda: "FILE-1" not in provider_ids(discover_in_host(self.host_pids["h1"], "FILE"), "FILE"),
                 self.heartbeat_wait, "Registry to mark FILE-1 inactive")
        pass_step("Registry excludes FILE-1 after heartbeat timeout")
        deadline = time.monotonic() + 10
        flows = ""
        while time.monotonic() < deadline:
            flows = dump_flows("s2")
            if has_provider_drop(flows, "10.0.0.4", "TCP", 5001):
                break
            time.sleep(0.25)
        if not has_provider_drop(flows, "10.0.0.4", "TCP", 5001):
            raise IntegrationFailure(f"s2 never installed FILE-1 drop rule:\n{flows}\n{tail(self.run_dir / 'controller.log')}")
        if has_provider_drop(flows, "10.0.0.5", "TCP", 5001) or has_provider_drop(flows, "10.0.0.8", "TCP", 5001):
            raise IntegrationFailure("s2 unexpectedly blocked FILE-2 or FILE-3")
        pass_step("s2 has only FILE-1's priority-100 TCP/5001 drop among FILE providers")

    def _verify_failed_and_healthy_traffic(self):
        success, output, elapsed_ms = timed_service_probe(
            self.host_pids["h1"], "FILE-1", "10.0.0.4", "TCP", 5001, timeout=1.2)
        if success or "TimeoutError" not in output:
            raise IntegrationFailure(f"expected TCP to FILE-1 to time out at the SDN drop, got: {output}")
        self.sdn_block_timeout_ms = elapsed_ms
        self._save_sdn_comparison()
        pass_step(f"direct cached-address FILE-1 timed out at the SDN drop in {elapsed_ms:.2f} ms; no-drop baseline was {self.no_drop_baseline_ms:.2f} ms")
        success, output, error = service_probe(self.host_pids["h1"], "FILE-2", "10.0.0.5", "TCP", 5001)
        if not success:
            raise IntegrationFailure(f"healthy FILE-2 traffic failed: {output} {error}")
        pass_step("healthy FILE-2 remains reachable")
        ids = set(provider_ids(discover_in_host(self.host_pids["h1"], "FILE"), "FILE"))
        if "FILE-1" in ids or not {"FILE-2", "FILE-3"}.issubset(ids):
            raise IntegrationFailure(f"wrong FILE discovery candidates after failure: {sorted(ids)}")
        code, output = client_request(self.host_pids["h1"])
        if code or not any(f"{pid} OK service=FILE" in output for pid in ("FILE-2", "FILE-3")):
            raise IntegrationFailure(f"client did not select a healthy FILE provider:\n{output}")
        pass_step("client discovery selects a remaining active FILE provider")

    def _recover_file1(self):
        lifecycle_request("10.0.0.4", self.token, "FILE-1", "RESUME_HEARTBEATS")
        self.failed_provider_paused = False
        wait_for(lambda: "FILE-1" in provider_ids(discover_in_host(self.host_pids["h1"], "FILE"), "FILE"),
                 8, "Registry to reactivate FILE-1")
        pass_step("Registry includes FILE-1 after resumed heartbeat")
        deadline = time.monotonic() + 10
        flows = ""
        while time.monotonic() < deadline:
            flows = dump_flows("s2")
            if not has_provider_drop(flows, "10.0.0.4", "TCP", 5001):
                break
            time.sleep(0.25)
        if has_provider_drop(flows, "10.0.0.4", "TCP", 5001):
            raise IntegrationFailure(f"FILE-1 drop rule remained after recovery:\n{flows}")
        pass_step("controller removed FILE-1's priority-100 drop after recovery")
        success, output, error = service_probe(self.host_pids["h1"], "FILE-1", "10.0.0.4", "TCP", 5001)
        if not success:
            raise IntegrationFailure(f"FILE-1 did not become reachable: {output} {error}")
        pass_step("TCP traffic to recovered FILE-1 works again")

    def _save_video_measurements(self):
        temporary = VIDEO_MEASUREMENTS_FILE.with_suffix(VIDEO_MEASUREMENTS_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "provider_id": "VIDEO-1", "service": "VIDEO", "protocol": "UDP",
            "endpoint": "10.0.0.7:5003", "heartbeat_timeout_seconds": self.observed_heartbeat_timeout,
            "measurements": self.video_measurements,
        }, indent=2), encoding="utf-8")
        os.replace(temporary, VIDEO_MEASUREMENTS_FILE)

    def _measure_video_failure_recovery(self):
        provider_id, ip, protocol, port = "VIDEO-1", "10.0.0.7", "UDP", 5003
        try:
            stats = json.loads(REGISTRY_STATS_FILE.read_text(encoding="utf-8"))
            self.observed_heartbeat_timeout = float(stats["heartbeat_timeout_seconds"])
            last_seen = datetime.fromisoformat(stats["last_seen_at"])
            if (datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)).total_seconds() > 4:
                raise ValueError("Registry liveness telemetry is stale")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise IntegrationFailure(f"cannot verify the active Registry heartbeat timeout: {exc}") from exc
        if self.observed_heartbeat_timeout != 30:
            raise IntegrationFailure(
                f"VIDEO failure measurement requires the production 30-second heartbeat timeout; "
                f"active Registry reports {self.observed_heartbeat_timeout:g}s")
        if has_provider_drop(dump_flows("s2"), ip, protocol, port):
            raise IntegrationFailure("VIDEO-1 already has an SDN drop rule before measurement")

        pause_requested_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.video_provider_paused = True
        lifecycle_request(ip, self.token, provider_id, "PAUSE_HEARTBEATS")
        pause_ack_mono = time.monotonic()
        pause_ack_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        wait_for(lambda: provider_id not in provider_ids(
            discover_in_host(self.host_pids["h1"], "VIDEO"), "VIDEO"),
            self.heartbeat_wait, "Registry to mark VIDEO-1 inactive")
        inactive_observed_mono = time.monotonic()
        inactive_observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        wait_for(lambda: has_provider_drop(dump_flows("s2"), ip, protocol, port),
                 10, "s2 to show the VIDEO-1 priority-100 UDP drop")
        drop_observed_mono = time.monotonic()
        drop_observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.video_measurements = {
            "heartbeat_pause_requested_at": pause_requested_at,
            "heartbeat_pause_acknowledged_at": pause_ack_at,
            "registry_inactive_observed_at": inactive_observed_at,
            "openflow_drop_observed_at": drop_observed_at,
            "heartbeat_failure_detection_ms": round((inactive_observed_mono - pause_ack_mono) * 1000, 3),
            "openflow_rule_update_observation_ms": round((drop_observed_mono - inactive_observed_mono) * 1000, 3),
        }
        self._save_video_measurements()
        pass_step(f"VIDEO-1 heartbeat failure detected in {self.video_measurements['heartbeat_failure_detection_ms']:.2f} ms; drop observed {self.video_measurements['openflow_rule_update_observation_ms']:.2f} ms after Registry inactivity")

        resume_requested_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        lifecycle_request(ip, self.token, provider_id, "RESUME_HEARTBEATS")
        self.video_provider_paused = False
        resume_ack_mono = time.monotonic()
        resume_ack_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        wait_for(lambda: provider_id in provider_ids(
            discover_in_host(self.host_pids["h1"], "VIDEO"), "VIDEO"),
            10, "Registry to reactivate VIDEO-1")
        active_observed_mono = time.monotonic()
        active_observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        wait_for(lambda: not has_provider_drop(dump_flows("s2"), ip, protocol, port),
                 10, "s2 to remove the VIDEO-1 priority-100 UDP drop")
        rule_removed_mono = time.monotonic()
        rule_removed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        response_code, response_output = client_request(self.host_pids["h1"], service="VIDEO")
        if response_code or "VIDEO-1 OK service=VIDEO" not in response_output:
            raise IntegrationFailure(f"recovered VIDEO-1 did not serve the first post-recovery client request: {response_output}")
        response_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        response_observed_mono = time.monotonic()
        self.video_measurements.update({
            "heartbeat_resume_requested_at": resume_requested_at,
            "heartbeat_resume_acknowledged_at": resume_ack_at,
            "registry_active_observed_at": active_observed_at,
            "openflow_drop_removal_observed_at": rule_removed_at,
            "first_successful_video_response_at": response_at,
            "provider_recovery_ms": round((active_observed_mono - resume_ack_mono) * 1000, 3),
            "openflow_rule_removal_observation_ms": round((rule_removed_mono - active_observed_mono) * 1000, 3),
            "first_successful_video_response_after_recovery_ms": round((response_observed_mono - resume_ack_mono) * 1000, 3),
            "first_successful_video_response": response_output.strip(),
        })
        self._save_video_measurements()
        pass_step(f"VIDEO-1 recovered in {self.video_measurements['provider_recovery_ms']:.2f} ms; first successful VIDEO response observed {self.video_measurements['first_successful_video_response_after_recovery_ms']:.2f} ms after heartbeat resume")

    def run(self):
        if os.geteuid() != 0:
            raise IntegrationFailure("requires root for Mininet namespaces, OVS and the 10.0.0.254 lifecycle source")
        if not Path(MNEXEC).is_file():
            raise IntegrationFailure("mnexec is unavailable; install Mininet before running this test")
        # Verify command-line tools before touching any existing lab.
        command(["ovs-vsctl", "--version"], timeout=3)
        existing = self._existing_lab()
        if existing:
            self._verify_hosts()
        else:
            if Path("/sys/class/net/sdn-root").exists():
                raise IntegrationFailure("stale sdn-root interface exists without a complete Mininet topology")
        self._ensure_controller()
        if not existing:
            self._start_owned_topology()
        self._verify_hosts()
        self._ensure_token()
        self._verify_baseline()
        self._ensure_registry()
        self._start_or_reuse_providers()
        self._wait_all_registered()
        self._verify_initial_traffic()
        self._pause_file1()
        self._verify_failed_and_healthy_traffic()
        self._recover_file1()
        self._measure_video_failure_recovery()
        return True

    def cleanup(self):
        # Always resume a borrowed or owned provider if a failure interrupted the test.
        if self.failed_provider_paused and self.token:
            try:
                lifecycle_request("10.0.0.4", self.token, "FILE-1", "RESUME_HEARTBEATS", timeout=1)
                report("CLEANUP: resumed FILE-1 heartbeats")
            except Exception as exc:
                report(f"CLEANUP WARNING: could not resume FILE-1 heartbeat: {exc}")
        if self.video_provider_paused and self.token:
            try:
                lifecycle_request("10.0.0.7", self.token, "VIDEO-1", "RESUME_HEARTBEATS", timeout=1)
                report("CLEANUP: resumed VIDEO-1 heartbeats")
            except Exception as exc:
                report(f"CLEANUP WARNING: could not resume VIDEO-1 heartbeat: {exc}")
        for provider_id, process in reversed(list(self.provider_procs.items())):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if self.registry_proc and self.registry_proc.poll() is None:
            self.registry_proc.terminate()
            try:
                self.registry_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.registry_proc.kill()
                self.registry_proc.wait(timeout=2)
        if self.net is not None:
            try:
                import topology
                topology.remove_root_api_link()
            except Exception:
                pass
            try:
                self.net.stop()
            except Exception as exc:
                report(f"CLEANUP WARNING: Mininet shutdown: {exc}")
            if self.created_token:
                TOKEN_FILE.unlink(missing_ok=True)
        if self.controller_proc and self.controller_proc.poll() is None:
            self.controller_proc.terminate()
            try:
                self.controller_proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.controller_proc.kill()
                self.controller_proc.wait(timeout=2)
        if self.net is not None:
            report("CLEANUP: stopped test-owned providers, Registry, controller, Mininet and root API link")
        else:
            report("CLEANUP: stopped test-owned providers/Registry/controller; preserved adopted Mininet lab")
        for handle in self.log_files:
            try:
                handle.close()
            except OSError:
                pass
        report(f"Logs: {self.run_dir}")


def main():
    runner = Runner()
    try:
        runner.run()
        pass_step("SDN provider failure and recovery integration test completed")
        return 0
    except KeyboardInterrupt:
        report("FAIL: interrupted by user")
        return 130
    except (IntegrationFailure, OSError, subprocess.SubprocessError) as exc:
        report(f"FAIL: {exc}")
        return 1
    finally:
        runner.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
