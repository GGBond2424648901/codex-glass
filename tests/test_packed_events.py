import unittest
import tracemalloc
from datetime import datetime, timezone, timedelta
from dataclasses import asdict

from codex_glass.core.usage import UsageDelta, UsageEvent


class PackedEventsTests(unittest.TestCase):
    def test_recent_ties_and_full_history_match_legacy_order(self):
        from codex_glass.core.usage import aggregate_usage_events, MonitorConfig
        from codex_glass.storage.compact import CompactHistory

        rows = [
            UsageEvent(datetime(2026, 9, 9, 10), f"model-{i}", None, UsageDelta(10, 2, 3, 1, 13), 0, "unpriced")
            for i in range(70)
        ]
        history = CompactHistory()
        result = aggregate_usage_events(
            iter(rows), [], MonitorConfig.load(), now=datetime(2026, 9, 9, 11), event_sink=history.append
        )
        self.assertEqual([f"model-{i}" for i in range(50)], [row["model"] for row in result["recent_calls"]])
        self.assertEqual([f"model-{i}" for i in reversed(range(70))], [row["model"] for row in history])
        self.assertEqual(1, result["metrics"]["active_cwds_5h"])

    def test_desktop_aggregation_does_not_retain_all_event_objects(self):
        from codex_glass.core.usage import aggregate_usage_events, MonitorConfig

        def events():
            for i in range(20000):
                yield UsageEvent(
                    datetime(2026, 9, 8, 12, 0, 0, i), "gpt-5", None, UsageDelta(i, 0, 2, 0, i + 2), 0, "default"
                )

        tracemalloc.start()
        try:
            data = aggregate_usage_events(
                events(), [], MonitorConfig.load(), now=datetime(2026, 9, 9), event_sink=lambda row: None
            )
            _, peak = tracemalloc.get_traced_memory()
            self.assertLess(peak, 4 * 1024 * 1024)
            self.assertEqual(20000, data["total"]["calls"])
            self.assertEqual(200030000, data["total"]["total_tokens"])
        finally:
            tracemalloc.stop()

    def test_large_event_buffer_has_bounded_per_row_storage(self):
        from codex_glass.storage.packed_events import PackedEvents

        tracemalloc.start()
        try:
            buffer = PackedEvents()
            for i in range(20000):
                buffer.append(
                    UsageEvent(
                        datetime(2026, 9, 9, 1, 2, 3, i),
                        "gpt-5",
                        "workspace",
                        UsageDelta(i, 1, 2, 3, i + 2),
                        0.5,
                        "direct",
                    )
                )
            current, _ = tracemalloc.get_traced_memory()
            self.assertLess(current, 20000 * 100)
            self.assertEqual(20000, len(buffer))
            self.assertEqual(19999, buffer[-1].delta.input_tokens)
        finally:
            tracemalloc.stop()

    def test_roundtrip_preserves_microseconds_timezone_negative_and_large_tokens(self):
        from codex_glass.storage.packed_events import PackedEvents

        rows = [
            UsageEvent(
                datetime(2026, 9, 9, 1, 2, 3, 456789, tzinfo=tz),
                "model",
                None,
                UsageDelta(-1, 2**55, 3, 4, 5),
                1.23456789,
                "custom",
            )
            for tz in (None, timezone(timedelta(hours=8)))
        ]
        buffer = PackedEvents()
        for row in rows:
            buffer.append(row)
        self.assertEqual([asdict(row) for row in rows], [asdict(row) for row in buffer])
        with self.assertRaises(IndexError):
            buffer[len(buffer)]

    def test_stable_sort_and_reorder_preserve_ties(self):
        from codex_glass.storage.packed_events import PackedEvents

        buffer = PackedEvents()
        for day, name in [(3, "c"), (1, "a"), (1, "b")]:
            buffer.append(UsageEvent(datetime(2026, 9, day), name, None, UsageDelta(), 0, "unpriced"))
        buffer.sort(key=lambda event: event.timestamp)
        self.assertEqual(["a", "b", "c"], [event.model for event in buffer])
        self.assertEqual(["c", "b", "a"], [event.model for event in reversed(buffer)])
