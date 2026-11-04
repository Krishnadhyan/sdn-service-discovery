#!/usr/bin/env python3
"""Run an overlapping batch of valid UDP DISCOVER requests from one host."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import socket
import threading


def discover(registry, service, timeout, start):
    start.wait()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(f"DISCOVER|{service}".encode(), registry)
        raw, _ = sock.recvfrom(8192)
    text = raw.decode("utf-8").strip()
    fields = text.split("|")
    if fields[:2] == ["ERROR", "SERVICE_UNAVAILABLE"] and len(fields) == 3:
        if fields[2] != service:
            raise RuntimeError(f"wrong service in response: {text!r}")
        return "unavailable"
    if len(fields) < 2 or fields[0] != "DISCOVER_RESPONSE" or fields[1] != service:
        raise RuntimeError(f"invalid discovery response: {text!r}")
    for item in fields[2:]:
        bits = item.split(",")
        if len(bits) != 4 or not bits[0] or bits[1] not in {"TCP", "UDP"}:
            raise RuntimeError(f"invalid provider record: {item!r}")
        int(bits[3])
    return "discovered"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-host", default="10.0.0.3")
    parser.add_argument("--registry-port", type=int, default=5000)
    parser.add_argument("--service", choices=("FILE", "CHAT", "VIDEO"), default="FILE")
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--parallelism", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    if args.count < 1 or args.parallelism < 1 or args.timeout <= 0:
        parser.error("count and parallelism must be positive; timeout must be > 0")

    registry = (args.registry_host, args.registry_port)
    # Keep only a bounded number of client threads; each wave starts together.
    outcomes = []
    with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        for offset in range(0, args.count, args.parallelism):
            batch = min(args.parallelism, args.count - offset)
            start = threading.Barrier(batch)
            futures = [pool.submit(discover, registry, args.service, args.timeout, start)
                       for _ in range(batch)]
            outcomes.extend(f.result() for f in futures)
    print(f"OK: {len(outcomes)} valid {args.service} discovery responses "
          f"({outcomes.count('discovered')} discovered, "
          f"{outcomes.count('unavailable')} unavailable)", flush=True)


if __name__ == "__main__":
    main()
