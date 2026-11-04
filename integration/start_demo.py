#!/usr/bin/env python3
"""Start the Service Discovery demo and keep an owned Mininet CLI interactive.

Run with ``sudo python3 integration/start_demo.py``. Stop it from another
terminal with ``sudo python3 integration/start_demo.py --stop`` or type
``exit`` at the Mininet prompt.
"""
import fcntl
import json
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = Path(__file__).resolve().parent
CONTROLLER = INTEGRATION / "run_controller.py"
TOPOLOGY = INTEGRATION / "topology.py"
REGISTRY = ROOT / "registry" / "registry.py"
PROVIDER = ROOT / "providers" / "provider.py"
MNEXEC = "/usr/bin/mnexec"
TOKEN_FILE = Path("/tmp/service_discovery_lifecycle.token")
STATE_FILE = Path("/tmp/service_discovery_demo.json")
LOCK_FILE = Path("/tmp/service_discovery_demo.lock")
REGISTRY_STATS = Path("/tmp/registry_stats.json")
REGISTRY_PORT = 5000
CONTROLLER_HTTP = "http://10.0.0.254:8080/provider-status"
HOSTS = tuple(f"h{i}" for i in range(1, 14))

# host, provider ID, service, bind IP, port
PROVIDERS = (
    ("h4", "FILE-1", "FILE", "10.0.0.4", 5001),
    ("h5", "FILE-2", "FILE", "10.0.0.5", 5001),
    ("h6", "CHAT-1", "CHAT", "10.0.0.6", 5002),
    ("h7", "VIDEO-1", "VIDEO", "10.0.0.7", 5003),
    ("h8", "FILE-3", "FILE", "10.0.0.8", 5001),
    ("h9", "CHAT-2", "CHAT", "10.0.0.9", 5002),
    ("h10", "VIDEO-2", "VIDEO", "10.0.0.10", 5003),
)

STOP_REQUESTED = False


class StartupError(RuntimeError):
    pass


def request_stop(_signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def read_processes():
    """Snapshot readable processes, including namespace and environment data."""
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [p.decode(errors="replace") for p in (entry / "cmdline").read_bytes().split(b"\0") if p]
            env = (entry / "environ").read_bytes().split(b"\0")
            netns = os.stat(entry / "ns/net").st_ino
        except (OSError, PermissionError):
            continue
        result.append({"pid": int(entry.name), "argv": argv, "env": env, "netns": netns})
    return result


def host_pids(processes):
    found = {host: [] for host in HOSTS}
    for process in processes:
        for host in HOSTS:
            if f"mininet:{host}" in process["argv"]:
                found[host].append(process["pid"])
    return {host: pids[0] for host, pids in found.items() if len(pids) == 1}


def process_in_netns(process, host_pid):
    try:
        return process["netns"] == os.stat(f"/proc/{host_pid}/ns/net").st_ino
    except OSError:
        return False


def script_arg(process, script):
    wanted = str(script.resolve())
    return any(arg == wanted for arg in process["argv"])


def option(argv, name):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def registry_command(host_pid):
    return [MNEXEC, "-a", str(host_pid), sys.executable, str(REGISTRY)]


def provider_command(host_pid, spec):
    host, provider_id, service, ip, port = spec
    return [MNEXEC, "-a", str(host_pid), sys.executable, str(PROVIDER),
            "--id", provider_id, "--service", service, "--host", ip,
            "--port", str(port), "--registry-host", "10.0.0.3",
            "--registry-port", str(REGISTRY_PORT),
            "--lifecycle-token-file", str(TOKEN_FILE)]


def providers_healthy(stats):
    records = stats.get("providers", {}) if stats else {}
    return all(records.get(spec[1], {}).get("status") == "ACTIVE"
               and records.get(spec[1], {}).get("heartbeat_count", 0) >= 1
               for spec in PROVIDERS)


def existing_registry(processes, host_pid):
    matches = [p for p in processes if script_arg(p, REGISTRY) and process_in_netns(p, host_pid)]
    if len(matches) > 1:
        raise StartupError("More than one Registry process already runs in h3; refusing to add another")
    return matches[0] if matches else None


def existing_providers(processes, host_pids_by_name):
    result = {}
    ids = {spec[1]: spec for spec in PROVIDERS}
    for process in processes:
        if not script_arg(process, PROVIDER):
            continue
        provider_id = option(process["argv"], "--id")
        if provider_id not in ids:
            continue
        host, _pid, service, ip, port = ids[provider_id]
        if not process_in_netns(process, host_pids_by_name[host]):
            raise StartupError(f"{provider_id} already runs outside its configured {host} namespace")
        expected = (service, ip, str(port))
        actual = (option(process["argv"], "--service"), option(process["argv"], "--host"), option(process["argv"], "--port"))
        if actual != expected:
            raise StartupError(f"{provider_id} is already running with unexpected service arguments: {actual}")
        if provider_id in result:
            raise StartupError(f"More than one {provider_id} process already runs; refusing to add another")
        result[provider_id] = process
    return result


def tcp_open(host, port, timeout=0.25):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def controller_state():
    openflow = tcp_open("127.0.0.1", 6653)
    api = tcp_open("127.0.0.1", 8080)
    if openflow != api:
        raise StartupError("Only one controller listener is available (6653 or 8080); refusing duplicate startup")
    return openflow and api


def complete_topology(processes):
    hosts = host_pids(processes)
    s1 = subprocess.run(["ovs-vsctl", "br-exists", "s1"], capture_output=True).returncode == 0
    s2 = subprocess.run(["ovs-vsctl", "br-exists", "s2"], capture_output=True).returncode == 0
    if not s1 and not s2 and not hosts:
        return False, {}
    if not (s1 and s2 and all(host in hosts for host in HOSTS)):
        raise StartupError("Found partial or ambiguous Mininet state; close it cleanly before starting the demo")
    if not Path("/sys/class/net/sdn-root").exists():
        raise StartupError("Existing topology is missing its sdn-root controller API link")
    return True, hosts


def registry_stats():
    try:
        data = json.loads(REGISTRY_STATS.read_text(encoding="utf-8"))
        last_seen = datetime.fromisoformat(data["last_seen_at"])
        seen = (datetime.now(timezone.utc) - last_seen).total_seconds()
        if seen > 5 or int(data.get("port", 0)) != REGISTRY_PORT:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def wait_for(predicate, timeout, description, interval=0.2):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if STOP_REQUESTED:
            raise StartupError("Startup interrupted")
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, StartupError) as exc:
            last_error = exc
        time.sleep(interval)
    suffix = f" (last error: {last_error})" if last_error else ""
    raise StartupError(f"Timed out waiting for {description}{suffix}")


