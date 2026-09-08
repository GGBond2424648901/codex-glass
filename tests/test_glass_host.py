import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QPushButton, QSystemTrayIcon, QWidget


class _ButtonSurface(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(200, 100)
        self.button = QPushButton("live", self)
        self.button.setGeometry(50, 30, 80, 32)


class _RoundedSurface(QWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(160, 100)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(30, 120, 220, 255))
        painter.drawRoundedRect(QRectF(4, 4, 152, 92), 28, 28)


class _LifecycleSurface(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(120, 80)
        self.motion_timer = QTimer(self)
        self.close_requested = False

    def showEvent(self, event):
        self.motion_timer.start(50)
        super().showEvent(event)

    def hideEvent(self, event):
        self.motion_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.close_requested = True
        event.ignore()


class GlassHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def tearDown(self):
        for widget in QApplication.topLevelWidgets():
            if widget.windowTitle() in ("", "Codex Glass"):
                widget.hide()
        QTest.qWait(10)

    def make_host(self, surface=None, **kwargs):
        from glass_host import ScaledSurfaceHost

        surface = surface or _ButtonSurface()
        host = ScaledSurfaceHost(surface, **kwargs)
        host.setAttribute(Qt.WA_DontShowOnScreen)
        host.show()
        QTest.qWait(20)
        self.addCleanup(host.hide)
        self.addCleanup(host.deleteLater)
        return surface, host

    def test_scaled_host_module_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec("glass_host"))

    def test_set_scale_clamps_resizes_and_emits_float(self):
        surface, host = self.make_host(scale=1.0, min_scale=0.7, max_scale=1.5)
        changed = QSignalSpy(host.scaleChanged)

        host.set_scale(2.0)

        self.assertEqual((300, 150), (host.width(), host.height()))
        self.assertAlmostEqual(1.5, host.scale)
        self.assertTrue(host.windowFlags() & Qt.FramelessWindowHint)
        self.assertEqual(1, len(changed))
        self.assertIsInstance(changed[0][0], float)

    def test_east_edge_drag_scales_content_without_distorting_ratio(self):
        surface, host = self.make_host(scale=1.0)
        viewport = host.viewport()

        QTest.mousePress(viewport, Qt.LeftButton, pos=QPoint(host.width() - 2, host.height() // 2))
        # Keep the synthetic move inside the offscreen viewport. Native mouse
        # grabs also deliver outward moves, but the offscreen plugin does not.
        QTest.mouseMove(viewport, QPoint(host.width() - 42, host.height() // 2), 10)
        QTest.mouseRelease(viewport, Qt.LeftButton, pos=QPoint(host.width() - 42, host.height() // 2))

        self.assertLess(host.width(), 200)
        self.assertAlmostEqual(2.0, host.width() / host.height(), places=1)
        self.assertAlmostEqual(host.width() / 200.0, host.scale, places=2)

    def test_north_west_corner_drag_preserves_opposite_corner(self):
        surface, host = self.make_host(scale=1.0)
        host.move(300, 260)
        original_anchor = host.geometry().bottomRight()
        emitted_positions = []
        host.scaleChanged.connect(lambda value: emitted_positions.append(QPoint(host.pos())))
        viewport = host.viewport()

        QTest.mousePress(viewport, Qt.LeftButton, pos=QPoint(2, 2))
        QTest.mouseMove(viewport, QPoint(42, 22), 10)
        QTest.mouseRelease(viewport, Qt.LeftButton, pos=QPoint(42, 22))

        self.assertLess(host.scale, 1.0)
        self.assertLessEqual((host.geometry().bottomRight() - original_anchor).manhattanLength(), 3)
        self.assertEqual(host.pos(), emitted_positions[-1])

    def test_clicks_are_mapped_to_live_controls_after_scaling(self):
        surface, host = self.make_host(scale=0.75)
        clicks = []
        surface.button.clicked.connect(lambda: clicks.append(True))
        centre = surface.button.mapTo(surface, surface.button.rect().center())
        mapped = host.mapFromScene(centre.x(), centre.y())

        QTest.mouseClick(host.viewport(), Qt.LeftButton, pos=mapped)

        self.assertEqual([True], clicks)

    def test_host_background_stays_transparent_outside_rounded_surface(self):
        surface, host = self.make_host(_RoundedSurface(), scale=1.0)

        image = host.grab().toImage()

        self.assertEqual(0, image.pixelColor(0, 0).alpha())
        self.assertGreater(image.pixelColor(host.width() // 2, host.height() // 2).alpha(), 200)

    def test_hiding_and_restoring_host_drives_surface_lifecycle(self):
        surface, host = self.make_host(_LifecycleSurface(), scale=1.0)
        self.assertTrue(surface.motion_timer.isActive())

        host.hide()
        QTest.qWait(10)
        self.assertFalse(surface.motion_timer.isActive())

        host.show()
        QTest.qWait(10)
        self.assertTrue(surface.motion_timer.isActive())

    def test_minimizing_host_pauses_surface_motion(self):
        surface, host = self.make_host(_LifecycleSurface(), scale=1.0)

        host.showMinimized()
        QTest.qWait(20)

        self.assertFalse(surface.motion_timer.isActive())

    def test_closing_host_delegates_to_surface_close_policy(self):
        surface, host = self.make_host(_LifecycleSurface(), scale=1.0)

        host.close()
        QTest.qWait(10)

        self.assertTrue(surface.close_requested)


class GlassWidgetHostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from frosted_desktop import GlassWidget
        from glass_host import ScaledSurfaceHost

        self.widget = GlassWidget(url="http://127.0.0.1:1", start_backend=False, persist=False)
        self.widget.timer.stop()
        self.widget.fetch = lambda: None
        self.host = ScaledSurfaceHost(self.widget, scale=0.8)
        self.host.setAttribute(Qt.WA_DontShowOnScreen)
        self.host.show()
        QTest.qWait(20)

    def tearDown(self):
        self.widget.timer.stop()
        self.widget.flow_timer.stop()
        self.widget.tray.hide()
        self.widget.appearance.hide()
        self.widget.detail_popup.hide()
        popup = getattr(self.widget.range_box, "popup_surface", None)
        if popup is not None:
            popup.hide()
        self.host.hide()
        self.host.deleteLater()
        QTest.qWait(20)

    def test_compact_mode_keeps_an_explicit_expand_button_and_syncs_host(self):
        QTest.mouseClick(self.widget.collapse, Qt.LeftButton)

        self.assertTrue(self.widget.compact_mode)
        self.assertTrue(self.widget.expand.isVisible())
        self.assertEqual(round(188 * 0.8), self.host.height())

        QTest.mouseClick(self.widget.expand, Qt.LeftButton)
        self.assertFalse(self.widget.compact_mode)
        self.assertEqual(round(686 * 0.8), self.host.height())

    def test_expand_from_screen_bottom_clamps_full_host_inside_screen(self):
        QTest.mouseClick(self.widget.collapse, Qt.LeftButton)
        area = QApplication.primaryScreen().availableGeometry()
        self.host.move(area.right() - self.host.width() + 1, area.bottom() - self.host.height() + 1)

        QTest.mouseClick(self.widget.expand, Qt.LeftButton)

        self.assertTrue(area.contains(self.host.geometry()))

    def test_appearance_popup_clamps_all_edges_from_bottom_right_mini_host(self):
        QTest.mouseClick(self.widget.collapse, Qt.LeftButton)
        area = QApplication.primaryScreen().availableGeometry()
        self.host.move(area.right() - self.host.width() + 1, area.bottom() - self.host.height() + 1)

        self.widget.show_appearance()
        QTest.qWait(10)

        self.assertTrue(area.contains(self.widget.appearance.geometry()))

    def test_background_flow_runs_at_same_rate_with_six_fps_timer(self):
        before = self.widget.flow_phase

        self.widget.flow_tick()

        change = self.widget.flow_phase - before
        self.assertEqual(160, self.widget.flow_timer.interval())
        self.assertAlmostEqual(0.05, change * 1000 / self.widget.flow_timer.interval(), places=6)

    def test_topmost_tray_hide_and_restore_target_the_host(self):
        self.widget.toggle_top(False)
        self.assertFalse(self.host.windowFlags() & Qt.WindowStaysOnTopHint)

        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            self.widget.hide_to_tray()
        self.assertFalse(self.host.isVisible())

        self.widget.restore()
        self.assertTrue(self.host.isVisible())

    def test_hiding_widget_also_closes_unparented_choice_popup(self):
        self.widget.range_box.show()
        self.widget.range_box.showPopup()
        popup = self.widget.range_box.popup_surface
        self.assertTrue(popup.isVisible())

        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            self.widget.hide_to_tray()

        self.assertFalse(popup.isVisible())

    def test_hiding_host_pauses_widget_polling_until_restore(self):
        self.widget.timer.start()

        self.host.hide()
        QTest.qWait(10)
        self.assertFalse(self.widget.timer.isActive())

        self.widget.restore()
        QTest.qWait(10)
        self.assertTrue(self.widget.timer.isActive())

    def test_widget_popups_are_unparented_glass_windows_and_screen_clamped(self):
        from glass_popups import GlassInfoPopup, GlassMenu

        self.assertIsInstance(self.widget.menu, GlassMenu)
        self.assertIsInstance(self.widget.detail_popup, GlassInfoPopup)
        self.assertTrue(self.widget.appearance.isWindow())
        self.widget.range_box.show()
        self.widget.range_box.showPopup()
        popup = self.widget.range_box.popup_surface
        QTest.qWait(10)
        screen = QApplication.screenAt(popup.geometry().center()) or QApplication.primaryScreen()
        self.assertTrue(popup.isWindow())
        self.assertTrue(screen.availableGeometry().contains(popup.geometry()))

    def test_range_popup_keeps_combobox_selection_signals(self):
        changed = QSignalSpy(self.widget.range_box.currentIndexChanged)
        self.widget.range_box.show()
        self.widget.range_box.showPopup()
        popup = self.widget.range_box.popup_surface

        QTest.mouseClick(popup.choices[0], Qt.LeftButton)
        QTest.mouseClick(popup.apply_button, Qt.LeftButton)

        self.assertEqual(0, self.widget.range_box.currentIndex())
        self.assertEqual("近7天", self.widget.range_box.currentText())
        self.assertEqual(1, len(changed))

    def test_open_dashboard_uses_native_lazy_entrypoint(self):
        calls = []
        module = types.ModuleType("native_dashboard")

        def open_dashboard(owner):
            calls.append(owner)
            return "native-dashboard"

        module.open_dashboard = open_dashboard
        with patch.dict(sys.modules, {"native_dashboard": module}):
            result = self.widget.open_dashboard()

        self.assertEqual("native-dashboard", result)
        self.assertEqual([self.widget], calls)


if __name__ == "__main__":
    unittest.main()
