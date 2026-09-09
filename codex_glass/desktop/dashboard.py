"""Native Codex Glass dashboard controller, asynchronous IO, one shared shell."""

import json
import math
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from dataclasses import asdict
from PyQt5.QtCore import Qt, QTimer, QRectF, QPoint, QObject, QRunnable, QThreadPool, pyqtSignal, QSettings
from PyQt5.QtGui import QPainter, QPainterPath, QPen, QColor, QLinearGradient, QRadialGradient, QPixmap
from PyQt5.QtWidgets import QWidget, QApplication, QDialog, QFileDialog
from codex_glass.desktop.components.style import STYLE, material, pulse_path, font, label, BLUE, MUTED, MINT
from codex_glass.desktop.controls import GlassButton, Segmented, icon, GlassPanel, NavigationButton
from codex_glass.desktop.pages import OverviewPage, ModelsPage, HistoryPage, QuotaPage, SettingsPage, text, at
from codex_glass.desktop.data import scope_bounds, export_events
from codex_glass.desktop.components.material import liquid_material


def retry_import(operation, pause=time.sleep, attempts=16):
    """Wait in the IO worker for a competing scanner, never on the UI thread."""
    for attempt in range(attempts):
        try:
            return operation()
        except RuntimeError as exc:
            if "holds the indexing lease" not in str(exc) or attempt == attempts - 1:
                raise
            pause(min(1 + attempt, 5))


class JobSignals(QObject):
    finished = pyqtSignal(object, object, object)


class Job(QRunnable):
    def __init__(self, key, operation):
        super().__init__()
        self.key = key
        self.operation = operation
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.key, self.operation(), None)
        except Exception as exc:
            self.signals.finished.emit(self.key, None, str(exc))


