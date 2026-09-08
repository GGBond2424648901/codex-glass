import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from codex_monitor_core import (
    MonitorConfig,
    PricingRatesPerMillion,
    aggregate_usage_events,
    build_usage_summary,
    parse_usage_events_from_session_file,
)
from tests.helpers import sample_records, write_jsonl
import codex_monitor_index as index_module


class AggregateUsageEventsTests(unittest.TestCase):
    def test_workspace_windows_follow_the_same_time_boundaries_as_models(self):
        events=[index_module.UsageEvent(self.now-timedelta(hours=h),'alpha','C:/work/a',index_module.UsageDelta(10,2,3,1,13),0,'unpriced') for h in (1,6,26)]
        summary=aggregate_usage_events(events,[],MonitorConfig.load(self.config_path),now=self.now)
        self.assertEqual(26,sum(r['total_tokens'] for r in summary['windows']['today']['by_cwd'].values()))
        self.assertEqual(13,sum(r['total_tokens'] for r in summary['windows']['last_5_hours']['by_cwd'].values()))
        self.assertEqual(39,sum(r['total_tokens'] for r in summary['by_cwd'].values()))
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.sessions = Path(self.temp_dir.name) / "sessions"
        self.config_path = Path(self.temp_dir.name) / "monitor_config.json"
        self.now = datetime(2026, 9, 8, 10, 0, 0)

    def test_aggregate_usage_events_matches_legacy_summary(self) -> None:
        write_jsonl(self.sessions / "one.jsonl", sample_records())
        cfg = MonitorConfig.load(self.config_path)
        events, snapshot = parse_usage_events_from_session_file(self.sessions / "one.jsonl", cfg)
        expected = build_usage_summary(self.sessions, cfg, now=self.now, include_events=True)
        actual = aggregate_usage_events(
            events,
            [snapshot] if snapshot else [],
            cfg,
            now=self.now,
            include_events=True,
            source_metadata={"files": 1},
        )
        self.assertEqual(expected["total"], actual["total"])
        self.assertEqual(expected["by_model"], actual["by_model"])
        self.assertEqual(expected["events"], actual["events"])

    def test_aggregate_usage_events_reprices_parsed_events_with_aggregation_config(self) -> None:
        write_jsonl(self.sessions / "one.jsonl", sample_records())
        parse_config = MonitorConfig(
            host="127.0.0.1",
            port=8081,
            pricing_per_million={"gpt-5": PricingRatesPerMillion(input=0.0, cached_input=0.0, output=0.0)},
            model_aliases={},
        )
        aggregation_config = MonitorConfig(
            host="127.0.0.1",
            port=8081,
            pricing_per_million={"gpt-5": PricingRatesPerMillion(input=10.0, cached_input=1.0, output=100.0)},
            model_aliases={},
        )
        events, snapshot = parse_usage_events_from_session_file(self.sessions / "one.jsonl", parse_config)

        summary = aggregate_usage_events(
            events,
            [snapshot] if snapshot else [],
            aggregation_config,
            now=self.now,
            include_events=True,
            source_metadata={"files": 1},
        )

        self.assertAlmostEqual(0.00393, summary["total"]["estimated_cost_usd"])
        self.assertAlmostEqual(0.00393, summary["by_model"]["gpt-5"]["estimated_cost_usd"])
        self.assertEqual(0.00211, summary["events"][0]["cost_usd"]["total"])
        self.assertEqual("direct", summary["events"][0]["pricing_source"])

    def test_charts_use_fixed_five_minute_rates_zero_fill_and_exclude_future(self) -> None:
        events = [
            index_module.UsageEvent(datetime(2026, 9, 8, 9, 34), "alpha", None,
                                    index_module.UsageDelta(0, 0, 0, 0, 50), 0, "unpriced"),
            index_module.UsageEvent(datetime(2026, 9, 8, 9, 36), "beta", None,
                                    index_module.UsageDelta(0, 0, 0, 0, 100), 0, "unpriced"),
            index_module.UsageEvent(datetime(2026, 9, 8, 10, 1), "future", None,
                                    index_module.UsageDelta(0, 0, 0, 0, 999), 0, "unpriced"),
        ]
        summary = aggregate_usage_events(events, [], self.config_path and MonitorConfig.load(self.config_path), now=self.now)
        today = summary["charts"]["today"]
        self.assertEqual(7, len(today["labels"]))
        self.assertEqual("2026-09-08T09:30:00", today["labels"][0])
        self.assertEqual("2026-09-08T10:00:00", today["labels"][-1])
        self.assertEqual([10.0, 20.0, 0.0, 0.0, 0.0, 0.0, 0.0], today["values"])
        self.assertEqual([10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], today["by_model"]["alpha"])
        self.assertNotIn("future", today["by_model"])
        self.assertEqual(61, len(summary["charts"]["last_5_hours"]["labels"]))

    def test_chart_buckets_do_not_include_events_before_non_aligned_cutoffs(self) -> None:
        now = datetime(2026, 9, 8, 10, 2, 30)
        events = [
            index_module.UsageEvent(datetime(2026, 9, 8, 9, 32, 29), "outside-30m", None,
                                    index_module.UsageDelta(0, 0, 0, 0, 500), 0, "unpriced"),
            index_module.UsageEvent(datetime(2026, 9, 8, 5, 2, 29), "outside-5h", None,
                                    index_module.UsageDelta(0, 0, 0, 0, 500), 0, "unpriced"),
            index_module.UsageEvent(datetime(2026, 9, 8, 9, 32, 30), "boundary", None,
                                    index_module.UsageDelta(0, 0, 0, 0, 50), 0, "unpriced"),
        ]
        charts = aggregate_usage_events(events, [], MonitorConfig.load(self.config_path), now=now)["charts"]
        self.assertEqual(10.0, sum(charts["today"]["values"]))
        self.assertNotIn("outside-30m", charts["today"]["by_model"])
        self.assertEqual(110.0, sum(charts["last_5_hours"]["values"]))
        self.assertNotIn("outside-5h", charts["last_5_hours"]["by_model"])

    def test_all_chart_is_daily_raw_tokens_capped_to_ninety_days(self) -> None:
        events = [
            index_module.UsageEvent(self.now - timedelta(days=days), "alpha", None,
                                    index_module.UsageDelta(0, 0, 0, 0, days + 1), 0, "unpriced")
            for days in range(100)
        ]
        chart = aggregate_usage_events(events, [], MonitorConfig.load(self.config_path), now=self.now)["charts"]["all"]
        self.assertEqual(90, len(chart["labels"]))
        self.assertEqual("2026-06-11", chart["labels"][0])
        self.assertEqual("2026-09-08", chart["labels"][-1])
        self.assertEqual(90, chart["values"][0])
        self.assertEqual(1, chart["values"][-1])
        self.assertEqual(chart["values"], chart["by_model"]["alpha"])


class IndexedSummaryEquivalenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sessions = self.root / "sessions"
        self.config = MonitorConfig.load(self.root / "config.json")
        self.now = datetime(2026, 9, 8, 10)
        self.index = index_module.SessionIndex(self.root / "index.sqlite3")
        self.index.initialize()
        self.addCleanup(self.index.close)

    def summary(self, **kwargs):
        self.assertTrue(hasattr(self.index, "build_summary"), "indexed summary is missing")
        return self.index.build_summary(kwargs.pop("config", self.config), now=kwargs.pop("now", self.now), **kwargs)

    def test_indexed_summary_matches_legacy_across_context_and_rate_limit_changes(self):
        records = sample_records()
        records[2]["timestamp"] = "2026-09-06T01:00:00"
        records[3]["timestamp"] = "2026-09-08T09:01:00"
        records.insert(3, {"type": "turn_context", "payload": {"model": "gpt-5-mini", "cwd": "C:/work/b"}})
        records.extend([
            {"timestamp": "2026-09-08T09:02:00", "type": "token_count", "payload": {"info": None, "rate_limits": {"limit_id": "codex", "primary": {"used_percent": 30, "window_minutes": 300, "resets_in_seconds": 3600}}}},
            {"timestamp": "2026-09-08T09:03:00", "type": "token_count", "payload": {"info": None, "rate_limits": {"limit_id": "model-only", "limit_name": "Special model", "primary": {"used_percent": 90, "window_minutes": 300, "resets_in_seconds": 7200}}}},
        ])
        write_jsonl(self.sessions / "one.jsonl", records)
        other = sample_records()
        other[0]["payload"]["cwd"] = "C:/work/a/subdirectory"
        other[2]["timestamp"] = "2026-09-08T09:01:00"
        other[3]["timestamp"] = "2026-09-08T09:30:00"
        other.append({"timestamp": "2026-09-08T09:40:00", "type": "token_count", "payload": {"rate_limits": {"limit_id": "codex", "primary": {"used_percent": 40, "window_minutes": 300, "resets_in_seconds": 3600}}}})
        write_jsonl(self.sessions / "two.jsonl", other)
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        for cwd_filter in (None, "C:/work/a"):
            with self.subTest(cwd_filter=cwd_filter):
                expected = build_usage_summary(self.sessions, self.config, cwd_filter=cwd_filter, now=self.now, include_events=True)
                actual = self.summary(cwd_filter=cwd_filter, include_events=True)
                self.assertEqual(expected, {key: value for key, value in actual.items() if key != "index"})
                self.assertEqual("ready", actual["index"]["status"])
                self.assertEqual(100.0, actual["index"]["progress_percent"])
        self.assertEqual(390, self.summary()["total"]["total_tokens"])

    def test_summary_reprices_existing_rows_after_config_and_alias_changes(self):
        write_jsonl(self.sessions / "one.jsonl", sample_records())
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.summary()
        config = replace(self.config, pricing_per_million={"new-price": PricingRatesPerMillion(10, 1, 100)}, model_aliases={"gpt-5": "new-price"})
        actual = self.summary(config=config, include_events=True)
        self.assertAlmostEqual(0.00393, actual["total"]["estimated_cost_usd"])
        self.assertEqual("alias:new-price", actual["events"][0]["pricing_source"])

    def test_equal_timestamps_and_rate_limit_ties_follow_platform_path_order(self):
        for filename, model in (("a.jsonl", "gpt-5"), ("Z.jsonl", "gpt-5-mini")):
            records = sample_records()
            records[1]["payload"]["model"] = model
            records[2]["timestamp"] = records[3]["timestamp"] = "2026-09-08T09:00:00"
            records.append({"timestamp": "2026-09-08T09:01:00", "type": "token_count", "payload": {
                "rate_limits": {"limit_id": "codex", "limit_name": model,
                                "primary": {"used_percent": 30, "window_minutes": 300, "resets_in_seconds": 3600}}}})
            write_jsonl(self.sessions / filename, records)
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        expected = build_usage_summary(self.sessions, self.config, now=self.now, include_events=True)
        actual = self.summary(include_events=True)
        self.assertEqual(expected, {key: value for key, value in actual.items() if key != "index"})

    def test_cache_survives_restart_but_invalidates_for_new_facts_and_clock(self):
        write_jsonl(self.sessions / "one.jsonl", sample_records())
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        original = self.summary()
        self.index.close()
        self.index.initialize()
        with mock.patch("codex_monitor_index.aggregate_usage_events", side_effect=AssertionError("cache miss")):
            cached = self.summary()
        self.assertEqual(original, cached)
        later = self.summary(now=self.now + timedelta(days=7))
        self.assertEqual(0, later["five_hour"]["total_tokens"])
        self.assertNotEqual(original["generated_at"], later["generated_at"])
        write_jsonl(self.sessions / "two.jsonl", sample_records())
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.assertEqual(390, self.summary()["total"]["total_tokens"])
        self.index.rebuild()
        self.assertEqual(0, self.summary()["total"]["total_tokens"])

    def test_summary_cache_key_is_stable_and_covers_presentation_inputs(self):
        self.assertTrue(hasattr(index_module, "summary_cache_key"), "summary cache key is missing")
        key = index_module.summary_cache_key(self.config, None, False)
        reordered = replace(self.config, host="other-host", port=9000, pricing_per_million=dict(reversed(list(self.config.pricing_per_million.items()))), model_aliases=dict(reversed(list(self.config.model_aliases.items()))))
        self.assertEqual(key, index_module.summary_cache_key(reordered, None, False))
        self.assertEqual(64, len(key))
        self.assertNotEqual(key, index_module.summary_cache_key(self.config, "C:/work/a", False))
        self.assertNotEqual(key, index_module.summary_cache_key(self.config, None, True))

    def test_older_read_cannot_replace_cache_published_for_a_newer_generation(self):
        write_jsonl(self.sessions / "one.jsonl", sample_records())
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        other = index_module.SessionIndex(self.index.path)
        other.initialize()
        self.addCleanup(other.close)
        advanced = False

        def advance_before_aggregation(*args, **kwargs):
            nonlocal advanced
            if not advanced:
                advanced = True
                write_jsonl(self.sessions / "two.jsonl", sample_records())
                index_module.SessionIndexer(other, self.config).scan_once(self.sessions)
                other.build_summary(self.config, now=self.now)
            return aggregate_usage_events(*args, **kwargs)

        with mock.patch("codex_monitor_index.aggregate_usage_events", side_effect=advance_before_aggregation):
            older_read = self.summary()
        self.assertEqual(195, older_read["total"]["total_tokens"])
        self.assertEqual(390, self.index.cached_summary(self.config)["total"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
