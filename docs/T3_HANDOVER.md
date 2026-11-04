# T3 Handover — Service Discovery System

## Included in this package

- `providers/provider.py`: real configurable provider process for seven independent instances: FILE-1/2/3 over TCP, CHAT-1/2 over TCP and VIDEO-1/2 over UDP; registration acceptance is checked and heartbeats are sent every 2 seconds.
- `client/client.py`: existing T1 UDP discovery protocol and direct TCP/UDP communication; dashboard one-shot service operations now attempt the Registry's ordered provider list sequentially with a bounded per-provider timeout, without changing Registry health state.
- `frontend/app.py`: browser dashboard that reads live Registry discovery, directly communicates with discovered providers, reads real Registry event logs, and queries actual OVS flows.
- `registry/`: existing T1 registry and tests, included for integrated repeatability.
- `integration/`: existing T2 topology/controller integration files, included for integrated repeatability.
- `tests/`: client and dashboard bridge tests.
- `frontend/README.md`, `README_T3.md`, `integration/INTEGRATION_README.md`: run and verification instructions.

T1/T2 source and message formats have been reused. The dashboard is not a substitute for the Registry or controller.

## Fixed endpoints

The topology has five clients (h1 .1, h2 .2, h11 .11, h12 .12, h13 .13),
one Registry (h3 .3), seven providers, and switches s1/s2. All host addresses
are in 10.0.0.0/24; clients and Registry connect to s1 and providers to s2.

| Component | Mininet host | Address | Transport |
|---|---|---|---|
| Registry | h3 | 10.0.0.3:5000 | UDP |
| FILE-1 | h4 | 10.0.0.4:5001 | TCP |
| FILE-2 | h5 | 10.0.0.5:5001 | TCP |
| FILE-3 | h8 | 10.0.0.8:5001 | TCP |
| CHAT-1 | h6 | 10.0.0.6:5002 | TCP |
| CHAT-2 | h9 | 10.0.0.9:5002 | TCP |
| VIDEO-1 | h7 | 10.0.0.7:5003 | UDP |
| VIDEO-2 | h10 | 10.0.0.10:5003 | UDP |
| Controller API | Ubuntu root namespace | 10.0.0.254:8080/provider-status | HTTP |
| Dashboard | Ubuntu root namespace | http://127.0.0.1:8088 | HTTP |

## Dashboard launch

Start only after the topology, controller, Registry, and providers are running. In a separate Ubuntu VM terminal:

```bash
cd /home/krishna-dhyan/Downloads/service_discovery_integration_final1
sudo python3 frontend/app.py --host 0.0.0.0 --port 8088
```

Open `http://127.0.0.1:8088`. The dashboard uses the root namespace's `sdn-root` interface to reach the Registry. It queries OVS with `ovs-ofctl`; run it with the permissions needed for that command. Keep it on localhost/trusted lab network only.

For event timeline records, launch the Registry with a log file from Mininet:

```text
h3 env CONTROLLER_URL=http://10.0.0.254:8080/provider-status python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/registry.py > /tmp/registry.log 2>&1 &
```

The log path is shared with the Ubuntu root filesystem. If using a different path, launch the dashboard with `REGISTRY_LOG=/path/to/registry.log`.

## Test discovery from every client

At the Mininet prompt, the Registry helper syntax remains unchanged:

```text
h1 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h2 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h11 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h12 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
h13 python3 /home/krishna-dhyan/Downloads/service_discovery_integration_final1/registry/tools/send.py 'DISCOVER|FILE' 10.0.0.3 5000
```

## Run tests

From package root:

```bash
python3 -m unittest discover -s registry/tests -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q providers client frontend registry integration tests
```

The dashboard tests mock Registry replies and validate response parsing / UNKNOWN handling; they do not prove the browser can reach Mininet. The manual integration checks recorded in `docs/TEST_REPORT.md` are separate from these automated tests.

## Demo outline

1. Open the dashboard and confirm Registry connected and all seven provider instances show ACTIVE.
2. Select FILE, CHAT, and VIDEO in turn; send `PING`. Confirm the provider response identifies the real provider.
3. Show the event panel and flow table before failure.
4. At the Mininet CLI stop FILE-1, wait beyond the configured heartbeat timeout, and show the Registry event and priority-100 FILE-1 drop rule. Verify FILE client uses FILE-2.
5. Restart FILE-1 and show its ACTIVE event, removal of the drop rule, and successful client request.
6. Stop CHAT-1, wait for timeout, show CHAT unavailable. Restart it and verify recovery.
7. Repeat the unavailable/recovery check for VIDEO-1.
8. Refresh dashboard panels after each event. It polls Registry every five seconds; events refresh approximately every eight seconds.

The dashboard has no “simulate failure” buttons. Lifecycle changes are performed through real Mininet provider processes; the UI only observes and reports the actual resulting state.

## Notes for teammates

- Extract this archive to a stable path and update the absolute paths in commands to match the VM.
- Do not run two Mininet topologies at once.
- Use the agreed controller virtual environment/launcher from T2 if OS-Ken dependencies are installed there.
- Do not report a check as passed unless its observed output was recorded. See `docs/TEST_REPORT.md` for which checks were actually observed in the shared VM session and which still need a final package rerun.