class GlassDialog(QDialog):
    def __init__(self, title, message, parent=None, confirm="知道了", cancel=False):
        super().__init__(None, Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.owner = parent
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(580, 300)
        self.setStyleSheet(STYLE)
        if parent is not None:
            host = parent.host_window() if hasattr(parent, "host_window") else parent.window()
            self.move(host.frameGeometry().center() - QPoint(290, 150))
        text(self, title, (30, 23, 520, 43), 26, bold=True)
        body = text(self, message, (30, 82, 520, 130), 18, MUTED)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        b = at(GlassButton(confirm, self, primary=True), (389, 235, 159, 44))
        b.clicked.connect(self.accept)
        if cancel:
            b = at(GlassButton("取消", self), (263, 235, 111, 44))
            b.clicked.connect(self.reject)

    def paintEvent(self, event):
        p = QPainter(self)
        material(p, QRectF(1, 1, 578, 298), 35, "frosted", 0)


class NativeDashboard(QWidget):
    FULL_SIZE = (1536, 1024)
    TITLES = {
        "overview": "用量总览",
        "models": "模型明细",
        "history": "历史记录",
        "quota": "账号额度",
        "settings": "外观与数据",
    }

    def __init__(self, owner=None, url="http://127.0.0.1:8081", persist=True, auto_fetch=True):
        super().__init__()
        self.owner = owner
        self.url = url.rstrip("/")
        self.persist = persist
        self.auto_fetch = auto_fetch
        self.settings = QSettings("CodexMonitor", "NativeDashboard")
        self.transparency = 65
        self.glass_preset = "balanced"
        self.motion = True
        self.chart_motion = True
        self.topmost = True
        self.flow_phase = 0.0
        if persist:
            self.transparency = max(15, min(90, self.settings.value("transparency", 65, type=int)))
            self.glass_preset = self.settings.value("preset", "balanced", type=str)
            self.motion = self.settings.value("motion", True, type=bool)
            self.chart_motion = self.settings.value("chart_motion", True, type=bool)
            self.topmost = self.settings.value("topmost", True, type=bool)
        if self.glass_preset not in ("balanced", "clear", "frosted"):
            self.glass_preset = "balanced"
        self.page = "overview"
        self.scope = "today"
        self.data = {}
        self.context = {}
        self.connected = False
        self.can_import = False
        self.jobs = {}
        self.history_generation = 0
        self.history_pending = False
        self.drag = None
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.topmost)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(*self.FULL_SIZE)
        self.setStyleSheet(STYLE)
        self.setWindowTitle("Codex Glass · 完整看板")
        from codex_glass.desktop.widget import app_icon

        self.setWindowIcon(app_icon())
        self.pages = {
            key: cls(self)
            for key, cls in zip(self.TITLES, (OverviewPage, ModelsPage, HistoryPage, QuotaPage, SettingsPage))
        }
        self.header_title = text(self, self.TITLES["overview"], (308, 53, 665, 60), 36, bold=True)
        self.nav_buttons = {}
        for i, (key, title) in enumerate(zip(self.TITLES, ("总览", "模型", "历史", "额度", "设置"))):
            b = at(NavigationButton(title, key, self), (21, 151 + i * 80, 234, 61))
            b.clicked.connect(lambda checked=False, k=key: self.change_page(k))
            self.nav_buttons[key] = b
        self.scope_tabs = at(
            Segmented([("今日", "today"), ("5小时", "last_5_hours"), ("累计", "all")], self), (1052, 58, 374, 52)
        )
        self.scope_tabs.changed.connect(self.change_scope)
        self.refresh = at(GlassButton("", self, kind="refresh"), (1444, 61, 59, 47))
        self.refresh.setToolTip("检查最新用量")
        self.refresh.clicked.connect(self.request_refresh)
        self.close_button = at(GlassButton("", self, kind="close", quiet=True), (1475, 5, 49, 39))
        self.close_button.setToolTip("关闭完整看板")
        self.close_button.clicked.connect(self.close)
        self.min_button = at(GlassButton("", self, kind="minimize", quiet=True), (1377, 5, 49, 39))
        self.min_button.clicked.connect(lambda: self.host_window().showMinimized())
        self.max_button = at(GlassButton("", self, kind="maximize", quiet=True), (1426, 5, 49, 39))
        self.max_button.clicked.connect(self.fit_window)
        self.status_label = text(self, "正在连接 SQLite", (82, 917, 178, 29), 18, MUTED)
        self.source_label = text(self, "", (82, 947, 178, 29), 18, MUTED)
        self.footer = text(self, "预估费用 · 非实际账单", (1125, 955, 371, 41), 16, MUTED)
        self.footer.setAlignment(Qt.AlignRight)
        self.toast = text(self, "", (306, 977, 1030, 34), 16, MUTED)
        self.toast.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.fetch)
        self.history_debounce = QTimer(self)
        self.history_debounce.setSingleShot(True)
        self.history_debounce.setInterval(300)
        self.history_debounce.timeout.connect(self.fetch_history)
        self.flow_timer = QTimer(self)
        self.flow_timer.setInterval(160)
        self.flow_timer.timeout.connect(self.flow_tick)
        self.change_page("overview")
        from codex_glass.desktop.components.tooltips import install_glass_tooltips

        install_glass_tooltips(self)
        if auto_fetch:
            QTimer.singleShot(50, self.fetch)

    def host_window(self):
        return getattr(self, "scaled_host", None) or self

    def global_point(self, widget, point):
        host = getattr(self, "scaled_host", None)
        return host.to_global(widget, point) if host else widget.mapToGlobal(point)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, 1534, 1022)
        # Cache only the procedural material, at diffusion resolution. Every
        # control remains a live native widget; no design screenshot is used.
        key = (self.transparency, self.glass_preset, round(self.flow_phase, 3))
        if getattr(self, "material_key", None) != key:
            self.material_key = key
            self.material_buffer = QPixmap(768, 512)
            self.material_buffer.fill(Qt.transparent)
            bg = QPainter(self.material_buffer)
            bg.scale(0.5, 0.5)
            liquid_material(bg, r, self.transparency, self.glass_preset, self.flow_phase)
            bg.end()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), self.material_buffer)
        clip = QPainterPath()
        clip.addRoundedRect(r, 28, 28)
        p.setClipPath(clip)
        p.setPen(QPen(QColor(255, 255, 255, 135), 1))
        p.drawLine(272, 0, 272, 1024)
        p.setPen(QPen(QColor("#008aff"), 3.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(pulse_path(QRectF(37, 52, 42, 36)))
        p.setFont(font(28, True))
        p.setPen(QColor("#07183e"))
        p.drawText(QRectF(92, 48, 175, 43), Qt.AlignVCenter, "Codex Glass")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(MINT if self.connected else "#d9a568"))
        p.drawEllipse(QRectF(47, 924, 18, 18))

    def raise_shell(self):
        for item in [
            self.header_title,
            self.scope_tabs,
            self.refresh,
            self.close_button,
            self.min_button,
            self.max_button,
            self.status_label,
            self.source_label,
            self.footer,
            self.toast,
        ] + list(self.nav_buttons.values()):
            item.raise_()

    def change_page(self, key):
        if key not in self.pages:
            return
        self.page = key
        for name, page in self.pages.items():
            page.setVisible(name == key)
        if hasattr(self, "header_title"):
            self.header_title.setText(self.TITLES[key])
            for name, b in self.nav_buttons.items():
                b.setChecked(name == key)
            self.raise_shell()
        self.pages[key].render()
        self.update()
        if key == "history":
            self.fetch_history()

    def change_scope(self, scope):
        if scope not in ("today", "last_5_hours", "all"):
            return
        self.scope = scope
        self.scope_tabs.set_value(scope)
        self.history_generation += 1
        self.pages["history"].offset = 0
        self.render_pages()
        if self.page == "history":
            self.fetch_history()

    def render_pages(self):
        for page in self.pages.values():
            page.render()

    def apply_data(self, data):
        self.data = data
        status = data.get("index", {}).get("status", "ready")
        self.connected = status == "ready"
        self.status_label.setText(
            {
                "ready": "SQLite 已连接",
                "error": "索引异常 · 保留数据",
                "waiting": "等待索引",
                "indexing": "正在索引",
                "starting": "正在加载",
            }.get(status, "索引更新中")
        )
        self.source_label.setText(f'{data.get("source",{}).get("imported_sources",0)} 个历史来源')
        if status == "error":
            self.toast.setText("索引异常，请在设置中重新扫描；已有数据仍保留。")
        self.pages["overview"].chart.set_live(self.connected)
        self.pages[self.page].render()
        self.update()

    def run_job(self, key, operation):
        if key in self.jobs:
            return False
        job = Job(key, operation)
        self.jobs[key] = job
        job.signals.finished.connect(self.job_finished)
        QThreadPool.globalInstance().start(job)
        return True

    def get_json(self, path):
        with urlopen(self.url + path, timeout=12) as response:
            return json.load(response)

    def fetch(self):
        if not self.auto_fetch:
            return

        def load():
            data = self.get_json("/api/data")
            # Aggregated history may contain imported quotas; it is never a
            # fallback for the current local account endpoint.
            data["rate_limits"] = {"limits": []}
            try:
                data["rate_limits"] = self.get_json("/api/widget").get("rate_limits")
            except Exception:
                pass
            return data

        self.run_job("data", load)
        if not self.context:
            self.run_job("context", lambda: self.get_json("/api/native-context"))

    def schedule_history(self):
        self.history_generation += 1
        if self.auto_fetch:
            self.history_debounce.start()

    def fetch_history(self):
        if not self.auto_fetch:
            return
        p = self.pages["history"]
        self.history_generation += 1
        if p.mode == "sources":
            p.render_sources()
            return
        if "history" in self.jobs:
            self.history_pending = True
            return
        generation = self.history_generation
        since, until = scope_bounds("this_week" if p.mode == "week" else self.scope)
        params = {
            "offset": p.offset,
            "limit": p.limit,
            "q": p.search.text(),
            "since": since,
            "until": until,
            "sort": p.sort,
            "descending": "1" if p.descending else "0",
        }
        if p.selected is not None:
            if p.selected:
                params["models"] = sorted(p.selected)
            else:
                params["empty_models"] = "1"
        path = "/api/events?" + urlencode(params, doseq=True)
        self.run_job("history", lambda: (generation, self.get_json(path)))

    def job_finished(self, key, result, error):
        self.jobs.pop(key, None)
        if error:
            if key == "data":
                self.connected = False
                self.status_label.setText("离线 · 保留上次数据")
                self.toast.setText("数据连接失败：" + error)
                self.pages["overview"].chart.set_live(False)
                self.pages["settings"].render()
                self.update()
            elif key != "context":
                self.toast.setText("操作未完成：" + error)
            if key == "import":
                self.enable_import(True)
        elif key == "data":
            if isinstance(result, dict) and isinstance(result.get("total"), dict):
                self.apply_data(result)
            if self.page == "history":
                self.fetch_history()
        elif key == "context":
            self.context = result
            host = urlparse(self.url).hostname
            self.can_import = host in ("127.0.0.1", "localhost", "::1") and bool(result.get("index_path"))
            self.pages["settings"].render()
            self.pages["history"].render()
        elif key == "history":
            generation, payload = result
            if generation == self.history_generation and self.pages["history"].mode != "sources":
                self.pages["history"].apply_history(payload)
        elif key == "refresh":
            self.toast.setText("正在增量扫描…")
            self.fetch()
        elif key == "import":
            self.enable_import(True)
            self.context = {}
            message = f'新增 {result.get("imported_usage_events",0):,} 条用量，跳过 {result.get("duplicate_usage_events",0):,} 条重复记录。导入前的数据库备份已保留。'
            self.toast.setText(message)
            self.request_refresh()
            GlassDialog("历史导入完成", message, self).exec_()
        if key == "history" and self.history_pending:
            self.history_pending = False
            self.fetch_history()

    def request_refresh(self):
        if not self.auto_fetch:
            return

        def refresh():
            with urlopen(Request(self.url + "/api/refresh", method="POST"), timeout=12) as response:
                return response.status

        self.run_job("refresh", refresh)

    def export_history(self):
        events = self.pages["history"].events
        if not events:
            self.toast.setText("当前页没有可导出的记录")
            return
        path, _ = QFileDialog.getSaveFileName(self.host_window(), "导出当前页", "Codex-用量.csv", "CSV (*.csv)")
        if path:
            try:
                export_events(path, events)
                self.toast.setText("当前页已导出")
            except OSError as exc:
                self.toast.setText("导出失败：" + str(exc))

    def enable_import(self, enabled):
        for p in (self.pages["history"], self.pages["settings"]):
            p.import_button.setEnabled(enabled and self.can_import)

    def import_history(self):
        if not self.can_import or "import" in self.jobs:
            return
        path, _ = QFileDialog.getOpenFileName(
            self.host_window(), "选择要导入的 SQLite 历史", "", "SQLite (*.sqlite3 *.sqlite *.db);;所有文件 (*)"
        )
        if not path:
            return
        if (
            GlassDialog(
                "导入历史",
                f"将导入 {Path(path).name}，自动去重，并在导入前备份现有索引。此操作可能需要几分钟。",
                self,
                "导入并合并",
                True,
            ).exec_()
            != QDialog.Accepted
        ):
            return
        target = Path(self.context["index_path"])
        self.enable_import(False)
        self.toast.setText("正在校验、备份并导入历史…")

        def merge():
            from codex_glass.storage.index import SessionIndex
            from codex_glass.storage.history_import import HistoryImporter

            index = SessionIndex(target)
            try:
                index.initialize()
                return asdict(retry_import(lambda: HistoryImporter(index).import_database(Path(path))))
            finally:
                index.close()

        self.run_job("import", merge)

    def set_transparency(self, value):
        self.transparency = max(15, min(90, int(value)))
        self.save_settings()
        self.update()

    def set_motion(self, value):
        self.motion = bool(value)
        if self.motion and self.isVisible():
            self.flow_timer.start()
        else:
            self.flow_timer.stop()
        self.save_settings()
        self.render_pages()

    def set_top(self, value):
        self.topmost = bool(value)
        host = self.host_window()
        host.setWindowFlag(Qt.WindowStaysOnTopHint, value)
        host.show()
        self.save_settings()

    def save_settings(self):
        if self.persist:
            for k, v in (
                ("transparency", self.transparency),
                ("preset", self.glass_preset),
                ("motion", self.motion),
                ("chart_motion", self.chart_motion),
                ("topmost", self.topmost),
            ):
                self.settings.setValue(k, v)

    def restore_defaults(self):
        p = self.pages["settings"]
        p.set_preset("balanced")
        p.opacity.setValue(65)
        for switch in p.switches.values():
            switch.setChecked(True)
        p.scale.setValue(100)

    def flow_tick(self):
        self.flow_phase += 0.008
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "timer"):
            if self.auto_fetch:
                self.timer.start()
            if self.motion:
                self.flow_timer.start()

    def hideEvent(self, event):
        if hasattr(self, "timer"):
            self.timer.stop()
            self.flow_timer.stop()
            self.history_debounce.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.save_settings()
        self.host_window().hide()
        event.ignore()

    def fit_window(self):
        host = getattr(self, "scaled_host", None)
        if host:
            a = self.screen().availableGeometry()
            host.set_scale(min(a.width() / 1536, a.height() / 1024, 1.5))
            host.move(a.topLeft())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.y() < 120:
            self.drag = event.globalPos() - self.host_window().pos()

    def mouseMoveEvent(self, event):
        if self.drag is not None and event.buttons() & Qt.LeftButton:
            self.host_window().move(event.globalPos() - self.drag)

    def mouseReleaseEvent(self, event):
        self.drag = None


