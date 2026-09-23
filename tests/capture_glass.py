"""Deterministic Qt visual evidence. Wallpaper is a test-only painted surface."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QPainter, QLinearGradient, QColor, QPainterPath, QImage
from PyQt5.QtTest import QTest
from codex_glass.desktop.widget import GlassWidget
from tests.glass_fixtures import invocation_activity, telemetry

output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parents[1] / "visual-evidence"
output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setQuitOnLastWindowClosed(False)


class Wallpaper(QWidget):
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0, QColor("#f8ddd8"))
        g.setColorAt(0.45, QColor("#c4dbf5"))
        g.setColorAt(1, QColor("#76cbed"))
        p.fillRect(self.rect(), g)
        for color, base in [("#f9c2b6", 170), ("#98c5f5", 390), ("#78d8e4", 540), ("#b2c1f0", 630)]:
            path = QPainterPath()
            path.moveTo(0, base)
            path.cubicTo(170, base - 90, 350, base + 180, self.width(), base - 60)
            path.lineTo(self.width(), self.height())
            path.lineTo(0, self.height())
            path.closeSubpath()
            p.fillPath(path, QColor(color))


bg = Wallpaper()
bg.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
bg.resize(1000, 800)
bg.move(30, 30)
bg.show()
w = GlassWidget(url="http://127.0.0.1:1", start_backend=False, persist=False)
w.move(80, 70)
w.timer.stop()
w.set_motion(False)
w.apply_data(telemetry())
w.show()
QTest.qWait(500)
captures = []
for scope in ("today", "last_5_hours", "all"):
    w.change_scope(scope)
    QTest.qWait(150)
    # Grab the actual compositor-rendered application region (not other desktop content).
    shot = w.screen().grabWindow(0, w.x(), w.y(), w.width(), w.height())
    shot.save(str(output / (scope + ".png")))
    captures.append(shot.toImage())
activity = telemetry()
activity["model_activity"] = invocation_activity()
w.change_scope("today")
w.apply_data(activity)
QTest.qWait(150)
w.grab().save(str(output / "invocation-compact.png"))
w.invocation.toggle()
QTest.qWait(150)
w.grab().save(str(output / "invocation-expanded.png"))
w.invocation.toggle()
w.toggle_compact()
QTest.qWait(150)
w.screen().grabWindow(0, w.x(), w.y(), w.width(), w.height()).save(str(output / "mini.png"))
w.toggle_compact()
two = telemetry()
first = invocation_activity()
second = invocation_activity()
for index, item in enumerate((first, second), 1):
    item["conversation_id"] = f"visual-conversation-{index}"
    item["telemetry"] = {
        "status": "recent", "output_tps": 3.2 if index == 1 else 8.4,
        "cache_hit_percent": 68.0 if index == 1 else 82.0,
        "trend_5s_output": [0, 1, 3, 2, 7, 11, 5, 9, 13, 7, 5, 12],
    }
    item["agents"][0]["telemetry"] = {
        "output_tps": 2.1 if index == 1 else 5.0, "cache_hit_percent": 70.0
    }
    item["agents"][1]["telemetry"] = {
        "output_tps": 1.1 if index == 1 else 3.4, "cache_hit_percent": 64.0
    }
two["model_activity"] = {
    "active": True, "agents": first["agents"], "conversations": [first, second]
}
w.apply_data(two)
w.select_conversation("visual-conversation-2")
w.invocation.expanded = True
w.toggle_invocation(True)
QTest.qWait(150)
w.grab().save(str(output / "conversation-telemetry-expanded.png"))
w.invocation.expanded = False
w.toggle_invocation(False)
w.toggle_compact()
w.set_mini_telemetry_mode(True)
QTest.qWait(150)
w.grab().save(str(output / "conversation-telemetry-mini.png"))
w.toggle_compact()
w.appearance.move(570, 180)
w.appearance.show()
QTest.qWait(300)
w.appearance.screen().grabWindow(
    0, w.appearance.x(), w.appearance.y(), w.appearance.width(), w.appearance.height()
).save(str(output / "appearance.png"))
sheet = QImage(1320, 686, QImage.Format_ARGB32)
sheet.fill(QColor("#d8e6f4"))
p = QPainter(sheet)
for i, img in enumerate(captures):
    p.drawImage(i * 440, 0, img)
p.end()
sheet.save(str(output / "three-pages.png"))
print("Captured three pages, mini, appearance. Native DWM request accepted:", w.blur_active)
w.appearance.hide()
from codex_glass.desktop.components.host import ScaledSurfaceHost

w.toggle_compact()
w.set_mini_telemetry_mode(True)
scaled = ScaledSurfaceHost(w, scale=0.55, min_scale=0.55, max_scale=1.5)
scaled.show()
QTest.qWait(180)
scaled.grab().save(str(output / "conversation-telemetry-mini-55pct.png"))
scaled.hide()
w.tray.hide()
w.hide()
bg.hide()
app.quit()
