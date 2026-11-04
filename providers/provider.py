#!/usr/bin/env python3
"""Configurable FILE/CHAT TCP and VIDEO UDP service provider."""
import argparse
import hmac
import signal
import socket
import threading
from pathlib import Path
from provider_telemetry import emit as emit_event

HEARTBEAT_INTERVAL = 2.0
LIFECYCLE_PORT = 55000
LIFECYCLE_SOURCE = "10.0.0.254"
DEFAULT_LIFECYCLE_TOKEN_FILE = Path("/tmp/service_discovery_lifecycle.token")

class Provider:
    def __init__(self, provider_id, service, host, port, registry_host, registry_port,
                 lifecycle_token_file=None):
        self.provider_id, self.service = provider_id, service
        self.host, self.port = host, port
        self.registry = (registry_host, registry_port)
        self.stop_event = threading.Event()
        self.heartbeats_paused = threading.Event()
        self.sock = None
        self.heartbeat_thread = None
        self.lifecycle_thread = None
        self.lifecycle_token = None
        if lifecycle_token_file is None:
            # Mininet starts providers as ordinary host commands. Automatically
            # opt in when the topology's shared token is available, so legacy
            # launch lines still start the control listener after topology setup.
            try:
                if DEFAULT_LIFECYCLE_TOKEN_FILE.is_file():
                    token = DEFAULT_LIFECYCLE_TOKEN_FILE.read_text(encoding="utf-8").strip()
                    if len(token) >= 32:
                        self.lifecycle_token = token
                    else:
                        print("heartbeat controls disabled: topology token is invalid", flush=True)
            except OSError as exc:
                print(f"heartbeat controls disabled: cannot read topology token: {exc}", flush=True)
        else:
            self.lifecycle_token = Path(lifecycle_token_file).read_text(encoding="utf-8").strip()
            if len(self.lifecycle_token) < 32:
                raise ValueError("lifecycle token must contain at least 32 characters")

    def _registry_message(self, message, expect_reply=False):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2.0)
            sock.sendto(message.encode(), self.registry)
            if expect_reply:
                data, _ = sock.recvfrom(2048)
                return data.decode().strip()
        return None

    def register(self):
        reply = self._registry_message(
            f"REGISTER|{self.provider_id}|{self.service}|{'UDP' if self.service == 'VIDEO' else 'TCP'}|{self.port}", True)
        expected = f"REGISTER_OK|{self.provider_id}|{self.service}"
        if reply != expected:
            raise RuntimeError(f"registry rejected registration: expected {expected!r}, got {reply!r}")
        print(f"Registered {self.provider_id}: {reply}", flush=True)

    def _heartbeats(self):
        while not self.stop_event.is_set():
            if not self.heartbeats_paused.is_set():
                try:
                    reply = self._registry_message(
                        f"HEARTBEAT|{self.provider_id}", True)
                    expected = f"HEARTBEAT_OK|{self.provider_id}"
                    if reply == expected:
                        pass
                    elif reply == f"ERROR|UNKNOWN_PROVIDER|{self.provider_id}":
                        print(f"Registry forgot {self.provider_id}; registering again", flush=True)
                        self.register()
                    elif reply and reply.startswith("ERROR|"):
                        print(f"heartbeat rejected: {reply}", flush=True)
                    else:
                        print(f"unexpected heartbeat reply: {reply!r}; registering again", flush=True)
                        self.register()
                except OSError as exc:
                    print(f"heartbeat error: {exc}; attempting registration", flush=True)
                    try:
                        self.register()
                    except (OSError, RuntimeError) as register_error:
                        print(f"registration retry failed: {register_error}", flush=True)
                except RuntimeError as exc:
                    print(f"heartbeat recovery failed: {exc}", flush=True)
            self.stop_event.wait(HEARTBEAT_INTERVAL)

    def handle_lifecycle_request(self, data, source_ip):
        """Apply only authenticated pause/resume requests from the dashboard bridge."""
        if not self.lifecycle_token or source_ip != LIFECYCLE_SOURCE:
            return None
        try:
            action, token, provider_id = data.decode("utf-8").strip().split("|")
        except (UnicodeDecodeError, ValueError):
            return None
        if provider_id != self.provider_id or not hmac.compare_digest(token, self.lifecycle_token):
            return None
        if action == "PAUSE_HEARTBEATS":
            if self.heartbeats_paused.is_set():
                state = "PAUSED"
            else:
                self.heartbeats_paused.set()
                state = "PAUSED"
        elif action == "RESUME_HEARTBEATS":
            self.heartbeats_paused.clear()
            state = "RESUMED"
        else:
            return None
        return f"LIFECYCLE_OK|{provider_id}|{state}".encode()

    def _lifecycle_control(self):
        if not self.lifecycle_token:
            return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
                control.settimeout(0.5)
                control.bind(("0.0.0.0", LIFECYCLE_PORT))
                print(f"{self.provider_id} heartbeat control listening UDP {LIFECYCLE_PORT}", flush=True)
                while not self.stop_event.is_set():
                    try:
                        data, addr = control.recvfrom(512)
                    except socket.timeout:
                        continue
                    response = self.handle_lifecycle_request(data, addr[0])
                    if response:
                        control.sendto(response, addr)
        except OSError as exc:
            print(f"lifecycle control unavailable: {exc}", flush=True)

    def _tcp_client(self, conn, addr):
        with conn:
            conn.settimeout(5.0)
            try:
                data = conn.recv(4096)
                request = data.decode('utf-8', errors='replace').strip() or 'PING'
                response = f"{self.provider_id} OK service={self.service} request={request}\n"
                conn.sendall(response.encode())
                emit_event(self.provider_id, "service_traffic_served",
                           service=self.service, protocol="TCP", ip=self.host,
                           port=self.port, client_ip=addr[0], client_port=addr[1],
                           response_sent=True)
                print(f"served {addr}: {request!r}", flush=True)
            except (OSError, UnicodeError) as exc:
                print(f"client {addr} error: {exc}", flush=True)

    def _serve_tcp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            self.sock = server
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(32)
            server.settimeout(1.0)
            print(f"{self.provider_id} listening TCP {self.host}:{self.port}", flush=True)
            while not self.stop_event.is_set():
                try: conn, addr = server.accept()
                except socket.timeout: continue
                threading.Thread(target=self._tcp_client, args=(conn, addr), daemon=True).start()

    def _serve_udp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            self.sock = server
            server.bind((self.host, self.port))
            server.settimeout(1.0)
            print(f"{self.provider_id} listening UDP {self.host}:{self.port}", flush=True)
            while not self.stop_event.is_set():
                try: data, addr = server.recvfrom(4096)
                except socket.timeout: continue
                request = data.decode('utf-8', errors='replace').strip() or 'PING'
                response = f"{self.provider_id} OK service=VIDEO request={request}"
                server.sendto(response.encode(), addr)
                emit_event(self.provider_id, "service_traffic_served",
                           service=self.service, protocol="UDP", ip=self.host,
                           port=self.port, client_ip=addr[0], client_port=addr[1],
                           response_sent=True)
                print(f"served {addr}: {request!r}", flush=True)

    def run(self):
        self.register()
        emit_event(self.provider_id, "provider_started", service=self.service,
                   protocol="UDP" if self.service == "VIDEO" else "TCP",
                   ip=self.host, port=self.port)
        self.heartbeat_thread = threading.Thread(target=self._heartbeats, daemon=True)
        self.heartbeat_thread.start()
        if self.lifecycle_token:
            self.lifecycle_thread = threading.Thread(target=self._lifecycle_control, daemon=True)
            self.lifecycle_thread.start()
        else:
            print("heartbeat controls disabled: topology lifecycle token not found", flush=True)
        try:
            (self._serve_udp if self.service == 'VIDEO' else self._serve_tcp)()
        finally:
            self.stop_event.set()
            if self.heartbeat_thread:
                self.heartbeat_thread.join(timeout=3)
            if self.lifecycle_thread:
                self.lifecycle_thread.join(timeout=1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--id', required=True, dest='provider_id')
    p.add_argument('--service', required=True, choices=['FILE', 'CHAT', 'VIDEO'])
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--registry-host', default='10.0.0.3')
    p.add_argument('--registry-port', type=int, default=5000)
    p.add_argument('--lifecycle-token-file', help='Enable dashboard heartbeat controls using this shared secret file')
    args = p.parse_args()
    provider = Provider(args.provider_id, args.service, args.host, args.port, args.registry_host, args.registry_port,
                        args.lifecycle_token_file)
    signal.signal(signal.SIGTERM, lambda *_: provider.stop_event.set())
    try: provider.run()
    except (OSError, RuntimeError, socket.timeout) as exc:
        raise SystemExit(f"provider startup/runtime failure: {exc}")

if __name__ == '__main__': main()