class DemoLauncher:
    def __init__(self):
        self.session = uuid.uuid4().hex
        self.log_dir = Path(tempfile.mkdtemp(prefix=f"service-discovery-demo-{os.getpid()}-"))
        self.logs = {}
        self.children = []
        self.controller = None
        self.registry = None
        self.providers = {}
        self.host_pids = {}
        self.topology_pid = None
        self.topology_fd = None
        self.owned_topology = False
        self.owned_controller = False
        self.owned_registry = False
        self.owned_providers = set()
        self.lock_handle = None

    def acquire_lock(self):
        self.lock_handle = LOCK_FILE.open("a+")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                detail = f" (launcher PID {state.get('launcher_pid')})"
            except (OSError, ValueError):
                detail = ""
            raise StartupError(f"A demo launcher is already active{detail}") from exc

    def save_state(self):
        state = {"launcher_pid": os.getpid(), "session": self.session,
                 "topology_pid": self.topology_pid, "owned_topology": self.owned_topology,
                 "owned_controller": self.owned_controller, "owned_registry": self.owned_registry,
                 "owned_providers": sorted(self.owned_providers), "logs": {k: str(v) for k, v in self.logs.items()}}
        temp = STATE_FILE.with_suffix(f".{self.session}.tmp")
        temp.write_text(json.dumps(state), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, STATE_FILE)

    def _open_log(self, name):
        path = self.log_dir / name
        self.logs[name] = path
        return path.open("ab", buffering=0)

    def _launch_controller(self):
        if controller_state():
            print("Using the existing controller listeners on 6653 and 8080.", flush=True)
            return
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "SERVICE_DISCOVERY_DEMO_SESSION": self.session}
        self.controller = subprocess.Popen([sys.executable, str(CONTROLLER)], cwd=ROOT,
                                           env=env, stdout=self._open_log("controller.log"),
                                           stderr=subprocess.STDOUT)
        self.children.append(self.controller)
        self.owned_controller = True
        self.save_state()
        wait_for(lambda: self.controller.poll() is None and controller_state(), 25,
                 "controller OpenFlow and HTTP listeners")
        print(f"Controller ready; log: {self.logs['controller.log']}", flush=True)

    def _start_topology(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(ROOT)
            os.environ["SERVICE_DISCOVERY_DEMO_SESSION"] = self.session
            os.execv(sys.executable, [sys.executable, str(TOPOLOGY)])
        self.topology_pid, self.topology_fd = pid, fd
        self.owned_topology = True
        self.save_state()
        print("Starting Mininet topology; its CLI will remain attached here.", flush=True)
        self._read_topology_until_prompt(90)
        processes = read_processes()
        self.host_pids = host_pids(processes)
        if set(self.host_pids) != set(HOSTS):
            raise StartupError(f"Mininet CLI became ready but host namespaces are incomplete: {sorted(set(HOSTS) - set(self.host_pids))}")
        wait_for(lambda: subprocess.run(["ovs-vsctl", "br-exists", "s1"], capture_output=True).returncode == 0
                 and subprocess.run(["ovs-vsctl", "br-exists", "s2"], capture_output=True).returncode == 0,
                 15, "Mininet switches")
        print("All 13 host namespaces and switches are ready.", flush=True)

    def _read_topology_until_prompt(self, timeout):
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            if STOP_REQUESTED:
                raise StartupError("Startup interrupted")
            ready, _, _ = select.select([self.topology_fd], [], [], 0.2)
            if not ready:
                if self._topology_exited():
                    raise StartupError("Mininet topology exited before its CLI became ready")
                continue
            try:
                data = os.read(self.topology_fd, 4096)
            except OSError as exc:
                raise StartupError(f"Could not read Mininet startup output: {exc}") from exc
            if not data:
                raise StartupError("Mininet topology closed its CLI before startup completed")
            self._write_topology_output(data)
            buffer = (buffer + data)[-8192:]
            if b"mininet>" in buffer.lower():
                return
        raise StartupError("Timed out waiting for the Mininet CLI prompt")

    def _write_topology_output(self, data):
        try:
            os.write(sys.stdout.fileno(), data)
        except (AttributeError, OSError):
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
        log = self.logs.get("topology.log")
        if log:
            with log.open("ab") as stream:
                stream.write(data)

    def _topology_exited(self):
        if self.topology_pid is None:
            return True
        try:
            pid, _status = os.waitpid(self.topology_pid, os.WNOHANG)
            if pid:
                self.topology_pid = None
                return True
        except ChildProcessError:
            self.topology_pid = None
            return True
        return False

    def _wait_for_hosts(self):
        processes = read_processes()
        existing, hosts = complete_topology(processes)
        if existing:
            self.host_pids = hosts
            self.owned_topology = False
            if not TOKEN_FILE.is_file():
                raise StartupError("Existing topology has no lifecycle token at /tmp/service_discovery_lifecycle.token")
            if TOKEN_FILE.stat().st_mode & 0o077 or len(TOKEN_FILE.read_text(encoding="utf-8").strip()) < 32:
                raise StartupError("Existing topology lifecycle token is invalid or not root-only")
            print("Using the existing complete Mininet topology; its CLI remains in its current terminal.", flush=True)
            return
        if Path("/sys/class/net/sdn-root").exists() or TOKEN_FILE.exists():
            raise StartupError("Found a stale root API interface or lifecycle token without a topology; refusing to overwrite it")
        self.logs["topology.log"] = self.log_dir / "topology.log"
        self._start_topology()
        if not TOKEN_FILE.is_file() or TOKEN_FILE.stat().st_mode & 0o077:
            raise StartupError("Topology did not create a root-only provider lifecycle token")

    def _spawn_in_host(self, host, args, env, log_name):
        full_env = {**os.environ, **env, "PYTHONUNBUFFERED": "1",
                    "SERVICE_DISCOVERY_DEMO_SESSION": self.session}
        log = self._open_log(log_name)
        process = subprocess.Popen([MNEXEC, "-a", str(self.host_pids[host]), *args],
                                   cwd=ROOT, env=full_env, stdout=log,
                                   stderr=subprocess.STDOUT)
        self.children.append(process)
        return process

    def _registry_process(self, processes):
        found = existing_registry(processes, self.host_pids["h3"])
        if found:
            wait_for(lambda: registry_stats(), 10, "live Registry status")
            return None
        previous = registry_stats()
        previous_session = previous.get("session_id") if previous else None
        env = {"REGISTRY_HOST": "0.0.0.0", "REGISTRY_PORT": str(REGISTRY_PORT),
               "CONTROLLER_URL": CONTROLLER_HTTP}
        self.registry = self._spawn_in_host("h3", registry_command(self.host_pids["h3"])[3:], env, "registry.log")
        self.owned_registry = True
        self.save_state()
        def registry_started():
            if self.registry.poll() is not None:
                raise StartupError(f"Registry exited with code {self.registry.returncode}")
            stats = registry_stats()
            return stats if stats and stats.get("session_id") != previous_session else None

        wait_for(registry_started, 10, "new Registry process to publish live status")
        return self.registry

    def _provider_processes(self, processes):
        found = existing_providers(processes, self.host_pids)
        by_id = {p["pid"]: p for p in processes}
        for host, provider_id, service, ip, port in PROVIDERS:
            if provider_id in found:
                print(f"Reusing {provider_id} already running in {host}.", flush=True)
                continue
            args = provider_command(self.host_pids[host], (host, provider_id, service, ip, port))[3:]
            process = self._spawn_in_host(host, args, {}, f"provider-{provider_id.lower()}.log")
            self.providers[provider_id] = process
            self.owned_providers.add(provider_id)
            self.save_state()
        return found

    def _verify_startup(self):
        def ready():
            for process in self.children:
                if process.poll() is not None:
                    raise StartupError(f"A launched process exited with code {process.returncode}")
            return providers_healthy(registry_stats())
        wait_for(ready, 35, "Registry startup and ACTIVE status plus heartbeat for all seven providers")
        print("Registry is live; all seven providers are ACTIVE and have sent heartbeats.", flush=True)

    def run(self):
        if os.geteuid() != 0:
            raise StartupError("Run this launcher as root: sudo python3 integration/start_demo.py")
        if not Path(MNEXEC).is_file():
            raise StartupError(f"Mininet namespace executor is unavailable: {MNEXEC}")
        self.acquire_lock()
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        self._launch_controller()
        self._wait_for_hosts()
        self.save_state()
        processes = read_processes()
        registry = existing_registry(processes, self.host_pids["h3"])
        self._registry_process(processes)
        self._provider_processes(processes)
        if registry:
            print("Reusing the existing Registry in h3.", flush=True)
        self._verify_startup()
        print(f"Logs: {self.log_dir}", flush=True)
        if self.owned_topology:
            self._interact_with_mininet()
        else:
            print("System ready. The adopted Mininet CLI is in its existing terminal; Ctrl-C shuts down only services started here.", flush=True)
            while not STOP_REQUESTED:
                for process in self.children:
                    if process.poll() is not None:
                        raise StartupError(f"A launched process exited with code {process.returncode}")
                time.sleep(0.25)

    def _interact_with_mininet(self):
        print("System ready. Use the Mininet prompt below; type 'exit' to shut down this launcher-owned topology.", flush=True)
        stdin_fd = None
        try:
            if sys.stdin.isatty():
                stdin_fd = sys.stdin.fileno()
        except (AttributeError, OSError):
            pass
        while not STOP_REQUESTED and self.topology_pid is not None:
            readers = [self.topology_fd]
            if stdin_fd is not None:
                readers.append(stdin_fd)
            ready, _, _ = select.select(readers, [], [], 0.2)
            if self.topology_fd in ready:
                try:
                    data = os.read(self.topology_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    self._topology_exited()
                    break
                self._write_topology_output(data)
            if stdin_fd is not None and stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if data:
                    os.write(self.topology_fd, data)
                else:
                    stdin_fd = None
            for process in self.children:
                if process.poll() is not None:
                    raise StartupError(f"A launched process exited with code {process.returncode}")

    def _terminate_child(self, process, timeout=4):
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def shutdown(self):
        # Stop only processes this launcher created. Adopted services/controllers
        # are intentionally left running.
        for process in reversed(self.children):
            self._terminate_child(process)
        if self.owned_topology and self.topology_pid is not None:
            try:
                os.write(self.topology_fd, b"exit\n")
                deadline = time.monotonic() + 8
                while self.topology_pid is not None and time.monotonic() < deadline:
                    if self._topology_exited():
                        break
                    time.sleep(0.1)
                if self.topology_pid is not None:
                    os.kill(self.topology_pid, signal.SIGINT)
                    deadline = time.monotonic() + 4
                    while self.topology_pid is not None and time.monotonic() < deadline:
                        if self._topology_exited():
                            break
                        time.sleep(0.1)
                if self.topology_pid is not None:
                    os.kill(self.topology_pid, signal.SIGTERM)
                    self._topology_exited()
            except (OSError, ProcessLookupError):
                pass
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if state.get("session") == self.session:
                STATE_FILE.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        if self.topology_fd is not None:
            try:
                os.close(self.topology_fd)
            except OSError:
                pass
        if self.lock_handle:
            self.lock_handle.close()


def stop_running_launcher():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        pid = int(state["launcher_pid"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StartupError("No active demo launcher state was found") from exc
    try:
        argv = [p.decode(errors="replace") for p in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if p]
    except OSError as exc:
        raise StartupError("Launcher process is no longer running; inspect its logs and Mininet state manually") from exc
    if not any(Path(arg).resolve() == Path(__file__).resolve() for arg in argv[1:]):
        raise StartupError("Recorded launcher PID no longer identifies this launcher; refusing to signal it")
    os.kill(pid, signal.SIGTERM)
    print(f"Requested clean shutdown of launcher PID {pid}.", flush=True)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="request shutdown of the active launcher")
    args = parser.parse_args(argv)
    if args.stop:
        try:
            stop_running_launcher()
            return 0
        except StartupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    launcher = DemoLauncher()
    try:
        launcher.run()
        return 0
    except (StartupError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nStopping launcher-owned demo processes…", flush=True)
        return 130
    finally:
        launcher.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
