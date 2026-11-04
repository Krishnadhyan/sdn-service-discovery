# Service discovery benchmarks and analysis

## FILE service comparison from the dashboard

For the service-level Performance Evaluation, start the dashboard in the Ubuntu
root namespace while Mininet is running. In the **Mininet client workload**
panel, use presets A, B and C for 1, 3 and 5 clients respectively, with 20
requests per client in each experiment. Run each preset once with **Dynamic · Registry** and
once with **Static · direct FILE-1**, keeping concurrency and timeout unchanged.
The static target is FILE-1 at `10.0.0.4:5001/TCP`; it uses the same provider
and client communication function but sends no Registry discovery request.

Dynamic mode measures mean and P95 discovery latency, discovery success rate,
and successful Registry discovery operations per second. P95 uses nearest rank
(`ceil(0.95 × n)`). Only valid Registry replies count as successful discovery;
provider communication failures remain service failures. These discovery
metrics are not applicable to static mode, which contacts FILE-1 directly.

The dashboard saves each completed run as
`benchmark-results/workload_<run-id>.json` and `.csv`. Each request record
includes mode, status, provider attempts/final provider, provider response,
discovery latency where applicable, and end-to-end latency. The dashboard
reports discovery and service metrics separately and shows the 1-, 3- and
5-client experiments in its results table. Failed or
cancelled requests count in the success-rate denominator, but only actual
successful service responses count toward throughput.

The standalone `benchmark_runner.py` below remains a discovery-only policy
benchmark. It does not execute FILE service requests or run in Mininet client
namespaces, so do not use its outputs as the static-versus-dynamic service
comparison.

## Run a benchmark

The runner creates one spawned OS process per benchmark client, each with its own UDP socket. The coordinator keeps active processes at or below `--max-concurrency`; its default is 2. A workload of 100 clients is scheduled in waves and does not imply 100 simultaneous processes.

```bash
python3 benchmark/benchmark_runner.py --clients 1 --requests 10 --max-concurrency 1 --service FILE --policy FIRST_AVAILABLE
python3 benchmark/benchmark_runner.py --clients 10 --requests 20 --max-concurrency 5 --service FILE --policy ROUND_ROBIN
python3 benchmark/benchmark_runner.py --clients 25 --requests 20 --max-concurrency 5 --service FILE --policy ROUND_ROBIN
```

The runner's `--policy` value is metadata. It does not configure the Registry. Start the Registry separately with the matching setting, for example:

```bash
PROVIDER_SELECTION_POLICY=FIRST_AVAILABLE python3 registry/registry.py
# For a separate ROUND_ROBIN run, restart the Registry with:
PROVIDER_SELECTION_POLICY=ROUND_ROBIN python3 registry/registry.py
```

Use the actual registry address for the deployment (`--registry-host`, `--registry-port`). Select FILE, CHAT, or VIDEO independently. The benchmark sends only discovery requests and does not contact the providers. Local request IDs associate every response with its client/request without changing the UDP protocol.

The benchmark writes a unique CSV and JSON pair under `benchmark-results/`. JSON carries workload config, elapsed execution duration and status. CSV contains individual request records only. A cancelled run records unfinished scheduled requests as worker errors; those are completed result records for throughput accounting, and the number of pending requests is derived from the planned total when JSON metadata is present.

## Analyze actual result files

```bash
python3 benchmark/analyze_results.py benchmark-results/benchmark_<run-id>.json
python3 benchmark/analyze_results.py benchmark-results/benchmark_<first>.json benchmark-results/benchmark_<round-robin>.json --compare-policies
python3 benchmark/analyze_results.py benchmark-results/benchmark_<run-id>.csv benchmark-results/benchmark_<run-id>.json
```

`--output-dir` selects a parent directory. Every analysis gets a unique child directory containing `analysis.json`, `summary.csv`, `provider_distribution.csv`, and six PNG charts. If CSV and JSON for the same run are supplied together, their normalized request records must match. The pair is analyzed once using JSON metadata. Duplicate request IDs are rejected; they are never silently deduplicated.

