import unittest
import copy
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

try:
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from codex_glass.desktop.widget import GlassWidget

    HAS_QT = True
except ImportError:
    HAS_QT = False
from tests.glass_fixtures import telemetry


@unittest.skipUnless(HAS_QT, "optional desktop dependency PyQt5 not installed")
class GlassUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.widget = GlassWidget(url="http://127.0.0.1:1", start_backend=False, persist=False)
        self.widget.timer.stop()
        self.widget.show()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.widget.timer.stop()
        self.widget.tray.hide()
        self.widget.hide()
        self.widget.deleteLater()
        QTest.qWait(20)

    def load(self):
        self.assertTrue(callable(getattr(self.widget, "apply_data", None)), "new UI needs apply_data consumer")
        self.widget.apply_data(telemetry())
        QTest.qWait(20)

    def test_pin_checked_shape_changes_and_window_flag_matches(self):
        from PyQt5.QtGui import QImage

        self.widget.top_action.setChecked(False)
        off = self.widget.pin.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        QTest.mouseClick(self.widget.pin, Qt.LeftButton)
        self.assertTrue(self.widget.pin.isChecked())
        self.assertTrue(self.widget.windowFlags() & Qt.WindowStaysOnTopHint)
        on = self.widget.pin.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        # Active pin must differ in silhouette, not merely a barely visible tint.
        changes = sum(
            (off.pixelColor(x, y).alpha() > 200) != (on.pixelColor(x, y).alpha() > 200)
            for y in range(5, 24)
            for x in range(5, 24)
        )
        self.assertGreater(changes, 12)
        self.assertIn("取消", self.widget.pin.toolTip())

    def test_local_backend_ignores_configured_listen_port(self):
        from unittest.mock import patch

        self.widget.url = "http://127.0.0.1:8081"
        self.widget.start_backend = True
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"LOCALAPPDATA": folder}),
            patch("codex_glass.desktop.widget.subprocess.Popen") as launch,
        ):
            self.widget.launch_backend()
        command = launch.call_args.args[0]
        self.assertIn("--host", command)
        self.assertEqual("127.0.0.1", command[command.index("--host") + 1])
        self.assertEqual(f"http://127.0.0.1:{command[command.index('--port') + 1]}", self.widget.url)

    def test_old_backend_version_is_not_mistaken_for_upgraded_backend(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.widget.start_backend = True
        reply = SimpleNamespace(
            error=lambda: 0,
            readAll=lambda: b'{"total":{"total_tokens":999},"server":{"version":"0.0.0"}}',
            deleteLater=lambda: None,
        )
        with patch.object(self.widget, "launch_backend"):
            self.widget.received(reply)
        self.assertFalse(self.widget.connected)
        self.assertEqual({}, self.widget.data)

    def test_index_error_is_visible_in_empty_chart(self):
        from datetime import datetime

        data = {
            "total": {},
            "generated_at": datetime.now().isoformat(),
            "index": {"status": "error", "last_error": "root mismatch"},
        }
        self.widget.apply_data(data)
        self.assertIn("索引失败", getattr(self.widget.chart, "empty_text", ""))
        self.assertIn("root mismatch", self.widget.dot.toolTip())
        self.assertFalse(self.widget.chart.live)

    def test_skipped_record_warning_does_not_mask_aggregation_or_ready_data(self):
        data = telemetry()
        data["index"] = {
            "status": "aggregating",
            "phase": "history",
            "last_error": "Skipped 1 malformed and 0 oversized relevant records",
        }
        self.widget.apply_data(data)
        self.assertNotIn("索引失败", self.widget.dot.toolTip())
        self.assertIn("历史", self.widget.dot.toolTip())
        self.assertNotEqual("—", self.widget.tokens.text())
        data["index"]["status"] = "ready"
        self.widget.apply_data(data)
        self.assertNotIn("索引失败", self.widget.dot.toolTip())

    def test_zero_series_still_renders_its_empty_state_message(self):
        self.widget.set_motion(False)
        chart = self.widget.chart
        chart.set_series([0, 0], ["10:00", "11:00"], animate=False)
        chart.empty_text = "正在扫描本机会话…"
        before = chart.grab().toImage()
        chart.empty_text = "当前时段暂无用量 · 可切换累计"
        after = chart.grab().toImage()
        self.assertNotEqual(before, after, "Zero-valued axis labels must not hide the status message")

    def test_diagnostics_distinguishes_missing_directory_and_index_failure(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"CODEX_HOME": folder}):
            self.widget.apply_data({"total": {}, "index": {"status": "error", "last_error": "root mismatch"}})
            report = self.widget.data_diagnostics()
        self.assertIn("目录不存在", report)
        self.assertIn("root mismatch", report)
        self.assertIn("sessions", report)
        self.assertIn("http://127.0.0.1:1", report)

    def test_loopback_data_bypasses_unavailable_system_proxy(self):
        import json
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from PyQt5.QtNetwork import QNetworkProxy

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(telemetry()).encode())

            def log_message(self, *args):
                pass

        previous = QNetworkProxy.applicationProxy()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.HttpProxy, "127.0.0.1", 1))
            self.widget.url = f"http://127.0.0.1:{server.server_port}"
            self.widget.fetch()
            deadline = time.monotonic() + 2
            while not self.widget.connected and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertTrue(self.widget.connected, "Loopback telemetry must not depend on a user's HTTP proxy")
        finally:
            QNetworkProxy.setApplicationProxy(previous)
            server.shutdown()
            server.server_close()
            thread.join()

    def test_occupied_port_recovers_from_unrelated_http_service(self):
        import json
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import patch
        from tests.helpers import sample_records, write_jsonl

        class Unrelated(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"other_app": true}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Unrelated)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.dict(os.environ, {"CODEX_HOME": folder, "LOCALAPPDATA": folder, "CODEX_MONITOR_CONFIG": ""}),
            ):
                write_jsonl(Path(folder) / "sessions" / "one.jsonl", sample_records())
                self.widget.url = f"http://127.0.0.1:{server.server_port}"
                original = self.widget.url
                from codex_glass.desktop.dashboard import NativeDashboard

                dashboard = NativeDashboard(self.widget, url=original, persist=False, auto_fetch=False)
                self.widget.dashboard = dashboard
                self.widget.start_backend = True
                self.widget.fetch()
                deadline = time.monotonic() + 8
                try:
                    while self.widget.data.get("total", {}).get("total_tokens") != 195 and time.monotonic() < deadline:
                        QTest.qWait(100)
                        self.widget.fetch()
                    self.assertEqual(195, self.widget.data.get("total", {}).get("total_tokens"))
                    self.assertNotEqual(original, self.widget.url)
                    self.assertEqual(self.widget.url, dashboard.url)
                    self.assertTrue((Path(folder) / "codex-monitor.sqlite3").exists())
                finally:
                    dashboard.hide()
                    dashboard.deleteLater()
                    process = self.widget.backend_process
                    if process is not None:
                        process.terminate()
                        process.wait(timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def _run_main_lifecycle_probe(self, code):
        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root)
        env["QT_QPA_PLATFORM"] = "windows"
        isolation = """
import os
from codex_glass.desktop import widget as _probe_module
from PyQt5.QtNetwork import QLocalServer as _Server, QLocalSocket as _Socket
_suffix = '-test-' + str(os.getpid())
class _IsolatedServer(_Server):
    @staticmethod
    def removeServer(name):
        return _Server.removeServer(name + _suffix)
    def listen(self, name):
        return super().listen(name + _suffix)
class _IsolatedSocket(_Socket):
    def connectToServer(self, name, *args):
        return super().connectToServer(name + _suffix, *args)
_probe_module.QLocalServer = _IsolatedServer
_probe_module.QLocalSocket = _IsolatedSocket
"""
        return subprocess.run(
            [sys.executable, "-c", isolation + textwrap.dedent(code)],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_capture_main_disposes_root_widget_before_qapplication(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shot = Path(temp_dir) / "capture.png"
            result = self._run_main_lifecycle_probe(
                f"""
                import os
                import sys
                from pathlib import Path
                from PyQt5 import sip
                from codex_glass.desktop import widget as module
                from tests.glass_fixtures import telemetry

                created = []

                class ReadyWidget(module.GlassWidget):
                    def __init__(self, url, start_backend, persist):
                        super().__init__(url, start_backend, persist=False)
                        self.set_motion(False)
                        self.apply_data(telemetry())
                        created.append(self)

                module.GlassWidget = ReadyWidget
                sys.argv = [
                    "desktop_widget.py",
                    "--url",
                    "http://127.0.0.1:1",
                    "--no-start-backend",
                    "--scope",
                    "all",
                    "--capture",
                    {str(shot)!r},
                ]
                result = module.main()
                if result != 0 or not Path({str(shot)!r}).is_file():
                    os._exit(20)
                if not sip.isdeleted(created[0]):
                    os._exit(21)
                """
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_normal_main_disposes_host_and_surface_before_qapplication(self):
        result = self._run_main_lifecycle_probe(
            """
            import os
            import sys
            from PyQt5 import sip
            from PyQt5.QtCore import QTimer
            from PyQt5.QtWidgets import QApplication
            from codex_glass.desktop import widget as module
            from codex_glass.desktop.components import host as host_module

            widgets = []
            hosts = []
            OriginalHost = host_module.ScaledSurfaceHost

            class LifecycleApplication(QApplication):
                def exec_(self):
                    self.aboutToQuit.connect(self.verify_shutdown)
                    return super().exec_()

                def verify_shutdown(self):
                    if any(not sip.isdeleted(item) for item in self.topLevelWidgets()):
                        os._exit(32)

            class ReadyWidget(module.GlassWidget):
                def __init__(self, url, start_backend, persist):
                    super().__init__(url, start_backend, persist=False)
                    widgets.append(self)
                    QTimer.singleShot(50, QApplication.quit)

            class RecordedHost(OriginalHost):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    hosts.append(self)

            module.GlassWidget = ReadyWidget
            module.QApplication = LifecycleApplication
            host_module.ScaledSurfaceHost = RecordedHost
            sys.argv = [
                "desktop_widget.py",
                "--url",
                "http://127.0.0.1:1",
                "--no-start-backend",
            ]
            result = module.main()
            if result != 0 or not widgets or not hosts:
                os._exit(30)
            if not sip.isdeleted(hosts[0]) or not sip.isdeleted(widgets[0]):
                os._exit(31)
            """
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_normal_main_disposes_open_dashboard_before_main_host(self):
        result = self._run_main_lifecycle_probe(
            """
            import os
            import sys
            from PyQt5 import sip
            from PyQt5.QtCore import QTimer
            from PyQt5.QtWidgets import QApplication
            from codex_glass.desktop import dashboard as dashboard_module
            from codex_glass.desktop import widget as module

            dashboards = []
            OriginalDashboard = dashboard_module.NativeDashboard

            class LifecycleApplication(QApplication):
                def exec_(self):
                    self.aboutToQuit.connect(self.verify_shutdown)
                    return super().exec_()

                def verify_shutdown(self):
                    if not dashboards:
                        os._exit(40)
                    dashboard = dashboards[0]
                    if not sip.isdeleted(dashboard.scaled_host) or not sip.isdeleted(dashboard):
                        os._exit(41)

            class QuietDashboard(OriginalDashboard):
                def __init__(self, owner, url):
                    super().__init__(owner, url, persist=False, auto_fetch=False)
                    self.set_motion(False)
                    dashboards.append(self)

            class DashboardWidget(module.GlassWidget):
                def __init__(self, url, start_backend, persist):
                    super().__init__(url, start_backend, persist=False)

                    def open_then_quit():
                        self.open_dashboard()
                        QTimer.singleShot(50, QApplication.quit)

                    QTimer.singleShot(50, open_then_quit)

            dashboard_module.NativeDashboard = QuietDashboard
            module.GlassWidget = DashboardWidget
            module.QApplication = LifecycleApplication
            sys.argv = [
                "desktop_widget.py",
                "--url",
                "http://127.0.0.1:1",
                "--no-start-backend",
            ]
            result = module.main()
            if result != 0 or not dashboards:
                os._exit(40)
            dashboard = dashboards[0]
            dashboard_host = dashboard.scaled_host
            if not sip.isdeleted(dashboard_host) or not sip.isdeleted(dashboard):
                os._exit(41)
            """
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_switches_preserve_geometry_and_display_all_models(self):
        self.load()
        w = self.widget
        size = w.size()
        chart = w.chart.geometry()
        for i, scope in enumerate(("today", "last_5_hours", "all")):
            QTest.mouseClick(w.group.buttons()[i], Qt.LeftButton)
            self.assertEqual(scope, w.scope)
            self.assertEqual(size, w.size())
            self.assertEqual(chart, w.chart.geometry())
        self.assertEqual(4, len(w.rows))
        self.assertIn("47,965.38", w.cost.text())
        w.range_box.setCurrentIndex(0)
        self.assertEqual(7, len(w.chart.target_values))
        self.assertIn("47,965.38", w.cost.text())

    def test_appearance_mini_tray_and_settings(self):
        self.load()
        w = self.widget
        self.assertTrue(hasattr(w, "appearance"), "appearance popover missing")
        w.appearance.opacity_slider.setValue(75)
        self.assertEqual(75, w.transparency)
        w.top_action.setChecked(False)
        self.assertFalse(w.windowFlags() & Qt.WindowStaysOnTopHint)
        w.top_action.setChecked(True)
        self.assertTrue(w.windowFlags() & Qt.WindowStaysOnTopHint)
        original = w.size()
        hero = w.cost.geometry()
        w.toggle_compact()
        QTest.qWait(20)
        self.assertLess(w.height(), 210)
        w.toggle_compact()
        self.assertEqual(original, w.size())
        self.assertEqual(hero, w.cost.geometry())
        w.hide_to_tray()
        self.assertFalse(w.isVisible())
        w.restore()
        self.assertTrue(w.isVisible())

    def test_chart_animates_real_changes_and_repeated_data_does_not_reanimate(self):
        self.load()
        w = self.widget
        first = w.chart.display_values[:]
        data = telemetry()
        data["charts"]["today"]["values"] = [v * 1.5 for v in data["charts"]["today"]["values"]]
        w.apply_data(data)
        self.assertTrue(w.chart.animation.state())
        QTest.qWait(180)
        self.assertNotEqual(first, w.chart.display_values)
        QTest.qWait(650)
        self.assertEqual(data["charts"]["today"]["values"], w.chart.display_values)
        w.apply_data(copy.deepcopy(data))
        self.assertFalse(w.chart.animation.state())

    def test_unknown_cost_is_not_presented_as_free_and_details_are_available(self):
        self.load()
        w = self.widget
        w.change_scope("all")
        row = w.rows["unknown-model"]
        self.assertTrue(row.unpriced)
        w.select_model("gpt-6-astra")
        self.assertIn("输入", w.detail_popup.text())

    def test_flowing_material_changes_background_without_changing_data(self):
        self.load()
        w = self.widget
        w.set_motion(False)
        before = w.grab().toImage()
        original = w.cost.text()
        w.flow_phase = 2.0
        w.update()
        QTest.qWait(20)
        after = w.grab().toImage()
        self.assertNotEqual(before.pixelColor(210, 218), after.pixelColor(210, 218))
        self.assertEqual(original, w.cost.text())

    def test_native_material_does_not_leave_a_rectangular_backplate(self):
        from PyQt5.QtWidgets import QWidget
        from PyQt5.QtGui import QColor

        backdrop = QWidget()
        backdrop.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        backdrop.setStyleSheet("background:#f0f3f8;")
        backdrop.setGeometry(60, 60, 550, 780)
        backdrop.show()
        self.addCleanup(backdrop.close)
        w = self.widget
        w.move(90, 90)
        w.raise_()
        QTest.qWait(250)
        for compact in (False, True):
            if compact:
                w.toggle_compact()
            QTest.qWait(100)
            shot = w.screen().grabWindow(0, w.x(), w.y(), w.width(), w.height()).toImage()
            for x, y in ((1, 40), (12, 12), (w.width() - 2, 40), (12, w.height() - 13)):
                self.assertEqual(
                    QColor("#f0f3f8").rgb(),
                    shot.pixelColor(x, y).rgb(),
                    f"opaque backplate outside rounded surface at {x},{y}",
                )

    def test_curve_has_visible_fading_area_not_just_a_line(self):
        from codex_glass.desktop.components.chart import GlassChart

        chart = GlassChart()
        chart.setAttribute(Qt.WA_TranslucentBackground)
        chart.resize(380, 137)
        chart.set_series([10, 10, 10], animate=False)
        frame = chart.grab().toImage()
        self.assertGreater(frame.pixelColor(120, 35).alpha(), 110)
        self.assertGreater(frame.pixelColor(120, 35).alpha(), frame.pixelColor(120, 90).alpha() + 50)
        mini = GlassChart(mini=True)
        mini.setAttribute(Qt.WA_TranslucentBackground)
        mini.resize(148, 38)
        mini.set_series([10, 10, 10], animate=False)
        self.assertGreater(mini.grab().toImage().pixelColor(50, 15).alpha(), 70)

    def test_packaged_backend_is_independent_of_widget_lifetime(self):
        import tempfile, os, sys
        from unittest.mock import patch

        w = self.widget
        w.start_backend = True
        w.url = "http://127.0.0.1:8081"
        with (
            tempfile.TemporaryDirectory() as runtime,
            patch.dict(os.environ, {"LOCALAPPDATA": runtime}),
            patch.object(sys, "frozen", True, create=True),
        ):
            # Boundary spy prevents launching another GUI/backend during this test.
            with patch("codex_glass.desktop.widget.subprocess.Popen") as launch:
                w.launch_backend()
                args, kwargs = launch.call_args
                self.assertIn("--backend", args[0])
                self.assertEqual("1", kwargs.get("env", {}).get("PYINSTALLER_RESET_ENVIRONMENT"))

    def test_backend_crash_retries_after_cooldown_without_duplicate_running_server(self):
        import tempfile, os
        from unittest.mock import patch, Mock

        w = self.widget
        w.start_backend = True
        w.url = "http://127.0.0.1:8081"
        child = Mock()
        child.poll.return_value = 1
        with (
            tempfile.TemporaryDirectory() as runtime,
            patch.dict(os.environ, {"LOCALAPPDATA": runtime}),
            patch("codex_glass.desktop.widget.subprocess.Popen", return_value=child) as launch,
        ):
            with patch("codex_glass.desktop.widget.time.monotonic", return_value=100):
                w.launch_backend()
            args, kwargs = launch.call_args
            project_root = Path(__file__).resolve().parents[1]
            self.assertEqual([sys.executable, str(project_root / "web_dashboard.py"), "--no-browser"], args[0][:3])
            self.assertEqual(["--host", "127.0.0.1", "--port"], args[0][3:6])
            self.assertEqual(f"http://127.0.0.1:{args[0][6]}", w.url)
            self.assertEqual(str(project_root), kwargs["cwd"])
            with patch("codex_glass.desktop.widget.time.monotonic", return_value=101):
                w.launch_backend()
            self.assertEqual(1, launch.call_count)
            with patch("codex_glass.desktop.widget.time.monotonic", return_value=131):
                w.launch_backend()
            self.assertEqual(2, launch.call_count)
            child.poll.return_value = None
            with patch("codex_glass.desktop.widget.time.monotonic", return_value=200):
                w.launch_backend()
            self.assertEqual(2, launch.call_count)

    def test_last_model_row_does_not_duplicate_quota_separator(self):
        self.load()
        w = self.widget
        w.set_motion(False)
        row = w.rows["gpt-5.6-terra"]
        point = row.mapTo(w, row.rect().bottomLeft())
        frame = w.grab().toImage()
        a = frame.pixelColor(140, point.y())
        b = frame.pixelColor(140, point.y() - 3)
        self.assertLessEqual(max(abs(x - y) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])), 3)
