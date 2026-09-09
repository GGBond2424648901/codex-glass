"""Five native page compositions; time scopes reuse these same components."""

from datetime import datetime
from PyQt5.QtCore import Qt, QPoint, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen
from PyQt5.QtWidgets import QWidget, QSlider
from codex_glass.desktop.components.style import label, font, INK, MUTED, GlassSwitch
from codex_glass.desktop.components.chart import GlassChart
from codex_glass.desktop.controls import (
    GlassPanel,
    GlassButton,
    Segmented,
    SearchBox,
    Progress,
    Donut,
    GlassTable,
    NameCell,
    PresetButton,
)
from codex_glass.desktop.data import number, model_rows, cost_text, coverage, token_structure, color_for
from codex_glass.core.presentation import window_data, quota_windows, snapshot_status, COST_ESTIMATE_NOTE


def at(widget, rect):
    widget.setGeometry(*rect)
    return widget


def text(parent, value, rect, size=20, color=INK, bold=False):
    return at(label(parent, value, size, color, bold), rect)


def line(parent, x, y, w):
    item = QWidget(parent)
    item.setStyleSheet("background:rgba(117,146,193,65);")
    item.setGeometry(x, y, w, 1)
    return item


def reset_label(row):
    try:
        date = datetime.fromisoformat(row["resets_at"])
        seconds = max(0, (date - datetime.now(date.tzinfo)).total_seconds())
        return (
            f"{int(seconds//86400)}天后重置"
            if seconds >= 86400
            else f"{int(seconds//3600)}时{int(seconds%3600//60)}分后重置"
        )
    except (ValueError, TypeError, KeyError):
        return "重置时间未知"


class Page(QWidget):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.setGeometry(0, 0, 1536, 1024)

    def panel(self, rect):
        return at(GlassPanel(self), rect)

    def choice(self, button, options, selected, callback, multi=False):
        from codex_glass.desktop.components.popups import GlassChoicePopup, release_owned_popup

        previous = getattr(self, "popup", None)
        if previous is not None and previous.options == list(options) and previous.multi == multi:
            self.popup.selection = self.popup._normalise_selection(selected)
            self.popup.callback = callback
        else:
            if previous is not None:
                release_owned_popup(self, previous)
            self.popup = GlassChoicePopup(options, selected, callback, owner=self, multi=multi)
        self.popup.popup(self.controller.global_point(button, QPoint(0, button.height() + 7)))


