import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

import codex_monitor_index as index_module
from codex_monitor_core import (
    MonitorConfig,
    PricingRatesPerMillion,
    UsageDelta,
    UsageEvent,
    aggregate_usage_events,
    estimate_cost_usd,
)
from tests.helpers import sample_records, write_jsonl


class CurrentPricingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_current_official_models_have_literal_standard_text_rates(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        expected = {
            "gpt-6-astra": (10.0, 1.0, 50.0),
            "gpt-5.6-sol": (4.0, 0.4, 20.0),
            "gpt-5.6-terra": (2.0, 0.2, 12.0),
            "gpt-5.6-luna": (0.2, 0.02, 1.2),
            "gpt-5.5": (5.0, 0.5, 30.0),
            "gpt-5.4-mini": (0.75, 0.075, 4.5),
        }

        for model, literal_rates in expected.items():
            with self.subTest(model=model):
                rates, source = config.rates_for_model(model)
                self.assertEqual(literal_rates, (rates.input, rates.cached_input, rates.output))
                self.assertEqual("direct", source)

    def test_resolution_is_exact_unless_an_alias_is_explicitly_configured(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        aliased = replace(config, model_aliases={"my-sol": "gpt-5.6-sol"})
        rates, alias_source = aliased.rates_for_model("my-sol")

        for model in ("gpt-5.6-sol-preview", "codex-auto-review"):
            with self.subTest(model=model):
                _, unknown_source = config.rates_for_model(model)
                self.assertEqual("unpriced", unknown_source)
        self.assertEqual((4.0, 0.4, 20.0), (rates.input, rates.cached_input, rates.output))
        self.assertEqual("alias:gpt-5.6-sol", alias_source)

    def test_dated_current_model_ids_resolve_only_to_their_exact_family(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        expected = {
            "gpt-6-astra-2026-09-08": (10.0, 1.0, 50.0, "heuristic:gpt-6-astra"),
            "gpt-5.6-sol-2026-09-08": (4.0, 0.4, 20.0, "heuristic:gpt-5.6-sol"),
            "gpt-5.6-terra-2026-09-08": (2.0, 0.2, 12.0, "heuristic:gpt-5.6-terra"),
            "gpt-5.6-luna-2026-09-08": (0.2, 0.02, 1.2, "heuristic:gpt-5.6-luna"),
            "gpt-5.5-2026-09-08": (5.0, 0.5, 30.0, "heuristic:gpt-5.5"),
        }

        for model, literal_resolution in expected.items():
            with self.subTest(model=model):
                rates, source = config.rates_for_model(model)
                self.assertEqual(
                    literal_resolution,
                    (rates.input, rates.cached_input, rates.output, source),
                )

        _, unsupported_source = config.rates_for_model("gpt-5.6-mini-2026-09-08")
        self.assertEqual("unpriced", unsupported_source)

    def test_cached_input_is_removed_from_full_price_input(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        cost, source = estimate_cost_usd(
            "gpt-5.6-sol",
            UsageDelta(input_tokens=100_000, cached_input_tokens=25_000, output_tokens=10_000),
            config,
        )

        self.assertAlmostEqual(0.51, cost)
        self.assertEqual("direct", source)

    def test_cached_input_cannot_exceed_total_input(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        cost, source = estimate_cost_usd(
            "gpt-5.6-sol",
            UsageDelta(input_tokens=100, cached_input_tokens=150, output_tokens=10),
            config,
        )

        self.assertAlmostEqual(0.00024, cost)
        self.assertEqual("direct", source)

    def test_long_context_boundary_uses_resolved_canonical_pricing_key(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        aliased = replace(config, model_aliases={"my-terra": "gpt-5.6-terra"})
        at_boundary, at_source = estimate_cost_usd(
            "gpt-5.6-terra",
            UsageDelta(input_tokens=272_000, cached_input_tokens=72_000, output_tokens=1_000),
            config,
        )
        over_boundary, over_source = estimate_cost_usd(
            "my-terra",
            UsageDelta(input_tokens=272_001, cached_input_tokens=72_000, output_tokens=1_000),
            aliased,
        )

        self.assertAlmostEqual(0.4264, at_boundary)
        self.assertEqual("direct", at_source)
        self.assertAlmostEqual(0.846804, over_boundary)
        self.assertEqual("alias:gpt-5.6-terra+long-context", over_source)

    def test_all_new_long_context_models_cross_the_boundary(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        expected_costs = {
            "gpt-6-astra": 5.44002,
            "gpt-5.6-sol": 2.176008,
            "gpt-5.6-terra": 1.088004,
            "gpt-5.6-luna": 0.1088004,
            "gpt-5.5": 2.72001,
        }

        for model, literal_cost in expected_costs.items():
            with self.subTest(model=model):
                cost, source = estimate_cost_usd(
                    model,
                    UsageDelta(input_tokens=272_001),
                    config,
                )
                self.assertAlmostEqual(literal_cost, cost)
                self.assertEqual("direct+long-context", source)

    def test_unknown_is_unpriced_but_explicit_zero_rate_is_priced(self) -> None:
        config = MonitorConfig(
            host="127.0.0.1",
            port=8081,
            pricing_per_million={"free-model": PricingRatesPerMillion(0.0, 0.0, 0.0)},
            model_aliases={},
        )

        unknown_cost, unknown_source = estimate_cost_usd("unknown", UsageDelta(input_tokens=1_000), config)
        free_cost, free_source = estimate_cost_usd("free-model", UsageDelta(input_tokens=1_000), config)

        self.assertEqual(0.0, unknown_cost)
        self.assertEqual("unpriced", unknown_source)
        self.assertEqual(0.0, free_cost)
        self.assertEqual("direct", free_source)

    def test_local_rate_override_wins_over_builtin_rate(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text(
            '{"pricing_per_million":{"gpt-5.6-sol":{"input":7,"cached_input":0.7,"output":35}}}',
            encoding="utf-8",
        )

        rates, source = MonitorConfig.load(config_path).rates_for_model("gpt-5.6-sol")

        self.assertEqual((7.0, 0.7, 35.0), (rates.input, rates.cached_input, rates.output))
        self.assertEqual("direct", source)


class PricingPolicyIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sessions = self.root / "sessions"
        self.index = index_module.SessionIndex(self.root / "index.sqlite3")
        self.index.initialize()
        self.addCleanup(self.index.close)

    def test_summary_cache_key_changes_with_pricing_policy_version(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        original = index_module.summary_cache_key(config, None, False)

        with mock.patch.object(index_module, "PRICING_POLICY_VERSION", "next-policy", create=True):
            changed = index_module.summary_cache_key(config, None, False)

        self.assertNotEqual(original, changed)

    def test_summary_cache_key_changes_with_pricing_policy_check_date(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        original = index_module.summary_cache_key(config, None, False)

        with mock.patch.object(index_module, "PRICING_POLICY_CHECKED_ON", "2026-09-09", create=True):
            changed = index_module.summary_cache_key(config, None, False)

        self.assertNotEqual(original, changed)

    def test_summary_cache_key_changes_with_public_summary_format(self) -> None:
        config = MonitorConfig.load(self.root / "missing.json")
        original = index_module.summary_cache_key(config, None, False)

        with mock.patch.object(index_module, "SUMMARY_FORMAT_VERSION", "next-format", create=True):
            changed = index_module.summary_cache_key(config, None, False)

        self.assertNotEqual(original, changed)

    def test_indexed_history_reprices_with_current_policy_without_raw_log_read(self) -> None:
        records = sample_records()
        records[1]["payload"]["model"] = "gpt-5.6-sol"
        source_file = self.sessions / "one.jsonl"
        write_jsonl(source_file, records)
        old_config = MonitorConfig(
            host="127.0.0.1",
            port=8081,
            pricing_per_million={"gpt-5.6-sol": PricingRatesPerMillion(1.0, 0.1, 5.0)},
            model_aliases={},
        )
        index_module.SessionIndexer(self.index, old_config).scan_once(self.sessions)
        source_file.unlink()

        current_config = MonitorConfig.load(self.root / "missing.json")
        summary = self.index.build_summary(
            current_config,
            now=datetime(2026, 9, 8, 10, 0, 0),
            include_events=True,
        )

        self.assertAlmostEqual(0.001072, summary["total"]["estimated_cost_usd"])
        self.assertEqual("direct", summary["events"][0]["pricing_source"])


class PricingCoverageSummaryTests(unittest.TestCase):
    def test_summary_reports_known_price_coverage_from_resolution_source(self) -> None:
        config = MonitorConfig(
            host="127.0.0.1",
            port=8081,
            pricing_per_million={
                "gpt-5.6-sol": PricingRatesPerMillion(4.0, 0.4, 20.0),
                "free-model": PricingRatesPerMillion(0.0, 0.0, 0.0),
            },
            model_aliases={},
        )
        timestamp = datetime(2026, 9, 8, 9, 0, 0)
        events = [
            UsageEvent(timestamp, "gpt-5.6-sol", None, UsageDelta(100, 20, 10, 0, 110), 999.0, "stale"),
            UsageEvent(timestamp, "free-model", None, UsageDelta(30, 0, 0, 0, 30), 999.0, "stale"),
            UsageEvent(timestamp, "z-unpriced", None, UsageDelta(50, 0, 0, 0, 50), 999.0, "stale"),
            UsageEvent(timestamp, "a-unpriced", None, UsageDelta(10, 0, 10, 0, 20), 999.0, "stale"),
        ]

        summary = aggregate_usage_events(events, [], config, now=datetime(2026, 9, 8, 10, 0, 0))

        self.assertEqual(
            {
                "policy_version": "2026-09-08-standard-text",
                "policy_checked_on": "2026-09-08",
                "method": "current-price-estimate",
                "estimate_scope": "known-price-usage-only",
                "priced_calls": 2,
                "priced_tokens": 140,
                "unpriced_calls": 2,
                "unpriced_tokens": 70,
                "total_calls": 4,
                "total_tokens": 210,
                "token_coverage_ratio": 0.6667,
                "token_coverage_percent": 66.67,
                "unpriced_models": [
                    {"model": "a-unpriced", "calls": 1, "total_tokens": 20},
                    {"model": "z-unpriced", "calls": 1, "total_tokens": 50},
                ],
            },
            summary["pricing"],
        )
        self.assertAlmostEqual(0.000528, summary["total"]["estimated_cost_usd"])


if __name__ == "__main__":
    unittest.main()
