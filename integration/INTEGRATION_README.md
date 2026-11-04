# Service Discovery System — integration package

This package keeps the existing Registry architecture in `registry/`; the only Registry lifecycle addition is an extracted timeout check used by tests.
The `integration/` directory contains a controller adaptation and a topology
that connects the Ubuntu root namespace to Mininet's L2 network for the Registry
to reach the controller HTTP API.

## What is and isn't verified

- Teammate 1's original unit tests are preserved and should be run below.
- The controller installs a priority-0 table-miss rule to send unmatched
  packets to OS-Ken, plus priority-1 IPv4 and ARP rules using Open vSwitch
  `NORMAL`. Provider-specific drop rules at priority 100 override the baseline.
- Provider-specific rules are installed/removed only on s2 (DPID 2).
- `topology.py` adds a veth from the Ubuntu root namespace into s1. The root
  interface is `10.0.0.254/24`, so h3 can post to `10.0.0.254:8080`.
- Mininet and Open vSwitch are installed in this execution environment, but its
  sandbox denies network namespace and Open vSwitch database access. End-to-end
  behavior still needs to be run in your Ubuntu VM. Do not claim Mininet
  integration is proven until the traffic and flow-table checks pass.

## Single-command demo startup

From the project root, launch the controller, topology, Registry and seven
providers with:

```bash
sudo python3 integration/start_demo.py
```

The launcher keeps its Mininet CLI attached to the terminal after startup. Use
that `mininet>` prompt normally; type `exit` to stop the launcher-owned
topology, or from another terminal request a clean shutdown with:

```bash
sudo python3 integration/start_demo.py --stop
```

Startup logs are placed under `/tmp/service-discovery-demo-<launcher-pid>-*/`;
the controller, Registry and each provider have separate log files. The
launcher reuses a complete existing topology, controller, Registry or correctly
placed provider instead of starting a duplicate. Adopted processes are left
running at shutdown. An incomplete topology or a provider running in the wrong
host namespace causes startup to stop with an error. The prior manual commands
below remain supported.

The launcher verifies that Registry telemetry is live and that each configured
provider is ACTIVE and has recorded a heartbeat. This is a process/readiness
check, not packet-capture proof of end-to-end data flow.

The failure/recovery measurement runner exercises VIDEO-1 using its
authenticated heartbeat pause/resume control; the VIDEO service process remains
running while Registry timeout and SDN drop behavior are measured. The runner
verifies that the active Registry reports the production 30-second heartbeat
timeout and writes timestamps and durations to
`/tmp/video_failure_recovery_measurements.json`. Durations use
Registry discovery and live `ovs-ofctl` polling observations, so rule times are
observation intervals bounded by the poll interval, not switch acknowledgements.
Run it only in the Ubuntu Mininet VM:

```bash
sudo python3 integration/test_sdn_failure_recovery.py
```

Launcher unit tests:

```bash
python3 -m unittest integration.tests.test_start_demo -v
```

## 1. Extract and test Teammate 1's unchanged Registry

In Ubuntu, extract this ZIP. From the directory containing `registry/`:

```bash
cd registry
python3 -m unittest discover -s tests -v
```

Run the complete Registry test suite and require every test to pass. This is
the existing Registry implementation with regression coverage, not a replacement Registry.

## 2. Start the controller

