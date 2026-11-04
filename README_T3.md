# T3 applications — service discovery integration

This adds runnable provider applications and a discovery/direct-communication client to the existing T1/T2 integration package. T1 registry message formats and T2 topology/controller files are preserved.

## Implemented behaviour

- `providers/provider.py` runs each of the seven configured provider instances independently. FILE and CHAT use TCP; VIDEO uses UDP. Every instance registers with T1, verifies `REGISTER_OK`, and sends `HEARTBEAT|<id>` every 2 seconds.
- TCP request format: UTF-8 text line (for example `PING` or `Hello`); response: `<PROVIDER-ID> OK service=<SERVICE> request=<REQUEST>`.
- UDP VIDEO request: UTF-8 text datagram; response: `VIDEO-1 OK service=VIDEO request=<REQUEST>`.
- `client/client.py` preserves the Registry's ordered provider list and, in the dashboard's one-shot service operation, tries providers sequentially with a bounded response timeout until one responds or all fail. A client-side failure does not change Registry health; provider availability is still determined by Registry heartbeats. The interactive CLI retains same-service fallback; CHAT and VIDEO never use another service type as a substitute.

## Expanded Mininet client topology

The current topology has five dedicated clients: h1 (10.0.0.1), h2 (.2), h11
(.11), h12 (.12), and h13 (.13). Registry h3 and all five clients attach to s1;
all seven provider hosts remain on s2. See `integration/INTEGRATION_README.md`
for the exact topology and VM validation commands.

## Launch from Mininet CLI

Assuming the ZIP is extracted at `/home/krishna-dhyan/Downloads/service_discovery_integration_final1`, T1 runs on h3 and the registry listens at `10.0.0.3:5000`:

```text
h4 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id FILE-1 --service FILE --host 10.0.0.4 --port 5001 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h5 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id FILE-2 --service FILE --host 10.0.0.5 --port 5001 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h8 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id FILE-3 --service FILE --host 10.0.0.8 --port 5001 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h6 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id CHAT-1 --service CHAT --host 10.0.0.6 --port 5002 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h9 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id CHAT-2 --service CHAT --host 10.0.0.9 --port 5002 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h7 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id VIDEO-1 --service VIDEO --host 10.0.0.7 --port 5003 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h10 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/providers/provider.py --id VIDEO-2 --service VIDEO --host 10.0.0.10 --port 5003 --lifecycle-token-file /tmp/service_discovery_lifecycle.token
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py FILE --request PING
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py FILE --request PING --prefer FILE-1
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py CHAT --request 'Hello CHAT-1'
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py VIDEO --request 'GET_FRAME 1'
h2 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py FILE --request PING
h11 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py CHAT --request 'Hello CHAT-1'
h12 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py VIDEO --request 'GET_FRAME 1'
h13 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/client/client.py FILE --request PING --prefer FILE-2
```

Start each provider command in the root-owned Mininet CLI created by `sudo python3 integration/topology.py`; the token has mode 0600. The explicit `--host` binds each service to that host's assigned address. The lifecycle listener uses UDP 55000 in each host's separate network namespace. Check each provider log for both `Registered <ID>` and `heartbeat control listening UDP 55000`. See `integration/INTEGRATION_README.md` for background commands and the full mapping.

## Standalone tests

From the project root:

```bash
python3 -m unittest discover -s registry/tests -v
python3 -m unittest discover -s tests -v
```

The tests cover client parsing of the existing discovery protocol and a direct TCP exchange. They do not prove Mininet, controller, OVS flow, or full failure/recovery integration. Those require running in the Ubuntu VM as specified in `integration/INTEGRATION_README.md` and the T3 guides.

## Failure/failover demonstration

1. Start all T1/T2 components and providers, then run `client.py FILE --prefer FILE-1`.
2. Stop one FILE provider process. After the registry's configured heartbeat timeout, discovery should exclude only that provider; the client can use the remaining FILE instances.
3. Verify controller's FILE-1 TCP/5001 drop rule and FILE-2 reachability with the T2 handoff commands.
4. Restart FILE-1; it registers again and should become discoverable. Verify the controller removes the matching drop rule.
5. Stop one CHAT or VIDEO instance and verify discovery retains any healthy peer of that service, never substituting another service.

The heartbeat timeout is whatever `registry/config.py` currently specifies (30 seconds in the supplied integration ZIP); allow that interval plus the check interval before expecting an INACTIVE event.

## Live frontend dashboard (T3 demonstration interface)

A dependency-free dashboard is provided in `frontend/app.py`. It queries the real Registry over UDP, uses discovery results to communicate directly with providers, reads actual Registry event logs, and queries the real OVS flow table. It does not fake provider status or flow changes.

With Mininet and all system components running, start the dashboard from a separate Ubuntu VM terminal (root namespace):

```bash
cd /home/krishna-dhyan/Downloads/service_discovery_integration_final1
sudo python3 frontend/app.py --host 0.0.0.0 --port 8088
```

Open `http://127.0.0.1:8088` in the Ubuntu VM browser. Full configuration and endpoint details are in `frontend/README.md`. For event history, start the Registry with output redirected to `/tmp/registry.log`, for example from the Mininet prompt:

```text
h3 env CONTROLLER_URL=http://10.0.0.254:8080/provider-status python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/registry.py > /tmp/registry.log 2>&1 &
```

Run `python3 -m unittest discover -s tests -v` to include dashboard bridge tests. The automated frontend tests mock the UDP replies and are not a substitute for checking the live dashboard inside Mininet.

## Handover status and known boundary

See `docs/T3_HANDOVER.md` and `docs/TEST_REPORT.md` for the handover checklist and observed manual integration results. The dashboard is read-only with respect to provider lifecycle: provider stop/restart actions are deliberately not simulated in the UI. Perform failure/recovery tests using the documented Mininet host commands, then use the dashboard to observe real results.
## Discovery Latency Measurement

The client measures service discovery latency using Python's `time.perf_counter()`. The measurement starts immediately before sending the UDP `DISCOVER` request to the Registry and ends when the Registry's response is received.

The measurement represents the **Registry discovery round-trip time**. It does not include the subsequent connection to or response from the selected service provider.

### Sample Results

Five FILE service discovery requests were measured during testing.

| Metric | Result |
|---|---:|
| Number of requests | 5 |
| Average latency | 0.909 ms |
| Minimum latency | 0.476 ms |
| Maximum latency | 1.755 ms |

These are observed results from one test session; latency may vary with system load and network conditions.
