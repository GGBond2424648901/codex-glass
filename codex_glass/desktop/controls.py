"""Canonical native controls shared by every dashboard page and time scope."""

import math
from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPainterPath, QLinearGradient
from PyQt5.QtWidgets import (
    QWidget,
    QPushButton,
    QButtonGroup,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QHeaderView,
)
from codex_glass.desktop.components.style import font, label, INK, MUTED, material

BLUE = "#078aff"


def icon(p, kind, rect, color=INK):
    p.save()
    p.translate(rect.x(), rect.y())
    p.scale(rect.width() / 32, rect.height() / 32)
    p.setPen(QPen(QColor(color), 2.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    if kind == "overview":
        path.moveTo(3, 15)
        path.lineTo(16, 4)
        path.lineTo(29, 15)
        path.moveTo(7, 12)
        path.lineTo(7, 28)
        path.lineTo(14, 28)
        path.lineTo(14, 20)
        path.lineTo(20, 20)
        path.lineTo(20, 28)
        path.lineTo(25, 28)
        path.lineTo(25, 12)
    elif kind == "models":
        path.moveTo(4, 9)
        path.lineTo(16, 3)
        path.lineTo(28, 9)
        path.lineTo(28, 23)
        path.lineTo(16, 30)
        path.lineTo(4, 23)
        path.closeSubpath()
        path.moveTo(4, 9)
        path.lineTo(16, 16)
        path.lineTo(28, 9)
        path.moveTo(16, 16)
        path.lineTo(16, 30)
    elif kind in ("history", "clock"):
        p.drawEllipse(QRectF(3, 3, 26, 26))
        path.moveTo(16, 7)
        path.lineTo(16, 17)
        path.lineTo(23, 21)
    elif kind in ("quota", "database"):
        p.drawEllipse(QRectF(4, 4, 24, 9))
        path.moveTo(4, 8)
        path.lineTo(4, 25)
        path.cubicTo(4, 32, 28, 32, 28, 25)
        path.lineTo(28, 8)
        path.moveTo(4, 16)
        path.cubicTo(4, 23, 28, 23, 28, 16)
        path.moveTo(4, 22)
        path.cubicTo(4, 29, 28, 29, 28, 22)
    elif kind == "settings":
        for i in range(24):
            a = math.pi * 2 * i / 24
            r = 14 if i % 3 in (0, 1) else 10
            point = QPointF(16 + r * math.cos(a), 16 + r * math.sin(a))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        path.closeSubpath()
        p.drawEllipse(QRectF(11, 11, 10, 10))
    elif kind == "refresh":
        p.drawArc(QRectF(5, 5, 22, 22), 35 * 16, 275 * 16)
        path.moveTo(26, 3)
        path.lineTo(27, 12)
        path.lineTo(19, 10)
    elif kind == "search":
        p.drawEllipse(QRectF(4, 4, 18, 18))
        path.moveTo(20, 20)
        path.lineTo(28, 28)
    elif kind in ("down", "up"):
        path.moveTo(9, 13 if kind == "down" else 20)
        path.lineTo(16, 20 if kind == "down" else 13)
        path.lineTo(23, 13 if kind == "down" else 20)
    elif kind == "filter":
        for y, x in ((8, 10), (16, 22), (24, 14)):
            p.drawLine(5, y, 27, y)
            p.setBrush(QColor("#e6f3ff"))
            p.drawEllipse(QPointF(x, y), 2.5, 2.5)
            p.setBrush(Qt.NoBrush)
    elif kind in ("export", "import"):
        path.moveTo(5, 18)
        path.lineTo(5, 28)
        path.lineTo(27, 28)
        path.lineTo(27, 18)
        path.moveTo(16, 3)
        path.lineTo(16, 22)
        if kind == "export":
            path.moveTo(10, 9)
            path.lineTo(16, 3)
            path.lineTo(22, 9)
        else:
            path.moveTo(10, 16)
            path.lineTo(16, 22)
            path.lineTo(22, 16)
    elif kind == "folder":
        path.moveTo(3, 10)
        path.lineTo(3, 27)
        path.lineTo(29, 27)
        path.lineTo(29, 9)
        path.lineTo(16, 9)
        path.lineTo(12, 5)
        path.lineTo(3, 5)
        path.closeSubpath()
    elif kind == "close":
        path.moveTo(9, 9)
        path.lineTo(23, 23)
        path.moveTo(23, 9)
        path.lineTo(9, 23)
    elif kind == "minimize":
        path.moveTo(9, 17)
        path.lineTo(23, 17)
    elif kind == "maximize":
        p.drawRect(QRectF(10, 10, 13, 13))
    p.drawPath(path)
    p.restore()


class GlassPanel(QWidget):
    def __init__(self, parent=None, radius=24, alpha=95):
        super().__init__(parent)
        self.radius = radius
        self.alpha = alpha

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0, QColor(255, 255, 255, self.alpha + 35))
        g.setColorAt(1, QColor(255, 255, 255, self.alpha))
        p.setBrush(g)
        p.setPen(QPen(QColor(255, 255, 255, 195), 1.3))
        p.drawRoundedRect(QRectF(0.8, 0.8, self.width() - 1.6, self.height() - 1.6), self.radius, self.radius)
        rim = QLinearGradient(0, 0, 0, self.height())
        rim.setColorAt(0, QColor(255, 255, 255, 120))
        rim.setColorAt(0.25, QColor(255, 255, 255, 15))
        rim.setColorAt(1, QColor(138, 173, 209, 27))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rim, 2.6))
        p.drawRoundedRect(QRectF(2.2, 2.2, self.width() - 4.4, self.height() - 4.4), self.radius - 2, self.radius - 2)