Use the controller launcher from this checkout. The launcher resolves and verifies
`integration/controller.py` by absolute path, so its source selection does not
depend on the shell's current directory. Activate the existing OS-Ken environment
(if it is stored elsewhere, use that environment's activation path), then run:

```bash
source ~/service-discovery/.venv/bin/activate
python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/integration/run_controller.py > /tmp/controller.log 2>&1
```

Startup output records the launcher path, Python executable, working directory,
and resolved controller source. Confirm the source line names this checkout's
`integration/controller.py`; each switch should log a table-miss flow and IPv4
and ARP NORMAL baselines.
## 3. Start the new topology

In a separate Ubuntu terminal, stop the previous Mininet topology first (type
`exit` at its `mininet>` prompt). Then:

```bash
cd ~/Downloads/service_discovery_integration_final1/integration
sudo python3 topology.py
```

This topology has 13 hosts: five clients, one Registry, and seven providers.
- Clients: h1 10.0.0.1, h2 .2, h11 .11, h12 .12, h13 .13
- Registry: h3 .3 on UDP 5000
- Providers: h4 .4 through h10 .10 with the service mapping below
- All addresses are in 10.0.0.0/24; root API bridge: 10.0.0.254/24
- Clients and Registry attach to s1; seven providers attach to s2
- s1 DPID 1; s2 DPID 2
- controller at 127.0.0.1:6653
- root API bridge address 10.0.0.254/24 on s1

Do not run a second topology while the previous one is still active.

At the new `mininet>` prompt, verify the expanded topology:

```text
nodes
net
dump
pingall
```

Expect 13 hosts, two switches, five clients on s1, h3 on s1, and all seven
providers on s2. `pingall` should report no packet loss when the controller's
baseline forwarding is active.

## 4. Verify baseline forwarding before testing drop rules

At `mininet>` run:

```text
net
h1 ping -c 2 10.0.0.5
h2 ping -c 2 10.0.0.5
h11 ping -c 2 10.0.0.5
h12 ping -c 2 10.0.0.5
h13 ping -c 2 10.0.0.5
h4 ping -c 2 10.0.0.5
```

If baseline forwarding fails, do not test status changes yet. Confirm both
switches have a priority-0 controller table miss and priority-1 IPv4/ARP
`actions=NORMAL` flows:

```text
sh ovs-ofctl -O OpenFlow13 dump-flows s1
sh ovs-ofctl -O OpenFlow13 dump-flows s2
```

Ping should work before provider drop testing. (If ICMP is blocked by some separate
local setup, verify TCP service reachability using Teammate 3's actual server.)

## 5. Start Registry inside h3

At `mininet>` run (adjust the path to where the ZIP was extracted):

```text
h3 env CONTROLLER_URL=http://10.0.0.254:8080/provider-status python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/registry.py
```

This occupies the Mininet CLI while running. For a background process and a log:

```text
h3 env CONTROLLER_URL=http://10.0.0.254:8080/provider-status python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/registry.py > /tmp/registry.log 2>&1 &
```

The registry files are standard-library Python and need no pip install.

## 6. Start the real provider service and heartbeat agents

The topology creates a root-only lifecycle token at
`/tmp/service_discovery_lifecycle.token`. Start each provider in its assigned
Mininet host from the root-owned `mininet>` prompt created by `sudo python3
topology.py`. Provider processes must run as root in this setup to read the
mode-0600 token. These are long-running independent service processes;
dashboard controls pause only the heartbeat sender inside the selected
process. Each host has its own network namespace, so service ports may be reused
on different hosts without conflicting.

| Host | IP | Provider | Service | Protocol/port |
|---|---|---|---|---|
| h4 | 10.0.0.4 | FILE-1 | FILE | TCP/5001 |
| h5 | 10.0.0.5 | FILE-2 | FILE | TCP/5001 |
| h8 | 10.0.0.8 | FILE-3 | FILE | TCP/5001 |
| h6 | 10.0.0.6 | CHAT-1 | CHAT | TCP/5002 |
| h9 | 10.0.0.9 | CHAT-2 | CHAT | TCP/5002 |
| h7 | 10.0.0.7 | VIDEO-1 | VIDEO | UDP/5003 |
| h10 | 10.0.0.10 | VIDEO-2 | VIDEO | UDP/5003 |

At `mininet>` (replace the project path if it differs):

```text
h4 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id FILE-1 --service FILE --host 10.0.0.4 --port 5001 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/file1-provider.log 2>&1 &
h5 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id FILE-2 --service FILE --host 10.0.0.5 --port 5001 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/file2-provider.log 2>&1 &
h8 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id FILE-3 --service FILE --host 10.0.0.8 --port 5001 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/file3-provider.log 2>&1 &
h6 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id CHAT-1 --service CHAT --host 10.0.0.6 --port 5002 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/chat1-provider.log 2>&1 &
h9 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id CHAT-2 --service CHAT --host 10.0.0.9 --port 5002 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/chat2-provider.log 2>&1 &
h7 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id VIDEO-1 --service VIDEO --host 10.0.0.7 --port 5003 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/video1-provider.log 2>&1 &
h10 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id VIDEO-2 --service VIDEO --host 10.0.0.10 --port 5003 --lifecycle-token-file /tmp/service_discovery_lifecycle.token > /tmp/video2-provider.log 2>&1 &
```

Each provider registers at startup and sends `HEARTBEAT|<provider-id>` every 2
seconds. The Registry's default heartbeat timeout is 30 seconds and its timeout
checker runs every second. A paused provider continues serving TCP/UDP requests
until the controller's provider-specific drop rule is installed on s2.

Confirm startup, registration and lifecycle listener binding in every log:

```text
sh grep -E 'Registered|heartbeat control listening|listening (TCP|UDP)' /tmp/file1-provider.log /tmp/file2-provider.log /tmp/file3-provider.log /tmp/chat1-provider.log /tmp/chat2-provider.log /tmp/video1-provider.log /tmp/video2-provider.log
```

Each log should show its own `Registered <ID>`, `heartbeat control listening
UDP 55000`, and service listener line. If a token permission or bind problem
occurs, the corresponding log reports it; do not treat registration alone as
proof that lifecycle control is enabled.

### Reproducible provider failure and recovery check

Starting the topology creates Mininet hosts and links; it does not launch
Registry or provider application processes. In particular, h4 has no listening
service until FILE-1 is explicitly started. To run a single end-to-end check,
use:

```bash
sudo python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/integration/test_sdn_failure_recovery.py
```

The runner adopts a complete existing topology or starts and cleans up its own
topology. It starts any missing Registry/providers using the project entry
points, confirms all seven registrations and service sockets, pauses only
FILE-1 heartbeats through the existing authenticated lifecycle interface,
checks Registry exclusion and the s2 priority-100 drop, verifies FILE-2/FILE-3
remain usable and selectable, then resumes FILE-1 and checks rule removal and
service recovery. It directly contacts FILE-1's fixed address immediately
after pausing heartbeats, verifies the service still answers while no drop rule
exists, and records that TCP exchange time. After the Registry timeout and
priority-100 rule installation, it repeats the same direct request with a
1.2-second socket timeout and records the blocked result. This is the
no-drop-versus-SDN-drop baseline; actual timings are printed by the VM run and
are not predicted here. It stops only the processes it started and preserves
an adopted topology and its existing provider processes. Do not run it
alongside another test that changes provider lifecycle state.

## 7. Test discovery from all five clients

At `mininet>` (use the project path shown in the topology launch command):

```text
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h2 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h11 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h12 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h13 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
```

Expected after all FILE providers have registered and are sending heartbeats
(the order reflects the Registry's configured provider-selection policy):

```text
DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001|FILE-3,TCP,10.0.0.8,5001
```

## 8. Exercise failure and recovery from the dashboard

Start the dashboard in an Ubuntu shell on the VM (not from a remote browser):

```bash
cd /home/krishna-dhyan/Downloads/service_discovery_integration_final1
sudo env REGISTRY_HOST=10.0.0.3 REGISTRY_PORT=5000 \
  REGISTRY_LOG=/tmp/registry.log CONTROLLER_LOG=/tmp/controller.log \
  python3 frontend/app.py --host 0.0.0.0 --port 8088
```

Open `http://127.0.0.1:8088`. Confirm both FILE providers show `ONLINE`, inspect
the FILE discovery list for all three FILE instances, then use FILE-1's **Take offline**
button. The UI first reports the acknowledged heartbeat pause as
`DETECTING FAILURE`. Wait for the Registry's timeout (up to about 31 seconds
with the default 30-second timeout). The Registry log should report
`FILE-1 timed out -> INACTIVE` and then `Controller event sent: FILE-1 INACTIVE
-> HTTP 200`; the controller log should report handling FILE-1 and installing a
drop rule. Refresh OpenFlow evidence and confirm the priority-100 drop matches
IPv4 destination `10.0.0.4`, TCP destination port 5001. A new FILE discovery
should return FILE-2 and FILE-3, with ordering determined by the configured
selection policy. `Discover & send` should contact one of those healthy FILE
instances while FILE-1 is blocked.

Click **Bring online** for FILE-1. The UI reports `RECOVERING` until a heartbeat
makes the Registry active again. Confirm the Registry logs `FILE-1 recovered ->
ACTIVE` and `Controller event sent: FILE-1 ACTIVE -> HTTP 200`; confirm the
controller log reports removing the FILE-1 drop rule. The OpenFlow panel should
no longer show that matching drop, and FILE discovery should again include all
three FILE providers. Use `Discover & send` and verify a healthy FILE response.
Repeat an independent pause/recovery with CHAT-1/CHAT-2 and VIDEO-1/VIDEO-2 to
verify that same-service peers remain available and the TCP/UDP policies match.

The controller HTTP 200 means the status update was queued by the controller;
it is not an OpenFlow switch acknowledgement. Controller log output records the
rule update sent, while the live `ovs-ofctl -O OpenFlow13 dump-flows s2` output
is the evidence that the switch currently has or lacks the provider drop rule.
The Registry/controller logs are retained in `/tmp/registry.log` and
`/tmp/controller.log` for the dashboard event timeline.

## Important integration caveat

The HTTP endpoint runs in Ubuntu's root namespace; h3 is a separate Mininet network
namespace. Therefore `127.0.0.1` from h3 points to h3, not the Ubuntu controller.
The veth added by this topology provides the root namespace with address
`10.0.0.254/24` on the same L2 network. This is why the Registry's `CONTROLLER_URL`
must use `http://10.0.0.254:8080/provider-status`.

## Files and ownership

- `registry/`: original Teammate 1 source, unmodified.
- `integration/controller.py`: adapted from the current Teammate 2 controller.
- `integration/run_controller.py`: custom OS-Ken launcher.
- `integration/topology.py`: fixed topology plus root-to-Mininet API link.

## Stage 6 — failure detection, OpenFlow and recovery verification

### Existing behavior and notification sequence

Each provider process registers its own ID, then sends its own `HEARTBEAT|<id>` every 2 seconds and expects `HEARTBEAT_OK|<id>`. The Registry replies `ERROR|UNKNOWN_PROVIDER|<id>` when its in-memory provider table does not contain that ID. This lets a still-running provider re-register after a Registry restart; a missing heartbeat acknowledgement also triggers a registration attempt. The Registry defaults to a 30 second heartbeat timeout and checks once per second. A timeout changes only that provider to `INACTIVE`; discovery filters it from both FIRST_AVAILABLE and ROUND_ROBIN candidate lists. A heartbeat or registration after inactivity changes it to `ACTIVE` again.

Registry request handling uses a bounded worker queue. If the queue fills, the Registry logs the rejected datagram and replies `ERROR|SERVER_BUSY` when possible, rather than silently dropping it.

The Registry POSTs JSON to `/provider-status` with `provider_id`, `service`, `status`, `ip`, `port`, and `protocol`. New registration, inactive transition, and recovery each produce status events; repeated active registration/heartbeat does not produce repeated transition events. Registry HTTP notifications run asynchronously and are retried with exponential backoff (1 to 30 seconds) until the controller returns 2xx. Events are sent in order so a delayed inactive event cannot overtake its recovery event. The controller API's HTTP 200 only confirms queue acceptance, not switch application.

On the controller, both switches get a priority-0 controller table miss and priority-1 IPv4/ARP `actions=NORMAL` baselines. An inactive FILE-1 event submits a priority-100 drop matching IPv4 destination `10.0.0.4`, IP protocol TCP (6), and TCP destination port 5001, with no instructions/actions. Recovery deletes that exact priority/match. Other provider IPs and ports do not match it. VIDEO uses UDP (17) and `udp_dst`. Duplicate identical status deliveries are ignored after accepted application. Desired inactive state is retained and replayed after s2 reconnects; if `send_msg` raises, controller status processing retries the event.

Discovery filtering and the OpenFlow rule protect different request paths. Discovery stops advertising an inactive provider to clients that ask the Registry first. The drop rule also blocks direct requests to a cached or hardcoded endpoint while the provider process is still serving. The failure/recovery runner demonstrates this by measuring a direct FILE-1 request before the rule exists, then repeating it after the drop is installed. If FILE-1 re-registers at a changed IP, protocol, or port, the controller removes the old endpoint rule before applying the new status.

### Demo security boundary

The Registry UDP protocol does not authenticate `REGISTER` messages, and the
controller's `/provider-status` HTTP endpoint listens on `0.0.0.0:8080` without
authentication. This is acceptable only for the isolated demo network; these
endpoints must not be exposed to untrusted networks. Provider heartbeat
lifecycle controls are separately protected by the topology's shared token.

### Prepare an isolated Ubuntu VM run

Run only against the project's Mininet topology. Keep the existing token at `/tmp/service_discovery_lifecycle.token`; do not replace or expose it. Start controller, topology, Registry and providers using Sections 2–6 above. Ensure `/tmp/registry.log` and `/tmp/controller.log` are writable. Confirm all seven providers registered independently.

Capture baseline forwarding and flows before failures:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
sudo ovs-ofctl -O OpenFlow13 dump-flows s2
```

From the Mininet CLI, check FILE discovery:

```text
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
```

### Scenario A: one provider fails

Record the FILE-1 provider PID in h4 before terminating that exact process (`h4 ps -ef`); use `h4 kill <FILE-1-PID>`. Do not use a broad `pkill`. Allow up to 31 seconds for timeout detection. Then collect:

```bash
tail -n 100 /tmp/registry.log
tail -n 100 /tmp/controller.log
sudo ovs-ofctl -O OpenFlow13 dump-flows s2
```

The flow dump must show a priority-100 match equivalent to `ip,nw_dst=10.0.0.4,nw_proto=6,tp_dst=5001 actions=drop`. Check the exact output on the installed OVS version; the command output is the evidence. Verify discovery omits FILE-1 and still returns FILE-2/FILE-3. Verify FILE-2/FILE-3 service connections still work. A controller log or HTTP response alone is not proof of blocking.

Restart FILE-1 in h4 with its original command, ID, address, port and lifecycle token argument. Wait for `Registered FILE-1`, then verify the Registry's ACTIVE event, the controller event, and a new s2 flow dump with the FILE-1 drop absent. Confirm discovery includes FILE-1 again and its service responds. Save before/after flow dumps and logs as evidence.

### Scenario B: independent failures and both policies

With all providers healthy, stop FILE-1 and FILE-2 by their individually recorded PIDs. After timeout, verify two separate inactive transitions and two distinct matches for `10.0.0.4` and `10.0.0.5`; FILE-3 must remain discoverable and reachable. Restart each provider and verify each rule disappears only after that provider recovers.

Run the same controlled failure checks with FIRST_AVAILABLE and ROUND_ROBIN. Configure the registry at process start, for example:

```text
h3 env PROVIDER_SELECTION_POLICY=FIRST_AVAILABLE CONTROLLER_URL=http://10.0.0.254:8080/provider-status python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/registry.py
h3 env PROVIDER_SELECTION_POLICY=ROUND_ROBIN CONTROLLER_URL=http://10.0.0.254:8080/provider-status python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/registry.py
```

Stop the old Registry before starting another. Under ROUND_ROBIN, confirm repeated discovery rotates only healthy providers, skips inactive IDs, and includes recovered providers again. Do not infer the policy used from the benchmark CLI flag.

### Scenario C: controller API unavailable

The unit test mocks a failed HTTP attempt followed by a successful retry. For a VM check, use a disposable isolated topology and avoid host firewall changes: start the Registry with `CONTROLLER_URL` aimed at a known unused loopback port, cause one provider transition, then restart the Registry with the real API URL. Because the Registry process owns the retry queue, restarting it discards queued events; therefore this is only an outage observation, not proof of retry across Registry restart. To prove in-process recovery, temporarily stop only the HTTP API listener while retaining the OS-Ken OpenFlow connection, restore the listener on the same URL, and verify the queued event is accepted and the flow appears. Do not claim this scenario passed without logs and a fresh flow dump. Avoid firewall rule edits in a shared VM.

### Scenario D: switch reconnect

With FILE-1 inactive and its drop visible on s2, record `sudo ovs-vsctl get-controller s2`. In the isolated Mininet test only, disconnect and reconnect s2 to the existing controller:

```bash
sudo ovs-vsctl del-controller s2
sudo ovs-vsctl set-controller s2 tcp:127.0.0.1:6653
sudo ovs-vsctl get-controller s2
sudo ovs-ofctl -O OpenFlow13 dump-flows s2
```

Wait until the controller logs s2 reconnecting. Verify baseline `NORMAL` forwarding and the inactive FILE-1 drop are restored. Then recover FILE-1 and confirm its rule is removed. Save both flow dumps. These commands were prepared for the Ubuntu VM; no live switch reconnection was run in this restricted environment.

### Automated tests and evidence boundary

Run from the repository root:

```bash
python3 -m unittest discover -s registry/tests -v
python3 -m unittest discover -s providers/tests -v
python3 -m unittest discover -s integration/tests -v
```

The controller flow tests use fake datapaths and verify FlowMod fields, idempotency, retryability, and reconnect replay. Registry/controller HTTP tests use mocks. These are unit tests, not OpenFlow evidence. Only `ovs-ofctl -O OpenFlow13 dump-flows s2` captured on the VM proves the switch's current flow table; test logs and illustrative commands do not.
