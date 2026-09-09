import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from codex_glass.core.presentation import desktop_snapshot, quota_windows
from codex_glass.core.usage import MonitorConfig
from codex_glass.storage.index import SessionIndex, SessionIndexer
from tests.helpers import write_jsonl


class QuotaAccuracyTests(unittest.TestCase):
    def payload(self):
        return {
            "limit_id": "codex",
            "plan_type": "pro",
            "primary": {"window_minutes": 300, "used_percent": 22, "resets_at": 2100000000},
            "secondary": {"window_minutes": 10080, "used_percent": 17, "resets_at": 2100000000},
        }

    def test_pro_preserves_both_actual_windows(self):
        quota = SessionIndexer._sanitize_quota_payload(datetime.now(), self.payload())
        self.assertEqual([300, 10080], [r["window_minutes"] for r in quota["limits"]])
        self.assertEqual([17, 22], [r["used_percent"] for _, r in quota_windows({"rate_limits": quota})])

    def test_presentation_does_not_hide_actual_pro_hour(self):
        data = {
            "rate_limits": {"plan_type": "pro", "limits": [dict(scope="global", window_minutes=300, used_percent=22)]}
        }
        self.assertEqual(["5 小时额度"], [name for name, _ in quota_windows(data)])

    def test_upgrade_restores_filtered_cache_without_reindexing_tokens(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            path = root / "sessions" / "one.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-09-08T01:00:00Z",
                        "type": "token_count",
                        "payload": {"rate_limits": self.payload()},
                    }
                ],
            )
            index = SessionIndex(root / "index.sqlite3")
            index.initialize()
            cleanup.callback(index.close)
            scanner = SessionIndexer(index, MonitorConfig.load(root / "absent.json"))
            scanner.scan_once(path.parent)
            old = {
                "plan_type": "pro",
                "limits": [
                    {"scope": "global", "window_minutes": 10080, "used_percent": 17, "resets_at": "2036-01-01T00:00:00"}
                ],
            }
            index.connection.execute("UPDATE quota_cache SET payload_json=?", (json.dumps(old, separators=(",", ":")),))
            index.connection.commit()
            scanner.scan_once(path.parent)
            data = desktop_snapshot(SimpleNamespace(index=index, snapshot=lambda: {}))
            self.assertEqual([17, 22], [r["used_percent"] for _, r in quota_windows(data)])
