import sqlite3
import tempfile
import unittest
import json
from pathlib import Path

from codex_glass.core.usage import MonitorConfig
from codex_glass.storage.history_import import (
    HistoryImportError,
    HistoryImporter,
    rate_limit_key,
    session_key,
    usage_event_key,
)
from codex_glass.storage.index import SessionIndex


class HistoryImportSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_initialize_adds_import_tables_without_removing_local_rows(self):
        path = self.root / "index.sqlite3"
        index = SessionIndex(path)
        index.initialize()
        index.connection.execute(
            "INSERT INTO session_files(path, created_at, updated_at) VALUES (?, 1, 1)",
            (str(self.root / "one.jsonl"),),
        )
        index.connection.commit()
        index.close()

        reopened = SessionIndex(path)
        reopened.initialize()
        self.addCleanup(reopened.close)
        tables = {row[0] for row in reopened.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(
            {
                "history_imports",
                "imported_session_files",
                "imported_usage_events",
                "imported_rate_limit_snapshots",
            }
            <= tables
        )
        self.assertEqual(1, reopened.connection.execute("SELECT COUNT(*) FROM session_files").fetchone()[0])

    def test_stable_keys_deduplicate_copied_session_facts(self):
        first = session_key(
            r"C:\Users\PC\.codex\sessions\rollout-2026-09-08T01-00-00-019eb98f-7ef0-7530-b0ea-8caa660869d0.jsonl",
            "head-a",
        )
        copied = session_key(
            r"D:\archive\rollout-2026-09-08T01-00-00-019eb98f-7ef0-7530-b0ea-8caa660869d0.jsonl",
            "head-b",
        )
        other = session_key(r"D:\archive\plain.jsonl", "head-c")
        self.assertEqual(first, copied)
        self.assertNotEqual(first, other)

        usage = (10, "2026-09-08T09:00:00", "gpt-5", "C:/work", 100, 20, 10, 2, 110)
        self.assertEqual(usage_event_key(first, *usage), usage_event_key(copied, *usage))
        self.assertNotEqual(usage_event_key(first, *usage), usage_event_key(first, 11, *usage[1:]))

        rate = (20, "codex", None, "2026-09-08T09:00:00", 17.0, 10080, None, 600)
        self.assertEqual(rate_limit_key(first, *rate), rate_limit_key(copied, *rate))
        self.assertNotEqual(rate_limit_key(first, *rate), rate_limit_key(first, 21, *rate[1:]))


class HistoryImporterTests(unittest.TestCase):
    def test_packed_summary_preserves_mixed_source_dedup_and_history(self):
        from datetime import datetime

        self.importer.import_database(self.make_source())
        self.importer.import_database(self.make_source("second.sqlite3", source_offset=21, tokens=120))
        self.add_matching_local_event()
        config = MonitorConfig.load(self.root / "missing.json")
        now = datetime(2026, 9, 9, 12)
        expected = self.target.build_summary(config, include_events=True, now=now)
        actual = self.target.build_summary(config, compact_events=True, now=now)
        self.assertEqual(230, actual["total"]["total_tokens"])
        self.assertEqual(expected.pop("events"), list(actual.pop("_history")))
        self.assertEqual(expected, actual)

    SESSION_ID = "019eb98f-7ef0-7530-b0ea-8caa660869d0"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = SessionIndex(self.root / "target.sqlite3")
        self.target.initialize()
        self.addCleanup(self.target.close)
        self.importer = HistoryImporter(self.target)

    def make_source(self, name="source.sqlite3", root=r"C:\Users\PC\.codex\sessions", tokens=110, source_offset=20):
        path = self.root / name
        source = SessionIndex(path)
        source.initialize()
        source.connection.execute("UPDATE index_state SET sessions_root=? WHERE singleton_id=1", (root,))
        source_path = root + rf"\rollout-2026-09-08T01-00-00-{self.SESSION_ID}.jsonl"
        cursor = source.connection.execute(
            """INSERT INTO session_files(path, size, head_hash, offset, created_at, updated_at)
               VALUES (?, 1000, 'head', 1000, 1, 1)""",
            (source_path,),
        )
        file_id = cursor.lastrowid
        source.connection.execute(
            """INSERT INTO usage_events(
                   file_id, source_offset, occurred_at, model, cwd, input_tokens,
                   cached_input_tokens, output_tokens, reasoning_output_tokens,
                   total_tokens, pricing_source, created_at)
               VALUES (?, ?, '2026-09-08T09:00:00', 'gpt-5', 'C:/work', 100, 20, 10, 2, ?, 'direct', 1)""",
            (file_id, source_offset, tokens),
        )
        source.connection.execute(
            """INSERT INTO rate_limit_snapshots(
                   file_id, source_offset, limit_id, limit_name, observed_at, used_percent,
                   window_minutes, resets_at, resets_in_seconds, created_at)
               VALUES (?, 30, 'codex', NULL, '2026-09-08T09:00:00', 17, 10080,
                       '2026-09-15T09:00:00', 604800, 1)""",
            (file_id,),
        )
        source.connection.commit()
        source.close()
        return path

    def add_matching_local_event(self):
        path = self.root / f"rollout-2026-09-08T01-00-00-{self.SESSION_ID}.jsonl"
        cursor = self.target.connection.execute(
            "INSERT INTO session_files(path, head_hash, created_at, updated_at) VALUES (?, 'local', 1, 1)",
            (str(path),),
        )
        self.target.connection.execute(
            """INSERT INTO usage_events(
                   file_id, source_offset, occurred_at, model, cwd, input_tokens,
                   cached_input_tokens, output_tokens, reasoning_output_tokens,
                   total_tokens, pricing_source, created_at)
               VALUES (?, 20, '2026-09-08T09:00:00', 'gpt-5', 'C:/work', 100, 20, 10, 2, 110, 'direct', 1)""",
            (cursor.lastrowid,),
        )
        self.target.connection.commit()

    def test_import_is_idempotent_and_summary_contains_imported_fact(self):
        source = self.make_source()
        first = self.importer.import_database(source)
        second = self.importer.import_database(source)

        self.assertEqual(
            (1, 1, 1), (first.imported_files, first.imported_usage_events, first.imported_rate_limit_snapshots)
        )
        self.assertEqual(
            (0, 0, 0), (second.imported_files, second.imported_usage_events, second.imported_rate_limit_snapshots)
        )
        summary = self.target.build_summary(MonitorConfig.load(self.root / "missing.json"), include_events=True)
        self.assertEqual(110, summary["total"]["total_tokens"])
        self.assertEqual(1, summary["source"]["imported_sources"])
        self.assertEqual(1, len(summary["events"]))

    def test_overlapping_sources_and_local_event_count_once_with_local_precedence(self):
        first = self.make_source("first.sqlite3")
        second = self.make_source("second.sqlite3", root=r"D:\copied-sessions")
        self.importer.import_database(first)
        self.importer.import_database(second)
        self.add_matching_local_event()

        summary = self.target.build_summary(MonitorConfig.load(self.root / "missing.json"), include_events=True)
        self.assertEqual(110, summary["total"]["total_tokens"])
        self.assertEqual(1, len(summary["events"]))
        self.assertEqual(17.0, summary["rate_limits"]["primary"]["used_percent"])
        self.assertEqual(1, summary["source"]["imported_usage_events"])
        self.assertEqual(1, summary["source"]["imported_rate_limit_snapshots"])
        self.assertNotIn("copied-sessions", json.dumps(summary))
        self.assertEqual(2, self.target.connection.execute("SELECT COUNT(*) FROM imported_usage_events").fetchone()[0])
        self.assertEqual(
            2, self.target.connection.execute("SELECT COUNT(*) FROM imported_rate_limit_snapshots").fetchone()[0]
        )

    def test_rebuild_preserves_import_and_remove_source_deletes_only_imported_history(self):
        source = self.make_source()
        result = self.importer.import_database(source)
        self.target.rebuild()
        self.assertEqual(1, self.target.connection.execute("SELECT COUNT(*) FROM imported_usage_events").fetchone()[0])

        removed = self.importer.remove_source(result.source_id)
        self.assertEqual(
            (1, 1, 1), (removed.removed_files, removed.removed_usage_events, removed.removed_rate_limit_snapshots)
        )
        self.assertEqual([], self.importer.list_sources())
        self.assertEqual(
            0, self.target.build_summary(MonitorConfig.load(self.root / "missing.json"))["total"]["total_tokens"]
        )

    def test_corrupt_source_is_rejected_without_target_mutation(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(HistoryImportError, "SQLite|integrity|database"):
            self.importer.import_database(corrupt)
        self.assertEqual([], self.importer.list_sources())

    def test_target_failure_rolls_back_entire_source(self):
        source = self.make_source()
        self.target.connection.execute(
            """CREATE TRIGGER reject_import BEFORE INSERT ON imported_usage_events
               BEGIN SELECT RAISE(ABORT, 'reject import'); END"""
        )
        self.target.connection.commit()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "reject import"):
            self.importer.import_database(source)
        self.assertEqual([], self.importer.list_sources())
        self.assertEqual(0, self.target.connection.execute("SELECT COUNT(*) FROM imported_session_files").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
