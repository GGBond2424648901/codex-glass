import io
import json
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from codex_monitor_index import IncompatibleIndexError, SessionIndex
import codex_monitor_index as index_module
from codex_monitor_core import MonitorConfig, parse_usage_events_from_session_file
from tests.helpers import sample_records, write_jsonl


class RelevantLineReaderTests(unittest.TestCase):
    def reader(self, stream, start_offset=0, **kwargs):
        self.assertTrue(hasattr(index_module, "iter_relevant_lines"), "bounded reader is missing")
        return index_module.iter_relevant_lines(stream, start_offset, **kwargs)

    def test_large_irrelevant_line_keeps_memory_bounded_and_offsets_exact(self):
        first = b'{"type":"token_count","payload":{}}\n'
        last = b'{"type":"turn_context","payload":{}}\n'
        with tempfile.TemporaryFile() as stream:
            stream.write(first)
            for _ in range(16):
                stream.write(b"x" * (1024 * 1024))
            stream.write(b"\n" + last)
            stream.seek(0)
            tracemalloc.start()
            try:
                baseline = tracemalloc.get_traced_memory()[0]
                lines = list(self.reader(stream))
                peak = tracemalloc.get_traced_memory()[1] - baseline
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertEqual([first, last], [line.data for line in lines])
        self.assertEqual([0, len(first) + 16 * 1024 * 1024 + 1], [line.source_offset for line in lines])
        self.assertEqual(len(first), lines[0].next_offset)
        self.assertEqual(len(first) + 16 * 1024 * 1024 + 1 + len(last), lines[1].next_offset)

    def test_partial_final_line_leaves_completed_offset_at_its_start(self):
        complete = b'ignored\n{"type":"session_meta"}\n'
        reader = self.reader(io.BytesIO(complete + b'{"type":"token_count"}'))
        self.assertEqual(1, len(list(reader)))
        self.assertEqual(len(complete), reader.completed_offset)

    def test_resume_and_markers_split_across_chunks(self):
        prefix = b"old\n"
        record = b'{"type":"token_count"}\n'
        reader = self.reader(io.BytesIO(prefix + record), len(prefix), chunk_size=3)
        line, = list(reader)
        self.assertEqual((4, 4 + len(record), record), (line.source_offset, line.next_offset, line.data))

    def test_marker_beyond_probe_is_ignored(self):
        reader = self.reader(io.BytesIO(b"x" * 32 + b'{"type":"token_count"}\n'), probe_limit=32, chunk_size=7)
        self.assertEqual([], list(reader))
        self.assertEqual(55, reader.completed_offset)

    def test_oversized_relevant_record_is_counted_and_skipped(self):
        good = b'{"type":"token_count"}\n'
        data = good[:-1] + b" " * (8 * 1024 * 1024) + b"\n" + good
        reader = self.reader(io.BytesIO(data))
        self.assertEqual([good], [line.data for line in reader])
        self.assertEqual(1, reader.skipped_oversized)
        self.assertEqual(len(data), reader.completed_offset)


class SessionIndexerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.sessions = root / "sessions"
        self.sessions.mkdir()
        self.log = self.sessions / "session.jsonl"
        self.index = SessionIndex(root / "index.sqlite3")
        self.index.initialize()
        self.addCleanup(self.index.close)
        self.config = MonitorConfig("127.0.0.1", 8081, {}, {})
        self.assertTrue(hasattr(index_module, "SessionIndexer"), "incremental indexer is missing")
        self.indexer = index_module.SessionIndexer(self.index, self.config)

    def append(self, records, final_newline=True):
        with self.log.open("ab") as stream:
            for record in records:
                stream.write(json.dumps(record, separators=(",", ":")).encode("utf-8"))
                if final_newline:
                    stream.write(b"\n")

    def token(self, total=250, input=210, output=40):
        return {"timestamp": "2026-09-08T01:02:02Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "total_tokens": total, "input_tokens": input, "output_tokens": output}}}}

    def test_first_scan_matches_legacy_events_and_reports_progress(self):
        records = sample_records() + [self.token(), self.token(), self.token(10, 8, 2), self.token(30, 20, 10)]
        write_jsonl(self.log, records)
        expected, _ = parse_usage_events_from_session_file(self.log, self.config)
        progress = []
        status = self.indexer.scan_once(self.sessions, progress.append)
        actual = self.index.load_events()
        self.assertEqual([110, 85, 55, 20], [row["total_tokens"] for row in actual])
        for event, row in zip(expected, actual):
            self.assertEqual((event.model, event.cwd, event.timestamp.isoformat(), vars(event.delta)),
                             (row["model"], row["cwd"], row["occurred_at"],
                              {key: row[key] for key in vars(event.delta)}))
        self.assertTrue(status.complete)
        self.assertEqual((1, 1, 1), (status.total_files, status.processed_files, status.changed_files))
        self.assertEqual(self.log.stat().st_size, status.processed_bytes)
        self.assertTrue(progress[-1].complete)
        self.assertTrue(self.index.read_state().complete)

    def test_second_scan_indexes_only_appended_record_even_with_short_head(self):
        write_jsonl(self.log, sample_records())
        self.indexer.scan_once(self.sessions)
        original = [row["event_id"] for row in self.index.load_events()]
        old_size = self.log.stat().st_size
        self.append([self.token()])
        starts = []
        real_reader = index_module.iter_relevant_lines
        def observe(stream, start_offset, **kwargs):
            starts.append(start_offset)
            return real_reader(stream, start_offset, **kwargs)
        with mock.patch.object(index_module, "iter_relevant_lines", side_effect=observe):
            status = self.indexer.scan_once(self.sessions)
        self.assertEqual([old_size], starts)
        self.assertEqual(original, [row["event_id"] for row in self.index.load_events()[:2]])
        self.assertEqual([110, 85, 55], [row["total_tokens"] for row in self.index.load_events()])
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)
        self.assertEqual(1, status.changed_files)

    def test_unchanged_scan_does_not_reparse_or_duplicate(self):
        write_jsonl(self.log, sample_records())
        self.indexer.scan_once(self.sessions)
        with mock.patch.object(index_module, "iter_relevant_lines", side_effect=AssertionError("must not reparse")):
            status = self.indexer.scan_once(self.sessions)
        self.assertEqual(0, status.changed_files)
        self.assertEqual(2, len(self.index.load_events()))

    def test_restart_restores_model_cwd_and_cumulative_baseline(self):
        write_jsonl(self.log, sample_records())
        self.indexer.scan_once(self.sessions)
        self.index.close()
        self.index.initialize()
        self.append([self.token()])
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        last = self.index.load_events()[-1]
        self.assertEqual(("gpt-5", "C:/work/a", 55, 40, 15),
                         (last["model"], last["cwd"], last["total_tokens"], last["input_tokens"], last["output_tokens"]))

    def test_partial_final_line_is_only_committed_after_newline(self):
        write_jsonl(self.log, sample_records())
        complete_size = self.log.stat().st_size
        self.append([self.token()], final_newline=False)
        self.indexer.scan_once(self.sessions)
        self.assertEqual(complete_size, self.index.get_file_state(self.log).offset)
        self.assertEqual(2, len(self.index.load_events()))
        with self.log.open("ab") as stream:
            stream.write(b"\n")
        self.indexer.scan_once(self.sessions)
        self.assertEqual(3, len(self.index.load_events()))
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)

    def test_malformed_relevant_json_does_not_block_later_records(self):
        self.log.write_bytes(b'{"type":"token_count",INVALID}\n')
        self.append(sample_records())
        self.indexer.scan_once(self.sessions)
        self.assertEqual([110, 85], [row["total_tokens"] for row in self.index.load_events()])
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)

    def test_out_of_range_counters_are_skipped_without_poisoning_baseline(self):
        for field in ("total_tokens", "input_tokens", "cached_input_tokens", "output_tokens",
                      "reasoning_output_tokens"):
            for value in (10**25, -(10**25)):
                with self.subTest(field=field, value=value):
                    self.index.rebuild()
                    invalid = self.token()
                    invalid["payload"]["info"]["total_token_usage"][field] = value
                    write_jsonl(self.log, sample_records() + [invalid, self.token()])
                    status = self.indexer.scan_once(self.sessions)
                    self.assertEqual([110, 85, 55], [row["total_tokens"] for row in self.index.load_events()])
                    state = self.index.get_file_state(self.log)
                    self.assertEqual((250, 210, 40), (state.previous_total_tokens,
                                     state.previous_input_tokens, state.previous_output_tokens))
                    self.assertEqual(self.log.stat().st_size, state.offset)
                    self.assertIn("1 malformed", status.last_error)

    def test_out_of_range_delta_is_skipped_without_poisoning_baseline(self):
        write_jsonl(self.log, [self.token(1, -(2**63), 0), self.token(2, 2**63 - 1, 0),
                              self.token(3, -(2**63) + 5, 0)])
        status = self.indexer.scan_once(self.sessions)
        self.assertEqual([1, 2], [row["total_tokens"] for row in self.index.load_events()])
        self.assertEqual([-(2**63), 5], [row["input_tokens"] for row in self.index.load_events()])
        self.assertIn("1 malformed", status.last_error)
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)

    def test_surrogate_context_is_skipped_without_poisoning_model_or_cwd(self):
        for invalid in (
            {"type": "session_meta", "payload": {"cwd": "\ud800"}},
            {"type": "turn_context", "payload": {"model": "poison", "cwd": "\ud800"}},
            {"type": "turn_context", "payload": {"model": "\ud800", "cwd": "poison"}},
        ):
            with self.subTest(invalid=invalid):
                self.index.rebuild()
                write_jsonl(self.log, sample_records() + [invalid, self.token()])
                status = self.indexer.scan_once(self.sessions)
                events = self.index.load_events()
                self.assertEqual([110, 85, 55], [row["total_tokens"] for row in events])
                self.assertEqual(("gpt-5", "C:/work/a"), (events[-1]["model"], events[-1]["cwd"]))
                state = self.index.get_file_state(self.log)
                self.assertEqual(("gpt-5", "C:/work/a"), (state.last_model, state.last_cwd))
                self.assertEqual(self.log.stat().st_size, state.offset)
                self.assertIn("1 malformed", status.last_error)

    def test_invalid_snapshot_skips_whole_record_without_poisoning_totals(self):
        for rate_limits in (
            {"limit_id": "\ud800", "primary": {}},
            {"limit_name": "\ud800", "primary": {}},
            {"primary": {"window_minutes": 10**25}},
            {"primary": {"resets_in_seconds": 10**25, "resets_at": 0}},
            {"primary": {"used_percent": float("inf")}},
            {"primary": {"used_percent": float("nan")}},
        ):
            with self.subTest(rate_limits=rate_limits):
                self.index.rebuild()
                invalid = self.token()
                invalid["payload"]["rate_limits"] = rate_limits
                write_jsonl(self.log, sample_records() + [invalid, self.token()])
                status = self.indexer.scan_once(self.sessions)
                self.assertEqual([110, 85, 55], [row["total_tokens"] for row in self.index.load_events()])
                self.assertEqual([], self.index.load_rate_limits())
                final_record_offset = self.log.read_bytes().rfind(b"\n", 0, -1) + 1
                self.assertEqual(final_record_offset, self.index.load_events()[-1]["source_offset"])
                self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)
                self.assertIn("1 malformed", status.last_error)

    def test_deeply_nested_relevant_json_is_skipped_and_following_record_indexes(self):
        write_jsonl(self.log, sample_records())
        with self.log.open("ab") as stream:
            stream.write(b'{"type":"token_count","payload":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}\n")
        self.append([self.token()])
        status = self.indexer.scan_once(self.sessions)
        self.assertEqual([110, 85, 55], [row["total_tokens"] for row in self.index.load_events()])
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)
        self.assertIn("1 malformed", status.last_error)

    def test_truncation_and_replacement_reset_only_affected_file(self):
        other = self.sessions / "other.jsonl"
        write_jsonl(other, sample_records())
        write_jsonl(self.log, sample_records())
        self.indexer.scan_once(self.sessions)
        other_id = self.index.get_file_state(other).file_id
        other_rows = [dict(row) for row in self.index.load_events() if row["file_id"] == other_id]
        for records in ([self.token(40, 30, 10)], [self.token(60, 40, 20)]):
            write_jsonl(self.log, records)
            self.indexer.scan_once(self.sessions)
            self.assertEqual(3, len(self.index.load_events()))
            self.assertEqual(other_rows, [dict(row) for row in self.index.load_events() if row["file_id"] == other_id])
        self.assertEqual([60], [row["total_tokens"] for row in self.index.load_events() if row["file_id"] != other_id])

    def test_missing_file_keeps_prior_events_and_marks_file_missing(self):
        write_jsonl(self.log, sample_records())
        self.indexer.scan_once(self.sessions)
        self.log.unlink()
        self.indexer.scan_once(self.sessions)
        self.assertTrue(self.index.get_file_state(self.log).missing)
        self.assertEqual(2, len(self.index.load_events()))

    def test_disappearing_between_discovery_and_open_is_reported(self):
        write_jsonl(self.log, sample_records())
        original_open = Path.open
        def disappearing(path, *args, **kwargs):
            if path == self.log:
                path.unlink(missing_ok=True)
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", disappearing):
            status = self.indexer.scan_once(self.sessions)
        self.assertTrue(self.index.get_file_state(self.log).missing)
        self.assertIsNotNone(status.last_error)
        self.assertEqual(0, len(self.index.load_events()))

    def test_rate_limit_without_usage_is_persisted_and_replacement_clears_it(self):
        token = self.token()
        token["payload"]["info"] = None
        token["payload"]["rate_limits"] = {"limit_id": "codex", "primary": {
            "used_percent": 25, "window_minutes": 300, "resets_in_seconds": 120}}
        write_jsonl(self.log, [token])
        self.indexer.scan_once(self.sessions)
        snapshot, = self.index.load_rate_limits()
        self.assertEqual(("codex", 25, 300, 120), (snapshot["limit_id"], snapshot["used_percent"],
                         snapshot["window_minutes"], snapshot["resets_in_seconds"]))
        self.assertEqual([], self.index.load_events())
        write_jsonl(self.log, [self.token()])
        self.indexer.scan_once(self.sessions)
        self.assertEqual([], self.index.load_rate_limits())

    def test_failed_batch_rolls_back_events_and_cursor_then_resumes(self):
        records = [self.token(total=i, input=i, output=0) for i in range(1, 301)]
        records.append(self.token(total=313, input=313, output=0))
        write_jsonl(self.log, records)
        self.index.connection.execute("""CREATE TEMP TRIGGER fail_last_batch BEFORE INSERT ON usage_events
            WHEN NEW.total_tokens=13 BEGIN SELECT RAISE(ABORT, 'simulated interrupted batch'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.indexer.scan_once(self.sessions)
        committed = self.index.get_file_state(self.log)
        self.assertEqual(256, len(self.index.load_events()))
        self.assertEqual(256, committed.previous_total_tokens)
        with self.log.open("rb") as stream:
            for _ in range(256):
                stream.readline()
            self.assertEqual(stream.tell(), committed.offset)
        self.index.connection.execute("DROP TRIGGER fail_last_batch")
        self.index.close()
        self.index.initialize()
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.assertEqual(301, len(self.index.load_events()))
        self.assertEqual(313, sum(row["total_tokens"] for row in self.index.load_events()))
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)

    def test_context_changes_and_zero_deltas_survive_restart(self):
        write_jsonl(self.log, sample_records() + [
            {"type": "turn_context", "payload": {"cwd": "C:/work/b", "model": "gpt-5-mini"}},
            self.token(10, 5, 5),
        ])
        self.indexer.scan_once(self.sessions)
        self.index.close()
        self.index.initialize()
        self.append([self.token(30, 20, 10)])
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        events = self.index.load_events()
        self.assertEqual(["C:/work/a", "C:/work/a", "C:/work/b"], [event["cwd"] for event in events])
        self.assertEqual((20, "gpt-5-mini"), (events[-1]["total_tokens"], events[-1]["model"]))

    def test_complete_irrelevant_tail_advances_cursor_but_partial_tail_does_not(self):
        write_jsonl(self.log, sample_records())
        with self.log.open("ab") as stream:
            stream.write(b"irrelevant\npartial")
        self.indexer.scan_once(self.sessions)
        self.assertEqual(self.log.stat().st_size - 7, self.index.get_file_state(self.log).offset)

    def test_oversized_record_is_skipped_and_reported_without_losing_later_usage(self):
        with self.log.open("wb") as stream:
            stream.write(b'{"type":"token_count","padding":"')
            for _ in range(9):
                stream.write(b"x" * 1024 * 1024)
            stream.write(b'"}\n')
        self.append(sample_records())
        status = self.indexer.scan_once(self.sessions)
        self.assertEqual(2, len(self.index.load_events()))
        self.assertIn("1 oversized", status.last_error)
        self.assertEqual(self.log.stat().st_size, self.index.get_file_state(self.log).offset)


class SessionIndexSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "session-index.sqlite3"

    def test_initialize_creates_versioned_wal_database(self) -> None:
        index = SessionIndex(self.db_path)
        self.addCleanup(index.close)

        index.initialize()

        self.assertEqual(1, index.schema_version())
        self.assertEqual("wal", index.connection.execute("PRAGMA journal_mode").fetchone()[0].lower())
        tables = {row[0] for row in index.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"session_files", "usage_events", "rate_limit_snapshots", "index_state", "summary_cache"} <= tables)

    def test_incompatible_schema_is_rejected_without_changing_database(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA user_version = 999")
        connection.execute("CREATE TABLE preserved_data (value TEXT)")
        connection.execute("INSERT INTO preserved_data VALUES ('keep')")
        connection.commit()
        connection.close()
        before = self.db_path.read_bytes()

        index = SessionIndex(self.db_path)
        self.addCleanup(index.close)

        with self.assertRaises(IncompatibleIndexError):
            index.initialize()

        self.assertEqual(before, self.db_path.read_bytes())
        check = sqlite3.connect(self.db_path)
        self.addCleanup(check.close)
        self.assertEqual("keep", check.execute("SELECT value FROM preserved_data").fetchone()[0])

    def test_unexpired_lease_rejects_second_owner_and_expired_lease_can_be_taken(self) -> None:
        first = SessionIndex(self.db_path)
        second = SessionIndex(self.db_path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.initialize()
        second.initialize()

        self.assertTrue(first.acquire_lease("first", ttl_seconds=60))
        self.assertFalse(second.acquire_lease("second", ttl_seconds=60))
        first.connection.execute("UPDATE index_state SET lease_expires_at = 0 WHERE singleton_id = 1")
        first.connection.commit()

        self.assertTrue(second.acquire_lease("second", ttl_seconds=60))
        self.assertFalse(first.renew_lease("first", ttl_seconds=60))
        self.assertTrue(second.release_lease("second"))

    def test_reader_cannot_rollback_active_writer_transaction(self) -> None:
        index = SessionIndex(self.db_path)
        self.addCleanup(index.close)
        index.initialize()
        writer_started = threading.Event()
        release_writer = threading.Event()
        writer_errors: list[BaseException] = []

        def hold_writer_transaction() -> None:
            try:
                with index.write_transaction() as connection:
                    connection.execute("UPDATE index_state SET status = 'indexing' WHERE singleton_id = 1")
                    writer_started.set()
                    release_writer.wait(timeout=2)
            except BaseException as error:
                writer_errors.append(error)

        writer = threading.Thread(target=hold_writer_transaction)
        writer.start()
        self.addCleanup(release_writer.set)
        self.addCleanup(writer.join)
        self.assertTrue(writer_started.wait(timeout=1))

        with index.read_transaction() as reader:
            self.assertEqual("starting", reader.execute("SELECT status FROM index_state").fetchone()[0])

        self.assertTrue(writer.is_alive())
        release_writer.set()
        writer.join(timeout=1)
        self.assertFalse(writer.is_alive())
        self.assertEqual([], writer_errors)
        self.assertEqual("indexing", index.read_state().status)

    def test_rebuild_preserves_active_lease(self) -> None:
        first = SessionIndex(self.db_path)
        second = SessionIndex(self.db_path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.initialize()
        second.initialize()
        self.assertTrue(first.acquire_lease("first", ttl_seconds=60))

        first.rebuild()

        self.assertFalse(second.acquire_lease("second", ttl_seconds=60))
        self.assertTrue(first.renew_lease("first", ttl_seconds=60))

    def test_initialize_failure_clears_connection_and_allows_retry(self) -> None:
        index = SessionIndex(self.db_path)
        self.addCleanup(index.close)

        with mock.patch.object(index, "_retry_locked", side_effect=sqlite3.OperationalError("forced failure")):
            with self.assertRaises(sqlite3.OperationalError):
                index.initialize()

        with self.assertRaises(RuntimeError):
            _ = index.connection
        index.initialize()
        self.assertEqual(1, index.schema_version())

    def test_acquire_lease_ttl_starts_after_waiting_for_write_transaction(self) -> None:
        index = SessionIndex(self.db_path)
        self.addCleanup(index.close)
        index.initialize()
        clock = [100.0]
        original_run_write = index._run_write

        def run_after_wait(operation):
            clock[0] = 102.0
            return original_run_write(operation)

        with mock.patch("codex_monitor_index.time.time", side_effect=lambda: clock[0]):
            with mock.patch.object(index, "_run_write", side_effect=run_after_wait):
                self.assertTrue(index.acquire_lease("owner", ttl_seconds=1))

        expires_at = index.connection.execute(
            "SELECT lease_expires_at FROM index_state WHERE singleton_id = 1"
        ).fetchone()[0]
        self.assertGreater(expires_at, clock[0])

    def test_renew_lease_ttl_starts_after_waiting_for_write_transaction(self) -> None:
        index = SessionIndex(self.db_path)
        self.addCleanup(index.close)
        index.initialize()
        clock = [100.0]
        with mock.patch("codex_monitor_index.time.time", side_effect=lambda: clock[0]):
            self.assertTrue(index.acquire_lease("owner", ttl_seconds=20))
            original_run_write = index._run_write

            def run_after_wait(operation):
                clock[0] = 112.0
                return original_run_write(operation)

            with mock.patch.object(index, "_run_write", side_effect=run_after_wait):
                self.assertTrue(index.renew_lease("owner", ttl_seconds=1))

        expires_at = index.connection.execute(
            "SELECT lease_expires_at FROM index_state WHERE singleton_id = 1"
        ).fetchone()[0]
        self.assertGreater(expires_at, clock[0])


class IndexCoordinatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sessions = self.root / "sessions"
        self.config = MonitorConfig.load(self.root / "config.json")
        self.index = SessionIndex(self.root / "index.sqlite3")
        self.index.initialize()
        self.addCleanup(self.index.close)
        write_jsonl(self.sessions / "one.jsonl", sample_records())

    def coordinator(self, interval=60):
        self.assertTrue(hasattr(index_module, "IndexCoordinator"), "background coordinator is missing")
        coordinator = index_module.IndexCoordinator(self.index, self.sessions, lambda: self.config, None, interval=interval)
        self.addCleanup(coordinator.stop)
        return coordinator

    def wait_status(self, coordinator, wanted, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = coordinator.snapshot()
            if snapshot["index"]["status"] == wanted:
                return snapshot
            time.sleep(0.01)
        self.fail(f"Expected {wanted}, got {coordinator.snapshot()['index']}")

    def test_start_is_nonblocking_and_refreshes_coalesce_while_cached_data_is_available(self):
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.index.build_summary(self.config)
        coordinator = self.coordinator()
        self.assertEqual("starting", coordinator.snapshot()["index"]["status"])
        entered = threading.Event()
        release = threading.Event()
        second = threading.Event()
        scans = []
        original_scan = index_module.SessionIndexer.scan_once

        def blocking_scan(indexer, sessions_dir, progress=None):
            scans.append(threading.current_thread())
            if len(scans) == 1:
                entered.set()
                release.wait(timeout=3)
            else:
                second.set()
            return original_scan(indexer, sessions_dir, progress)

        with mock.patch.object(index_module.SessionIndexer, "scan_once", blocking_scan):
            self.addCleanup(release.set)
            started = time.monotonic()
            coordinator.start()
            coordinator.start()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(entered.wait(timeout=1))
            during = coordinator.snapshot()
            self.assertEqual("indexing", during["index"]["status"])
            self.assertEqual(195, during["total"]["total_tokens"])
            during["total"]["total_tokens"] = -1
            self.assertEqual(195, coordinator.snapshot()["total"]["total_tokens"])
            callers = [threading.Thread(target=coordinator.request_refresh) for _ in range(24)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=1)
            release.set()
            self.assertTrue(second.wait(timeout=2))
            ready = self.wait_status(coordinator, "ready")
            coordinator.stop()
        self.assertTrue(ready["index"]["complete"])
        self.assertEqual(2, len(scans))
        self.assertTrue(all(worker.daemon for worker in scans))
        self.assertIs(scans[0], scans[1])

    def test_scan_error_keeps_snapshot_available_and_retries_on_request(self):
        coordinator = self.coordinator()
        original_scan = index_module.SessionIndexer.scan_once
        failed = False

        def fail_once(indexer, sessions_dir, progress=None):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("Cannot read 'C:/private/customer/session.jsonl'")
            return original_scan(indexer, sessions_dir, progress)

        with mock.patch.object(index_module.SessionIndexer, "scan_once", fail_once):
            coordinator.start()
            error = self.wait_status(coordinator, "error")
            self.assertEqual(0, error["total"]["total_tokens"])
            self.assertNotIn("customer", json.dumps(error["index"]))
            self.assertEqual("error", self.index.read_state().status)
            coordinator.request_refresh()
            ready = self.wait_status(coordinator, "ready")
            self.assertEqual(195, ready["total"]["total_tokens"])
            coordinator.stop()

    def test_recoverable_scan_error_retries_on_the_next_interval(self):
        coordinator = self.coordinator(interval=0.02)
        original_scan = index_module.SessionIndexer.scan_once
        failed = False

        def fail_once(indexer, sessions_dir, progress=None):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("Temporary read failure")
            return original_scan(indexer, sessions_dir, progress)

        with mock.patch.object(index_module.SessionIndexer, "scan_once", fail_once):
            coordinator.start()
            ready = self.wait_status(coordinator, "ready")
            coordinator.stop()
        self.assertEqual(195, ready["total"]["total_tokens"])
        self.assertIsNone(ready["index"]["last_error"])

    def test_status_payload_redacts_network_share_paths(self):
        status = replace(self.index.read_state(), current_file=r"\\server\private\session.jsonl",
                         last_error=r"Cannot read '\\server\private\session.jsonl'")
        payload = status.to_payload()
        self.assertEqual("会话文件 1", payload["current_file"])
        self.assertNotIn("server", json.dumps(payload))
        self.assertNotIn("private", json.dumps(payload))

    def test_status_payload_redacts_full_paths_with_embedded_quotes(self):
        for path in (r"C:\sessions\O'customer-name.jsonl", "/home/o'brien/customer-project/session.jsonl",
                     '/home/o"brien/customer-project/session.jsonl'):
            with self.subTest(path=path):
                status = replace(self.index.read_state(), last_error=f"Cannot read '{path}'")
                self.assertEqual("Cannot read [本地路径]", status.to_payload()["last_error"])

    def test_structured_exception_filenames_are_removed_before_error_is_saved(self):
        coordinator = self.coordinator()
        error = OSError(2, "File unavailable", r"C:\sessions\O'customer-name.jsonl")
        with mock.patch.object(index_module.SessionIndexer, "scan_once", side_effect=error):
            coordinator.start()
            snapshot = self.wait_status(coordinator, "error")
            coordinator.stop()
        self.assertNotIn("customer-name", snapshot["index"]["last_error"])
        self.assertNotIn("customer-name", self.index.read_state().last_error)
        self.assertIn("File unavailable", snapshot["index"]["last_error"])

    def run_slow_summary_phase(self, phase):
        coordinator = self.coordinator(interval=3)
        elapsed = [1000.0]
        fake_time = SimpleNamespace(time=lambda: elapsed[0], monotonic=lambda: elapsed[0], sleep=time.sleep)
        original_aggregate = index_module.aggregate_usage_events
        original_dumps = json.dumps

        def aggregate(*args, **kwargs):
            if phase == "aggregation":
                elapsed[0] += 31
            return original_aggregate(*args, **kwargs)

        def dumps(value, **kwargs):
            if phase == "serialization" and isinstance(value, dict) and "total" in value and "index" in value:
                elapsed[0] += 31
            return original_dumps(value, **kwargs)

        fake_json = SimpleNamespace(dumps=dumps, loads=json.loads)
        with mock.patch.object(index_module, "time", fake_time), mock.patch.object(index_module, "json", fake_json), \
                mock.patch.object(index_module, "aggregate_usage_events", aggregate):
            coordinator.start()
            ready = self.wait_status(coordinator, "ready")
            coordinator.stop()
        self.assertEqual(195, ready["total"]["total_tokens"])
        self.assertIsNone(ready["index"]["last_error"])
        self.assertEqual(2, len(coordinator.events_snapshot()))

    def test_31_second_aggregation_does_not_expire_the_scan_lease(self):
        self.run_slow_summary_phase("aggregation")

    def test_31_second_serialization_does_not_expire_the_scan_lease(self):
        self.run_slow_summary_phase("serialization")

    def test_new_generation_during_summary_is_retried_before_publication(self):
        coordinator = self.coordinator()
        other = SessionIndex(self.index.path)
        other.initialize()
        self.addCleanup(other.close)
        original_aggregate = index_module.aggregate_usage_events
        advanced = False

        def advance_before_aggregation(*args, **kwargs):
            nonlocal advanced
            if not advanced:
                advanced = True
                self.assertTrue(other.acquire_lease("competing-indexer", 60))
                try:
                    write_jsonl(self.sessions / "two.jsonl", sample_records())
                    index_module.SessionIndexer(other, self.config).scan_once(self.sessions)
                    other.build_summary(self.config, include_events=True)
                finally:
                    other.release_lease("competing-indexer")
            return original_aggregate(*args, **kwargs)

        with mock.patch.object(index_module, "aggregate_usage_events", advance_before_aggregation):
            coordinator.start()
            ready = self.wait_status(coordinator, "ready")
            coordinator.stop()
        self.assertEqual(390, ready["total"]["total_tokens"])
        self.assertEqual(4, len(coordinator.events_snapshot()))
        self.assertEqual(390, self.index.cached_summary(self.config, include_events=True)["total"]["total_tokens"])

    def test_empty_rebuild_during_aggregation_cannot_cache_or_publish_old_summary(self):
        coordinator = self.coordinator()
        other = SessionIndex(self.index.path)
        other.initialize()
        self.addCleanup(other.close)
        original_aggregate = index_module.aggregate_usage_events

        def rebuild_before_aggregation(*args, **kwargs):
            self.assertTrue(other.acquire_lease("rebuild", 60))
            try:
                other.rebuild()
            finally:
                other.release_lease("rebuild")
            return original_aggregate(*args, **kwargs)

        with mock.patch.object(index_module, "aggregate_usage_events", rebuild_before_aggregation):
            stale_summary = self.index.build_summary(self.config, include_events=True)
        self.assertIsNone(self.index.cached_summary(self.config, include_events=True))
        self.assertFalse(coordinator._publish_summary(self.index.read_state(), stale_summary))
        self.assertEqual([], self.index.load_events())
        self.assertEqual("starting", self.index.read_state().status)

    def test_summary_error_is_persisted_after_the_scan_lease_is_released(self):
        coordinator = self.coordinator()
        with mock.patch.object(index_module, "aggregate_usage_events", side_effect=RuntimeError("Summary failed")):
            coordinator.start()
            self.wait_status(coordinator, "error")
            coordinator.stop()
        self.assertEqual("error", self.index.read_state().status)
        self.assertEqual("Summary failed", self.index.read_state().last_error)

    def test_superseded_summary_error_cannot_overwrite_newer_index_state(self):
        coordinator = self.coordinator()

        def advance_then_fail(*args, **kwargs):
            write_jsonl(self.sessions / "two.jsonl", sample_records())
            index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
            raise RuntimeError("Old summary failed")

        with mock.patch.object(index_module, "aggregate_usage_events", advance_then_fail):
            coordinator.start()
            self.wait_status(coordinator, "error")
            coordinator.stop()
        self.assertEqual("ready", self.index.read_state().status)
        self.assertIsNone(self.index.read_state().last_error)

    def test_foreign_lease_publishes_waiting_then_recovers_after_release(self):
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.index.build_summary(self.config)
        other = SessionIndex(self.index.path)
        other.initialize()
        self.addCleanup(other.close)
        self.assertTrue(other.acquire_lease("external-owner", 60))
        coordinator = self.coordinator()
        coordinator.start()
        waiting = self.wait_status(coordinator, "waiting")
        self.assertIsNone(waiting["index"]["last_error"])
        self.assertEqual(195, waiting["total"]["total_tokens"])
        other.release_lease("external-owner")
        coordinator.request_refresh()
        self.wait_status(coordinator, "ready")
        coordinator.stop()
        self.assertTrue(other.acquire_lease("next-owner", 60))

    def test_history_is_cached_separately_from_the_lightweight_data_snapshot(self):
        index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.index.build_summary(self.config, include_events=True)
        coordinator = self.coordinator()
        self.assertTrue(hasattr(coordinator, "events_snapshot"), "separate history snapshot is missing")
        self.assertNotIn("events", coordinator.snapshot())
        self.assertEqual([], coordinator.events_snapshot(), "history loads only after background start")
        coordinator.start()
        self.wait_status(coordinator, "ready")
        self.assertEqual(2, len(coordinator.events_snapshot()))
        events = coordinator.events_snapshot()
        events[0]["tokens"]["total"] = -1
        self.assertEqual(85, coordinator.events_snapshot()[0]["tokens"]["total"])
        self.assertNotIn("events", coordinator.snapshot())
        self.assertEqual(2, len(coordinator.events_snapshot()))
        coordinator.stop()

    def test_large_irrelevant_content_reports_intermediate_progress_for_lease_renewal(self):
        path = self.sessions / "large.jsonl"
        path.write_bytes((b'{"type":"unrelated","text":"' + b'x' * 600000 + b'"}\n') * 8)
        statuses = []
        ticks = iter(range(100))
        with mock.patch("codex_monitor_index.time.monotonic", side_effect=lambda: next(ticks)):
            index_module.SessionIndexer(self.index, self.config).scan_once(self.sessions, progress=statuses.append)
        self.assertTrue(any(status.current_file == str(path) and 0 < status.processed_bytes < path.stat().st_size for status in statuses),
                        "skipped content must still provide lease/shutdown checkpoints")

    def test_lease_renews_during_scan_and_is_released_on_orderly_stop(self):
        coordinator = self.coordinator(interval=0.05)
        other = SessionIndex(self.index.path)
        other.initialize()
        self.addCleanup(other.close)
        renewed = threading.Event()
        release = threading.Event()

        def long_scan(indexer, sessions_dir, progress=None):
            status = replace(self.index.read_state(), status="indexing", complete=False)
            deadline = time.monotonic() + 1.3
            while time.monotonic() < deadline:
                progress(status)
                time.sleep(0.1)
            renewed.set()
            while not release.wait(0.1):
                progress(status)
            return replace(status, status="ready", complete=True)

        with mock.patch.object(index_module, "_LEASE_TTL_SECONDS", 1, create=True), mock.patch.object(index_module.SessionIndexer, "scan_once", long_scan):
            self.addCleanup(release.set)
            coordinator.start()
            self.assertTrue(renewed.wait(timeout=2.5))
            self.assertFalse(other.acquire_lease("competitor", 10))
            release.set()
            coordinator.stop()
        self.assertTrue(other.acquire_lease("competitor", 10))


if __name__ == "__main__":
    unittest.main()
