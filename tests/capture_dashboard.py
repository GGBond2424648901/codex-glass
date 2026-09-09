"""Capture actual native surfaces at the approved reference size; no desktop wallpaper."""

import sys
from pathlib import Path
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from codex_glass.desktop.dashboard import NativeDashboard
from tests.native_fixtures import dashboard_fixture


def run(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    w = NativeDashboard(persist=False, auto_fetch=False)
    w.setAttribute(Qt.WA_DontShowOnScreen)
    w.set_motion(False)
    w.show()
    data, context, events = dashboard_fixture()
    w.context = context
    w.can_import = True
    w.apply_data(data)
    for page in w.pages:
        for scope, key in [("today", "today"), ("last_5_hours", "5h"), ("all", "all")]:
            w.change_page(page)
            w.change_scope(scope)
            QTest.qWait(40)
            if page == "history":
                w.pages["history"].apply_history(
                    {
                        "events": events,
                        "total": 14578 if scope == "today" else 1500 if scope == "last_5_hours" else 610931,
                        "offset": 0,
                    }
                )
            w.grab().save(str(out / f"{page}-{key}.png"))
    w.hide()
    w.deleteLater()
    app.processEvents()
    print(out)


if __name__ == "__main__":
    run(sys.argv[1])
