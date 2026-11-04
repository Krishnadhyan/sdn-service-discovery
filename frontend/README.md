# T3 Live Dashboard

The dashboard is a small, dependency-free Python HTTP server and browser UI. It is an observability/demo bridge, not a replacement for T1/T2/T3.

The current Mininet topology has five clients (h1/h2/h11/h12/h13), the Registry
on h3, and seven providers on h4–h10. This dashboard runs in the Ubuntu root
namespace; the client count does not change its provider inventory or API.

## What it shows

- Provider inventory for FILE-1/2/3, CHAT-1/2 and VIDEO-1/2. Status, heartbeat timeouts and the selection policy come from Registry telemetry; if telemetry is missing or stale, state is shown as unavailable.
- Actual `DISCOVER|SERVICE` response strings from the Registry.
- Real direct TCP/UDP requests to a provider returned by the Registry, and the response bytes decoded as text.
- Recent event-related lines from the configured Registry log, including timeout/recovery and controller HTTP result lines.
- Manual `Discover & send` transactions in a separate JSONL activity log, including the request, selected provider, result, and actual response/error. Background status polls are excluded from this history and discovery lines are excluded from Registry events.
- The actual output of `ovs-ofctl -O OpenFlow13 dump-flows s2`.

Dashboard refreshes only read Registry telemetry; they do not send discovery packets. The Registry's discovery count includes packets sourced by h1, h2, h11, h12 or h13 and excludes the root dashboard bridge. The dashboard controls provider heartbeat transmission; it does not stop or start provider service processes.

## Run

Run in the Ubuntu VM's **root namespace**, while Mininet is active and the root-to-s1 bridge from `integration/topology.py` is present. This allows the dashboard to query `10.0.0.3:5000` through the existing `sdn-root` interface and run `ovs-ofctl` against the real switch.

```bash
cd /home/krishna-dhyan/Downloads/service_discovery_integration_final1
sudo python3 frontend/app.py --host 0.0.0.0 --port 8088
```

Open `http://127.0.0.1:8088` in the Ubuntu VM browser. Remote browsers may inspect the dashboard, but lifecycle controls accept requests only from localhost on the VM. Do not expose this development dashboard to an untrusted network.

Optional environment variables:

```bash
sudo env REGISTRY_HOST=10.0.0.3 REGISTRY_PORT=5000 \
  REGISTRY_LOG=/tmp/registry.log OVS_OFCTL=ovs-ofctl \
  python3 frontend/app.py --host 0.0.0.0 --port 8088
```

`DASHBOARD_ACTIVITY_LOG` can override the manual transaction history path; it defaults to `/tmp/dashboard_activity.jsonl`.

`REGISTRY_LOG` and `CONTROLLER_LOG` are optional legacy text-log inputs for
`GET /api/events`. The Stage 7 timeline uses the structured JSONL event files
described below and does not depend on stdout redirection.

## API routes

- `GET /api/status` — reads Registry status/liveness telemetry without sending discovery packets.
- `GET /api/discover?service=FILE|CHAT|VIDEO` — returns the actual Registry reply and parsed providers.
- `POST /api/communicate` with `{"service":"FILE","request":"PING"}` — discovers first, then sends the request directly to a returned provider.
- `GET /api/flows` — queries live OVS flow table on s2.
- `GET /api/events` — reads actual Registry lifecycle lines and filtered controller processing lines when `CONTROLLER_LOG` exists.
- `GET /api/activities` — reads manual dashboard transaction history.

This bridge uses only the Python standard library; no `pip install` is required.

## Run a real Mininet client workload

Start the topology, controller, Registry and providers using the existing D2
startup instructions. Start this dashboard as root in the VM namespace. In the
**Mininet client workload** panel, select one or more of h1/h2/h11/h12/h13, a
service, requests per client, per-client concurrency and sequential or
concurrent client execution. Requests are sent by the existing `client.py`
inside each selected host namespace via `mnexec`; the dashboard never sends a
substitute DISCOVER packet. The client uses the existing `DISCOVER|SERVICE`
wire format. Request IDs are local result IDs and are not added to the protocol.

