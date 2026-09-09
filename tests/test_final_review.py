"""Regression cases from the persistent index final review."""

import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

import codex_glass.storage.index as indexing
from codex_glass.cli import main as monitor
from codex_glass.services import dashboard as web_dashboard
from codex_glass.core.usage import MonitorConfig
from tests.helpers import sample_records, write_jsonl


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sessions = self.root / "sessions"
        self.log = self.sessions / "one.jsonl"
        write_jsonl(self.log, sample_records())
        self.config = MonitorConfig.load(self.root / "config.json")
        self.index = indexing.SessionIndex(self.root / "index.sqlite3")
        self.index.initialize()
        self.addCleanup(self.index.close)

    def scan(self):
        return indexing.SessionIndexer(self.index, self.config).scan_once(self.sessions)

    def wait_status(self, coordinator, expected):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = coordinator.snapshot()["index"]
            if status["status"] == expected:
                return status
            time.sleep(0.01)
        self.fail(f"Expected {expected}, got {status}")

    def test_different_session_root_is_rejected_before_history_or_cache_is_served(self):
        self.scan()
        self.index.build_summary(self.config, include_events=True)
        other_root = self.root / "other-sessions"
        write_jsonl(other_root / "two.jsonl", sample_records())
        before = [dict(row) for row in self.index.load_events()]
        with self.assertRaisesRegex(RuntimeError, "session.*root|root.*session"):
            indexing.SessionIndexer(self.index, self.config).scan_once(other_root)
        self.assertEqual(before, [dict(row) for row in self.index.load_events()])
        coordinator = indexing.IndexCoordinator(self.index, other_root, lambda: self.config, None, interval=60)
        self.addCleanup(coordinator.stop)
        self.assertEqual(0, coordinator.snapshot()["total"]["total_tokens"])
        coordinator.start()
        self.wait_status(coordinator, "error")
        self.assertEqual(0, coordinator.snapshot()["total"]["total_tokens"])
        self.assertEqual([], coordinator.events_snapshot())

    def test_unreadable_file_is_incomplete_and_retried_without_crediting_unread_bytes(self):
        original = Path.open

        def unreadable(path, *args, **kwargs):
            if path == self.log:
                raise PermissionError(13, "Permission denied", str(path))
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", unreadable):
            status = self.scan()
        self.assertFalse(status.complete)
        self.assertEqual("error", status.status)
        self.assertEqual(1, status.failed_files)
        self.assertEqual(0, status.processed_bytes)
        self.assertEqual(0, status.processed_files)
        self.assertIn("retry", status.last_error.lower())
        self.assertNotIn(str(self.root), json.dumps(status.to_payload()))
        ready = self.scan()
        self.assertTrue(ready.complete)
        self.assertEqual(0, ready.failed_files)
        self.assertIsNone(ready.last_error)
        self.assertEqual(195, sum(row["total_tokens"] for row in self.index.load_events()))

    def test_rebuild_for_different_root_cannot_erase_the_bound_history(self):
        self.scan()
        before = [dict(row) for row in self.index.load_events()]
        other_root = self.root / "other-sessions"
        write_jsonl(other_root / "two.jsonl", sample_records())
        coordinator = indexing.IndexCoordinator(
            self.index, other_root, lambda: self.config, None, interval=60, rebuild_index=True
        )
        self.addCleanup(coordinator.stop)
        coordinator.start()
        self.wait_status(coordinator, "error")
        coordinator.stop()
        self.assertEqual(before, [dict(row) for row in self.index.load_events()])

    def test_same_size_in_place_change_uses_modification_time(self):
        prefix = b"ignored" + b"x" * 5000 + b"\n"
        self.log.write_bytes(prefix + self.log.read_bytes())
        self.scan()
        old = self.log.stat()
        self.log.write_bytes(self.log.read_bytes().replace(b'"total_tokens":195', b'"total_tokens":295'))
        os.utime(self.log, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
        self.scan()
        self.assertEqual(295, sum(row["total_tokens"] for row in self.index.load_events()))

    def test_same_prefix_atomic_replacement_rebuilds_only_changed_file(self):
        prefix = b'{"type":"unrelated","padding":"' + b"x" * 5000 + b'"}\n'
        self.log.write_bytes(prefix + self.log.read_bytes())
        untouched = self.sessions / "untouched.jsonl"
        write_jsonl(untouched, sample_records())
        self.scan()
        old_stat = self.log.stat()
        untouched_id = self.index.get_file_state(untouched).file_id
        before = [dict(row) for row in self.index.load_events() if row["file_id"] == untouched_id]
        replacement = self.root / "replacement.jsonl"
        replacement.write_bytes(self.log.read_bytes().replace(b'"total_tokens":195', b'"total_tokens":295'))
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        os.replace(replacement, self.log)
        self.assertEqual(old_stat.st_size, self.log.stat().st_size)
        status = self.scan()
        self.assertEqual(1, status.changed_files)
        rows = self.index.load_events()
        self.assertEqual(295, sum(row["total_tokens"] for row in rows if row["file_id"] != untouched_id))
        self.assertEqual(before, [dict(row) for row in rows if row["file_id"] == untouched_id])

    def test_lost_lease_rolls_back_batch_cursor_generation_and_status(self):
        self.assertTrue(self.index.acquire_lease("stale-owner", 60))
        other = indexing.SessionIndex(self.index.path)
        other.initialize()
        self.addCleanup(other.close)
        original = indexing.SessionIndexer._parse_line
        captured = {}

        def steal_before_batch(indexer, *args, **kwargs):
            result = original(indexer, *args, **kwargs)
            if not captured:
                with other.write_transaction() as writer:
                    writer.execute("UPDATE index_state SET lease_expires_at=0 WHERE singleton_id=1")
                self.assertTrue(other.acquire_lease("new-owner", 60))
                captured["file"] = self.index.get_file_state(self.log)
                captured["state"] = tuple(other.connection.execute("SELECT * FROM index_state").fetchone())
            return result

        with mock.patch.object(indexing.SessionIndexer, "_parse_line", steal_before_batch):
            with self.assertRaisesRegex(RuntimeError, "lease"):
                self.scan()
        self.assertEqual([], self.index.load_events())
        self.assertEqual(captured["file"], self.index.get_file_state(self.log))
        self.assertEqual(captured["state"], tuple(other.connection.execute("SELECT * FROM index_state").fetchone()))

    def test_lease_expiring_inside_transaction_rolls_back_before_commit(self):
        with mock.patch.object(indexing.time, "time", return_value=100.0):
            self.assertTrue(self.index.acquire_lease("owner", 5))
        before = tuple(self.index.connection.execute("SELECT * FROM index_state").fetchone())
        with mock.patch.object(indexing.time, "time", side_effect=[101.0, 106.0]):
            with self.assertRaisesRegex(RuntimeError, "lease"):
                with self.index.write_transaction(expected_owner="owner") as writer:
                    writer.execute("UPDATE index_state SET status='ready', generation=generation+1")
        self.assertEqual(before, tuple(self.index.connection.execute("SELECT * FROM index_state").fetchone()))

    def test_history_page_memory_is_bounded_and_filters_preserve_public_labels(self):
        records = sample_records()[:2]
        for value in range(1, 4001):
            record = sample_records()[2]
            record["payload"]["info"]["total_token_usage"]["total_tokens"] = value
            records.append(record)
        write_jsonl(self.log, records)
        self.scan()
        self.index.build_summary(self.config, include_events=True)
        coordinator = indexing.IndexCoordinator(self.index, self.sessions, lambda: self.config, None, interval=60)
        self.addCleanup(coordinator.stop)
        coordinator.start()
        self.wait_status(coordinator, "ready")
        coordinator.stop()
        self.assertTrue(hasattr(coordinator, "events_page"), "bounded history API is missing")
        label = coordinator.events_snapshot()[0]["cwd"]
        tracemalloc.start()
        try:
            page = coordinator.events_page(offset=11, limit=3, q="GPT", model="GpT-5", cwd=label)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(4000, page["total"])
        self.assertEqual(3, len(page["events"]))
        self.assertLess(peak, 250_000, "a small history page must not copy the full corpus")
        self.assertEqual(0, coordinator.events_page(q="private-absent")["total"])
        self.assertNotIn("C:/work/a", json.dumps(page))
        page["events"][0]["tokens"]["total"] = -1
        self.assertEqual(1, coordinator.events_page(offset=11, limit=1)["events"][0]["tokens"]["total"])
        server = web_dashboard.create_server("127.0.0.1", 0, coordinator)
        self.wait_status(coordinator, "ready")
        coordinator.stop()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tracemalloc.start()
            try:
                with urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/events?offset=11&limit=3", timeout=2
                ) as response:
                    result = json.loads(response.read())
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual((4000, 3), (result["total"], len(result["events"])))
            self.assertLess(peak, 500_000, "HTTP history must use the bounded page API")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_index_status_malformed_schema_returns_json_error(self):
        path = self.root / "malformed.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=1")
            connection.execute("CREATE TABLE index_state(singleton_id INTEGER PRIMARY KEY, status TEXT)")
            connection.execute("INSERT INTO index_state VALUES (1, 'ready')")
            connection.commit()
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["monitor.py", "index-status", "--index-db", str(path)]),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(1, monitor.main())
        self.assertEqual("error", json.loads(output.getvalue())["status"])

    def run_bootstrap_server(self, path, blocker=None):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        servers, errors = [], []
        entered, release = threading.Event(), threading.Event()
        original_create = web_dashboard.create_server
        original_load = indexing.SessionIndex.cached_summary
        original_initialize = indexing.SessionIndex.initialize

        def create(*args):
            server = original_create(*args)
            servers.append(server)
            return server

        def slow_load(index, *args, **kwargs):
            entered.set()
            release.wait(5)
            return original_load(index, *args, **kwargs)

        def initialize(index):
            if blocker == "database":
                entered.set()
                release.wait(5)
            return original_initialize(index)

        def run():
            try:
                web_dashboard.main()
            except BaseException as error:
                errors.append(error)

        args = [
            "web_dashboard.py",
            "--no-browser",
            "--port",
            str(port),
            "--index-db",
            str(path),
            "--sessions-dir",
            str(self.sessions),
            "--config",
            str(self.root / "config.json"),
        ]
        patch_load = mock.patch.object(
            indexing.SessionIndex, "cached_summary", slow_load if blocker == "cache" else original_load
        )
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(sys, "stdout", io.StringIO()),
            mock.patch.object(web_dashboard, "create_server", create),
            patch_load,
            mock.patch.object(indexing.SessionIndex, "initialize", initialize),
        ):
            main_thread = threading.Thread(target=run, daemon=True)
            main_thread.start()
            try:
                if blocker:
                    self.assertTrue(entered.wait(2), repr(errors))
                deadline = time.monotonic() + 1
                while not servers and not errors and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(servers, f"HTTP must bind before bootstrap completes: {errors}")
                with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    self.assertEqual(b"ok\n", response.read())
                if blocker:
                    self.assertEqual("starting", servers[0].coordinator.snapshot()["index"]["status"])
                else:
                    self.wait_status(servers[0].coordinator, "error")
                    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        self.assertEqual(200, response.status)
                self.assertEqual([], errors)
            finally:
                release.set()
                deadline = time.monotonic() + 2
                while not servers and main_thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                for server in servers:
                    server.shutdown()
                main_thread.join(3)

    def test_health_responds_while_cached_history_deserialization_is_blocked(self):
        self.scan()
        self.index.build_summary(self.config, include_events=True)
        self.run_bootstrap_server(self.index.path, blocker="cache")

    def test_health_responds_while_database_initialization_is_blocked(self):
        self.run_bootstrap_server(self.root / "slow-initialization.sqlite3", blocker="database")

    def test_health_survives_corrupt_database_bootstrap(self):
        path = self.root / "corrupt.sqlite3"
        path.write_bytes(b"not a SQLite database")
        self.run_bootstrap_server(path)

    def test_health_survives_corrupt_cached_history_json(self):
        self.scan()
        self.index.build_summary(self.config, include_events=True)
        with self.index.write_transaction() as writer:
            writer.execute("UPDATE summary_cache SET payload_json='invalid json'")
        self.run_bootstrap_server(self.index.path)
