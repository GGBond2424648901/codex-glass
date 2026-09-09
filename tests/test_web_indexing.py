import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from codex_glass.services import dashboard as web_dashboard
from codex_glass.core.usage import MonitorConfig
from codex_glass.storage.index import IndexCoordinator, SessionIndex, SessionIndexer
from tests.helpers import sample_records, write_jsonl


class WebIndexingTests(unittest.TestCase):
    def test_idle_scans_reuse_history_until_clock_bucket_or_explicit_refresh(self):
        self.coordinator.interval = 0.025
        with mock.patch.object(self.index, "build_summary", wraps=self.index.build_summary) as builds:
            self.coordinator.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and builds.call_count < 1:
                time.sleep(0.01)
            time.sleep(0.15)
            self.assertEqual(1, builds.call_count)
            self.coordinator.request_refresh()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and builds.call_count < 2:
                time.sleep(0.01)
            self.assertEqual(2, builds.call_count)
            self.coordinator.stop()

    def test_native_context_handles_unavailable_reader_and_rejects_browser_origin(self):
        from urllib.error import HTTPError

        self.serve()
        self.wait_status("ready")
        with mock.patch.object(self.index, "read_transaction", side_effect=RuntimeError("not ready")):
            self.assertEqual(200, self.request("/api/native-context")[0])
        for headers in ({"Origin": "https://example.com"}, {"Host": "example.com"}):
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(self.url + "/api/native-context", headers=headers), timeout=2)
            self.assertEqual(403, caught.exception.code)

    def test_native_history_filters_and_context_are_served(self):
        self.serve()
        self.wait_status("ready")
        code, data = self.request("/api/events?since=2099-01-01&models=not-a-model")
        self.assertEqual(200, code)
        self.assertEqual(0, data["total"])
        code, data = self.request("/api/native-context")
        self.assertEqual(str(self.index.path.resolve()), data["index_path"])
        self.assertEqual([], data["sources"])

    def test_native_invalid_history_date_returns_bad_request(self):
        from urllib.error import HTTPError

        self.serve()
        self.wait_status("ready")
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/events?since=invalid")
        self.assertEqual(400, caught.exception.code)

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
        SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.index.build_summary(self.config, include_events=True)
        self.coordinator = IndexCoordinator(self.index, self.sessions, lambda: self.config, None, interval=60)
        self.addCleanup(self.coordinator.stop)

    def serve(self):
        self.assertTrue(hasattr(web_dashboard, "create_server"), "testable indexed server is missing")
        server = web_dashboard.create_server("127.0.0.1", 0, self.coordinator)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.url = "http://127.0.0.1:%d" % server.server_port
        return server

    def request(self, path, method="GET"):
        with urlopen(Request(self.url + path, method=method), timeout=1.5) as response:
            body = response.read()
            return response.status, (
                json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body
            )

    def wait_status(self, wanted):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, status = self.request("/api/index-status")
            if status["status"] == wanted:
                return status
            time.sleep(0.01)
        self.fail(f"Expected {wanted}, got {status}")

    def test_health_and_partial_summary_respond_during_blocked_scan(self):
        entered, release = threading.Event(), threading.Event()
        original = SessionIndexer.scan_once

        def blocked(indexer, sessions_dir, progress=None):
            entered.set()
            release.wait(5)
            return original(indexer, sessions_dir, progress)

        with mock.patch.object(SessionIndexer, "scan_once", blocked):
            self.addCleanup(release.set)
            started = time.monotonic()
            self.serve()
            self.assertTrue(entered.wait(1))
            self.assertEqual((200, b"ok\n"), self.request("/healthz"))
            status_code, status = self.request("/api/index-status")
            self.assertEqual(200, status_code)
            self.assertEqual("indexing", status["status"])
            self.assertFalse(status["complete"])
            code, data = self.request("/api/data")
            self.assertEqual(200, code)
            self.assertEqual(195, data["total"]["total_tokens"])
            self.assertEqual("indexing", data["index"]["status"])
            self.assertNotIn("events", data)
            self.assertLess(time.monotonic() - started, 2)
            release.set()
            self.wait_status("ready")

    def test_desktop_endpoint_returns_compact_summary_without_events(self):
        self.serve()
        self.wait_status("ready")
        code, data = self.request("/api/widget")
        self.assertEqual(200, code)
        self.assertEqual(195, data["total"]["total_tokens"])
        self.assertNotIn("events", data)
        self.assertIn("today", data["windows"])
        self.assertEqual([], data["rate_limits"]["limits"])

    def test_refresh_returns_202_and_schedules_next_scan_without_waiting(self):
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        original = SessionIndexer.scan_once
        scans = 0

        def blocked(indexer, sessions_dir, progress=None):
            nonlocal scans
            scans += 1
            if scans == 1:
                entered.set()
                release.wait(5)
            else:
                second.set()
            return original(indexer, sessions_dir, progress)

        with mock.patch.object(SessionIndexer, "scan_once", blocked):
            self.addCleanup(release.set)
            self.serve()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertEqual(202, self.request("/api/refresh", "POST")[0])
            self.assertLess(time.monotonic() - started, 1)
            release.set()
            self.assertTrue(second.wait(2))
            self.wait_status("ready")

    def test_events_remain_paginated_and_filtered_outside_summary(self):
        self.serve()
        self.wait_status("ready")
        _, data = self.request("/api/data")
        self.assertNotIn("events", data)
        _, history = self.request("/api/events?offset=1&limit=1")
        self.assertEqual(2, history["total"])
        self.assertEqual(1, len(history["events"]))
        self.assertEqual(110, history["events"][0]["tokens"]["total"])
        _, filtered = self.request("/api/events?q=does-not-exist")
        self.assertEqual(0, filtered["total"])

    def test_data_api_exposes_total_history_price_coverage(self):
        records = sample_records()
        records[1]["payload"]["model"] = "unknown-api-model"
        write_jsonl(self.sessions / "unknown.jsonl", records)
        SessionIndexer(self.index, self.config).scan_once(self.sessions)
        self.index.build_summary(self.config, include_events=True)

        self.serve()
        self.wait_status("ready")
        code, data = self.request("/api/data")

        self.assertEqual(200, code)
        self.assertEqual(390, data["pricing"]["total_tokens"])
        self.assertEqual(195, data["pricing"]["priced_tokens"])
        self.assertEqual(195, data["pricing"]["unpriced_tokens"])
        self.assertEqual(4, data["pricing"]["total_calls"])
        self.assertEqual(2, data["pricing"]["priced_calls"])
        self.assertEqual(2, data["pricing"]["unpriced_calls"])
        self.assertEqual(50.0, data["pricing"]["token_coverage_percent"])
        self.assertEqual(
            [{"model": "unknown-api-model", "calls": 2, "total_tokens": 195}],
            data["pricing"]["unpriced_models"],
        )

    def test_dashboard_labels_cost_as_known_price_estimate_and_renders_coverage_details(self):
        self.serve()
        code, page = self.request("/")
        html = page.decode("utf-8")

        self.assertEqual(200, code)
        self.assertIn("已知价格估算", html)
        self.assertNotIn("累计花费", html)
        self.assertNotIn("全量估算费用", html)
        self.assertIn('id="pricingCoverage"', html)
        self.assertIn('id="unpricedUsage"', html)
        render_overview = html.split("function renderOverview", 1)[1].split("function renderTrend", 1)[0]
        self.assertIn("const pricing = data.pricing || {};", render_overview)
        self.assertIn('class="v mono bounded-detail" id="unpricedUsage"', html)

    def test_dashboard_uses_known_price_estimate_for_every_dollar_label(self):
        self.serve()
        code, page = self.request("/")
        html = page.decode("utf-8")

        ambiguous_labels = [
            label for label in ("费用（美元）", "每小时花费", "令牌 · 费用", "{label: '费用'", "成本") if label in html
        ]
        self.assertEqual(200, code)
        self.assertEqual([], ambiguous_labels)
        self.assertIn('<option value="cost">已知价格估算（美元）</option>', html)
        self.assertIn("每小时已知价格估算", html)
        self.assertIn("令牌 · 已知价格估算", html)

    def test_dashboard_displays_aggregate_priced_and_unpriced_call_coverage(self):
        self.serve()
        code, page = self.request("/")
        html = page.decode("utf-8")
        render_overview = html.split("function renderOverview", 1)[1].split("function renderTrend", 1)[0]

        self.assertEqual(200, code)
        self.assertIn("调用价格覆盖", html)
        self.assertIn('id="pricingCallCoverage"', html)
        self.assertIn("pricing.priced_calls", render_overview)
        self.assertIn("pricing.total_calls", render_overview)
        self.assertIn("pricing.unpriced_calls", render_overview)
        self.assertIn("已知价格估算", render_overview)
        self.assertIn("未定价", render_overview)

    def test_scan_error_keeps_cached_data_available_and_refresh_recovers(self):
        original = SessionIndexer.scan_once
        failed = False

        def fail_once(indexer, sessions_dir, progress=None):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("Cannot read 'C:/private/customer/session.jsonl'")
            return original(indexer, sessions_dir, progress)

        with mock.patch.object(SessionIndexer, "scan_once", fail_once):
            self.serve()
            status = self.wait_status("error")
            self.assertNotIn("customer", json.dumps(status))
            self.assertEqual(195, self.request("/api/data")[1]["total"]["total_tokens"])
            self.assertEqual(202, self.request("/api/refresh", "POST")[0])
            self.assertTrue(self.wait_status("ready")["complete"])

    def test_socket_is_bound_before_worker_starts(self):
        self.assertTrue(hasattr(web_dashboard, "create_server"), "testable indexed server is missing")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        original = self.coordinator.start

        def start_after_bind():
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
            original()

        with mock.patch.object(self.coordinator, "start", start_after_bind):
            server = web_dashboard.create_server("127.0.0.1", port, self.coordinator)
            self.addCleanup(server.server_close)

    def test_stop_reports_timeout_then_confirmed_worker_termination(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(indexer, sessions_dir, progress=None):
            entered.set()
            release.wait(10)
            return indexer.index.read_state()

        with mock.patch.object(SessionIndexer, "scan_once", blocked):
            try:
                self.coordinator.start()
                self.assertTrue(entered.wait(1))
                self.assertIs(self.coordinator.stop(timeout=0.01), False)
                release.set()
                self.assertIs(self.coordinator.stop(timeout=2), True)
            finally:
                release.set()
                self.coordinator.stop()

    def test_main_waits_beyond_default_stop_timeout_before_closing_index(self):
        entered, release, shutdown_started, closed = (threading.Event() for _ in range(4))
        worker = []
        alive_at_close = []
        errors = []
        real_create_server = web_dashboard.create_server
        real_close = SessionIndex.close

        def blocked(indexer, sessions_dir, progress=None):
            worker.append(threading.current_thread())
            entered.set()
            release.wait(15)
            return indexer.index.read_state()

        def interrupt_server(host, port, coordinator):
            server = real_create_server("127.0.0.1", 0, coordinator)

            def serve_until_interrupt():
                if not entered.wait(2):
                    raise RuntimeError("Indexer did not start")
                shutdown_started.set()
                raise KeyboardInterrupt

            server.serve_forever = serve_until_interrupt
            return server

        def close_after_worker(index):
            alive_at_close.append(bool(worker and worker[0].is_alive()))
            real_close(index)
            closed.set()

        def run_main():
            try:
                web_dashboard.main()
            except BaseException as error:
                errors.append(error)

        args = [
            "web_dashboard.py",
            "--no-browser",
            "--sessions-dir",
            str(self.sessions),
            "--config",
            str(self.root / "config.json"),
        ]
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(sys, "stdout", io.StringIO()),
            mock.patch.object(web_dashboard, "default_index_path", return_value=self.root / "main.sqlite3"),
            mock.patch.object(web_dashboard, "create_server", interrupt_server),
            mock.patch.object(SessionIndexer, "scan_once", blocked),
            mock.patch.object(SessionIndex, "close", close_after_worker),
        ):
            main_thread = threading.Thread(target=run_main, daemon=True)
            main_thread.start()
            try:
                self.assertTrue(shutdown_started.wait(2), repr(errors))
                self.assertFalse(closed.wait(5.2), "Index closed before blocked worker terminated")
                self.assertTrue(main_thread.is_alive())
                release.set()
                main_thread.join(3)
                self.assertFalse(main_thread.is_alive())
                self.assertTrue(closed.is_set())
                self.assertEqual([False], alive_at_close)
                self.assertEqual([], errors)
            finally:
                release.set()
                main_thread.join(3)
                if worker:
                    worker[0].join(3)


if __name__ == "__main__":
    unittest.main()
