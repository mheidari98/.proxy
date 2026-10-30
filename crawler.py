#!/usr/bin/env python3
"""Fetch every source listed in nodes.md and publish deduplicated proxy lists."""

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from proxyUtil.net import ScrapURLs
from proxyUtil.parsers import tagChanger, tagsChanger
from proxyUtil.schemes import ss_scheme, trojan_scheme, vless_scheme, vmess_scheme

LOG = logging.getLogger(__name__)

WORKERS = 10
STALE_AFTER = timedelta(days=7)
MIN_CONFIGS = 100
MIN_PREVIOUS_RATIO = 0.25
CANON_TAG = "_"  # placeholder tag, so the same server compares equal across sources

TAGS = ("4FreeIran", "4Nika", "4Sarina", "4Jadi", "4Kian", "4Mohsen")
OUTPUTS = {
    "all": "",  # every scheme starts with ""
    "ss": ss_scheme,
    "vmess": vmess_scheme,
    "vless": vless_scheme,
    "trojan": trojan_scheme,
}


def table_rows(markdown: str):
    """Yield (line, columns) per line; columns is [status, count, every, url] on a
    source row, else None."""
    for line in markdown.splitlines():
        columns = line.strip("|").split("|") if line.startswith("|") else []
        source = len(columns) >= 4 and columns[3].strip().startswith(("http://", "https://"))
        yield line, columns if source else None


def load_sources(markdown: str) -> dict[str, str]:
    """Map source url -> its advertised update interval, in nodes.md order."""
    sources = {}
    for _, columns in table_rows(markdown):
        if columns:
            sources.setdefault(columns[3].strip(), columns[2].strip())
    if not sources:
        raise RuntimeError("No sources found in nodes.md")
    return sources


def update_nodes(markdown: str, results) -> str:
    by_url = {result.url: result for result in results}
    output = []
    for line, columns in table_rows(markdown):
        if columns and (result := by_url.get(columns[3].strip())):
            columns[0] = f" {'✅' if result.has_proxies else '❌'} "
            columns[1] = f" {len(result.proxies)} "
            line = "|" + "|".join(columns) + "|"
        output.append(line)
    return "\n".join(output)


def canonicalize(results) -> dict[str, list[str]]:
    """Strip display tags so the same server compares equal across sources."""
    return {result.url: tagsChanger(result.proxies, CANON_TAG) for result in results}


def campaign_tag(proxy: str) -> str:
    """Stable per-proxy tag, so unchanged proxies produce no diff between runs."""
    digest = hashlib.sha256(proxy.encode()).hexdigest()
    return f"{TAGS[int(digest[:8], 16) % len(TAGS)]}-{digest[:10]}"


def classify(record: dict, now: datetime) -> str:
    def at(key):
        return datetime.fromisoformat(record[key]) if record[key] else None

    if record["proxy_count"]:
        changed = at("last_changed_at")
        return "stale" if changed and now - changed >= STALE_AFTER else "healthy"
    known_good = at("last_valid_at") or at("first_seen_at")
    if known_good and now - known_good >= STALE_AFTER:
        return "dead"
    return "empty" if record["error"] is None else "failing"


def build_state(results, canonical, previous: dict, sources: dict, now: datetime) -> dict:
    records = {}
    stamp = now.isoformat().replace("+00:00", "Z")
    for result in results:
        old = previous.get("sources", {}).get(result.url, {})
        servers = sorted(set(canonical[result.url]))
        digest = hashlib.sha256("\n".join(servers).encode()).hexdigest() if servers else None
        changed = result.has_proxies and digest != old.get("content_hash")
        records[result.url] = record = {
            "expected_update": sources[result.url],
            "first_seen_at": old.get("first_seen_at", stamp),
            "last_reachable_at": stamp if result.reachable else old.get("last_reachable_at"),
            "last_valid_at": stamp if result.has_proxies else old.get("last_valid_at"),
            "last_changed_at": stamp if changed else old.get("last_changed_at"),
            "content_hash": digest or old.get("content_hash"),
            "proxy_count": len(result.proxies),
            "http_status": result.http_status,
            "bytes_downloaded": result.bytes_downloaded,
            "elapsed_ms": result.elapsed_ms,
            "consecutive_failures": 0
            if result.has_proxies
            else old.get("consecutive_failures", 0) + 1,
            "error": result.error,
        }
        record["status"] = classify(record, now)

    return {
        "schema_version": 1,
        "generated_at": stamp,
        "summary": {
            "sources": len(records),
            "valid_sources": sum(r.has_proxies for r in results),
            "raw_proxies": sum(len(r.proxies) for r in results),
            "statuses": Counter(r["status"] for r in records.values()),
        },
        "sources": records,
    }


def atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.chmod(tmp.name, 0o644)  # NamedTemporaryFile creates 0600
    os.replace(tmp.name, path)


def run(root: Path | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = root or Path(__file__).resolve().parent
    nodes_path, state_path, published = root / "nodes.md", root / "source_status.json", root / "all"
    started = time.monotonic()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    markdown = nodes_path.read_text(encoding="utf-8")
    sources = load_sources(markdown)
    LOG.info("Fetching %d sources with %d workers", len(sources), WORKERS)
    results = ScrapURLs(sources, workers=WORKERS)

    canonical = canonicalize(results)
    invalid = sum(len(r.proxies) - len(canonical[r.url]) for r in results)
    configs = [
        tagChanger(proxy, campaign_tag(proxy))
        for proxy in sorted({p for proxies in canonical.values() for p in proxies})
    ]

    # Never overwrite a good list with the fallout of a bad hour upstream.
    previous_count = (
        len(base64.b64decode(published.read_bytes()).splitlines()) if published.exists() else 0
    )
    if len(configs) < max(MIN_CONFIGS, previous_count * MIN_PREVIOUS_RATIO):
        raise RuntimeError(f"refusing to publish {len(configs)} configs (previous {previous_count})")

    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    state = build_state(results, canonical, previous, sources, now)
    state["summary"] |= {"published_proxies": len(configs), "invalid_proxies": invalid}

    for name, scheme in OUTPUTS.items():
        lines = [config for config in configs if config.startswith(scheme)]
        atomic_write(root / name, base64.b64encode("\n".join(lines).encode()))
    atomic_write(nodes_path, update_nodes(markdown, results).encode())
    atomic_write(state_path, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())

    LOG.info(
        "Published %d configs from %d valid sources in %.2fs (%d invalid)",
        len(configs),
        state["summary"]["valid_sources"],
        time.monotonic() - started,
        invalid,
    )


if __name__ == "__main__":
    run()
