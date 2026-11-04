#!/usr/bin/env python3
"""Mininet topology with a root-namespace veth so h3 can reach controller HTTP API."""
import os
import secrets
import json
import socket
import socketserver
import stat
import subprocess
import threading
from pathlib import Path
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

ROOT_IF = "sdn-root"
SW_IF = "sdn-s1"
ROOT_IP = "10.0.0.254/24"
LIFECYCLE_TOKEN_FILE = Path("/tmp/service_discovery_lifecycle.token")
CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653
OPENFLOW_PROTOCOL = "OpenFlow13"
S1_DPID = "0000000000000001"
S2_DPID = "0000000000000002"
CLIENT_HOSTS = ("h1", "h2", "h11", "h12", "h13")
REGISTRY_HOST = "h3"
PROVIDER_HOSTS = ("h4", "h5", "h6", "h7", "h8", "h9", "h10")
S1_HOSTS = ("h1", "h2", "h3", "h11", "h12", "h13")
S2_HOSTS = PROVIDER_HOSTS
LINK_CONTROL_SOCKET = Path(os.getenv("MININET_LINK_CONTROL_SOCKET", "/tmp/service_discovery_mininet.sock"))
HOST_SPECS = (
    ("h1", "10.0.0.1/24"), ("h2", "10.0.0.2/24"), ("h3", "10.0.0.3/24"),
    ("h4", "10.0.0.4/24"), ("h5", "10.0.0.5/24"), ("h6", "10.0.0.6/24"),
    ("h7", "10.0.0.7/24"), ("h8", "10.0.0.8/24"), ("h9", "10.0.0.9/24"),
    ("h10", "10.0.0.10/24"), ("h11", "10.0.0.11/24"),
    ("h12", "10.0.0.12/24"), ("h13", "10.0.0.13/24"),
)

def run(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True)

def add_root_api_link(switch_name="s1"):
    # Expose the Ubuntu root namespace to the Mininet L2 network.
    # The controller HTTP server listens on 0.0.0.0:8080; h3 calls 10.0.0.254:8080.
    run(["ip", "link", "del", ROOT_IF], check=False)
    run(["ip", "link", "add", ROOT_IF, "type", "veth", "peer", "name", SW_IF])
    run(["ip", "addr", "add", ROOT_IP, "dev", ROOT_IF])
    run(["ip", "link", "set", ROOT_IF, "up"])
    run(["ovs-vsctl", "add-port", switch_name, SW_IF])
    run(["ip", "link", "set", SW_IF, "up"])
    info("*** Root API bridge: %s on %s; h3 should use 10.0.0.254:8080\n" % (ROOT_IF, switch_name))

def remove_root_api_link():
    run(["ip", "link", "del", ROOT_IF], check=False)

