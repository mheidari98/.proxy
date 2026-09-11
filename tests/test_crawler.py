import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from proxyUtil.net import FetchResult

import crawler
from crawler import build_state, campaign_tag, canonicalize, load_sources, run, update_nodes

NODES = """# free-node

| available | proxy count | updated every | url |
|:---------:|:---------:|:-------------:|-----|
| ✅ | 1 | 4h |https://example.com/sub|
<!--| ✅ | 1 | 1h |https://example.com/disabled|-->
"""

URL = "https://example.com/sub"
NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
LONG_AGO = (NOW - timedelta(days=8)).isoformat().replace("+00:00", "Z")


def result(proxies=(), **kwargs):
    return FetchResult(url=URL, proxies=tuple(proxies), **kwargs)


class LoadSources(unittest.TestCase):
    def test_reads_only_enabled_table_rows(self):
        self.assertEqual(load_sources(NODES), {URL: "4h"})

    def test_rejects_a_table_with_no_sources(self):
        with self.assertRaises(RuntimeError):
            load_sources("# no table here\n")


class Tagging(unittest.TestCase):
    def test_tag_is_stable_for_the_same_proxy(self):
        proxy = "vless://id@example.com:443"
        self.assertEqual(campaign_tag(proxy), campaign_tag(proxy))

    def test_tag_uses_only_known_campaigns(self):
        tags = {campaign_tag(f"vless://id{i}@example.com:443").rsplit("-", 1)[0] for i in range(200)}
        self.assertTrue(tags <= set(crawler.TAGS))
        self.assertGreater(len(tags), 1, "hash should spread across campaigns")


class NodesTable(unittest.TestCase):
    def test_marks_reachable_but_empty_source_as_failed(self):
        markdown = "| ✅ | 99 | 4h |https://example.com/sub|"
        self.assertEqual(
            update_nodes(markdown, [result(http_status=200)]),
            "| ❌ | 0 | 4h |https://example.com/sub|",
        )

    def test_leaves_non_source_lines_untouched(self):
        self.assertEqual(update_nodes("# title\n\n---", []), "# title\n\n---")


class State(unittest.TestCase):
    def setUp(self):
        self.sources = {URL: "4h"}

    def state_for(self, res, previous):
        return build_state([res], canonicalize([res]), previous, self.sources, NOW)["sources"][URL]

    def test_unchanged_content_for_a_week_is_stale(self):
        valid = result(["vless://id@example.com:443"], http_status=200)
        digest = build_state([valid], canonicalize([valid]), {}, self.sources, NOW)
        previous = {
            "sources": {
                URL: {
                    "first_seen_at": LONG_AGO,
                    "last_valid_at": LONG_AGO,
                    "last_changed_at": LONG_AGO,
                    "content_hash": digest["sources"][URL]["content_hash"],
                }
            }
        }
        self.assertEqual(self.state_for(valid, previous)["status"], "stale")

    def test_no_valid_data_for_a_week_is_dead(self):
        previous = {"sources": {URL: {"first_seen_at": LONG_AGO}}}
        self.assertEqual(self.state_for(result(error="timeout"), previous)["status"], "dead")

    def test_http_error_is_failing_not_empty(self):
        record = self.state_for(result(http_status=404, error="HTTP 404"), {})
        self.assertEqual(record["status"], "failing")

    def test_reachable_with_no_configs_is_empty(self):
        self.assertEqual(self.state_for(result(http_status=200), {})["status"], "empty")

    def test_failures_accumulate_and_reset(self):
        previous = {"sources": {URL: {"consecutive_failures": 2, "first_seen_at": LONG_AGO}}}
        self.assertEqual(self.state_for(result(error="boom"), previous)["consecutive_failures"], 3)
        recovered = result(["vless://id@example.com:443"], http_status=200)
        self.assertEqual(self.state_for(recovered, previous)["consecutive_failures"], 0)

    def test_content_hash_ignores_display_tags(self):
        first = result(["vless://id@example.com:443#one"], http_status=200)
        second = result(["vless://id@example.com:443#two"], http_status=200)
        self.assertEqual(
            self.state_for(first, {})["content_hash"],
            self.state_for(second, {})["content_hash"],
        )


class Run(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "nodes.md").write_text(NODES)

    def test_publishes_every_output_and_records_health(self):
        proxies = [f"vless://id{i}@example.com:443#upstream" for i in range(150)]
        proxies.append("trojan://pw@example.com:443#upstream")
        with patch.object(crawler, "ScrapURLs", return_value=[result(proxies, http_status=200)]):
            run(self.root)

        published = base64.b64decode((self.root / "all").read_bytes()).decode().splitlines()
        self.assertEqual(len(published), 151)
        self.assertTrue(all("#upstream" not in line for line in published))
        self.assertEqual(
            len(base64.b64decode((self.root / "trojan").read_bytes()).decode().splitlines()), 1
        )

        state = json.loads((self.root / "source_status.json").read_text())
        self.assertEqual(state["sources"][URL]["status"], "healthy")
        self.assertEqual(state["summary"]["published_proxies"], 151)
        self.assertIn("✅", (self.root / "nodes.md").read_text())

    def test_refuses_to_publish_a_collapsed_list(self):
        (self.root / "all").write_bytes(base64.b64encode(b"\n".join([b"vless://x"] * 1000)))
        thin = [f"vless://id{i}@example.com:443" for i in range(120)]
        with patch.object(crawler, "ScrapURLs", return_value=[result(thin, http_status=200)]):
            with self.assertRaises(RuntimeError):
                run(self.root)
        # the previously published list must survive the refusal
        self.assertEqual(len(base64.b64decode((self.root / "all").read_bytes()).splitlines()), 1000)

    def test_counts_unparsable_proxies_as_invalid(self):
        proxies = [f"vless://id{i}@example.com:443" for i in range(150)]
        proxies.append("vmess://not-base64-!!")
        with patch.object(crawler, "ScrapURLs", return_value=[result(proxies, http_status=200)]):
            run(self.root)
        state = json.loads((self.root / "source_status.json").read_text())
        self.assertEqual(state["summary"]["invalid_proxies"], 1)
        self.assertEqual(state["summary"]["published_proxies"], 150)


if __name__ == "__main__":
    unittest.main()
