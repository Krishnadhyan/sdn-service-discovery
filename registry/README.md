# Service Registry (Teammate 1)

The Registry is the "phone book" of the Service Discovery System. Providers register with it, clients ask it who offers a service, and it tells the SDN controller when a provider goes silent or comes back.

Pure Python 3 standard library. No pip install needed. Python 3.8 or newer.

## Files

| File | Purpose |
|---|---|
| `config.py` | All settings (ports, timeouts, controller URL, valid services and protocols) |
| `protocol.py` | Parses incoming UDP messages and formats replies |
| `provider_store.py` | Provider records, heartbeat timestamps, ACTIVE/INACTIVE status (thread-safe) |
| `controller_client.py` | Sends HTTP status events to the SDN controller from a background thread |
| `registry.py` | Main program: UDP server plus the timeout-checker loop |
| `tools/send.py` | Test helper: send one UDP message and print the reply |
| `tools/fake_controller.py` | Test helper: stands in for the SDN controller and prints events |
| `tests/` | Unit tests (7 tests) |

## Configuration (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `REGISTRY_HOST` | `0.0.0.0` | Address the Registry binds to |
| `REGISTRY_PORT` | `5000` | UDP port for REGISTER, HEARTBEAT, DISCOVER |
| `HEARTBEAT_INTERVAL` | `2` s | How often providers should send heartbeats |
| `HEARTBEAT_TIMEOUT` | `6` s | Provider becomes INACTIVE after this long without a heartbeat |
| `CHECK_INTERVAL` | `1` s | How often the timeout checker runs |
| `PROVIDER_SELECTION_POLICY` | `FIRST_AVAILABLE` | `FIRST_AVAILABLE` preserves sorted D1 order; `ROUND_ROBIN` rotates the active provider list per service |
| `CONTROLLER_URL` | `http://127.0.0.1:8080/provider-status` | Teammate 2's endpoint (**must be set for Mininet**) |
| `CONTROLLER_TIMEOUT` | `2` s | HTTP timeout so a dead controller cannot stall the Registry |

`REGISTRY_HOST`, `REGISTRY_PORT`, `HEARTBEAT_TIMEOUT`, `PROVIDER_SELECTION_POLICY` and `CONTROLLER_URL` can be overridden with environment variables. Set `PROVIDER_SELECTION_POLICY=ROUND_ROBIN` before starting the Registry to rotate which active provider is first in each discovery response; the full active list and response format are preserved.

## Run it (standalone, no Mininet)

Open 3 terminals, `cd` into the registry folder in each.

```bash
# Terminal 1: fake controller
python3 tools/fake_controller.py

# Terminal 2: the Registry
python3 registry.py

# Terminal 3: test commands
python3 tools/send.py "REGISTER|FILE-1|FILE|TCP|5001"
python3 tools/send.py "REGISTER|FILE-2|FILE|TCP|5001"
python3 tools/send.py "DISCOVER|FILE"
```

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

## Run it inside Mininet (on host h3)

In the `mininet>` prompt:

```
h3 CONTROLLER_URL=http://<AGREED_ADDRESS>:8080/provider-status python3 /path/to/registry/registry.py > ~/registry.log 2>&1 &
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py "DISCOVER|FILE" 10.0.0.3 5000
h2 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py "DISCOVER|FILE" 10.0.0.3 5000
h11 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py "DISCOVER|FILE" 10.0.0.3 5000
h12 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py "DISCOVER|FILE" 10.0.0.3 5000
h13 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py "DISCOVER|FILE" 10.0.0.3 5000
```

`<AGREED_ADDRESS>` is the address Teammate 2's controller is reachable at from inside h3. `127.0.0.1` inside h3 is NOT the Ubuntu VM. Agree the final value with Teammate 2 and record it here: `____________`

## Protocol (UDP, UTF-8, `|` delimited, no spaces)

| Message | Example | Reply |
|---|---|---|
| Register | `REGISTER\|FILE-1\|FILE\|TCP\|5001` | `REGISTER_OK\|FILE-1\|FILE` |
| Heartbeat | `HEARTBEAT\|FILE-1` | none |
| Discover | `DISCOVER\|FILE` | `DISCOVER_RESPONSE\|FILE\|FILE-1,TCP,10.0.0.4,5001\|FILE-2,TCP,10.0.0.5,5001\|FILE-3,TCP,10.0.0.8,5001` |
| Discover, none active | `DISCOVER\|VIDEO` | `ERROR\|SERVICE_UNAVAILABLE\|VIDEO` |

Rules:
- Provider IP is taken from the UDP packet's source address, not from the message.
- Provider IDs are unique per service instance (FILE-1/2/3, CHAT-1/2, VIDEO-1/2 in the Mininet topology).
- Heartbeat from an unknown ID is ignored (log warning). Register first.
- A heartbeat from an INACTIVE provider reactivates it. Re-registering also works.
- Malformed messages are logged and ignored (no reply). Clients must use timeouts.
- Discovery returns only ACTIVE providers with a non-expired heartbeat. The default policy sorts by ID; round-robin rotates the list while keeping the selected provider first.
- Valid services: FILE, CHAT, VIDEO. Valid protocols: TCP, UDP.

## Registry to SDN controller (HTTP)

Sent only when a provider changes status (not on every heartbeat).

```
POST /provider-status
Content-Type: application/json

{"provider_id": "FILE-1", "service": "FILE", "status": "INACTIVE",
 "ip": "10.0.0.4", "port": 5001, "protocol": "TCP"}
```

`status` is `ACTIVE` or `INACTIVE`. The call runs in a background thread with a 2 s timeout. If the controller is down, the error is logged and the Registry keeps serving UDP.

## Test results (standalone, run on Ubuntu VM)

| Test | Result |
|---|---|
| A: starts, listens, shuts down cleanly (Ctrl+C) | Passed |
| B: registration, REGISTER_OK, IP from packet | Passed |
| C: multiple providers in one response | Passed |
| D: SERVICE_UNAVAILABLE when none active | Passed |
| E: heartbeats keep provider active beyond timeout | Passed |
| F: expiry without any discovery request, one INACTIVE event | Passed |
| G: recovery by heartbeat, one ACTIVE event | Passed |
| H: controller down, error logged, discovery continues | Passed |
| I: malformed messages do not crash the Registry | Passed |
| Unit tests | 7 of 7 OK |
| J: Mininet integration (h3) | **Not yet tested** |

## Known limitations / not done

- Mininet integration (Test J) is pending Teammate 2's topology and the controller URL for h3.
- Registry state is in memory only; restarting it clears all providers (providers must register again).
- No retry queue for failed controller events. A failed INACTIVE/ACTIVE event is logged and not resent.
- Single UDP response; fine for a few providers, would need limits for many.
- The Registry is a single point of failure (by project design).

## Logs to keep as evidence

Registration, discovery, timeout, recovery and controller-failure logs from the tests above (see the screenshots in the `evidence` folder).