One bounded client process is created per selected host. Concurrent mode runs
those processes together; each process uses up to 10 worker threads. Sequential
mode runs hosts in turn with one request active per host. The interface limits
requests to 500 per client and runs one workload at a time. Stop terminates only
the processes owned by that workload. Every request has a recorded success,
failure or explicit worker/cancellation outcome. Discovery-only workload runs
measure UDP request-to-response time and do not contact the selected provider.

The Registry policy shown is read-only and is captured as run metadata. Dynamic
mode preserves the Registry's returned order and existing sequential fallback.
Static mode is available for FILE only; it directly contacts configured FILE-1
at `10.0.0.4:5001/TCP` using the same client communication function and request
format, without contacting the Registry.

Failure/recovery demonstration: start a workload and use the existing provider
heartbeat controls in the inventory. The workload continues to issue real
discoveries while the Registry timeout and controller drop-rule events occur.
Provider transitions and OpenFlow rules remain based on Registry/controller
telemetry and live switch flow dumps; the dashboard does not invent them.

Workload requests are shown with request ID, Mininet host, actual provider
attempts, final provider, response, discovery latency and end-to-end latency.
Service success means a nonempty response was received through the existing
client communication path. Throughput counts successful service responses per
second; success rate uses completed requests as its denominator. Each completed
run saves raw request records and configuration to
`benchmark-results/workload_<run-id>.json` and `.csv`.

For the Performance Evaluation presets, run each preset in both Dynamic and
Static modes with the same timeout and concurrency. Experiments A, B and C use
1, 3 and 5 clients respectively, with 20 requests per client in each run. The
dashboard reports Registry discovery metrics separately from service
communication metrics. Discovery latency samples include only valid Registry
responses; P95 uses the nearest-rank definition. Static mode displays discovery
metrics as not applicable because it contacts FILE-1 directly. The results table
retains completed runs and shows each experiment and mode; matched static and
dynamic runs also have a compact service comparison.

The process launcher requires root privileges, `mnexec`, and running Mininet
host shells whose `/proc` command lines identify the expected `mininet:<host>`
namespaces. Mocked tests verify command construction and accounting only; they
do not demonstrate live Mininet connectivity. A live workload must be run on the
Ubuntu Mininet VM.

## Stage 7 operations dashboard

The dashboard retains the existing Python HTTP server and browser-only UI. It
renders the project's actual s1/s2 host map, including h1/h2/h11/h12/h13 on s1,
h3 as Registry, h4-h10 as the seven mapped providers on s2, and the s1-s2 link.
The VM API bridge and controller are shown as external management endpoints;
they are not additional Mininet hosts.

Provider status remains derived from a successful live Registry discovery.
Heartbeat receipt time, Registry selection policy/timeout, discovery request
counts and selection candidate counts come from Registry instrumentation.
The Registry writes:

- `/tmp/registry_events.jsonl`: observed heartbeat, provider status, discovery
  request/response and controller notification events with UTC timestamps.
- `/tmp/registry_stats.json`: exact counters and last-seen state for the
  current Registry process session. The event history is bounded; counters are
  kept separately, so log rotation does not lower the session request count.

The controller writes `/tmp/controller_events.jsonl` with switch connection
changes, HTTP status receipt and submitted baseline/drop FlowMods. These are
controller observations. The dashboard separately queries both actual OVS
flow tables and switch connection state; a submitted FlowMod is not presented
as installed unless it appears in the live s2 flow dump.

As the dashboard polls the flow table, it records provider-drop state changes
actually observed in `/tmp/dashboard_observed_events.jsonl`. The first
successful dump establishes a baseline and is not misreported as an install or
removal. Later appearance/disappearance events carry the observer timestamp;
the rule panel continues to show the current live OVS result.

