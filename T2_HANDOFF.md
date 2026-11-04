# T2 Handoff and T3 Integration Reference

This bundled handoff is included so the T3 archive is easier to pass to teammates. It preserves the integration/run instructions from `integration/INTEGRATION_README.md`; the authoritative T2 topology/controller files remain in `integration/`.

---

# Service Discovery System — integration package

This package preserves the uploaded Teammate 1 Registry source in `registry/`.
The `integration/` directory contains a controller adaptation and a topology
that connects the Ubuntu root namespace to Mininet's L2 network for the Registry
to reach the controller HTTP API.

## What is and isn't verified

- Teammate 1's original unit tests are preserved and should be run below.
- The controller installs a priority-0 table miss to OS-Ken and priority-1
  IPv4/ARP `NORMAL` forwarding baselines. Provider-specific priority-100 drops
  take precedence.
- Provider-specific rules are installed/removed only on s2 (DPID 2).
- `topology.py` adds a veth from the Ubuntu root namespace into s1. The root
  interface is `10.0.0.254/24`, so h3 can post to `10.0.0.254:8080`.
- I cannot execute Mininet/Open vSwitch inside this packaging environment, so
  end-to-end behavior still needs to be run in your Ubuntu VM. Do not claim
  Mininet integration is proven until the traffic and flow-table checks pass.

## 1. Extract and test Teammate 1's unchanged Registry

In Ubuntu, extract this ZIP. From the directory containing `registry/`:

```bash
cd registry
python3 -m unittest discover -s tests -v
```

Run the complete current Registry test suite and require every test to pass.
This is the actual Teammate 1 implementation, not a replacement Registry.

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

This topology has 13 hosts: clients h1 (.1), h2 (.2), h11 (.11), h12 (.12),
and h13 (.13); Registry h3 (.3); and seven providers h4 (.4) through h10 (.10).
All hosts use 10.0.0.0/24. Clients and h3 connect to s1; providers connect to s2.
- s1 DPID 1; s2 DPID 2
- controller at 127.0.0.1:6653
- root API bridge address 10.0.0.254/24 on s1

Do not run a second topology while the previous one is still active.

## 4. Verify baseline forwarding before testing drop rules

At `mininet>` run:

```text
nodes
net
dump
pingall
h1 ping -c 2 10.0.0.5
h2 ping -c 2 10.0.0.5
h11 ping -c 2 10.0.0.5
h12 ping -c 2 10.0.0.5
h13 ping -c 2 10.0.0.5
h4 ping -c 2 10.0.0.5
```

If baseline forwarding fails, do not test status changes yet. Check both switches
for a priority-0 controller table miss and priority-1 IPv4/ARP `NORMAL` flows:

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

## 6. Start provider registration/heartbeat agents

Teammate 1's ZIP includes a manual sender, not a long-running heartbeat agent.
Use the provider processes written by Teammate 3 if available. They should send:
- `REGISTER|FILE-1|FILE|TCP|5001` from h4
- `REGISTER|FILE-2|FILE|TCP|5001` from h5
- `REGISTER|CHAT-1|CHAT|TCP|5002` from h6
- `REGISTER|VIDEO-1|VIDEO|UDP|5003` from h7
- `HEARTBEAT|<provider-id>` every 2 seconds

The current seven-provider launch map and exact provider commands are maintained
in `integration/INTEGRATION_README.md`. Start those providers from the root-owned
Mininet CLI so they can read `/tmp/service_discovery_lifecycle.token`. The
Registry learns each provider IP from its UDP source address. One-off registration
messages expire unless independent heartbeats continue.

## 7. Test discovery from each client

At `mininet>` using the existing Registry helper command:

```text
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h2 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h11 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h12 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h13 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
```

Expected after all three FILE providers have registered and are sending heartbeats:

```text
DISCOVER_RESPONSE|FILE|FILE-1,TCP,10.0.0.4,5001|FILE-2,TCP,10.0.0.5,5001|FILE-3,TCP,10.0.0.8,5001
```

## 8. Test controller API and drop rule (after baseline passes)

From an Ubuntu shell:

```bash
curl -sS -X POST http://127.0.0.1:8080/provider-status \
  -H 'Content-Type: application/json' \
  -d '{"provider_id":"FILE-1","service":"FILE","status":"INACTIVE","ip":"10.0.0.4","port":5001,"protocol":"TCP"}'
```

Then in Mininet:

```text
sh ovs-ofctl -O OpenFlow13 dump-flows s2
```

Expect a priority-100 drop matching IPv4 destination 10.0.0.4, TCP destination port 5001.
`FILE-2` at 10.0.0.5 should remain reachable. Test with the actual service on h4
still running; don't stop the service process for this SDN test.

Restore FILE-1:

```bash
curl -sS -X POST http://127.0.0.1:8080/provider-status \
  -H 'Content-Type: application/json' \
  -d '{"provider_id":"FILE-1","service":"FILE","status":"ACTIVE","ip":"10.0.0.4","port":5001,"protocol":"TCP"}'
```

Check that only the matching priority-100 drop disappears and normal forwarding remains.

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