class GlassButton(QPushButton):
    def __init__(self, text="", parent=None, kind=None, primary=False, quiet=False):
        super().__init__(text, parent)
        self.kind = kind
        self.primary = primary
        self.quiet = quiet
        self.setFont(font(20))
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(text or kind or "按钮")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.8, 0.8, self.width() - 1.6, self.height() - 1.6)
        radius = min(26, r.height() / 2)
        active = self.isChecked()
        fill = self.primary
        if not self.isEnabled():
            p.setOpacity(0.38)
        if not self.quiet or active or self.underMouse() or self.hasFocus():
            g = QLinearGradient(0, 0, 0, self.height())
            g.setColorAt(0, QColor("#72b8ff") if fill else QColor(255, 255, 255, 225 if active else 135))
            g.setColorAt(1, QColor("#0874ff") if fill else QColor(243, 251, 255, 200 if active else 65))
            p.setBrush(g)
            p.setPen(QPen(QColor("#68acff") if self.hasFocus() else QColor(255, 255, 255, 220), 1.3))
            p.drawRoundedRect(r, radius, radius)
        color = "#ffffff" if fill else (BLUE if active else INK)
        p.setPen(QColor(color))
        p.setFont(font(self.font().pixelSize(), active or self.font().bold()))
        if self.kind:
            if self.text():
                total = p.fontMetrics().horizontalAdvance(self.text()) + 44
                left = (self.width() - total) / 2
                icon(p, self.kind, QRectF(left, self.height() / 2 - 15, 30, 30), color)
                p.drawText(QRectF(left + 44, 0, total - 44, self.height()), Qt.AlignVCenter, self.text())
            else:
                icon(p, self.kind, QRectF(self.width() / 2 - 16, self.height() / 2 - 16, 32, 32), color)
        else:
            p.drawText(r, Qt.AlignCenter, self.text())


class Segmented(GlassPanel):
    changed = pyqtSignal(str)

    def __init__(self, items, parent=None):
        super().__init__(parent, radius=26, alpha=15)
        self.items = items
        self.buttons = {}
        self.group = QButtonGroup(self)
        for text, key in items:
            b = GlassButton(text, self, quiet=True)
            b.setCheckable(True)
            b.clicked.connect(lambda checked=False, k=key: self.changed.emit(k))
            self.buttons[key] = b
            self.group.addButton(b)
        self.set_value(items[0][1])

    def resizeEvent(self, event):
        width = (self.width() - 8) / len(self.items)
        for i, (_, key) in enumerate(self.items):
            self.buttons[key].setGeometry(round(4 + i * width), 4, round(width), self.height() - 8)

    def set_value(self, key):
        if key in self.buttons:
            self.buttons[key].setChecked(True)


