import unittest
import tempfile
import json
from types import SimpleNamespace
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from codex_glass.core.usage import MonitorConfig, UsageEvent, UsageDelta, aggregate_usage_events


class WidgetDataTests(unittest.TestCase):
    def test_pro_only_shows_weekly_but_plus_can_show_both(self):
        from codex_glass.core.presentation import quota_windows

        week = {"scope": "global", "window_minutes": 10080, "used_percent": 17}
        hour = {"scope": "global", "window_minutes": 300, "used_percent": 22}
        self.assertEqual(["周额度"], [x[0] for x in quota_windows({"rate_limits": {"limits": [week]}})])
        self.assertEqual(
            ["周额度", "5 小时额度"], [x[0] for x in quota_windows({"rate_limits": {"limits": [week, hour]}})]
        )
        self.assertEqual([], quota_windows({"rate_limits": None}))
        expired = {**hour, "resets_at": "2020-01-01T00:00:00"}
        self.assertEqual(["周额度"], [x[0] for x in quota_windows({"rate_limits": {"limits": [week, expired]}})])

    def test_today_excludes_yesterday_and_keeps_every_model(self):
        events = [
            UsageEvent(datetime(2026, 9, 8, 10), f"model-{i}", None, UsageDelta(100, 20, 10, 2, 110), 0, "unpriced")
            for i in range(20)
        ]
        events.append(
            UsageEvent(datetime(2026, 9, 7, 23), "yesterday", None, UsageDelta(100, 20, 10, 2, 110), 0, "unpriced")
        )
        result = aggregate_usage_events(
            events, [], MonitorConfig.load(Path("absent.json")), now=datetime(2026, 9, 8, 11)
        )
        self.assertEqual(2200, result["windows"]["today"]["total"]["total_tokens"])
        self.assertEqual(20, len(result["windows"]["today"]["by_model"]))
        self.assertEqual(20, len(result["windows"]["last_5_hours"]["by_model"]))

    def test_delta_baseline_repeat_and_counter_reset(self):
        from codex_glass.core.presentation import DeltaTracker

        tracker = DeltaTracker()

        def sample(n, stamp):
            return {"generated_at": stamp, "by_model": {"gpt-5": {"total_tokens": n}}}

        self.assertEqual({}, tracker.update(sample(100, "one")))
        self.assertEqual({"gpt-5": 40}, tracker.update(sample(140, "two")))
        self.assertEqual({"gpt-5": 40}, tracker.update(sample(140, "two")))
        self.assertEqual({}, tracker.update(sample(20, "three")))
        self.assertEqual({"gpt-5": 10}, tracker.update(sample(30, "four")))
        new = sample(30, "five")
        new["by_model"]["new-model"] = {"total_tokens": 80}
        self.assertEqual({"new-model": 80}, tracker.update(new))

    def test_import_change_resets_delta_baseline(self):
        from codex_glass.core.presentation import DeltaTracker

        tracker = DeltaTracker()

        def sample(n, stamp, imports):
            return {
                "generated_at": stamp,
                "by_model": {"x": {"total_tokens": n}},
                "source": {"imported_usage_events": imports},
            }

        tracker.update(sample(100, "one", 0))
        self.assertEqual({}, tracker.update(sample(10000, "two", 500)))
        self.assertEqual({"x": 20}, tracker.update(sample(10020, "three", 500)))

    def test_recent_window_excludes_future_events(self):
        events = [UsageEvent(datetime(2026, 9, 9, 10), "future", None, UsageDelta(100, 20, 10, 2, 110), 0, "unpriced")]
        data = aggregate_usage_events(events, [], MonitorConfig.load(Path("absent.json")), now=datetime(2026, 9, 8, 11))
        self.assertEqual(0, data["windows"]["last_5_hours"]["total"]["total_tokens"])

    def test_desktop_snapshot_passes_cached_chart_series(self):
        from codex_glass.core.presentation import desktop_snapshot

        charts = {
            "today": {"labels": ["x"], "values": [1.0], "by_model": {}, "unit": "Token / min", "bucket_minutes": 5}
        }
        coordinator = SimpleNamespace(snapshot=lambda: {"total": {}, "charts": charts})
        self.assertEqual(charts, desktop_snapshot(coordinator)["charts"])

    def test_latest_local_record_preserves_both_windows_without_old_account_mix(self):
        from codex_glass.core.presentation import desktop_snapshot, quota_windows
        from codex_glass.storage.index import SessionIndex, SessionIndexer
        from tests.helpers import write_jsonl

        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            cleanup.callback(index.close)
            config = MonitorConfig.load(root / "absent.json")

            def record(stamp, plan, primary, secondary=None):
                return {
                    "timestamp": stamp,
                    "type": "token_count",
                    "payload": {
                        "rate_limits": {
                            "limit_id": "codex",
                            "plan_type": plan,
                            "primary": primary,
                            "secondary": secondary,
                        }
                    },
                }

            hour = {"window_minutes": 300, "used_percent": 22, "resets_at": 2100000000}
            week = {"window_minutes": 10080, "used_percent": 17, "resets_at": 2100000000}
            records = [record("2026-09-08T01:00:00Z", "plus", hour, week)]
            path = root / "sessions" / "one.jsonl"
            write_jsonl(path, records)
            SessionIndexer(index, config).scan_once(path.parent)
            coordinator = SimpleNamespace(
                index=index,
                snapshot=lambda: {
                    "total": {},
                    "rate_limits": {"limits": [dict(week, scope="global", used_percent=99)]},
                },
            )
            data = desktop_snapshot(coordinator)
            self.assertEqual([17, 22], [x[1]["used_percent"] for x in quota_windows(data)])
            self.assertEqual("plus", data["rate_limits"]["plan_type"])
            self.assertEqual(22, data["rate_limits"]["primary"]["used_percent"])
            self.assertEqual(17, data["rate_limits"]["secondary"]["used_percent"])
            self.assertNotIn(str(root), json.dumps(data))
            path.unlink()
            data = desktop_snapshot(coordinator)
            self.assertEqual([17, 22], [x[1]["used_percent"] for x in quota_windows(data)])
            index.close()
            index.initialize()
            self.assertEqual([17, 22], [x[1]["used_percent"] for x in quota_windows(desktop_snapshot(coordinator))])
            write_jsonl(path, records)
            records.append(record("2026-09-08T02:00:00Z", "pro", dict(week, used_percent=31)))
            write_jsonl(path, records)
            SessionIndexer(index, config).scan_once(path.parent)
            data = desktop_snapshot(coordinator)
            self.assertEqual([31], [x[1]["used_percent"] for x in quota_windows(data)])
            self.assertNotIn(str(root), str(data))
            records.append(record("2026-09-08T03:00:00Z", "plus", hour, week))
            records[-1]["payload"]["rate_limits"].pop("limit_id")
            write_jsonl(path, records)
            SessionIndexer(index, config).scan_once(path.parent)
            self.assertEqual([17, 22], [x[1]["used_percent"] for x in quota_windows(desktop_snapshot(coordinator))])
            index.close()

    def test_unchanged_legacy_index_backfills_quota_before_ready(self):
        from codex_glass.core.presentation import desktop_snapshot, quota_windows
        from codex_glass.storage.index import SessionIndex, SessionIndexer
        from tests.helpers import write_jsonl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sessions" / "one.jsonl"
            hour = {"window_minutes": 300, "used_percent": 22, "resets_at": 2100000000}
            week = {"window_minutes": 10080, "used_percent": 17, "resets_at": 2100000000}
            write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-09-08T01:00:00Z",
                        "type": "token_count",
                        "payload": {
                            "rate_limits": {
                                "limit_id": "codex",
                                "plan_type": "plus",
                                "primary": hour,
                                "secondary": week,
                            }
                        },
                    }
                ],
            )
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            config = MonitorConfig.load(root / "absent.json")
            SessionIndexer(index, config).scan_once(path.parent)
            index.connection.execute("DROP TABLE quota_cache")
            index.connection.commit()
            index.close()
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            status = SessionIndexer(index, config).scan_once(path.parent)
            coordinator = SimpleNamespace(index=index, snapshot=lambda: {"total": {}})
            self.assertEqual("ready", status.status)
            self.assertEqual([17, 22], [x[1]["used_percent"] for x in quota_windows(desktop_snapshot(coordinator))])
            index.close()

    def test_malformed_newer_quota_does_not_replace_complete_observation(self):
        from codex_glass.core.presentation import desktop_snapshot, quota_windows
        from codex_glass.storage.index import SessionIndex, SessionIndexer
        from tests.helpers import write_jsonl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sessions" / "one.jsonl"
            hour = {"window_minutes": 300, "used_percent": 22, "resets_at": 2100000000}
            week = {"window_minutes": 10080, "used_percent": 17, "resets_at": 2100000000}
            records = [
                {
                    "timestamp": "2026-09-08T01:00:00Z",
                    "type": "token_count",
                    "payload": {
                        "rate_limits": {"limit_id": "codex", "plan_type": "plus", "primary": hour, "secondary": week}
                    },
                },
                {
                    "timestamp": "2026-09-08T02:00:00Z",
                    "type": "token_count",
                    "payload": {
                        "rate_limits": {
                            "limit_id": "codex",
                            "plan_type": "plus",
                            "primary": {},
                            "secondary": {"window_minutes": 0, "used_percent": float("nan")},
                        }
                    },
                },
            ]
            write_jsonl(path, records)
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            config = MonitorConfig.load(root / "absent.json")
            SessionIndexer(index, config).scan_once(path.parent)
            coordinator = SimpleNamespace(index=index, snapshot=lambda: {"total": {}})
            self.assertEqual([17, 22], [row[1]["used_percent"] for row in quota_windows(desktop_snapshot(coordinator))])
            index.close()

    def test_legacy_backfill_tries_older_bounded_candidate_after_malformed_latest(self):
        from codex_glass.core.presentation import desktop_snapshot, quota_windows
        from codex_glass.storage.index import SessionIndex, SessionIndexer
        from tests.helpers import write_jsonl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sessions" / "one.jsonl"
            hour = {"window_minutes": 300, "used_percent": 22, "resets_at": 2100000000}
            week = {"window_minutes": 10080, "used_percent": 17, "resets_at": 2100000000}
            write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-09-08T01:00:00Z",
                        "type": "token_count",
                        "payload": {
                            "rate_limits": {
                                "limit_id": "codex",
                                "plan_type": "plus",
                                "primary": hour,
                                "secondary": week,
                            }
                        },
                    },
                    {
                        "timestamp": "2026-09-08T02:00:00Z",
                        "type": "token_count",
                        "payload": {"rate_limits": {"limit_id": "codex", "primary": {}}},
                    },
                ],
            )
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            config = MonitorConfig.load(root / "absent.json")
            SessionIndexer(index, config).scan_once(path.parent)
            index.connection.execute("DROP TABLE quota_cache")
            index.connection.commit()
            index.close()
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            SessionIndexer(index, config).scan_once(path.parent)
            coordinator = SimpleNamespace(index=index, snapshot=lambda: {"total": {}})
            self.assertEqual([17, 22], [row[1]["used_percent"] for row in quota_windows(desktop_snapshot(coordinator))])
            index.close()


if __name__ == "__main__":
    unittest.main()
