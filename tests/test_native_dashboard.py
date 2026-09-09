import unittest
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from tests.glass_fixtures import telemetry


class NativeDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from codex_glass.desktop.dashboard import NativeDashboard

        self.w = NativeDashboard(url="http://127.0.0.1:1", persist=False, auto_fetch=False)
        self.w.setAttribute(Qt.WA_DontShowOnScreen)
        self.w.show()
        self.w.apply_data(telemetry())
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.w.timer.stop()
        self.w.hide()
        self.w.deleteLater()
        QTest.qWait(10)

    def test_all_fifteen_states_reuse_shell_and_only_five_pages(self):
        w = self.w
        rail = w.nav_buttons["overview"].geometry()
        segment = w.scope_tabs.geometry()
        self.assertEqual(5, len(w.pages))
        for page in ("overview", "models", "history", "quota", "settings"):
            for scope in ("today", "last_5_hours", "all"):
                w.change_page(page)
                w.change_scope(scope)
                self.assertTrue(w.pages[page].isVisible())
                self.assertEqual(rail, w.nav_buttons["overview"].geometry())
                self.assertEqual(segment, w.scope_tabs.geometry())

    def test_scope_changes_overview_values_and_preserves_quota(self):
        w = self.w
        w.change_scope("last_5_hours")
        self.assertIn("46.82", w.pages["overview"].metrics[0].text())
        before = w.pages["quota"].remaining.text()
        w.change_scope("all")
        self.assertIn("47,965.38", w.pages["overview"].metrics[0].text())
        self.assertEqual(before, w.pages["quota"].remaining.text())

    def test_model_search_and_selected_structure(self):
        w = self.w
        w.change_scope("all")
        w.change_page("models")
        page = w.pages["models"]
        page.search.setText("sol")
        self.app.processEvents()
        self.assertEqual(1, page.table.rowCount())
        self.assertEqual("gpt-5.6-sol", page.visible_rows[0]["name"])
        page.search.clear()
        self.app.processEvents()
        self.assertTrue(any(r["unpriced"] for r in page.visible_rows))

    def test_settings_change_material_and_hidden_window_stops_motion(self):
        w = self.w
        page = w.pages["settings"]
        page.opacity.setValue(80)
        self.assertEqual(80, w.transparency)
        w.hide()
        self.assertFalse(w.flow_timer.isActive())

    def test_history_payload_updates_real_table_and_pagination(self):
        w = self.w
        p = w.pages["history"]
        w.change_page("history")
        p.apply_history(
            {
                "events": [
                    {
                        "timestamp": "2026-09-08 20:00:00",
                        "model": "gpt-6-astra",
                        "cwd": "project",
                        "tokens": {"total": 10},
                        "cost_usd": {"total": 0.2},
                    }
                ],
                "total": 21,
                "offset": 0,
                "limit": 7,
            }
        )
        self.assertEqual(1, p.table.rowCount())
        self.assertTrue(p.next_button.isEnabled())
        self.assertFalse(p.prev_button.isEnabled())
        self.assertIn("21", p.page_label.text())

    def test_corners_are_transparent_and_no_design_image_is_rendered(self):
        w = self.w
        w.set_motion(False)
        frame = w.grab().toImage()
        self.assertEqual(0, frame.pixelColor(0, 0).alpha())
        self.assertGreater(frame.pixelColor(300, 300).alpha(), 0)

    def test_navigation_and_headers_have_shared_material_renderers(self):
        from codex_glass.desktop.controls import NavigationButton, GlassHeader

        self.assertIsInstance(self.w.nav_buttons["overview"], NavigationButton)
        self.assertEqual("总览", self.w.nav_buttons["overview"].text())
        for name in ("overview", "models", "history"):
            self.assertIsInstance(self.w.pages[name].table.horizontalHeader(), GlassHeader)

    def test_background_and_chart_motion_are_independent(self):
        self.w.set_motion(False)
        self.w.pages["settings"].switch_changed("chart", True)
        self.assertTrue(self.w.pages["overview"].chart.motion)
        self.w.pages["settings"].switch_changed("chart", False)
        self.assertFalse(self.w.pages["overview"].chart.motion)

    def test_settings_reflect_state_and_import_stays_disabled_during_job(self):
        w = self.w
        w.transparency = 80
        w.motion = False
        w.chart_motion = False
        w.topmost = False
        w.can_import = True
        w.jobs["import"] = object()
        w.render_pages()
        p = w.pages["settings"]
        self.assertEqual("80%", p.opacity_value.text())
        self.assertEqual(80, p.opacity.value())
        self.assertFalse(p.switches["motion"].isChecked())
        self.assertFalse(p.switches["chart"].isChecked())
        self.assertFalse(p.switches["top"].isChecked())
        self.assertFalse(p.import_button.isEnabled())
        self.assertFalse(w.pages["history"].import_button.isEnabled())

    def test_live_poll_does_not_rebuild_hidden_pages(self):
        from unittest.mock import patch

        p = self.w.pages["models"]
        with patch.object(p, "render", wraps=p.render) as render:
            self.w.apply_data(telemetry())
            render.assert_not_called()
            self.w.change_page("models")
            render.assert_called_once()

    def test_index_failure_preserves_facts_but_is_not_live(self):
        data = telemetry()
        data["index"] = {"status": "error", "last_error": "scan failed"}
        self.w.apply_data(data)
        self.assertFalse(self.w.connected)
        self.assertIn("异常", self.w.status_label.text())
        self.assertFalse(self.w.pages["overview"].chart.live)
        self.w.change_page("settings")
        self.assertIn("异常", self.w.pages["settings"].index_status.text())

    def test_quota_fetch_failure_never_uses_imported_summary_snapshot(self):
        from unittest.mock import patch

        data = telemetry()
        w = self.w
        w.auto_fetch = True

        def run(key, operation):
            if key == "data":
                w.apply_data(operation())

        with (
            patch.object(w, "get_json", side_effect=[data, RuntimeError("widget unavailable")]),
            patch.object(w, "run_job", side_effect=run),
        ):
            w.fetch()
        self.assertEqual({"limits": []}, w.data["rate_limits"])

    def test_reopening_page_choices_does_not_retain_old_widget_trees(self):
        page = self.w.pages["history"]
        for i in range(20):
            page.choose_models()
            page.popup.hide()
        self.assertLessEqual(len(page._glass_popup_children), 1)

    def test_transport_failure_stops_live_indicator(self):
        self.w.job_finished("data", None, "connection refused")
        self.assertFalse(self.w.pages["overview"].chart.live)

    def test_initial_loading_is_not_measured_zero(self):
        from codex_glass.desktop.dashboard import NativeDashboard

        w = NativeDashboard(persist=False, auto_fetch=False)
        self.assertEqual(["—"] * 4, [v.text() for v in w.pages["overview"].metrics])
        w.deleteLater()

    def test_quota_observation_freshness_is_explicit(self):
        from codex_glass.desktop.pages import snapshot_status
        from datetime import datetime, timedelta

        self.assertEqual("快照较旧", snapshot_status({"observed_at": (datetime.now() - timedelta(days=6)).isoformat()}))
        self.assertEqual("最近快照", snapshot_status({"observed_at": datetime.now().isoformat()}))
        self.assertEqual("时间未知", snapshot_status({}))

    def test_import_retries_only_lease_contention(self):
        from codex_glass.desktop.dashboard import retry_import
        from unittest.mock import Mock

        action = Mock(side_effect=[RuntimeError("Another process holds the indexing lease"), {"ok": True}])
        pause = Mock()
        self.assertEqual({"ok": True}, retry_import(action, pause=pause))
        self.assertEqual(2, action.call_count)
        pause.assert_called_once()
        with self.assertRaises(ValueError):
            retry_import(Mock(side_effect=ValueError("invalid")), pause=pause)

    def test_header_tooltips_use_shared_glass_popup(self):
        from PyQt5.QtCore import QEvent, QPoint
        from PyQt5.QtGui import QHelpEvent
        from codex_glass.desktop.components.tooltips import install_glass_tooltips

        router = install_glass_tooltips(self.w)
        button = self.w.refresh
        event = QHelpEvent(QEvent.ToolTip, QPoint(10, 10), QPoint(400, 300))
        self.app.sendEvent(button, event)
        self.assertTrue(router.popup.isVisible())
        self.assertEqual(button.toolTip(), router.popup.text())
        self.assertIsNone(router.popup.parentWidget())
        router.dismiss()

    def test_native_import_workflow_merges_and_repeated_import_deduplicates(self):
        import tempfile, time
        from pathlib import Path
        from unittest.mock import patch
        from PyQt5.QtWidgets import QDialog
        from codex_glass.core.usage import MonitorConfig
        from codex_glass.storage.index import SessionIndex, SessionIndexer
        from tests.helpers import write_jsonl, sample_records

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            write_jsonl(root / "sessions/one.jsonl", sample_records())
            cfg = MonitorConfig.load(root / "config.json")
            index = SessionIndex(source)
            index.initialize()
            SessionIndexer(index, cfg).scan_once(root / "sessions")
            index.close()
            index = SessionIndex(target)
            index.initialize()
            index.close()
            with (
                patch("codex_glass.desktop.dashboard.QFileDialog.getOpenFileName", return_value=(str(source), "")),
                patch("codex_glass.desktop.dashboard.GlassDialog") as dialog,
            ):
                dialog.return_value.exec_.return_value = QDialog.Accepted
                for i in range(2):
                    self.w.context = {"index_path": str(target)}
                    self.w.can_import = True
                    self.w.import_history()
                    deadline = time.monotonic() + 10
                    while "import" in self.w.jobs and time.monotonic() < deadline:
                        QTest.qWait(20)
                    self.assertNotIn("import", self.w.jobs)
                index = SessionIndex(target)
                try:
                    index.initialize()
                    data = index.build_summary(cfg)
                    self.assertEqual(195, data["total"]["total_tokens"])
                    self.assertEqual(2, data["total"]["calls"])
                finally:
                    index.close()
                self.assertIn("新增 0", dialog.call_args.args[1])
            self.assertTrue(list(root.glob("*.before-import-*.sqlite3")))

    def test_busy_history_invalidates_old_navigation_result(self):
        w = self.w
        w.auto_fetch = True
        w.jobs["history"] = object()
        before = w.history_generation
        w.fetch_history()
        self.assertGreater(w.history_generation, before)

    def test_material_has_visible_pastel_variation_and_moves_inside_mask(self):
        w = self.w
        w.set_motion(False)
        first = w.grab().toImage()
        w.flow_phase = 2.0
        second = w.grab().toImage()
        self.assertNotEqual(first.pixelColor(700, 120), second.pixelColor(700, 120))
        self.assertEqual(0, second.pixelColor(0, 0).alpha())

    def test_first_live_payload_applies_today_default_range(self):
        from tests.native_fixtures import dashboard_fixture

        w = self.w
        p = w.pages["overview"]
        p.chart.set_series([], [])
        w.apply_data(dashboard_fixture()[0])
        self.assertEqual((54.0, 60.0), p.chart.view_range)

    def test_large_axis_labels_are_not_clipped(self):
        from PyQt5.QtGui import QFontMetrics
        from codex_glass.desktop.components.style import font

        chart = self.w.pages["overview"].chart
        chart.set_series([360000] * 5, animate=False)
        needed = max(
            QFontMetrics(font(18)).horizontalAdvance(chart._compact_number(chart._visible_peak() * i / 4))
            for i in range(5)
        )
        self.assertGreaterEqual(chart.plot_rect().left() - 7, needed)

    def test_native_dialog_is_not_embedded_and_import_result_uses_real_counts(self):
        from codex_glass.desktop.dashboard import GlassDialog
        from PyQt5.QtWidgets import QGraphicsScene
        from unittest.mock import patch

        scene = QGraphicsScene()
        parent = __import__("PyQt5.QtWidgets", fromlist=["QWidget"]).QWidget()
        scene.addWidget(parent)
        dialog = GlassDialog("完成", "测试", parent)
        self.assertIsNone(dialog.parentWidget())
        dialog.deleteLater()
        with patch("codex_glass.desktop.dashboard.GlassDialog") as kind:
            self.w.job_finished("import", {"imported_usage_events": 123, "duplicate_usage_events": 456}, None)
            self.assertIn("123", kind.call_args.args[1])
            self.assertIn("456", kind.call_args.args[1])
