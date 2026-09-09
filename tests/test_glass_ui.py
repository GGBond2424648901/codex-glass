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

    def _run_main_lifecycle_probe(self, code):
        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root)
        env["QT_QPA_PLATFORM"] = "windows"
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
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
            self.assertEqual([sys.executable, str(project_root / "web_dashboard.py"), "--no-browser"], args[0])
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
