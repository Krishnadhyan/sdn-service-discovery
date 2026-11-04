import ipaddress
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3
from controller_telemetry import emit as emit_event


# Provider status notifications waiting to be processed.
status_queue = queue.Queue()


class ProviderStatusHandler(BaseHTTPRequestHandler):
    """HTTP endpoint used by the registry."""

    def do_POST(self):
        if self.path != "/provider-status":
            self.send_error(404, "Unknown endpoint")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16384:
                self.send_error(400, "Invalid content length")
                return

            payload = json.loads(self.rfile.read(length))

            required = {
                "provider_id",
                "service",
                "status",
                "ip",
                "port",
                "protocol",
            }

            if not isinstance(payload, dict) or not required.issubset(payload):
                self.send_error(400, "Missing required fields")
                return

            if payload["status"] not in {"ACTIVE", "INACTIVE"}:
                self.send_error(400, "Invalid provider status")
                return

            if payload["protocol"].upper() not in {"TCP", "UDP"}:
                self.send_error(400, "Unsupported protocol")
                return

            try:
                ipaddress.IPv4Address(payload["ip"])
                port = int(payload["port"])
                if not 1 <= port <= 65535:
                    raise ValueError
                if not isinstance(payload["provider_id"], str) or not payload["provider_id"]:
                    raise ValueError
                if not isinstance(payload["service"], str) or not payload["service"]:
                    raise ValueError
            except (ValueError, TypeError):
                self.send_error(400, "Invalid provider details")
                return

            payload["protocol"] = payload["protocol"].upper()
            payload["port"] = port
            status_queue.put(payload)
            emit_event("status_notification_received", provider_id=payload["provider_id"],
                       service=payload["service"], status=payload["status"],
                       ip=payload["ip"], protocol=payload["protocol"], port=payload["port"])

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"message": "Status queued"}).encode()
            )

        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "Invalid JSON")

    def log_message(self, format, *args):
        print(f"HTTP: {format % args}")


