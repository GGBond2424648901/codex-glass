import unittest
from datetime import datetime
from codex_glass.core.usage import MonitorConfig, aggregate_usage_events
from tests.test_core_aggregation import index_module


class CompactHistoryTests(unittest.TestCase):
    def test_long_context_history_cost_matches_aggregate_cost(self):
        event = index_module.UsageEvent(
            datetime(2026, 9, 8),
            "gpt-6-astra",
            None,
            index_module.UsageDelta(300000, 100000, 10000, 0, 310000),
            0,
            "default",
        )
        data = aggregate_usage_events([event], [], MonitorConfig.load(), include_events=True)
        self.assertAlmostEqual(data["total"]["estimated_cost_usd"], data["events"][0]["cost_usd"]["total"], places=6)

    def test_transient_event_object_avoids_per_event_attribute_dictionary(self):
        event = index_module.UsageEvent(datetime(2026, 9, 8), "alpha", None, index_module.UsageDelta(), 0, "unpriced")
        self.assertFalse(hasattr(event, "__dict__"))

    def test_coordinator_builds_and_publishes_compact_history_without_json_event_cache(self):
        import tempfile, json
        from pathlib import Path
        from tests.helpers import sample_records, write_jsonl
        from codex_glass.storage.compact import CompactHistory

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            sessions = root / "sessions"
            write_jsonl(sessions / "one.jsonl", sample_records())
            index = index_module.SessionIndex(root / "index.sqlite3")
            index.initialize()
            try:
                config = MonitorConfig.load(root / "config.json")
                index_module.SessionIndexer(index, config).scan_once(sessions)
                expected = index.build_summary(config, include_events=True)
                actual = index.build_summary(config, compact_events=True)
                self.assertIsInstance(actual["_history"], CompactHistory)
                self.assertEqual(expected["events"], list(actual["_history"]))
                cached = index.cached_summary(config, include_events=False)
                self.assertNotIn("events", cached)
                self.assertNotIn("_history", cached)
                c = index_module.IndexCoordinator(index, sessions, lambda: config, None)
                c._publish(index.read_state(), actual)
                self.assertEqual(expected["events"], c.events_snapshot())
                self.assertEqual(2, c.events_page()["total"])
                c._publish(index.read_state(), cached)
                self.assertEqual(
                    2, c.events_page()["total"], "same-generation summary cache cannot erase published history"
                )
            finally:
                index.close()

    def test_compact_history_roundtrips_and_filters_without_retained_dicts(self):
        from codex_glass.storage.compact import CompactHistory

        history = CompactHistory()
        rows = [
            dict(
                timestamp=f"2026-09-08 {h}:00:00",
                model=m,
                cwd="工作区 1",
                pricing_source="direct",
                tokens={
                    "input": n,
                    "cached_input": 1,
                    "uncached_input": n - 1,
                    "output": 3,
                    "reasoning_output": 2,
                    "total": n + 3,
                },
                rates_per_million={"input": 1.0, "cached_input": 0.1, "output": 10.0},
                cost_usd={"uncached_input": 0.1, "cached_input": 0.2, "output": 0.3, "total": cost},
            )
            for h, m, n, cost in [("22", "a", 10, 0.5), ("21", "b", 30, 0.4), ("20", "a", 20, 0.6)]
        ]
        for row in rows:
            history.append(row)
        self.assertEqual(rows, list(history))
        self.assertEqual(
            [rows[2]], history.page(since="2026-09-08T20:00:00", models=["a"], sort="cost", offset=0, limit=1)["events"]
        )
        self.assertEqual(0, history.page(models=[])["total"])
        self.assertEqual(1, history.page(since="2026-09-08T21:30:00")["total"])
        self.assertEqual([rows[1]], history.page(q="B", cwd="工作区")["events"])
        self.assertLess(history.storage_bytes, 1000)

    def test_aggregate_sink_produces_identical_facts_without_events_list(self):
        from codex_glass.storage.compact import CompactHistory

        events = [
            index_module.UsageEvent(
                datetime(2026, 9, 8, 9), "gpt-5", None, index_module.UsageDelta(10, 2, 3, 1, 13), 0, "unpriced"
            )
        ]
        config = MonitorConfig.load()
        h = CompactHistory()
        expected = aggregate_usage_events(events, [], config, include_events=True)
        actual = aggregate_usage_events(events, [], config, event_sink=h.append)
        self.assertNotIn("events", actual)
        self.assertEqual(expected["events"], list(h))
        self.assertEqual(expected["total"], actual["total"])