class OverviewPage(Page):
    def __init__(self, c):
        super().__init__(c)
        self.chart_key = None
        self.range_days = 30
        self.range_minutes = 30
        strip = at(GlassPanel(self, 24, 35), (291, 122, 1215, 116))
        self.metrics = []
        for i, title in enumerate(("预估费用", "Token", "缓存命中", "调用次数")):
            x = 23 + i * 309
            text(strip, title, (x, 15, 270, 30), 20, MUTED)
            self.metrics.append(text(strip, "—", (x, 44, 286, 62), 48, bold=True))
            if i == 0:
                self.metrics[-1].setToolTip(COST_ESTIMATE_NOTE)
            if i:
                line(strip, x - 28, 25, 1).setFixedHeight(72)
        panel = self.panel((289, 249, 802, 357))
        text(panel, "用量趋势", (27, 19, 300, 39), 26, bold=True)
        self.unit = text(panel, "Token / min", (27, 62, 280, 30), 20, MUTED)
        self.range_button = at(GlassButton("近30分钟", panel), (608, 18, 145, 44))
        self.range_button.clicked.connect(self.choose_range)
        self.reset = at(GlassButton("", panel, kind="refresh"), (758, 18, 38, 44))
        self.reset.setToolTip("复位时间范围")
        self.reset.clicked.connect(self.reset_range)
        self.rate = text(panel, "—", (542, 68, 237, 40), 34, "#008bff", True)
        self.rate.setAlignment(Qt.AlignRight)
        self.chart = at(GlassChart(panel), (25, 101, 752, 234))
        self.chart.axis_font_size = 18
        self.chart.y_axis = True
        if hasattr(self.chart, "rangeChanged"):
            self.chart.rangeChanged.connect(self.range_changed)
        q = self.panel((1105, 249, 402, 357))
        self.quota_title = text(q, "额度", (28, 19, 345, 38), 25, bold=True)
        self.quota_ring = at(Donut(q, width=27), (108, 62, 190, 190))
        self.quota_bar = at(Progress(q), (29, 265, 345, 21))
        self.quota_reset = text(q, "暂无快照", (29, 308, 215, 28), 18, MUTED)
        self.quota_stamp = text(q, "", (238, 308, 140, 28), 17, MUTED)
        self.quota_stamp.setAlignment(Qt.AlignRight)
        panel = self.panel((289, 619, 1218, 325))
        text(panel, "模型用量", (27, 18, 600, 40), 26, bold=True)
        link = at(GlassButton("查看全部 ›", panel, quiet=True), (1056, 18, 135, 38))
        link.setFont(font(18))
        link.clicked.connect(lambda: c.change_page("models"))
        self.table = at(
            GlassTable(["模型", "趋势", "Token", "预估费用", "占比"], [225, 310, 168, 198, 292], panel, row_height=68),
            (14, 64, 1190, 249),
        )
        self.table.horizontalHeader().setFixedHeight(42)
        self.table.cellClicked.connect(self.select_model)
        self.visible_rows = []

    def choose_range(self):
        opts = ["近7天", "近30天", "近90天"] if self.controller.scope == "all" else ["近30分钟", "近1小时", "近5小时"]
        self.choice(self.range_button, opts, self.range_button.text(), self.set_range)

    def set_range(self, value):
        if self.controller.scope == "all":
            self.range_days = {"近7天": 7, "近30天": 30, "近90天": 90}[value]
        else:
            self.range_minutes = {"近30分钟": 30, "近1小时": 60, "近5小时": 300}[value]
        self.reset_range()

    def range_changed(self, start, end):
        if not start or not end:
            return
        try:
            a = datetime.fromisoformat(start)
            b = datetime.fromisoformat(end)
            fmt = "%m/%d" if self.controller.scope == "all" else "%H:%M"
            self.range_button.setText(a.strftime(fmt) + "–" + b.strftime(fmt))
            self.range_button.setFont(font(17))
        except ValueError:
            pass

    def reset_range(self):
        n = len(self.chart.target_values)
        daily = self.controller.scope == "all"
        count = self.range_days if daily else self.range_minutes // 5 + 1
        if hasattr(self.chart, "set_view_range"):
            self.chart.set_view_range(max(0, n - count), max(0, n - 1))
        elif hasattr(self.chart, "reset_zoom"):
            self.chart.reset_zoom()
        self.range_button.setText(
            f"近{self.range_days}天"
            if daily
            else ("近30分钟" if self.range_minutes == 30 else f"近{self.range_minutes//60}小时")
        )
        self.range_button.setFont(font(18))

    def select_model(self, index, column):
        if index < len(self.visible_rows):
            self.controller.change_page("models")
            self.controller.pages["models"].search.setText(self.visible_rows[index]["name"])

    def render(self):
        c = self.controller
        data = c.data
        total, _ = window_data(data, c.scope)
        cache = total.get("cached_input_tokens", 0) / max(1, total.get("input_tokens", 0)) * 100
        vals = [cost_text(total), number(total.get("total_tokens")), f"{cache:.1f}%", f'{total.get("calls",0):,}']
        if not data or (
            not total.get("calls") and data.get("index", {}).get("status") in ("starting", "indexing", "waiting")
        ):
            vals = ["—"] * 4
        for lab, value in zip(self.metrics, vals):
            lab.setText(value)
            lab.setFont(font(43 if len(value) > 11 else 48, True))
        chart = data.get("charts", {}).get("all" if c.scope == "all" else "last_5_hours", {})
        if not chart:
            chart = data.get("charts", {}).get(c.scope, {})
        changed = self.chart_key != c.scope or not self.chart.target_values
        daily = c.scope == "all"
        self.chart.motion = c.chart_motion
        self.chart.set_series(
            chart.get("values", []),
            chart.get("labels", []),
            not changed and c.chart_motion,
            "每日 Token" if daily else "Token / min",
        )
        if changed:
            self.range_minutes = 300 if c.scope == "last_5_hours" else 30
            self.reset_range()
            self.chart_key = c.scope
        self.unit.setText("每日 Token" if daily else "Token / min")
        values = chart.get("values", [])
        self.rate.setText(number(values[-1]) if values else "—")
        self.chart.set_live(c.connected)
        quotas = quota_windows(data)
        if quotas:
            title, row = quotas[0]
            remaining = max(0, min(100, 100 - float(row["used_percent"])))
            plan = str((data.get("rate_limits") or {}).get("plan_type", "")).upper()
            self.quota_title.setText((plan + " · " if plan else "") + title)
            self.quota_ring.set_values([remaining, 100 - remaining], ["#28d1b1", "#a8e4ea"], f"{remaining:g}%")
            self.quota_bar.setValue(remaining)
            self.quota_reset.setText(reset_label(row))
            self.quota_stamp.setText("同步 " + str(row.get("observed_at", ""))[11:16])
        else:
            self.quota_ring.set_values([], [], "—")
            self.quota_title.setText("额度 · 暂无快照")
            self.quota_bar.setValue(0)
            self.quota_reset.setText("等待有效账号快照")
            self.quota_stamp.clear()
        self.visible_rows = model_rows(data, c.scope)[: 4 if daily else 3]
        rows = []
        self.table.row_height = 55 if daily else 68
        ownchart = data.get("charts", {}).get(c.scope, {})
        for r in self.visible_rows:
            spark = GlassChart(color=r["color"], mini=True)
            spark.setAttribute(Qt.WA_TransparentForMouseEvents)
            spark.motion = c.chart_motion
            spark.set_series(ownchart.get("by_model", {}).get(r["name"], []), animate=False)
            share = r.get("total_tokens", 0) / max(1, total.get("total_tokens", 0)) * 100
            sharecell = QWidget()
            text(sharecell, f"{share:.1f}%", (15, 0, 85, self.table.row_height), 20)
            bar = at(Progress(sharecell, r["color"]), (101, self.table.row_height // 2 - 6, 174, 12))
            bar.setValue(share)
            rows.append(
                [
                    NameCell(r["display_name"], r["color"], bold=False),
                    spark,
                    number(r.get("total_tokens")),
                    cost_text(r, r["unpriced"]),
                    sharecell,
                ]
            )
        self.table.set_rows(rows)


class ModelsPage(Page):
    def __init__(self, c):
        super().__init__(c)
        self.workspace = False
        self.selected = None
        self.visible_rows = []
        self.tabs = at(Segmented([("模型", "model"), ("工作区", "workspace")], self), (295, 131, 337, 58))
        self.tabs.changed.connect(self.change_kind)
        self.search = at(SearchBox("搜索模型", self), (1148, 135, 356, 50))
        self.search.setTextMargins(54, 0, 62, 0)
        self.search.textChanged.connect(self.render)
        self.filter = at(GlassButton("", self, kind="filter", quiet=True), (1444, 139, 47, 42))
        self.filter.clicked.connect(self.choose_models)
        panel = self.panel((290, 208, 1217, 444))
        self.table = at(
            GlassTable(
                ["模型", "输入", "缓存", "输出", "推理", "Token", "预估费用"],
                [268, 156, 155, 154, 154, 161, 160],
                panel,
                row_height=74,
            ),
            (10, 0, 1197, 432),
        )
        self.table.cellClicked.connect(self.detail)
        self.table.show_selection = True
        self.tabs.buttons["model"].kind = "models"
        self.tabs.buttons["workspace"].kind = "folder"
        p = self.panel((290, 667, 613, 271))
        text(p, "Token 结构", (27, 16, 300, 40), 25, bold=True)
        self.donut = at(Donut(p, width=26), (35, 70, 182, 182))
        self.donut.big = 25
        self.legend = []
        for i, (name, color) in enumerate(zip(("未缓存输入", "缓存输入", "输出"), ("#08c6e7", "#28d1b1", "#168fff"))):
            y = 69 + i * 53
            text(p, "●", (230, y, 25, 34), 23, color)
            text(p, name, (264, y, 140, 34), 19)
            value = text(p, "—", (385, y, 130, 34), 20)
            value.setAlignment(Qt.AlignRight)
            ratio = text(p, "", (522, y, 66, 34), 18, MUTED)
            ratio.setAlignment(Qt.AlignRight)
            self.legend.append((value, ratio))
        text(p, "缓存包含在输入内；推理包含在输出内", (236, 226, 363, 27), 15, MUTED)
        p = self.panel((916, 667, 591, 271))
        text(p, "已知价格覆盖", (28, 16, 520, 40), 25, bold=True)
        self.coverbar = at(Progress(p, "#168fff"), (28, 75, 533, 22))
        self.covervalue = text(p, "—", (28, 119, 225, 55), 40, bold=True)
        self.unknown = text(p, "—", (325, 119, 233, 55), 40, bold=True)
        text(p, "已覆盖 Token", (28, 174, 220, 30), 22, MUTED)
        text(p, "未定价 Token", (325, 174, 235, 30), 22, MUTED)
        self.coverdetail = text(p, "", (28, 208, 270, 30), 20, MUTED)
        self.unknowndetail = text(p, "", (325, 208, 230, 30), 20, MUTED)

    def change_kind(self, key):
        self.workspace = key == "workspace"
        self.tabs.set_value(key)
        self.search.setPlaceholderText("搜索工作区" if self.workspace else "搜索模型")
        self.search.clear()
        self.filter.setVisible(not self.workspace)
        self.render()

    def choose_models(self):
        options = [r["name"] for r in model_rows(self.controller.data, self.controller.scope)]
        self.choice(
            self.filter, options, set(options) if self.selected is None else self.selected, self.apply_filter, True
        )

    def apply_filter(self, selected):
        self.selected = set(selected)
        self.render()

    def render(self, *args):
        c = self.controller
        self.visible_rows = model_rows(
            c.data, c.scope, self.search.text(), None if self.workspace else self.selected, self.workspace
        )
        self.table.horizontalHeaderItem(0).setText("工作区" if self.workspace else "模型")
        rows = []
        for r in self.visible_rows:
            rows.append(
                [NameCell(r["display_name"], r["color"])]
                + [
                    number(r.get(k))
                    for k in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                        "total_tokens",
                    )
                ]
                + [cost_text(r, r["unpriced"])]
            )
        self.table.set_rows(rows)
        self.detail(max(0, self.table.currentRow()), 0)
        total, unknown, percent = coverage(c.data, c.scope)
        self.coverbar.setValue(percent)
        self.covervalue.setText(f"{percent:.1f}%")
        self.unknown.setText(number(unknown))
        self.coverdetail.setText(f"{number(total-unknown)} / {number(total)}")
        self.unknowndetail.setText(f"{100-percent:.1f}%")

    def detail(self, index, column):
        row = self.visible_rows[index] if index < len(self.visible_rows) else {}
        values = token_structure(row)
        total = sum(values)
        self.donut.set_values(values, ["#08c6e7", "#28d1b1", "#168fff"], number(total), "Total")
        for (lab, ratio), v in zip(self.legend, values):
            lab.setText(number(v))
            ratio.setText(f"{100*v/total:.1f}%" if total else "0%")


class HistoryPage(Page):
    def __init__(self, c):
        super().__init__(c)
        self.offset = 0
        self.limit = 7
        self.selected = None
        self.events = []
        self.mode = "events"
        self.sort = "timestamp"
        self.descending = True
        p = self.panel((290, 122, 1217, 92))
        self.search = at(SearchBox("搜索模型或工作区", p), (20, 18, 580, 55))
        self.search.textChanged.connect(self.search_changed)
        self.filter = at(GlassButton("模型筛选", p, kind="filter"), (610, 18, 202, 55))
        self.filter.clicked.connect(self.choose_models)
        self.export = at(GlassButton("导出本页", p, kind="export"), (827, 18, 179, 55))
        self.export.clicked.connect(c.export_history)
        self.import_button = at(GlassButton("导入 SQLite", p, kind="import", primary=True), (1019, 18, 181, 55))
        self.import_button.clicked.connect(c.import_history)
        self.tabs = at(
            Segmented([("事件", "events"), ("本周", "week"), ("导入来源", "sources")], self), (302, 231, 401, 53)
        )
        self.tabs.changed.connect(self.change_mode)
        p = self.panel((290, 296, 1217, 549))
        self.table = at(
            GlassTable(
                ["时间", "模型", "工作区", "输入", "缓存", "输出", "Token", "预估费用"],
                [149, 194, 193, 110, 110, 112, 150, 137],
                p,
                row_height=59,
            ),
            (13, 4, 1190, 473),
        )
        self.table.horizontalHeader().setFixedHeight(54)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_by)
        self.page_label = text(p, "第1页", (667, 490, 292, 44), 18, MUTED)
        self.prev_button = at(GlassButton("上一页", p), (970, 490, 111, 44))
        self.next_button = at(GlassButton("下一页", p), (1090, 490, 111, 44))
        self.prev_button.clicked.connect(lambda: self.navigate(-1))
        self.next_button.clicked.connect(lambda: self.navigate(1))
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        p = self.panel((290, 859, 1217, 96))
        self.source_label = text(p, "本机历史", (33, 25, 460, 44), 21, bold=True)
        self.import_label = text(p, "", (512, 25, 440, 44), 20)
        text(p, "数据保留在本机", (1000, 25, 192, 44), 17, MUTED)

    def search_changed(self):
        self.offset = 0
        self.controller.schedule_history()

    def change_mode(self, key):
        self.mode = key
        self.tabs.set_value(key)
        self.offset = 0
        self.search.setEnabled(key != "sources")
        self.filter.setEnabled(key != "sources")
        self.export.setEnabled(key != "sources")
        self.controller.fetch_history()

    def choose_models(self):
        options = list(self.controller.data.get("by_model", {}))
        self.choice(
            self.filter, options, set(options) if self.selected is None else self.selected, self.apply_filter, True
        )

    def apply_filter(self, values):
        self.selected = set(values)
        self.offset = 0
        self.controller.fetch_history()

    def sort_by(self, col):
        key = {0: "timestamp", 1: "model", 6: "tokens", 7: "cost"}.get(col)
        if key:
            self.descending = not self.descending if self.sort == key else True
            self.sort = key
            self.controller.fetch_history()

    def navigate(self, direction):
        self.offset = max(0, self.offset + direction * self.limit)
        self.controller.fetch_history()

    def apply_history(self, payload):
        self.events = payload.get("events", [])
        total = payload.get("total", 0)
        self.offset = payload.get("offset", self.offset)
        self.table.setHorizontalHeaderLabels(["时间", "模型", "工作区", "输入", "缓存", "输出", "Token", "预估费用"])
        rows = []
        for r in self.events:
            t = r.get("tokens", {})
            rows.append(
                [
                    str(r.get("timestamp", ""))[5:16],
                    NameCell(r.get("model", ""), color_for(r.get("model", "")), bold=False, compact=True),
                    r.get("cwd", ""),
                ]
                + [number(t.get(k)) for k in ("input", "cached_input", "output", "total")]
                + [
                    (
                        "未计价"
                        if r.get("pricing_source") == "unpriced"
                        else f'${r.get("cost_usd",{}).get("total",0):,.2f}'
                    )
                ]
            )
        self.table.set_rows(rows)
        self.page_label.setText(f"第 {self.offset//self.limit+1} 页 · 共 {total:,} 条")
        self.prev_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(self.offset + self.limit < total)

    def render_sources(self):
        sources = self.controller.context.get("sources", [])
        self.events = []
        self.table.setHorizontalHeaderLabels(["导入时间", "来源标识", "文件数", "用量事件", "额度快照", "", "", ""])
        rows = []
        for r in sources:
            stamp = r.get("first_imported_at", "")
            try:
                stamp = datetime.fromtimestamp(float(stamp)).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                stamp = str(stamp)[5:16]
            rows.append(
                [
                    stamp,
                    str(r.get("source_id", ""))[:14],
                    r.get("source_files", 0),
                    r.get("source_usage_events", 0),
                    r.get("source_rate_limit_snapshots", 0),
                    "",
                    "",
                    "",
                ]
            )
        self.table.set_rows(rows)
        self.page_label.setText(f"{len(sources)} 个历史来源")
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

    def render(self):
        source = self.controller.data.get("source", {})
        self.source_label.setText(f'本机 + {source.get("imported_sources",0)} 个历史来源')
        self.import_label.setText(f'{source.get("imported_usage_events",0):,} 条导入用量 · 已去重')
        self.import_button.setEnabled(self.controller.can_import and "import" not in self.controller.jobs)
        if self.mode == "sources":
            self.render_sources()


