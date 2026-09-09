import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication, QGraphicsScene, QWidget

    HAS_QT = True
except ImportError:
    HAS_QT = False

if HAS_QT:
    from codex_glass.desktop.components.popups import GlassChoicePopup, GlassInfoPopup, GlassMenu


@unittest.skipUnless(HAS_QT, "optional desktop dependency PyQt5 not installed")
class GlassPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owner = QWidget()
        self.owner.setAttribute(Qt.WA_DontShowOnScreen)
        self.owner.show()
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.owner.close()
        self.owner.deleteLater()
        QTest.qWait(5)

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def test_glass_menu_keeps_qmenu_api_and_keyboard_activation(self):
        menu = self.keep(GlassMenu(self.owner))
        fired = []
        first = menu.addAction("第一项")
        second = menu.addAction("第二项")
        second.setCheckable(True)
        second.setChecked(True)
        first.triggered.connect(lambda: fired.append("first"))

        self.assertEqual(["第一项", "第二项"], [a.text() for a in menu.actions()])
        self.assertTrue(menu.testAttribute(Qt.WA_TranslucentBackground))
        menu.popup(QPoint(20, 20))
        QTest.qWait(10)
        QTest.keyClick(menu, Qt.Key_Down)
        QTest.keyClick(menu, Qt.Key_Return)
        self.assertEqual(["first"], fired)

    def test_owner_retains_top_level_popups_without_graphics_proxy_embedding(self):
        self.scene = QGraphicsScene()
        self.scene.addWidget(self.owner)
        menu = self.keep(GlassMenu(self.owner))
        choice = self.keep(GlassChoicePopup(["A", "B"], "A", None, self.owner))
        info = self.keep(GlassInfoPopup(self.owner))

        self.assertIsNone(menu.parentWidget())
        self.assertIsNone(choice.parentWidget())
        self.assertIsNone(info.parentWidget())
        self.assertIsNone(menu.graphicsProxyWidget())
        self.assertIsNone(choice.graphicsProxyWidget())
        self.assertIsNone(info.graphicsProxyWidget())
        self.assertIn(choice, self.owner._glass_popup_children)

    def test_single_choice_cancel_discards_draft_and_apply_commits(self):
        selected = []
        popup = self.keep(GlassChoicePopup(["A", "B", "C"], "A", selected.append, self.owner))
        popup.show()
        QTest.mouseClick(popup.choices[1], Qt.LeftButton)
        self.assertEqual("A", popup.selection)
        QTest.keyClick(popup, Qt.Key_Escape)
        self.assertEqual([], selected)

        popup.show()
        self.assertTrue(popup.choices[0].isChecked())
        QTest.mouseClick(popup.choices[1], Qt.LeftButton)
        QTest.mouseClick(popup.apply_button, Qt.LeftButton)
        self.assertEqual("B", popup.selection)
        self.assertEqual(["B"], selected)

    def test_multi_choice_search_all_clear_cancel_and_apply(self):
        selected = []
        popup = self.keep(
            GlassChoicePopup(["astra", "sol", "terra"], {"astra"}, selected.append, self.owner, multi=True)
        )
        popup.show()
        popup.search.setText("so")
        self.assertTrue(popup.choices[1].isVisible())
        self.assertFalse(popup.choices[0].isVisible())
        popup.search.clear()
        QTest.mouseClick(popup.all_button, Qt.LeftButton)
        QTest.keyClick(popup, Qt.Key_Escape)
        self.assertEqual({"astra"}, popup.selection)
        self.assertEqual([], selected)

        popup.show()
        QTest.mouseClick(popup.clear_button, Qt.LeftButton)
        QTest.mouseClick(popup.choices[1], Qt.LeftButton)
        QTest.mouseClick(popup.apply_button, Qt.LeftButton)
        self.assertEqual({"sol"}, popup.selection)
        self.assertEqual([{"sol"}], selected)

    def test_tall_choice_list_scrolls_and_stays_inside_active_screen(self):
        popup = self.keep(GlassChoicePopup([f"option-{i}" for i in range(100)], set(), None, multi=True))
        popup.popup(QPoint(0, 0))
        bounds = self.app.primaryScreen().availableGeometry()
        self.assertTrue(bounds.contains(popup.geometry()), (bounds, popup.geometry()))
        self.assertGreater(popup.choice_scroll.verticalScrollBar().maximum(), 0)

    def test_model_choices_are_readable_and_not_compressed_before_parenting(self):
        popup = self.keep(GlassChoicePopup(["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"], set(), None, multi=True))
        popup.popup(QPoint(20, 20))
        QTest.qWait(10)
        for choice in popup.choices:
            self.assertGreaterEqual(choice.height(), 44)
            self.assertGreaterEqual(choice.font().pixelSize(), 18)
        self.assertGreaterEqual(popup.choice_scroll.height(), 132)

    def test_choice_keyboard_moves_focus_and_escape_never_applies(self):
        selected = []
        popup = self.keep(GlassChoicePopup(["A", "B", "C"], "A", selected.append, self.owner))
        popup.show()
        popup.setFocus()
        QTest.keyClick(popup, Qt.Key_Down)
        self.assertTrue(popup.choices[1].hasFocus())
        QTest.keyClick(popup.choices[1], Qt.Key_Space)
        QTest.keyClick(popup, Qt.Key_Escape)
        self.assertEqual("A", popup.selection)
        self.assertEqual([], selected)

    def test_info_popup_supports_structured_and_generic_text(self):
        popup = self.keep(GlassInfoPopup(self.owner))
        popup.show_info("09月08日 · 21:35", 837190, "Token / min", QPoint(30, 30))
        self.assertIn("09月08日 · 21:35", popup.text())
        self.assertIn("837,190", popup.text())
        self.assertEqual(18, popup.time_label.font().pixelSize())
        self.assertEqual(30, popup.value_label.font().pixelSize())
        self.assertEqual(16, popup.unit_label.font().pixelSize())

        popup.setText("输入 123\n输出 45")
        popup.adjustSize()
        self.assertEqual("输入 123\n输出 45", popup.text())
        self.assertGreaterEqual(popup.generic_label.font().pixelSize(), 16)

        popup.hide()
        popup.setText("隐藏时仍可读取")
        self.assertEqual("隐藏时仍可读取", popup.text())


if __name__ == "__main__":
    unittest.main()
