#!/usr/bin/env python3
"""Measure which server:port endpoints answer a TCP connect from *this* vantage point.

Run on a GitHub runner (see .github/workflows/measure.yml) to compare against the endpoints
known to work from Iran: which live-from-Iran configs would a runner-side prefilter drop?
Output is JSON: {"host:port": {"r": result, "ip": resolved ip or null, "ms": connect ms}}.
"""

import base64
import json
import queue
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from proxyUtil.prefilter import needs_tcp_check, server_endpoint

DNS_DEADLINE = 30.0
CONNECT_TIMEOUT = 4.0


def resolve(hosts: list[str]) -> dict[str, str | None]:
    """Resolve on daemon threads; a hung getaddrinfo must not block the run."""
    todo, out = queue.Queue(), {}
    for host in hosts:
        todo.put(host)

    def work():
        while True:
            try:
                host = todo.get_nowait()
            except queue.Empty:
                return
            try:
                infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
                out[host] = (v4 or [i[4][0] for i in infos])[0]
            except (OSError, UnicodeError):
                out[host] = None

    for _ in range(200):
        threading.Thread(target=work, daemon=True).start()
    end = time.monotonic() + DNS_DEADLINE
    while len(out) < len(hosts) and time.monotonic() < end:
        time.sleep(0.2)
    return {h: out.get(h) for h in hosts}


def connect(ip: str, port: int) -> tuple[str, int]:
    start = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT):
            return "ok", int((time.monotonic() - start) * 1000)
    except TimeoutError:
        return "timeout", 0
    except ConnectionRefusedError:
        return "refused", 0
    except OSError as exc:
        return f"oserr:{exc.errno}", 0


def main(src: str, dest: str, workers: int = 500) -> None:
    urls = base64.b64decode(open(src, "rb").read()).decode().splitlines()
    endpoints = {ep for u in urls if needs_tcp_check(u) and (ep := server_endpoint(u))}
    resolved = resolve(sorted({h for h, _ in endpoints}))
    print(f"endpoints {len(endpoints)}, hosts {len(resolved)}, dns-fail "
          f"{sum(v is None for v in resolved.values())}", flush=True)
    result: dict[str, dict] = {}
    todo = []
    for host, port in sorted(endpoints):
        ip = resolved.get(host)
        if ip is None:
            result[f"{host}:{port}"] = {"r": "dns", "ip": None, "ms": 0}
        else:
            todo.append((host, port, ip))
    with ThreadPoolExecutor(workers) as pool:
        for (host, port, ip), (r, ms) in zip(todo, pool.map(lambda t: connect(t[2], t[1]), todo)):
            result[f"{host}:{port}"] = {"r": r, "ip": ip, "ms": ms}
    json.dump(result, open(dest, "w"), separators=(",", ":"))
    counts: dict[str, int] = {}
    for v in result.values():
        counts[v["r"]] = counts.get(v["r"], 0) + 1
    print(counts, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