class QuotaPage(Page):
    def __init__(self, c):
        super().__init__(c)
        p = self.panel((290, 123, 1217, 478))
        self.title = text(p, "账号额度", (25, 24, 750, 43), 32, bold=True)
        self.ring = at(Donut(p, width=28), (50, 101, 280, 280))
        self.ring.big = 57
        line(p, 382, 83, 1).setFixedHeight(312)
        text(p, "剩余", (429, 90, 650, 39), 23, MUTED)
        self.remaining = text(p, "—", (429, 128, 690, 85), 72, bold=True)
        self.bar = at(Progress(p), (429, 230, 751, 36))
        self.details = []
        for x, title in ((429, "重置时间"), (731, "距离重置"), (978, "快照时间")):
            text(p, title, (x, 309, 225, 37), 23, MUTED)
            self.details.append(text(p, "—", (x, 348, 234, 44), 30, bold=True))
        text(p, "当前账号快照 · 不随统计范围变化", (26, 421, 1110, 34), 20, MUTED)
        p = self.panel((290, 616, 1217, 311))
        text(p, "快照详情", (25, 18, 850, 45), 32, bold=True)
        self.fields = []
        for x, y, caption in ((35, 98, "账户方案"), (650, 98, "窗口长度"), (35, 177, "已使用"), (650, 177, "数据状态")):
            text(p, caption, (x, y, 195, 45), 24, MUTED)
            self.fields.append(text(p, "—", (x + 204, y, 332, 45), 26, bold=True))
        for y in (82, 161, 242):
            line(p, 27, y, 1160)
        self.note = text(p, "官方额度不等于费用账单", (26, 260, 1160, 30), 18, MUTED)

    def render(self):
        data = self.controller.data
        quotas = quota_windows(data)
        if not quotas:
            self.remaining.setText("—")
            self.title.setText("额度 · 暂无有效快照")
            self.ring.set_values([], [], "—")
            self.bar.setValue(0)
            for lab in self.details + self.fields:
                lab.setText("—")
            return
        name, row = quotas[0]
        plan = str((data.get("rate_limits") or {}).get("plan_type", "")).upper()
        remaining = max(0, min(100, 100 - float(row["used_percent"])))
        self.title.setText((plan + " · " if plan else "") + name)
        self.remaining.setText(f"{remaining:g}%")
        self.ring.set_values([remaining, 100 - remaining], ["#28d1b1", "#a8e4ea"], f"{remaining:g}%")
        self.bar.setValue(remaining)
        for lab, v in zip(
            self.details,
            [
                str(row.get("resets_at", "")).replace("T", " ")[5:16],
                reset_label(row).replace("后重置", ""),
                str(row.get("observed_at", "")).replace("T", " ")[5:16],
            ],
        ):
            lab.setText(v)
        for lab, v in zip(
            self.fields,
            [
                plan or "未知",
                "7 天" if row.get("window_minutes") == 10080 else "5 小时",
                f'{row["used_percent"]:g}%',
                snapshot_status(row),
            ],
        ):
            lab.setText(v)
        extra = next((r for name, r in quotas if r is not row), None)
        self.note.setText(
            f'5 小时额度：剩余 {100-extra["used_percent"]:g}% · {reset_label(extra)}'
            if extra
            else "官方额度不等于费用账单"
        )


