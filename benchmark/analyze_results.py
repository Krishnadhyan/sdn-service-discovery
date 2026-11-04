#!/usr/bin/env python3
"""Analyze recorded discovery benchmark CSV/JSON outputs and generate reports."""
import argparse
import csv
import json
import math
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = {"run_id", "client_id", "request_id", "requested_service", "selection_policy",
            "discovery_latency_ms", "selected_provider_id", "success", "failure_reason"}
POLICIES = {"FIRST_AVAILABLE", "ROUND_ROBIN"}


class AnalysisError(ValueError):
    pass


def _bool(value, field):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise AnalysisError(f"invalid {field}: expected true or false")


def _latency(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AnalysisError("discovery_latency_ms must be numeric or empty") from exc
    if not math.isfinite(number) or number < 0:
        raise AnalysisError("discovery_latency_ms must be a finite nonnegative number")
    return number


def _normalize_records(rows, source):
    records = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise AnalysisError(f"{source}: record {index} must be an object")
        missing = REQUIRED - row.keys()
        if missing:
            raise AnalysisError(f"{source}: record {index} missing fields: {', '.join(sorted(missing))}")
        record = dict(row)
        for field in ("run_id", "client_id", "request_id", "requested_service", "selection_policy"):
            if not isinstance(record[field], str) or not record[field]:
                raise AnalysisError(f"{source}: record {index} has invalid {field}")
        if record["selection_policy"] not in POLICIES:
            raise AnalysisError(f"{source}: record {index} has unsupported selection policy")
        record["success"] = _bool(record["success"], "success")
        record["discovery_latency_ms"] = _latency(record["discovery_latency_ms"])
        for optional in ("selected_provider_id", "failure_reason", "request_start_timestamp", "response_timestamp", "worker_pid"):
            if record.get(optional) == "":
                record[optional] = None
        if isinstance(record.get("worker_pid"), str):
            try:
                record["worker_pid"] = int(record["worker_pid"])
            except ValueError as exc:
                raise AnalysisError(f"{source}: record {index} has invalid worker_pid") from exc
        key = (record["run_id"], record["request_id"])
        if key in seen:
            raise AnalysisError(f"{source}: duplicate request ID {key}; duplicates are rejected")
        seen.add(key)
        if record["success"] and not record.get("selected_provider_id"):
            raise AnalysisError(f"{source}: successful request has no selected_provider_id")
        records.append(record)
    return records


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
            missing = REQUIRED - set(reader.fieldnames or ())
            raise AnalysisError(f"{path}: CSV header missing required fields: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise AnalysisError(f"{path}: CSV contains no request records")
    return {"records": _normalize_records(rows, str(path)), "configuration": None,
            "metadata": {}, "status": "unknown", "aggregate_counts": None,
            "source": str(path), "format": "csv"}


def _read_json(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AnalysisError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("request_results"), list):
        raise AnalysisError(f"{path}: expected benchmark JSON with request_results")
    records = _normalize_records(payload["request_results"], str(path))
    if not records:
        raise AnalysisError(f"{path}: JSON contains no request records")
    config = payload.get("configuration")
    metadata = payload.get("run_metadata")
    if not isinstance(config, dict) or not isinstance(metadata, dict):
        raise AnalysisError(f"{path}: missing configuration or run_metadata")
    status = payload.get("execution_status")
    if status not in {"completed", "cancelled"}:
        raise AnalysisError(f"{path}: unsupported execution_status {status!r}")
    return {"records": records, "configuration": config, "metadata": metadata,
            "status": status, "aggregate_counts": payload.get("aggregate_counts"),
            "source": str(path), "format": "json"}


def load_run(path):
    path = Path(path)
    if not path.is_file():
        raise AnalysisError(f"input file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path)
    if suffix == ".json":
        return _read_json(path)
    raise AnalysisError(f"unsupported input format for {path}; use CSV or JSON")


def consistency_check(csv_run, json_run):
    """Require identical per-request records when files claim the same run."""
    csv_ids = {r["run_id"] for r in csv_run["records"]}
    json_ids = {r["run_id"] for r in json_run["records"]}
    if len(csv_ids) != 1 or csv_ids != json_ids:
        raise AnalysisError("CSV/JSON consistency check requires one matching run_id in each file")
    def key(record):
        return (record["run_id"], record["client_id"], record["request_id"])
    left = {key(r): r for r in csv_run["records"]}
    right = {key(r): r for r in json_run["records"]}
    if left != right:
        raise AnalysisError("CSV and JSON request records differ")
    return {"consistent": True, "run_id": next(iter(csv_ids)), "record_count": len(left)}


def _config_value(run, key):
    config = run["configuration"] or {}
    if key in config:
        return config[key]
    return None


def analyze_run(run):
    records = run["records"]
    policies = {r["selection_policy"] for r in records}
    services = {r["requested_service"] for r in records}
    run_ids = {r["run_id"] for r in records}
    if len(policies) != 1 or len(services) != 1 or len(run_ids) != 1:
        raise AnalysisError(f"{run['source']}: mixed run IDs, services, or policies")
    latencies = [r["discovery_latency_ms"] for r in records
                 if r["success"] and r["discovery_latency_ms"] is not None]
    successes = sum(r["success"] for r in records)
    total_config = _config_value(run, "clients")
    per_client = _config_value(run, "requests_per_client")
    planned = total_config * per_client if isinstance(total_config, int) and isinstance(per_client, int) else None
    if planned is None and run["aggregate_counts"]:
        possible = run["aggregate_counts"].get("total_requests")
        if isinstance(possible, int) and possible >= len(records):
            planned = possible
    completed = len(records)
    if planned is not None and planned < completed:
        raise AnalysisError(f"{run['source']}: recorded requests exceed configured total")
    config = run["configuration"] or {}
    if config.get("service") is not None and config["service"] not in services:
        raise AnalysisError(f"{run['source']}: service does not match configuration")
    if config.get("policy") is not None and config["policy"] not in policies:
        raise AnalysisError(f"{run['source']}: policy does not match configuration")
    aggregate = run["aggregate_counts"] or {}
    expected_aggregates = {"completed_requests": completed, "successful_requests": successes,
                           "failed_requests": completed - successes}
    for field, value in expected_aggregates.items():
        if field in aggregate and aggregate[field] != value:
            raise AnalysisError(f"{run['source']}: aggregate {field} does not match request records")
    pending = max(0, planned - completed) if planned is not None else None
    duration = run["metadata"].get("elapsed_seconds")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise AnalysisError(f"{run['source']}: invalid elapsed_seconds") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise AnalysisError(f"{run['source']}: elapsed_seconds must be positive")
    throughput = completed / duration if duration else None
    distribution = Counter(r["selected_provider_id"] for r in records
                           if r["success"] and r.get("selected_provider_id"))
    selected = sum(distribution.values())
    provider_distribution = {provider: {"requests": count,
                              "selection_percentage": 100.0 * count / selected if selected else None}
                             for provider, count in sorted(distribution.items())}
    latency_summary = {
        "sample_count": len(latencies),
        "average_ms": statistics.mean(latencies) if latencies else None,
        "median_ms": statistics.median(latencies) if latencies else None,
        "p95_ms": sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None,
        "minimum_ms": min(latencies) if latencies else None,
        "maximum_ms": max(latencies) if latencies else None,
    }
    return {
        "run_id": next(iter(run_ids)), "service": next(iter(services)),
        "policy": next(iter(policies)), "execution_status": run["status"],
        "total_logical_requests": planned, "completed_requests": completed,
        "successful_discoveries": successes, "failed_discoveries": completed - successes,
        "pending_or_incomplete_requests": pending,
        "success_rate_percent": 100.0 * successes / completed if completed else None,
        "latency_ms": latency_summary, "throughput_requests_per_second": throughput,
        "provider_distribution": provider_distribution,
        "source": run["source"], "source_format": run["format"],
        "configuration": run["configuration"], "run_metadata": run["metadata"],
    }


WORKLOAD_KEYS = ("service", "clients", "requests_per_client", "max_concurrency", "registry_host", "registry_port")


def compare_runs(runs):
    """Compare different selection policies only when known workload config matches."""
    analyses = [analyze_run(run) for run in runs]
    by_policy = defaultdict(list)
    for analysis in analyses:
        by_policy[analysis["policy"]].append(analysis)
    if not by_policy.get("FIRST_AVAILABLE") or not by_policy.get("ROUND_ROBIN"):
        raise AnalysisError("comparison needs at least one FIRST_AVAILABLE and one ROUND_ROBIN run")
    comparisons = []
    for first in by_policy["FIRST_AVAILABLE"]:
        for round_robin in by_policy["ROUND_ROBIN"]:
            mismatches = []
            unknown = []
            first_run = next(r for r in runs if r["source"] == first["source"])
            rr_run = next(r for r in runs if r["source"] == round_robin["source"])
            for key in WORKLOAD_KEYS:
                left = first["service"] if key == "service" else _config_value(first_run, key)
                right = round_robin["service"] if key == "service" else _config_value(rr_run, key)
                if left is None or right is None:
                    unknown.append(key)
                elif left != right:
                    mismatches.append({"field": key, "first_available": left, "round_robin": right})
            # Compare provider inventory when both outputs provide it; otherwise report unknown inventory.
            first_config = first_run["configuration"] or {}
            rr_config = rr_run["configuration"] or {}
            inventory_key = next((key for key in ("provider_ids", "provider_configuration", "providers")
                                  if key in first_config or key in rr_config), None)
            if inventory_key:
                left, right = first_config.get(inventory_key), rr_config.get(inventory_key)
                if left is None or right is None:
                    unknown.append(inventory_key)
                elif left != right:
                    mismatches.append({"field": inventory_key, "first_available": left, "round_robin": right})
            if mismatches or unknown:
                comparisons.append({"compatible": False, "first_available_run": first["run_id"],
                    "round_robin_run": round_robin["run_id"], "mismatches": mismatches,
                    "unknown_settings": unknown,
                    "reason": "known workload mismatch" if mismatches else "required workload settings are unknown"})
                continue
            comparisons.append({"compatible": True, "first_available_run": first["run_id"],
                "round_robin_run": round_robin["run_id"], "mismatches": [], "unknown_settings": unknown,
                "metrics": {"FIRST_AVAILABLE": _comparison_metrics(first), "ROUND_ROBIN": _comparison_metrics(round_robin)}})
    return comparisons


def _comparison_metrics(analysis):
    return {"success_rate_percent": analysis["success_rate_percent"],
            "average_latency_ms": analysis["latency_ms"]["average_ms"],
            "median_latency_ms": analysis["latency_ms"]["median_ms"],
            "p95_latency_ms": analysis["latency_ms"]["p95_ms"],
            "throughput_requests_per_second": analysis["throughput_requests_per_second"],
            "provider_distribution": analysis["provider_distribution"]}


def _make_output_dir(root):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = Path(root) / f"analysis_{stamp}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write_summary_csv(path, analyses):
    fields = ["run_id", "service", "policy", "execution_status", "total_logical_requests",
              "completed_requests", "successful_discoveries", "failed_discoveries",
              "pending_or_incomplete_requests", "success_rate_percent", "average_latency_ms",
              "median_latency_ms", "p95_latency_ms", "minimum_latency_ms", "maximum_latency_ms",
              "throughput_requests_per_second", "source"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in analyses:
            writer.writerow({"run_id": item["run_id"], "service": item["service"], "policy": item["policy"],
                "execution_status": item["execution_status"], "total_logical_requests": item["total_logical_requests"],
                "completed_requests": item["completed_requests"], "successful_discoveries": item["successful_discoveries"],
                "failed_discoveries": item["failed_discoveries"],
                "pending_or_incomplete_requests": item["pending_or_incomplete_requests"],
                "success_rate_percent": item["success_rate_percent"],
                "average_latency_ms": item["latency_ms"]["average_ms"], "median_latency_ms": item["latency_ms"]["median_ms"],
                "p95_latency_ms": item["latency_ms"]["p95_ms"], "minimum_latency_ms": item["latency_ms"]["minimum_ms"],
                "maximum_latency_ms": item["latency_ms"]["maximum_ms"],
                "throughput_requests_per_second": item["throughput_requests_per_second"], "source": item["source"]})


def generate_graphs(analyses, output_dir, comparisons=None):
    """Write six source-traceable PNG charts using Pillow; plot only observed groups."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise AnalysisError("PNG chart generation requires Pillow (python3-pil)") from exc
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    colors = {"FIRST_AVAILABLE": (35, 95, 180), "ROUND_ROBIN": (210, 90, 45)}
    def chart(filename, title, ylabel, series, percent=False, xlabel="Number of clients", tick_labels=None):
        width, height = 1100, 680
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        left, right, top, bottom = 100, 960, 75, 510
        draw.text((left, 20), title, fill="black")
        draw.text((12, 45), ylabel, fill="black")
        draw.line((left, top, left, bottom, right, bottom), fill="black", width=2)
        values = [y for points, _label, _color in series for _x, y in points if y is not None]
        if not values:
            draw.text((left + 20, top + 30), "No recorded values", fill="gray")
            image.save(out / filename)
            return
        ymin = 0 if percent else min(0, min(values))
        ymax = max(values) * 1.1 if max(values) > 0 else 1
        if percent:
            ymax = 100
        def xy(index, x, y, count):
            px = left + 20 + index * (right - left - 40) / max(1, count - 1)
            py = bottom - 20 - (y - ymin) * (bottom - top - 50) / max(1e-9, ymax - ymin)
            return int(px), int(py)
        xs = sorted({x for points, _, _ in series for x, y in points if y is not None})
        for tick in range(5):
            y = ymin + (ymax - ymin) * tick / 4
            py = bottom - 20 - (y - ymin) * (bottom - top - 50) / max(1e-9, ymax - ymin)
            draw.line((left, int(py), right, int(py)), fill=(225, 225, 225))
            draw.text((45, int(py)-6), f"{y:.1f}", fill="black")
        xindex = {x: i for i, x in enumerate(xs)}
        for points, label, color in series:
            mapped = [(xy(xindex[x], x, y, len(xs)), x, y) for x, y in points if y is not None]
            for idx in range(1, len(mapped)):
                # Leave a visible gap when a workload category has no recorded value.
                if xindex[mapped[idx][1]] == xindex[mapped[idx-1][1]] + 1:
                    draw.line((*mapped[idx-1][0], *mapped[idx][0]), fill=color, width=3)
            for point, _x, _y in mapped:
                px, py = point
                draw.ellipse((px-5, py-5, px+5, py+5), fill=color)
        for x, idx in xindex.items():
            px, _ = xy(idx, x, ymin, len(xs))
            label = tick_labels.get(x, str(x)) if tick_labels else str(x)
            draw.text((px-10, bottom+8), label, fill="black")
        draw.text((left + (right-left)//2 - 60, height-35), xlabel, fill="black")
        for i, (_points, label, color) in enumerate(series):
            yy = 550 + i*18
            draw.line((left+10, yy+5, left+35, yy+5), fill=color, width=3)
            draw.text((left+42, yy), label, fill="black")
        sources = ", ".join(Path(a["source"]).name for a in analyses)
        draw.text((left, 630), "Sources: " + sources[:150], fill=(70, 70, 70))
        image.save(out / filename)
    def groups(metric):
        by_policy = defaultdict(list)
        for a in analyses:
            clients = a["configuration"].get("clients") if a["configuration"] else None
            y = metric(a)
            if clients is not None and y is not None:
                by_policy[a["policy"]].append((clients, y))
        return [(sorted(points), policy, colors[policy]) for policy, points in sorted(by_policy.items())]
    chart("average_latency_vs_clients.png", "Average discovery latency vs clients", "Latency (ms)", groups(lambda a: a["latency_ms"]["average_ms"]))
    chart("p95_latency_vs_clients.png", "P95 discovery latency vs clients", "Latency (ms)", groups(lambda a: a["latency_ms"]["p95_ms"]))
    chart("throughput_vs_clients.png", "Throughput vs clients", "Requests per second", groups(lambda a: a["throughput_requests_per_second"]))
    chart("success_rate_vs_clients.png", "Success rate vs clients", "Success (%)", groups(lambda a: a["success_rate_percent"]), percent=True)
    # Provider counts are grouped by provider and policy, with no unobserved provider entries added.
    providers = sorted({p for a in analyses for p in a["provider_distribution"]})
    provider_series = []
    for policy in sorted(POLICIES):
        policy_runs = [a for a in analyses if a["policy"] == policy]
        if not policy_runs:
            continue
        total = sum(sum(v["requests"] for v in a["provider_distribution"].values()) for a in policy_runs)
        counts = Counter()
        for a in policy_runs:
            for provider, value in a["provider_distribution"].items():
                counts[provider] += value["requests"]
        provider_series.append(([(i, counts[p]) for i, p in enumerate(providers)], policy, colors[policy]))
    chart("provider_request_distribution.png", "Provider request distribution", "Successful discoveries", provider_series,
          xlabel="Provider category", tick_labels={i: provider for i, provider in enumerate(providers)})
    # This graph shows only workload pairs that passed the compatibility check.
    pair_series = []
    compatible = [c for c in comparisons if c["compatible"]] if comparisons is not None else []
    for policy in ("FIRST_AVAILABLE", "ROUND_ROBIN"):
        points = []
        for index, comparison in enumerate(compatible, start=1):
            metric = comparison["metrics"][policy]["average_latency_ms"]
            if metric is not None:
                points.append((index, metric))
        pair_series.append((points, policy, colors[policy]))
    chart("policy_latency_comparison.png", "Policy comparison: average discovery latency", "Latency (ms)", pair_series,
          xlabel="Compatible workload pair")
    return [str(out / name) for name in ("average_latency_vs_clients.png", "p95_latency_vs_clients.png",
        "throughput_vs_clients.png", "success_rate_vs_clients.png", "provider_request_distribution.png",
        "policy_latency_comparison.png")]


def analyze_files(paths, output_root="analysis-results", compare=False):
    runs = [load_run(path) for path in paths]
    # If both formats for a run are present, validate them rather than double-counting the run.
    by_run_format = defaultdict(dict)
    for run in runs:
        run_ids = {r["run_id"] for r in run["records"]}
        if len(run_ids) == 1:
            by_run_format[next(iter(run_ids))][run["format"]] = run
    for formats in by_run_format.values():
        if "csv" in formats and "json" in formats:
            consistency_check(formats["csv"], formats["json"])
    selected = []
    consumed = set()
    for run in runs:
        run_id = run["records"][0]["run_id"]
        formats = by_run_format.get(run_id, {})
        if len(formats) == 2:
            chosen = formats["json"]
            if chosen["source"] not in consumed:
                selected.append(chosen)
                consumed.update(item["source"] for item in formats.values())
        elif run["source"] not in consumed:
            selected.append(run)
            consumed.add(run["source"])
    analyses = [analyze_run(run) for run in selected]
    comparisons = compare_runs(selected) if compare else []
    output = _make_output_dir(output_root)
    sources = [{"filename": run["source"], "format": run["format"], "configuration": run["configuration"]}
               for run in runs]
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "source_files": sources,
              "runs": analyses, "policy_comparisons": comparisons,
              "methodology": {"p95": "nearest-rank percentile: sorted successful latencies, rank=ceil(0.95*n)",
                  "latency_population": "successful request records with a valid recorded discovery_latency_ms",
                  "throughput": "completed request records divided by recorded benchmark elapsed_seconds",
                  "csv_only_note": "CSV lacks configuration and duration metadata; planned total, pending count, and throughput remain unknown unless supplied by paired JSON."}}
    (output / "analysis.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _write_summary_csv(output / "summary.csv", analyses)
    provider_fields = ["run_id", "policy", "provider_id", "requests", "selection_percentage", "source"]
    with (output / "provider_distribution.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=provider_fields)
        writer.writeheader()
        for a in analyses:
            for provider, data in a["provider_distribution"].items():
                writer.writerow({"run_id": a["run_id"], "policy": a["policy"], "provider_id": provider,
                                 "requests": data["requests"], "selection_percentage": data["selection_percentage"],
                                 "source": a["source"]})
    graphs = generate_graphs(analyses, output / "graphs", comparisons)
    return {"output_dir": str(output), "report": report, "graphs": graphs}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="benchmark CSV and/or JSON output files")
    parser.add_argument("--output-dir", default="analysis-results", help="parent directory for unique analysis output")
    parser.add_argument("--compare-policies", action="store_true", help="compare compatible FIRST_AVAILABLE and ROUND_ROBIN runs")
    args = parser.parse_args(argv)
    try:
        result = analyze_files(args.inputs, args.output_dir, args.compare_policies)
    except (AnalysisError, OSError) as exc:
        parser.error(str(exc))
    print(f"Analysis written to {result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