def create_lifecycle_token():
    """Share a fresh root-only secret with the provider hosts and dashboard."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LIFECYCLE_TOKEN_FILE, flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(32))


class LinkControl:
    """Narrow control interface to the existing Mininet topology object."""
    def __init__(self, net):
        self.net = net
        self.operation_lock = threading.Lock()

    def _link(self):
        try:
            s1, s2 = self.net.get("s1"), self.net.get("s2")
            links = self.net.linksBetween(s1, s2)
        except (KeyError, AttributeError) as exc:
            raise RuntimeError("Mininet switches s1 and s2 are unavailable") from exc
        if len(links) != 1:
            raise RuntimeError("Expected exactly one Mininet link between s1 and s2")
        return links[0]

    def status(self):
        try:
            link = self._link()
            states = (bool(link.intf1.isUp()), bool(link.intf2.isUp()))
            state = "UP" if all(states) else "DOWN" if not any(states) else "UNKNOWN"
            return {"ok": True, "target": "s1 ↔ s2", "status": state,
                    "interfaces": {"s1": str(link.intf1), "s2": str(link.intf2)}}
        except Exception as exc:
            return {"ok": False, "target": "s1 ↔ s2", "status": "UNKNOWN", "error": str(exc)}

    def execute(self, operation):
        if operation == "status":
            return self.status()
        if operation not in {"down", "up"}:
            return {"ok": False, "target": "s1 ↔ s2", "status": "UNKNOWN", "error": "Unsupported link operation"}
        if not self.operation_lock.acquire(blocking=False):
            return {"ok": False, "target": "s1 ↔ s2", "status": self.status()["status"],
                    "error": "A link operation is already in progress"}
        try:
            before = self.status()
            if not before["ok"]:
                return before
            target = "DOWN" if operation == "down" else "UP"
            if before["status"] == target:
                return {**before, "operation": operation, "changed": False,
                        "message": f"Link is already {target}"}
            try:
                self.net.configLinkStatus("s1", "s2", operation)
            except Exception as exc:
                state = self.status()
                return {**state, "ok": False, "operation": operation,
                        "error": f"Mininet link command failed: {exc}"}
            after = self.status()
            if not after["ok"] or after["status"] != target:
                return {**after, "ok": False, "operation": operation,
                        "error": f"Mininet command returned but link state is {after['status']}"}
            return {**after, "operation": operation, "changed": True,
                    "message": f"Link brought {operation}"}
        finally:
            self.operation_lock.release()


class _LinkControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(2049)
            if len(line) > 2048 or not line.endswith(b"\n"):
                raise ValueError("Invalid link-control request")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be an object")
            result = self.server.link_control.execute(request.get("operation"))
        except (ValueError, json.JSONDecodeError) as exc:
            result = {"ok": False, "target": "s1 ↔ s2", "status": "UNKNOWN", "error": str(exc)}
        self.wfile.write(json.dumps(result).encode() + b"\n")


class _LinkControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path, link_control):
        self.link_control = link_control
        super().__init__(str(path), _LinkControlHandler)


def start_link_control(net):
    path = LINK_CONTROL_SOCKET
    if path.exists() or path.is_symlink():
        info_stat = path.lstat()
        if not stat.S_ISSOCK(info_stat.st_mode) or info_stat.st_uid != os.geteuid():
            raise RuntimeError(f"Refusing to replace unexpected link-control path: {path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(path))
        except OSError:
            path.unlink(missing_ok=True)
        else:
            raise RuntimeError("A Mininet link-control server is already running")
        finally:
            probe.close()
    server = _LinkControlServer(path, LinkControl(net))
    os.chmod(path, 0o600)
    threading.Thread(target=server.serve_forever, name="mininet-link-control", daemon=True).start()
    return server

def create_topology():
    create_lifecycle_token()
    net = None
    link_control_server = None
    try:
        net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
        net.addController("c0", controller=RemoteController, ip=CONTROLLER_IP, port=CONTROLLER_PORT)
        s1 = net.addSwitch("s1", dpid=S1_DPID, protocols=OPENFLOW_PROTOCOL)
        s2 = net.addSwitch("s2", dpid=S2_DPID, protocols=OPENFLOW_PROTOCOL)

        hosts = {}
        for name, ip in HOST_SPECS:
            hosts[name] = net.addHost(name, ip=ip)

        for name in S1_HOSTS:
            net.addLink(hosts[name], s1)
        for name in S2_HOSTS:
            net.addLink(hosts[name], s2)
        net.addLink(s1, s2)

        net.build()
        net.start()
        add_root_api_link("s1")
        link_control_server = start_link_control(net)
        info("*** Provider lifecycle token ready at %s\n" % LIFECYCLE_TOKEN_FILE)
        CLI(net)
    finally:
        try:
            if link_control_server is not None:
                link_control_server.shutdown()
                link_control_server.server_close()
                LINK_CONTROL_SOCKET.unlink(missing_ok=True)
            remove_root_api_link()
            if net is not None:
                net.stop()
        finally:
            LIFECYCLE_TOKEN_FILE.unlink(missing_ok=True)

if __name__ == "__main__":
    setLogLevel("info")
    create_topology()
