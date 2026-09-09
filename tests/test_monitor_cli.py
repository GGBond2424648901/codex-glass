import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_glass.cli import main as monitor
from codex_glass.services import dashboard as web_dashboard
from codex_glass.core.usage import MonitorConfig
from codex_glass.storage.index import SessionIndex, SessionIndexer
from tests.helpers import sample_records, write_jsonl


class MonitorCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_web_and_background_forward_exact_index_options(self):
        options = [
            "--sessions-dir",
            "sessions with spaces",
            "--index-db",
            "缓存/index.sqlite3",
            "--rebuild-index",
            "--no-index",
            "--config",
            "custom.json",
            "--cwd",
            "project",
            "--port",
            "8099",
            "--host",
            "127.0.0.2",
            "--no-browser",
        ]
        for mode in ("web", "background"):
            with (
                self.subTest(mode=mode),
                mock.patch.object(Path, "home", return_value=self.root),
                mock.patch.object(sys, "argv", ["monitor.py", mode, *options]),
                mock.patch.object(sys, "stdout", io.StringIO()),
                mock.patch.object(monitor.subprocess, "run") as run,
                mock.patch.object(monitor.subprocess, "Popen") as popen,
                mock.patch.object(monitor, "_wait_for_server", return_value=True),
            ):
                run.return_value.returncode = 0
                popen.return_value.pid = 12345
                self.assertEqual(0, monitor.main())
                command = (run if mode == "web" else popen).call_args.args[0]
                self.assertEqual(sys.executable, command[0])
                self.assertEqual("web_dashboard.py", Path(command[1]).name)
                for flag in ("--sessions-dir", "--index-db", "--config", "--cwd", "--port", "--host"):
                    self.assertEqual(options[options.index(flag) + 1], command[command.index(flag) + 1])
                for flag in ("--rebuild-index", "--no-index", "--no-browser"):
                    self.assertEqual(1, command.count(flag))

    def test_script_path_and_server_use_the_same_moved_dashboard_build(self):
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(project_root / "web_dashboard.py", monitor._script_path("web_dashboard.py"))
        self.assertEqual(web_dashboard._DASHBOARD_BUILD, monitor._local_dashboard_build())

    def test_terminal_modes_keep_relative_arguments_from_unrelated_working_directory(self):
        cases = (
            ("simple", "codex_glass.cli.simple", []),
            ("enhanced", "codex_glass.cli.enhanced", ["--once"]),
        )
        for mode, module, extra in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                arguments = [
                    mode,
                    "--sessions-dir",
                    "relative-sessions",
                    "--config",
                    "relative-config.json",
                    "--cwd",
                    "relative-project",
                    *extra,
                ]
                args = monitor.build_parser().parse_args(arguments)
                original_cwd = Path.cwd()
                try:
                    os.chdir(temporary_directory)
                    with (
                        mock.patch.object(sys, "stdout", io.StringIO()),
                        mock.patch.object(monitor.subprocess, "run") as run,
                    ):
                        run.return_value.returncode = 0
                        function = monitor.run_simple_terminal if mode == "simple" else monitor.run_enhanced_terminal
                        self.assertEqual(0, function(args))
                finally:
                    os.chdir(original_cwd)

                command = run.call_args.args[0]
                self.assertEqual([sys.executable, "-m", module], command[:3])
                self.assertEqual("relative-sessions", command[command.index("--sessions-dir") + 1])
                self.assertEqual("relative-config.json", command[command.index("--config") + 1])
                self.assertEqual("relative-project", command[command.index("--cwd") + 1])
                self.assertNotIn("cwd", run.call_args.kwargs)
                self.assertEqual(
                    str(Path(__file__).resolve().parents[1]),
                    run.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
                )

    def test_console_configuration_allows_emoji_on_gbk_streams(self):
        self.assertTrue(hasattr(monitor, "configure_console_output"))
        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream=stream_name):
                buffer = io.BytesIO()
                stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
                with mock.patch.object(sys, stream_name, stream):
                    monitor.configure_console_output()
                    print("🚀 ✅ 中文", file=stream, flush=True)
                self.assertEqual("🚀 ✅ 中文\n", buffer.getvalue().decode("utf-8").replace("\r\n", "\n"))
                stream.close()

    def test_console_configuration_tolerates_unsupported_streams(self):
        self.assertTrue(hasattr(monitor, "configure_console_output"))
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", None):
            monitor.configure_console_output()

    def populated_index(self, name="index.sqlite3"):
        sessions = self.root / "sessions"
        write_jsonl(sessions / "one.jsonl", sample_records())
        index = SessionIndex(self.root / name)
        index.initialize()
        self.addCleanup(index.close)
        SessionIndexer(index, MonitorConfig.load(self.root / "config.json")).scan_once(sessions)
        return index, sessions

    def test_index_status_reads_existing_state_without_browser_or_database_writes(self):
        index, _ = self.populated_index("索引 #1.sqlite3")
        expected = index.read_state().to_payload()
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["monitor.py", "index-status", "--index-db", str(index.path)]),
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(SessionIndex, "initialize", side_effect=AssertionError("status must not initialize")),
            mock.patch.object(monitor, "_try_open_browser", side_effect=AssertionError("status must not browse")),
        ):
            self.assertEqual(0, monitor.main())
        self.assertEqual(expected, json.loads(output.getvalue()))

    def test_index_status_missing_database_emits_json_without_creating_it(self):
        path = self.root / "missing" / "index.sqlite3"
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["monitor.py", "index-status", "--index-db", str(path)]),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(1, monitor.main())
        self.assertEqual("error", json.loads(output.getvalue())["status"])
        self.assertFalse(path.parent.exists())

    def test_web_rebuild_clears_only_resolved_database_in_background_after_bind(self):
        index, sessions = self.populated_index()
        other, _ = self.populated_index("other.sqlite3")
        source = (sessions / "one.jsonl").read_bytes()
        calls = []
        rebuilt = threading.Event()
        original = SessionIndex.rebuild
        original_create = web_dashboard.create_server

        def rebuild(target):
            calls.append(target.path)
            original(target)
            self.assertEqual([], target.load_events())
            rebuilt.set()

        def server_factory(host, port, coordinator):
            server = original_create("127.0.0.1", 0, coordinator)

            def until_rebuilt():
                self.assertTrue(rebuilt.wait(3))
                raise KeyboardInterrupt

            server.serve_forever = until_rebuilt
            return server

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "web_dashboard.py",
                    "--no-browser",
                    "--sessions-dir",
                    str(sessions),
                    "--index-db",
                    str(self.root / "." / "index.sqlite3"),
                    "--rebuild-index",
                ],
            ),
            mock.patch.object(sys, "stdout", io.StringIO()),
            mock.patch.object(SessionIndex, "rebuild", rebuild),
            mock.patch.object(web_dashboard, "create_server", side_effect=server_factory),
        ):
            web_dashboard.main()
        self.assertEqual([index.path.resolve()], calls)
        self.assertTrue(index.path.exists())
        self.assertTrue(other.load_events())
        self.assertEqual(source, (sessions / "one.jsonl").read_bytes())
        self.assertTrue(index.acquire_lease("next-writer", 60), "rebuild must release its lease")

    def test_rebuild_refuses_live_writer_without_clearing_data_or_lease(self):
        index, sessions = self.populated_index()
        self.assertTrue(index.acquire_lease("live-writer", 60))
        original_create = web_dashboard.create_server

        def server_factory(host, port, coordinator):
            server = original_create("127.0.0.1", 0, coordinator)

            def until_error():
                deadline = time.monotonic() + 3
                while coordinator.snapshot()["index"]["status"] != "error" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertRegex(coordinator.snapshot()["index"]["last_error"], "another.*process")
                raise KeyboardInterrupt

            server.serve_forever = until_error
            return server

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "web_dashboard.py",
                    "--no-browser",
                    "--sessions-dir",
                    str(sessions),
                    "--index-db",
                    str(index.path),
                    "--rebuild-index",
                ],
            ),
            mock.patch.object(sys, "stdout", io.StringIO()),
            mock.patch.object(web_dashboard, "create_server", server_factory),
        ):
            web_dashboard.main()
        self.assertTrue(index.load_events())
        self.assertFalse(index.acquire_lease("other-writer", 60))

    def test_no_index_uses_legacy_parser_without_opening_database(self):
        _, sessions = self.populated_index()
        path = self.root / "unused.sqlite3"

        def server_factory(host, port, coordinator):
            self.assertEqual(195, coordinator.snapshot()["total"]["total_tokens"])
            self.assertTrue(coordinator.events_snapshot())
            coordinator.request_refresh()
            self.assertEqual(195, coordinator.snapshot()["total"]["total_tokens"])
            server = mock.Mock(server_port=8081)
            server.serve_forever.side_effect = KeyboardInterrupt
            return server

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "web_dashboard.py",
                    "--no-browser",
                    "--sessions-dir",
                    str(sessions),
                    "--index-db",
                    str(path),
                    "--no-index",
                    "--rebuild-index",
                ],
            ),
            mock.patch.object(sys, "stdout", io.StringIO()),
            mock.patch.object(SessionIndex, "initialize", side_effect=AssertionError("no-index must not initialize")),
            mock.patch.object(web_dashboard, "create_server", side_effect=server_factory),
        ):
            web_dashboard.main()
        self.assertFalse(path.exists())

    def test_import_list_and_remove_history_sources_emit_json(self):
        source, _ = self.populated_index("source.sqlite3")
        target_path = self.root / "target.sqlite3"

        output = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["monitor.py", "import-index", "--source-index", str(source.path), "--index-db", str(target_path)],
            ),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(0, monitor.main())
        imported = json.loads(output.getvalue())
        self.assertEqual("ok", imported["status"])
        self.assertEqual(2, imported["imported_usage_events"])
        self.assertTrue(Path(imported["backup_path"]).exists())

        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["monitor.py", "list-imports", "--index-db", str(target_path)]),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(0, monitor.main())
        listed = json.loads(output.getvalue())
        self.assertEqual(1, len(listed["sources"]))

        output = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["monitor.py", "remove-import", "--source-id", imported["source_id"], "--index-db", str(target_path)],
            ),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(0, monitor.main())
        removed = json.loads(output.getvalue())
        self.assertEqual((1, 2), (removed["removed_files"], removed["removed_usage_events"]))

    def test_import_index_requires_source_and_does_not_create_target(self):
        target = self.root / "unused.sqlite3"
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["monitor.py", "import-index", "--index-db", str(target)]),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(1, monitor.main())
        self.assertEqual("error", json.loads(output.getvalue())["status"])
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