Analysis validates record fields, policy values, row uniqueness, successful provider IDs, configured service/policy, and JSON aggregate counts. Missing latency remains missing. A CSV by itself has no duration or planned-workload metadata, so its throughput, planned total, and pending count remain unknown. Supply its paired JSON for those metrics. Corrupt or incompatible inputs produce a clear error.

## Metric definitions

- **Total logical requests:** `clients × requests_per_client` from JSON configuration; unknown for standalone CSV unless its paired JSON is supplied.
- **Completed requests:** number of unique individual result records present, including failures and worker-error records.
- **Successful discoveries / failed discoveries:** count of `success=true` / `success=false` records.
- **Pending/incomplete:** planned total minus recorded results, floored at zero. Interrupted runs remain marked `cancelled`; missing results are not turned into successes.
- **Success rate:** successful discoveries divided by recorded completed results, as a percentage. It is unknown for an empty run.
- **Latency:** only successful records with a valid numeric `discovery_latency_ms` contribute. This is Registry UDP DISCOVER send-to-response time and excludes service communication. Missing values and failures are excluded, never treated as zero.
- **Average, median, min, max:** standard arithmetic mean, median, minimum, and maximum of the valid successful latency sample.
- **P95:** nearest-rank percentile: sort the `n` valid latency values and select the value at one-based rank `ceil(0.95 × n)`. It is unknown when there are no valid samples.
- **Throughput:** completed result records divided by the benchmark's recorded `elapsed_seconds`. Failures count as completed results; missing/pending work does not. For cancelled runs, the denominator is the duration measured up to cancellation and the numerator is the result records collected or synthesized by the runner. CSV alone has no duration, so throughput is unknown.
- **Provider distribution:** successful selected-provider counts and each provider's percentage of all successful selections in that run.

## Policy comparison

Use JSON outputs from both policies. Comparisons are marked compatible only when the service, clients, requests per client, concurrency, registry host and port are known and equal. Known provider inventory is compared when supplied; Stage 4's runner does not record an inventory, so inventory equivalence cannot be independently verified from its current outputs. Mismatches and unknown required settings are reported, and incompatible pairs are excluded from the policy comparison graph. Success rate, latency, throughput, and provider distribution are reported per compatible run. Provider distributions may differ, which is an observed outcome rather than a reason to reject otherwise equivalent workloads.

The `--policy` field only records the expected setting. Ensure the Registry was restarted/configured with the corresponding `PROVIDER_SELECTION_POLICY` before collecting each policy's results. The analysis cannot verify that the live Registry actually used the requested policy.

## Graphs and exports

The six PNGs show average latency vs clients, P95 latency vs clients, throughput vs clients, success rate vs clients, observed provider selection counts, and average latency for compatible FIRST_AVAILABLE/ROUND_ROBIN workload pairs. Axes include units/categories; missing metrics are omitted rather than filled. The graph files are generated from the source run records, with filenames and configurations retained in `analysis.json`. `summary.csv` has one row per analyzed run, and `provider_distribution.csv` has one row per observed provider/run.

PNG generation uses Pillow (`python3-pil` on Ubuntu); analysis and CSV/JSON processing otherwise use the Python standard library. Graph-generation tests use small deterministic fixtures and do not represent live network performance.

## Tests and environment limits

```bash
python3 -m unittest benchmark.tests.test_analyze_results -v
python3 -m unittest benchmark.tests.test_benchmark_runner -v
```

Tests verify known deterministic metrics, validation, consistency, policy compatibility, and PNG generation. Fixture values are test data only. In this restricted environment, socket creation for existing live-style client tests is denied; no live Registry or Mininet benchmark is run here. For actual measurements, use the Ubuntu ARM64 VM with the Registry and seven providers running, start with a small baseline, and choose concurrency according to available memory.