def open_dashboard(owner):
    existing = getattr(owner, "dashboard", None)
    if existing is not None:
        host = existing.host_window()
        host.showNormal()
        host.raise_()
        host.activateWindow()
        existing.fetch()
        return existing
    dashboard = NativeDashboard(owner, url=owner.url)
    owner.dashboard = dashboard
    from codex_glass.desktop.components.host import ScaledSurfaceHost

    area = QApplication.primaryScreen().availableGeometry()
    scale = min(0.8, (area.width() - 60) / 1536, (area.height() - 60) / 1024)
    host = ScaledSurfaceHost(dashboard, scale=scale, min_scale=0.5, max_scale=1.5)
    dashboard.scaled_host = host

    def sync_scale(value):
        page = dashboard.pages["settings"]
        page.scale.blockSignals(True)
        page.scale.setValue(round(value * 100))
        page.scale.blockSignals(False)
        page.scale_label.setText(f"{round(value*100)}%")
        if dashboard.persist:
            dashboard.settings.setValue("scale", value)

    host.scaleChanged.connect(sync_scale)
    host.set_scale(
        min(
            (area.width() - 60) / 1536,
            (area.height() - 60) / 1024,
            dashboard.settings.value("scale", scale, type=float),
        )
    )
    sync_scale(host.scale)
    host.move(area.center() - QPoint(host.width() // 2, host.height() // 2))
    host.show()
    dashboard.fetch()
    return dashboard