class ServiceDiscoveryController(app_manager.OSKenApp):
    """Install provider-specific drop rules on s2."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.status_queue = status_queue
        self.datapaths = {}

        # Most recent status payload for each provider.
        # Retained so drop rules can be restored after a switch reconnects.
        self.provider_status = {}
        # Old endpoints whose drop rules still need deletion after a status
        # change received while s2 was disconnected from the controller.
        self.stale_provider_endpoints = {}

        self._server_lock = threading.Lock()
        self.status_server = ThreadingHTTPServer(
            ("0.0.0.0", 8080),
            ProviderStatusHandler,
        )
        self.status_server.daemon_threads = True

        self.status_thread = threading.Thread(
            target=self.status_server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
        )
        self.status_thread.start()

        print("Provider status API listening on port 8080")
        emit_event("controller_started", http_port=8080, openflow_version="1.3")

        hub.spawn(self.process_status_updates)

    def close(self):
        """Release the HTTP server when the controller application closes."""
        with self._server_lock:
            server = getattr(self, "status_server", None)
            if server is not None:
                self.status_server = None
                server.shutdown()
                server.server_close()

        thread = getattr(self, "status_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

        super().close()

    def install_table_miss_flow(self, datapath):
        """Send packets without a more specific forwarding rule to OS-Ken."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_ADD,
            priority=0,
            match=parser.OFPMatch(),
            instructions=[
                parser.OFPInstructionActions(
                    ofproto.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                            ofproto.OFPCML_NO_BUFFER)],
                )
            ],
        )
        datapath.send_msg(flow_mod)
        print(f"Installed table-miss flow on switch {datapath.id}")
        emit_event("baseline_rule_installed", switch=f"s{datapath.id}", priority=0,
                   match="all", action="CONTROLLER")

    def install_baseline_flow(self, datapath):
        """Use OVS NORMAL for IPv4 and ARP while retaining a table miss.

        The match is intentionally narrower than table-miss and has a higher
        priority. Provider drops at priority 100 remain more specific overrides.
        """
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        for eth_type, label in ((0x0800, "IPv4"), (0x0806, "ARP")):
            flow_mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_ADD,
                priority=1,
                match=parser.OFPMatch(eth_type=eth_type),
                instructions=[
                    parser.OFPInstructionActions(
                        ofproto.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionOutput(ofproto.OFPP_NORMAL)],
                    )
                ],
            )
            datapath.send_msg(flow_mod)
            print(f"Installed {label} NORMAL baseline on switch {datapath.id}")
            emit_event("baseline_rule_installed", switch=f"s{datapath.id}", priority=1,
                       match={"eth_type": eth_type}, action="NORMAL")

    def apply_provider_rule(self, datapath, payload, status):
        """Add or remove only the rule matching this provider's endpoint."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        protocol = payload["protocol"].upper()
        port = int(payload["port"])

        if protocol == "TCP":
            ip_proto = 6
            transport_match = {"tcp_dst": port}
        elif protocol == "UDP":
            ip_proto = 17
            transport_match = {"udp_dst": port}
        else:
            self.logger.warning("Unsupported protocol: %s", protocol)
            return

        match = parser.OFPMatch(
            eth_type=0x0800,
            ipv4_dst=payload["ip"],
            ip_proto=ip_proto,
            **transport_match,
        )

        if status == "INACTIVE":
            # Repeated ADDs with the same table, priority and match replace
            # the existing flow rather than creating duplicate entries.
            flow_mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_ADD,
                priority=100,
                match=match,
                instructions=[],
            )
            datapath.send_msg(flow_mod)
            print(
                f"Installed drop rule for {payload['provider_id']} "
                f"on switch {datapath.id}"
            )
            emit_event("provider_drop_flow_mod_submitted", switch=f"s{datapath.id}",
                       provider_id=payload["provider_id"], ip=payload["ip"],
                       protocol=protocol, port=port, priority=100)

        elif status == "ACTIVE":
            flow_mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                priority=100,
                match=match,
            )
            datapath.send_msg(flow_mod)
            print(
                f"Removed drop rule for {payload['provider_id']} "
                f"on switch {datapath.id}"
            )
            emit_event("provider_drop_removal_submitted", switch=f"s{datapath.id}",
                       provider_id=payload["provider_id"], ip=payload["ip"],
                       protocol=protocol, port=port, priority=100)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install baseline forwarding and restore inactive-provider rules."""
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath

        print(f"Switch connected: {datapath.id}")
        emit_event("switch_connected", switch=f"s{datapath.id}", datapath_id=datapath.id)
        self.install_table_miss_flow(datapath)
        self.install_baseline_flow(datapath)

        # In this topology, s2 (datapath ID 2) connects to the providers.
        if datapath.id == 2:
            for provider_id, payload in self.provider_status.items():
                self._remove_stale_provider_rules(datapath, provider_id)
                if payload["status"] == "INACTIVE":
                    self.apply_provider_rule(datapath, payload, "INACTIVE")

    @set_ev_cls(
        ofp_event.EventOFPStateChange,
        [MAIN_DISPATCHER, DEAD_DISPATCHER],
    )
    def switch_state_change_handler(self, ev):
        datapath = ev.datapath

        if ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)
            emit_event("switch_disconnected", switch=f"s{datapath.id}", datapath_id=datapath.id)
            print(f"Switch disconnected: {datapath.id}")

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """Record port state reported by OpenFlow; this is switch evidence."""
        datapath = ev.msg.datapath
        desc = ev.msg.desc
        ofproto = datapath.ofproto
        raw_name = getattr(desc, "name", "")
        port_name = raw_name.decode(errors="replace").rstrip("\x00") if isinstance(raw_name, bytes) else str(raw_name)
        state = getattr(desc, "state", None)
        if state is None:
            status = "UNKNOWN"
        else:
            status = "DOWN" if state & ofproto.OFPPS_LINK_DOWN else "UP"
        reason_value = getattr(ev.msg, "reason", None)
        reasons = {
            getattr(ofproto, "OFPPR_ADD", object()): "ADD",
            getattr(ofproto, "OFPPR_DELETE", object()): "DELETE",
            getattr(ofproto, "OFPPR_MODIFY", object()): "MODIFY",
        }
        reason = reasons.get(reason_value, str(reason_value))
        port_no = getattr(desc, "port_no", None)
        emit_event("openflow_port_status", switch=f"s{datapath.id}",
                   datapath_id=datapath.id, port_no=port_no, port_name=port_name,
                   status=status, reason=reason)
        print(f"OpenFlow port status: s{datapath.id} {port_name or port_no} {status} ({reason})")

    def handle_provider_status(self, payload):
        provider_id = payload["provider_id"]
        status = payload["status"]

        # Retain desired status for switch reconnects and avoid resending an
        # identical FlowMod after a successful datapath submission.
        previous = self.provider_status.get(provider_id)
        if previous == payload:
            print(f"Ignoring duplicate status for {provider_id}: {status}")
            return

        endpoint_changed = previous is not None and any(
            previous.get(field) != payload.get(field)
            for field in ("ip", "protocol", "port")
        )
        print(f"Handling provider {provider_id}: {status}")

        # The agreed topology connects providers to s2 (datapath ID 2).
        datapath = self.datapaths.get(2)
        if datapath is None:
            # A drop may still be present in OVS while its controller
            # connection is down. Defer its endpoint-specific delete.
            if endpoint_changed:
                self._remember_stale_provider_endpoint(provider_id, previous)
            self.provider_status[provider_id] = payload
            print("s2 is not connected; status retained for later")
            return

        if endpoint_changed:
            self._remember_stale_provider_endpoint(provider_id, previous)
        self._remove_stale_provider_rules(datapath, provider_id)

        # Commit desired state only after all required FlowMods were submitted.
        # If a send fails, the queue worker retries the event.
        self.apply_provider_rule(datapath, payload, status)
        self.provider_status[provider_id] = payload

    def _remember_stale_provider_endpoint(self, provider_id, payload):
        if not hasattr(self, "stale_provider_endpoints"):
            self.stale_provider_endpoints = {}
        pending = self.stale_provider_endpoints.setdefault(provider_id, [])
        endpoint = (payload.get("ip"), payload.get("protocol"), payload.get("port"))
        if all((item.get("ip"), item.get("protocol"), item.get("port")) != endpoint
               for item in pending):
            pending.append(payload)

    def _remove_stale_provider_rules(self, datapath, provider_id):
        pending_by_provider = getattr(self, "stale_provider_endpoints", {})
        pending = pending_by_provider.get(provider_id, [])
        for old_endpoint in pending:
            # ACTIVE submits a strict endpoint-specific delete. Keep pending
            # state until submission succeeds so controller retries can retry.
            self.apply_provider_rule(datapath, old_endpoint, "ACTIVE")
        if pending:
            pending_by_provider.pop(provider_id, None)

    def _process_status_with_retry(self, payload):
        """Retry transient switch-send failures without losing a status event."""
        delay = 0.25
        while True:
            try:
                self.handle_provider_status(payload)
                return
            except Exception:
                self.logger.exception(
                    "Failed to process provider status notification; retrying"
                )
                hub.sleep(delay)
                delay = min(5.0, delay * 2)

    def process_status_updates(self):
        while True:
            try:
                payload = self.status_queue.get_nowait()
            except queue.Empty:
                hub.sleep(0.1)
                continue
            # Process queued transitions in order while a transient FlowMod
            # submission failure is retried.
            self._process_status_with_retry(payload)
