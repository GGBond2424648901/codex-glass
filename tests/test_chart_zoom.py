import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent, QWheelEvent
    from PyQt5.QtTest import QSignalSpy
    from PyQt5.QtWidgets import QApplication

    from glass_chart import GlassChart

    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "optional desktop dependency PyQt5 not installed")
class GlassChartZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chart = GlassChart()
        self.chart.setAttribute(Qt.WA_DontShowOnScreen)
        self.chart.resize(500, 220)
        self.labels = [f"2026-09-08T10:{i:02d}:00" for i in range(10)]
        self.chart.set_series(range(10), self.labels, animate=False)
        self.chart.show()

    def tearDown(self):
        self.chart.close()
        self.chart.deleteLater()

    @staticmethod
    def wheel(widget, x, y, delta):
        pos = QPointF(x, y)
        global_pos = QPointF(widget.mapToGlobal(QPoint(x, y)))
        event = QWheelEvent(
            pos,
            global_pos,
            QPoint(),
            QPoint(0, delta),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        event.ignore()
        QApplication.sendEvent(widget, event)
        return event

    def test_actual_wheel_event_zooms_in_and_out_around_cursor(self):
        before = self.chart.view_range
        event = self.wheel(self.chart, 250, 100, 120)
        zoomed = self.chart.view_range
        self.assertTrue(event.isAccepted())
        self.assertLess(zoomed[1] - zoomed[0], before[1] - before[0])
        self.assertAlmostEqual(4.5, sum(zoomed) / 2, places=1)

        self.wheel(self.chart, 250, 100, -120)
        self.assertAlmostEqual(0.0, self.chart.view_range[0], places=5)
        self.assertAlmostEqual(9.0, self.chart.view_range[1], places=5)

    def test_anchor_bounds_minimum_span_and_reset(self):
        self.chart.zoom_at(0.0, 20)
        self.assertEqual(0.0, self.chart.view_range[0])
        self.assertAlmostEqual(4.0, self.chart.view_range[1] - self.chart.view_range[0], places=5)
        self.chart.reset_zoom()
        self.chart.zoom_at(1.0, 20)
        self.assertEqual(9.0, self.chart.view_range[1])
        self.assertAlmostEqual(4.0, self.chart.view_range[1] - self.chart.view_range[0], places=5)
        self.chart.reset_zoom()
        self.assertEqual((0.0, 9.0), self.chart.view_range)

    def test_public_view_range_can_seed_recent_window_then_zoom_out(self):
        self.chart.set_view_range(3, 9)
        self.assertEqual((3.0, 9.0), self.chart.view_range)
        self.chart.zoom_at(0.5, -1)
        self.assertGreater(self.chart.view_range[1] - self.chart.view_range[0], 6.0)
        self.assertLessEqual(self.chart.view_range[1], 9.0)

    def test_rolling_labels_keep_historical_viewport_anchor(self):
        self.chart.zoom_at(0.5, 2)
        old_range = self.chart.view_range
        shifted = self.labels[1:] + ["2026-09-08T10:10:00"]
        self.chart.set_series([v + 10 for v in range(10)], shifted, animate=False)
        self.assertAlmostEqual(old_range[0] - 1, self.chart.view_range[0], places=5)
        self.assertAlmostEqual(old_range[1] - 1, self.chart.view_range[1], places=5)

        unrelated = [f"2026-09-09T12:{i:02d}:00" for i in range(10)]
        self.chart.set_series(range(10), unrelated, animate=False)
        self.assertEqual((0.0, 9.0), self.chart.view_range)

    def test_downward_animation_keeps_old_display_curve_inside_plot(self):
        self.chart.set_series([100] * 10, self.labels, animate=False)
        self.chart.set_series([1] * 10, self.labels, animate=True)
        rect = self.chart.plot_rect()
        peak = self.chart._visible_peak()
        _, points = self.chart.path_for(self.chart.display_values, rect, list(range(10)), peak)
        self.assertGreaterEqual(peak, 100.0)
        self.assertTrue(all(rect.top() <= point.y() <= rect.bottom() for point in points))

    def test_range_signal_and_tooltip_use_real_historical_samples(self):
        spy = QSignalSpy(self.chart.rangeChanged)
        self.chart.zoom_at(0.5, 2)
        self.assertGreaterEqual(len(spy), 1)
        self.assertIn(spy[-1][0], self.labels)
        self.assertIn(spy[-1][1], self.labels)

        rect = self.chart.plot_rect()
        event = QMouseEvent(
            QMouseEvent.MouseMove,
            rect.center(),
            self.chart.mapToGlobal(rect.center().toPoint()),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        self.chart.mouseMoveEvent(event)
        self.assertIn(self.labels[self.chart.hover_index], self.chart.info_popup.text())
        self.assertIn(f"{self.chart.target_values[self.chart.hover_index]:,.0f}", self.chart.info_popup.text())
        self.assertNotIn("现在", self.chart.info_popup.text())

    def test_mini_chart_ignores_wheel_for_parent_scrolling(self):
        mini = GlassChart(mini=True)
        mini.setAttribute(Qt.WA_DontShowOnScreen)
        mini.resize(148, 38)
        mini.set_series(range(10), self.labels, animate=False)
        mini.show()
        event = self.wheel(mini, 70, 18, 120)
        self.assertFalse(event.isAccepted())
        self.assertEqual((0.0, 9.0), mini.view_range)
        mini.close()
        mini.deleteLater()

    def test_unlabelled_series_still_uses_its_full_sample_domain(self):
        chart = GlassChart(mini=True)
        chart.set_series(range(7), animate=False)
        self.assertEqual((0.0, 6.0), chart.view_range)
        chart.deleteLater()

    def test_wheel_outside_plot_is_not_consumed_and_double_click_resets(self):
        event = self.wheel(self.chart, 250, self.chart.height() - 2, 120)
        self.assertFalse(event.isAccepted())
        self.chart.zoom_at(0.5, 2)
        event = QMouseEvent(
            QMouseEvent.MouseButtonDblClick,
            self.chart.plot_rect().center(),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        self.chart.mouseDoubleClickEvent(event)
        self.assertEqual((0.0, 9.0), self.chart.view_range)


if __name__ == "__main__":
    unittest.main()