class NavigationButton(GlassButton):
    def __init__(self, title, key, parent=None):
        super().__init__(title, parent, quiet=True)
        self.nav_kind = key
        self.setCheckable(True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.isChecked() or self.underMouse() or self.hasFocus():
            g = QLinearGradient(0, 0, self.width(), self.height())
            g.setColorAt(0, QColor(255, 255, 255, 205))
            g.setColorAt(0.55, QColor(189, 219, 255, 135))
            g.setColorAt(1, QColor(246, 253, 255, 170))
            p.setBrush(g)
            p.setPen(QPen(QColor(255, 255, 255, 235), 1.5))
            p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 30, 30)
        color = "#0755ff" if self.isChecked() else "#304b78"
        icon(p, self.nav_kind, QRectF(23, 16, 32, 32), color)
        p.setPen(QColor(INK if self.isChecked() else color))
        p.setFont(font(22, self.isChecked()))
        p.drawText(QRectF(76, 0, 146, self.height()), Qt.AlignVCenter, self.text())


class GlassHeader(QHeaderView):
    """Paint one translucent band, never OS-themed opaque table sections."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setFont(font(20))
        self.setSectionsClickable(True)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)

    def paintEvent(self, event):
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(122, 144, 190, 17))
        p.drawRoundedRect(QRectF(0, 0, self.viewport().width(), self.height()), 14, 14)
        p.setFont(font(20))
        p.setPen(QColor("#304c7b"))
        for i in range(self.count()):
            r = QRectF(self.sectionViewportPosition(i) + 14, 0, self.sectionSize(i) - 25, self.height())
            p.drawText(
                r,
                Qt.AlignVCenter,
                p.fontMetrics().elidedText(
                    str(self.model().headerData(i, Qt.Horizontal) or ""), Qt.ElideRight, int(r.width())
                ),
            )


class SearchBox(QLineEdit):
    def __init__(self, placeholder, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setFont(font(20))
        self.setTextMargins(54, 0, 16, 0)
        self.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,55);border:1px solid rgba(255,255,255,230);border-radius:24px;color:#18375e;selection-background-color:#a8d5ff;}QLineEdit:focus{border:1.5px solid #6ab9ff;}"
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        icon(p, "search", QRectF(18, self.height() / 2 - 13, 26, 26), MUTED)


class Progress(QWidget):
    def __init__(self, parent=None, color="#28d1b1"):
        super().__init__(parent)
        self.value = 0
        self.color = color

    def setValue(self, value):
        self.value = max(0, min(100, float(value)))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(116, 139, 185, 55))
        p.drawRoundedRect(r, self.height() / 2, self.height() / 2)
        if self.value:
            p.setBrush(QColor(self.color))
            r.setWidth(r.width() * self.value / 100)
            p.drawRoundedRect(r, self.height() / 2, self.height() / 2)


class Donut(QWidget):
    def __init__(self, parent=None, width=27):
        super().__init__(parent)
        self.values = []
        self.colors = []
        self.center = "—"
        self.caption = "剩余"
        self.stroke = width
        self.big = 44

    def set_values(self, values, colors, center, caption="剩余"):
        self.values = values
        self.colors = colors
        self.center = center
        self.caption = caption
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        size = min(self.width(), self.height())
        r = QRectF(
            (self.width() - size) / 2 + self.stroke / 2,
            (self.height() - size) / 2 + self.stroke / 2,
            size - self.stroke,
            size - self.stroke,
        )
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(125, 199, 213, 75), self.stroke))
        p.drawEllipse(r)
        angle = 90 * 16
        total = sum(self.values)
        quota = self.caption == "剩余" and len(self.values) == 2
        if quota:
            p.setPen(QPen(QColor(self.colors[1]), self.stroke))
            p.drawEllipse(r)
        for i, (v, c) in enumerate(zip(self.values, self.colors)):
            if quota and i:
                continue
            span = round(v / total * 360 * 16) if total else 0
            gradient = QLinearGradient(r.topLeft(), r.bottomRight())
            gradient.setColorAt(0, QColor("#32e3c4") if quota else QColor(c))
            gradient.setColorAt(1, QColor(c))
            p.setPen(QPen(gradient, self.stroke, Qt.SolidLine, Qt.RoundCap if quota else Qt.FlatCap))
            p.drawArc(r, angle, -span)
            angle -= span
        p.setPen(QColor(INK))
        p.setFont(font(self.big, True))
        p.drawText(QRectF(0, self.height() / 2 - 39, self.width(), 54), Qt.AlignCenter, self.center)
        p.setFont(font(22))
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(0, self.height() / 2 + 16, self.width(), 30), Qt.AlignCenter, self.caption)


class NameCell(QWidget):
    def __init__(self, text, color, parent=None, bold=True, compact=False):
        super().__init__(parent)
        self.text = text
        self.color = color
        self.bold = bold
        self.compact = compact
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(
            QPointF(15 if self.compact else 21, self.height() / 2), 9 if self.compact else 10, 9 if self.compact else 10
        )
        left = 36 if self.compact else 53
        width = self.width() - left - 5
        p.setFont(font(20 if self.compact else 21, self.bold))
        p.setPen(QColor(INK))
        p.drawText(
            QRectF(left, 0, width, self.height()),
            Qt.AlignVCenter,
            p.fontMetrics().elidedText(self.text, Qt.ElideRight, width),
        )


class GlassTable(QTableWidget):
    def __init__(self, headers, widths, parent=None, row_height=72):
        super().__init__(0, len(headers), parent)
        self.headers = headers
        self.widths = widths
        self.row_height = row_height
        self.show_selection = False
        self.setHorizontalHeader(GlassHeader(self))
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(row_height)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.horizontalHeader().setFixedHeight(62)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setShowGrid(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFont(font(20))
        self.setFrameShape(QTableWidget.NoFrame)
        self.setStyleSheet(
            "QTableWidget{background:transparent;border:0;outline:0;color:#07183e;selection-background-color:rgba(86,181,255,55);selection-color:#07183e;}QTableWidget::item{padding:0px 14px;border-bottom:1px solid rgba(137,167,211,75);}QHeaderView::section{background:rgba(255,255,255,18);color:#304c7b;border:0;padding-left:15px;font-size:20px;}QTableCornerButton::section{border:0;background:transparent;}QScrollBar:vertical{width:6px;background:transparent;}QScrollBar::handle:vertical{background:rgba(113,133,173,85);min-height:25px;border-radius:3px;}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        )
        self.viewport().setAutoFillBackground(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        total = sum(self.widths)
        w = self.viewport().width()
        for i, v in enumerate(self.widths):
            self.setColumnWidth(i, round(v / total * w))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.show_selection and self.currentRow() >= 0:
            row = self.currentRow()
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor("#4f9fff"), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(
                QRectF(1, self.rowViewportPosition(row) + 2, self.viewport().width() - 3, self.rowHeight(row) - 4),
                14,
                14,
            )

    def set_rows(self, rows):
        self.setUpdatesEnabled(False)
        selected = self.currentRow()
        self.clearContents()
        self.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.setRowHeight(i, self.row_height)
            for j, value in enumerate(row):
                if isinstance(value, QWidget):
                    self.setCellWidget(i, j, value)
                else:
                    item = QTableWidgetItem(str(value))
                    item.setFont(font(20))
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                    self.setItem(i, j, item)
        if rows:
            self.selectRow(max(0, min(selected, len(rows) - 1)))
        self.setUpdatesEnabled(True)


class PresetButton(GlassButton):
    def __init__(self, text, key, parent=None):
        super().__init__(text, parent)
        self.key = key
        self.setCheckable(True)

    def paintEvent(self, event):
        p = QPainter(self)
        r = QRectF(2, 2, self.width() - 4, self.height() - 42)
        material(p, r, {"clear": 80, "balanced": 65, "frosted": 35}[self.key], self.key, 0)
        p.setPen(QPen(QColor(BLUE if self.isChecked() else "#ffffff"), 3 if self.isChecked() else 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 22, 22)
        p.setFont(font(22, self.isChecked()))
        p.setPen(QColor(INK))
        p.drawText(QRectF(0, self.height() - 35, self.width(), 32), Qt.AlignCenter, self.text())