The topology animates a packet only when a new observed event arrives. Heartbeat
markers follow provider → s2 → s1 → Registry and stop when no new heartbeat
event is read. Discovery directions use the recorded client IP. Dashboard
service transactions record their actual source IP and selected provider. The
provider application records a service-traffic event only after it sends a real
TCP/UDP response, including the observed client IP. This supports traffic from
the five Mininet clients as well as the VM dashboard bridge without inferring
service delivery from a Registry response.
Controller status and drop rule changes use controller events; the current rule
state is independently checked against OVS. The selection chart reports the
first provider in each Registry discovery response, which is the Registry's
candidate under FIRST_AVAILABLE or ROUND_ROBIN; client failover may contact a
later returned candidate if the first endpoint fails.

If event files or current-session counters are absent, the page says
`Unavailable`; it does not substitute zero. Event records are historical
observations, while Registry provider state, switch connectivity, and flow
rules are queried live. Marker animation runs only for newly read event IDs,
not for old history on initial page load. Demonstration fixtures are used only
in automated tests and are not loaded by the dashboard.

The telemetry writers use only the standard library and retain bounded JSONL
history (Registry default 2 MiB, controller default 1 MiB). Override paths or
limits before starting the respective process with `REGISTRY_EVENT_LOG`,
`REGISTRY_STATS_FILE`, `REGISTRY_EVENT_LOG_MAX_BYTES`,
`CONTROLLER_EVENT_LOG`, or `CONTROLLER_EVENT_LOG_MAX_BYTES`. Each provider
stores its own bounded event file under `/tmp/service_discovery_provider_events`
(256 KiB per provider by default); override with `PROVIDER_EVENT_LOG_DIR` and
`PROVIDER_EVENT_LOG_MAX_BYTES`. Restart providers after deploying this code
to enable service-traffic events from their process. The dashboard
must receive matching `REGISTRY_EVENT_LOG`, `REGISTRY_STATS_FILE`,
`CONTROLLER_EVENT_LOG`, `DASHBOARD_EVENT_LOG`, and `PROVIDER_EVENT_LOG_DIR`
values if custom paths are used.

### Additional dashboard APIs

- `GET /api/telemetry` — current-session Registry counters, heartbeat history,
  provider transitions and timestamped Registry/controller event records.
- `GET /api/controller` — live connection state and flow dumps for s1 and s2,
  plus actual provider-specific priority-100 drop presence on s2.

Run tests with:

```bash
python3 -m unittest tests.test_frontend -v
python3 -m unittest discover -s registry/tests -v
python3 -m unittest discover -s integration/tests -v
```

## Provider heartbeat controls

Provider cards include **Take offline** and **Bring online**. The controls change
only that provider's heartbeat sender; the service socket stays open. The card
shows `DETECTING FAILURE` after an acknowledged pause until a successful Registry
discovery omits the provider, then `OFFLINE`. After an acknowledged resume it
shows `RECOVERING` until Registry discovery includes the provider and the live flow dump no longer contains its matching drop rule. If Registry
queries fail, the dashboard does not claim `ONLINE` or `OFFLINE`.

The topology creates `/tmp/service_discovery_lifecycle.token` with mode 0600.
Each provider started with `--lifecycle-token-file` reads that shared secret and
listens on UDP 55000 for only its own `PAUSE_HEARTBEATS` and
`RESUME_HEARTBEATS` requests. It accepts those requests only from
`10.0.0.254` and validates both the secret and provider ID. The dashboard sends
requests only to a fixed provider ID/IP map; the lifecycle HTTP endpoint accepts
requests only from localhost. The API has no shell, PID, executable, or
arbitrary environment controls. Stop and restart the topology to rotate and
remove the lifecycle token.

Launch the dashboard from the Ubuntu VM itself to use the controls. The
structured timeline distinguishes Registry status transitions, notification
attempt/acknowledgement, controller receipt and FlowMod submission. Actual
provider drop presence and switch state are independently queried from OVS.

The lifecycle endpoint accepts only `{"provider":"FILE-1","operation":"offline"}`
or `online` at `POST /api/provider-lifecycle`. Requests return HTTP 202 only
after the provider acknowledges the heartbeat command. Registry state changes
and controller rule changes are observed separately.