class SettingsPage(Page):
    def __init__(self, c):
        super().__init__(c)
        text(self, "全局设置 · 不随统计范围变化", (309, 108, 650, 29), 18, MUTED)
        p = self.panel((290, 149, 624, 797))
        text(p, "外观", (27, 17, 310, 40), 30, bold=True)
        reset = at(GlassButton("恢复默认", p), (456, 17, 150, 47))
        reset.clicked.connect(c.restore_defaults)
        text(p, "透明度", (28, 76, 120, 42), 24, bold=True)
        self.opacity_value = text(p, "65%", (548, 76, 56, 42), 23)
        self.opacity = at(QSlider(Qt.Horizontal, p), (135, 79, 385, 36))
        self.opacity.setRange(15, 90)
        self.opacity.setValue(c.transparency)
        self.opacity.setAccessibleName("透明度")
        self.opacity.valueChanged.connect(self.opacity_changed)
        self.presets = {}
        for i, (name, key) in enumerate((("通透", "clear"), ("平衡", "balanced"), ("磨砂", "frosted"))):
            b = at(PresetButton(name, key, p), (28 + i * 196, 137, 173, 210))
            b.setChecked(key == c.glass_preset)
            b.clicked.connect(lambda checked=False, k=key: self.set_preset(k))
            self.presets[key] = b
        self.switches = {}
        for i, (title, subtitle, key) in enumerate(
            (
                ("动态流彩", "窗口背景使用柔和的动态渐变效果", "motion"),
                ("始终置顶", "让窗口始终显示在其他窗口之上", "top"),
                ("图表动画", "启用图表的平滑动画效果", "chart"),
            )
        ):
            y = 386 + i * 87
            line(p, 27, y - 17, 570)
            text(p, title, (28, y, 470, 35), 23, bold=True)
            text(p, subtitle, (28, y + 34, 475, 27), 18, MUTED)
            b = at(GlassSwitch(p), (528, y + 7, 50, 27))
            b.setAccessibleName(title)
            b.setChecked(True)
            b.toggled.connect(lambda v, k=key: self.switch_changed(k, v))
            self.switches[key] = b
        line(p, 27, 640, 570)
        text(p, "窗口缩放", (28, 657, 400, 38), 24, bold=True)
        self.scale_label = text(p, "100%", (287, 674, 95, 31), 18)
        text(p, "50%", (28, 698, 70, 36), 20, MUTED)
        text(p, "150%", (548, 698, 70, 36), 20, MUTED)
        self.scale = at(QSlider(Qt.Horizontal, p), (89, 699, 438, 34))
        self.scale.setRange(50, 150)
        self.scale.setValue(100)
        self.scale.setAccessibleName("窗口缩放")
        self.scale.valueChanged.connect(self.scale_changed)
        text(p, "拖动窗口边缘也可缩放", (28, 748, 550, 29), 18, MUTED)
        p = self.panel((927, 149, 584, 797))
        text(p, "本地数据", (25, 17, 520, 43), 30, bold=True)
        self.data_values = []
        self.index_status = text(p, "正在连接", (295, 22, 261, 33), 18, MUTED)
        self.index_status.setAlignment(Qt.AlignRight)
        for i, (caption, value) in enumerate(
            (("数据库", "SQLite"), ("本机文件", "—"), ("历史来源", "—"), ("导入用量", "—"), ("额度快照", "—"))
        ):
            box = at(GlassPanel(p, 20, 60), (21, 78 + i * 85, 542, 80))
            text(box, caption, (22, 18, 177, 45), 24)
            self.data_values.append(text(box, value, (201, 18, 315, 45), 27, bold=i > 0))
        self.import_button = at(GlassButton("导入 SQLite", p, kind="database", primary=True), (24, 518, 536, 87))
        self.import_button.setFont(font(26))
        self.import_button.clicked.connect(c.import_history)
        self.rescan = at(GlassButton("重新扫描", p, kind="refresh"), (24, 620, 536, 81))
        self.rescan.setFont(font(26))
        self.rescan.clicked.connect(c.request_refresh)
        text(p, "数据保留在本机", (329, 724, 226, 38), 18, MUTED)

    def opacity_changed(self, value):
        self.opacity_value.setText(f"{value}%")
        self.controller.set_transparency(value)

    def set_preset(self, key):
        self.controller.glass_preset = key
        for k, b in self.presets.items():
            b.setChecked(k == key)
        self.opacity.setValue({"clear": 80, "balanced": 65, "frosted": 35}[key])
        self.controller.save_settings()
        self.controller.update()

    def switch_changed(self, key, value):
        if key == "top":
            self.controller.set_top(value)
        elif key == "motion":
            self.controller.set_motion(value)
        else:
            self.controller.chart_motion = value
            self.controller.render_pages()
            self.controller.save_settings()

    def scale_changed(self, value):
        self.scale_label.setText(f"{value}%")
        host = getattr(self.controller, "scaled_host", None)
        if host:
            host.set_scale(value / 100)

    def render(self):
        source = self.controller.data.get("source", {})
        for lab, key in zip(
            self.data_values[1:],
            ("files", "imported_sources", "imported_usage_events", "imported_rate_limit_snapshots"),
        ):
            lab.setText(f"{source.get(key,0):,}")
        c = self.controller
        self.import_button.setEnabled(c.can_import and "import" not in c.jobs)
        if hasattr(c, "status_label"):
            self.index_status.setText(c.status_label.text())
        self.opacity.blockSignals(True)
        self.opacity.setValue(c.transparency)
        self.opacity.blockSignals(False)
        self.opacity_value.setText(f"{c.transparency}%")
        for key, value in (("motion", c.motion), ("top", c.topmost), ("chart", c.chart_motion)):
            b = self.switches[key]
            b.blockSignals(True)
            b.setChecked(value)
            b.blockSignals(False)
