"""Small native vector charts; animation and zoom retain real samples."""

import math

from PyQt5.QtCore import QEasingCurve, QPoint, QPointF, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

from codex_glass.desktop.components.popups import GlassInfoPopup


class GlassChart(QWidget):
    rangeChanged = pyqtSignal(str, str)

    def __init__(self, parent=None, color="#149cff", mini=False):
        super().__init__(parent)
        self.color = QColor(color)
        self.mini = mini
        self.labels = []
        self.show_endpoint = False
        self.target_values = []
        self.display_values = []
        self.start_values = []
        self.unit = "Token / min"
        self.motion = True
        self.live = False
        self.phase = 0
        self.hover_index = None
        self.axis_font_size = 13
        self.y_axis = False
        self._view_start = 0.0
        self._view_end = 0.0
        self.info_popup = GlassInfoPopup(self)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(620)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self.interpolate)
        self.pulse = QTimer(self)
        self.pulse.setInterval(40)
        self.pulse.timeout.connect(self.tick)
        self.setMouseTracking(True)
        self.setAccessibleName("用量趋势图")

    def set_series(self, values, labels=None, animate=True, unit="Token / min"):
        clean = [max(0, float(v)) if math.isfinite(float(v)) else 0 for v in values]
        new_labels = list(labels or [])
        old_labels = self.labels[:]
        old_unit = self.unit
        old_count = len(self.target_values)
        old_view = (self._view_start, self._view_end)
        was_zoomed = old_count > 1 and old_view[1] - old_view[0] < old_count - 1 - 1e-7
        values_changed = clean != self.target_values
        self.target_values = clean
        self.labels = new_labels
        self.unit = unit
        self._preserve_or_reset_view(
            old_labels, new_labels, old_unit, unit, old_count, len(clean), old_view, was_zoomed
        )
        if not values_changed:
            self.update()
            return
        self.animation.stop()
        old = self.display_values[:]
        if not old or not animate or not self.motion or not clean:
            self.display_values = clean[:]
            self.update()
            return
        # Right-align sample windows so a rolling time range does not stretch existing history.
        self.start_values = ([0.0] * max(0, len(clean) - len(old)) + old)[-len(clean) :]
        self.animation.start()

    @property
    def view_range(self):
        return (self._view_start, self._view_end)

    def _preserve_or_reset_view(
        self, old_labels, new_labels, old_unit, new_unit, old_count, new_count, old_view, was_zoomed
    ):
        if new_count <= 1:
            self._set_view(0.0, 0.0)
            return
        if not was_zoomed or old_unit != new_unit:
            self._set_view(0.0, float(new_count - 1))
            return
        offset = None
        if old_labels and new_labels:
            new_index = {label: index for index, label in enumerate(new_labels)}
            offsets = [new_index[label] - index for index, label in enumerate(old_labels) if label in new_index]
            if offsets and all(value == offsets[0] for value in offsets):
                offset = float(offsets[0])
        elif not old_labels and not new_labels and old_count == new_count:
            offset = 0.0
        if offset is None:
            self._set_view(0.0, float(new_count - 1))
        else:
            self._set_view(old_view[0] + offset, old_view[1] + offset)

    def _set_view(self, start, end, emit=True):
        full = max(0.0, float(len(self.target_values) - 1))
        if full <= 0:
            self._view_start = self._view_end = 0.0
        else:
            span = min(full, max(0.0, float(end) - float(start)))
            start = float(start)
            end = start + span
            if start < 0:
                end -= start
                start = 0.0
            if end > full:
                start -= end - full
                end = full
            self._view_start = max(0.0, start)
            self._view_end = min(full, end)
        self.hover_index = None
        self.info_popup.hide()
        if emit:
            self._emit_range()
        self.update()

    def reset_zoom(self):
        count = len(self.target_values)
        self._set_view(0.0, float(max(0, count - 1)))

    def set_view_range(self, start, end):
        """Set a continuous sample-index viewport within the full series."""
        count = len(self.target_values)
        if count < 2:
            self.reset_zoom()
            return self.view_range
        start = float(start)
        end = float(end)
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("view range endpoints must be finite")
        if end < start:
            start, end = end, start
        minimum = min(4.0, float(count - 1))
        if end - start < minimum:
            center = (start + end) / 2
            start = center - minimum / 2
            end = center + minimum / 2
        self._set_view(start, end)
        return self.view_range

    def zoom_at(self, anchor, steps):
        count = len(self.target_values)
        if self.mini or count < 2 or not steps:
            return False
        anchor = max(0.0, min(1.0, float(anchor)))
        full = float(count - 1)
        current = max(0.0, self._view_end - self._view_start)
        minimum = min(4.0, full)
        new_span = max(minimum, min(full, current / (1.25 ** float(steps))))
        anchor_index = self._view_start + anchor * current
        self._set_view(anchor_index - anchor * new_span, anchor_index + (1 - anchor) * new_span)
        return True

    def _viewport_indices(self):
        count = len(self.target_values)
        if not count:
            return (0, -1)
        return (max(0, int(math.ceil(self._view_start - 1e-9))), min(count - 1, int(math.floor(self._view_end + 1e-9))))

    def _emit_range(self):
        first, last = self._viewport_indices()
        if first < 0 or last < first:
            self.rangeChanged.emit("", "")
            return
        left = self.labels[first] if first < len(self.labels) else str(first)
        right = self.labels[last] if last < len(self.labels) else str(last)
        self.rangeChanged.emit(str(left), str(right))

    def interpolate(self, value):
        t = float(value)
        self.display_values = [a + (b - a) * t for a, b in zip(self.start_values, self.target_values)]
        if t >= 1:
            self.display_values = self.target_values[:]
        self.update()

    def set_live(self, live):
        self.live = bool(live)
        if self.live and self.motion and self.isVisible() and not self.mini:
            self.pulse.start()
        else:
            self.pulse.stop()
        self.update()

    def tick(self):
        self.phase = (self.phase + 0.08) % (2 * math.pi)
        self.update()

    def hideEvent(self, event):
        self.pulse.stop()
        self.info_popup.hide()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.set_live(self.live)

    def plot_rect(self):
        left = 3
        if self.y_axis and not self.mini:
            from PyQt5.QtGui import QFontMetrics

            f = QFont("Arial")
            f.setPixelSize(self.axis_font_size)
            metrics = QFontMetrics(f)
            left = max(
                52,
                max(metrics.horizontalAdvance(self._compact_number(self._visible_peak() * i / 4)) for i in range(5))
                + 8,
            )
        return QRectF(left, 6, max(1, self.width() - left - 6), max(1, self.height() - (29 if not self.mini else 9)))

    def path_for(self, values, rect, indices=None, peak=None):
        path = QPainterPath()
        if not values:
            return path, []
        if indices is None:
            indices = list(range(len(values)))
        peak = max(1.0, float(peak if peak is not None else max(max(values), max(self.target_values or [0])) * 1.12))
        span = max(1e-9, self._view_end - self._view_start)
        if self._view_end <= self._view_start and len(values) > 1:
            span = float(len(values) - 1)
        points = [
            QPointF(
                rect.left() + (i - self._view_start) * rect.width() / span, rect.bottom() - v / peak * rect.height()
            )
            for i, v in zip(indices, values)
        ]
        path.moveTo(points[0])
        for a, b in zip(points, points[1:]):
            mid = (a.x() + b.x()) / 2
            path.cubicTo(QPointF(mid, a.y()), QPointF(mid, b.y()), b)
        return path, points

    def _visible_peak(self):
        if not self.target_values:
            return 1.0
        first = max(0, int(math.floor(self._view_start)))
        last = int(math.ceil(self._view_end))
        visible = []
        for series in (self.target_values, self.display_values):
            series_last = min(len(series) - 1, last)
            if series_last >= first:
                visible.extend(series[first : series_last + 1])
        return max(1.0, max(visible or [0]) * 1.12)

    @staticmethod
    def _compact_number(value):
        value = float(value)
        for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if abs(value) >= divisor:
                number = value / divisor
                return f"{number:.1f}".rstrip("0").rstrip(".") + suffix
        return f"{value:.0f}"

    def _format_axis_label(self, index):
        if index >= len(self.labels):
            return str(index)
        text = str(self.labels[index])
        if self.unit == "每日 Token":
            return text[5:10].replace("-", "/") if len(text) >= 10 else text
        if len(text) >= 16 and ("T" in text[:12] or " " in text[:12]):
            return text[11:16]
        return text[-5:]

    def _axis_indexes(self):
        first, last = self._viewport_indices()
        if last < first:
            return []
        middle = max(first, min(last, round((self._view_start + self._view_end) / 2)))
        return [first, middle, last]

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.plot_rect()
        peak = self._visible_peak()
        first = max(0, int(math.floor(self._view_start)) - 1)
        last = min(len(self.display_values) - 1, int(math.ceil(self._view_end)) + 1)
        indices = list(range(first, last + 1)) if last >= first else []
        visible = [self.display_values[index] for index in indices]
        path, points = self.path_for(visible, rect, indices, peak)
        if not self.mini:
            font = QFont("Arial")
            font.setPixelSize(self.axis_font_size)
            p.setFont(font)
            p.setPen(QPen(QColor(255, 255, 255, 80), 0.6))
            for i in range(7):
                x = rect.left() + rect.width() * i / 6
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            if self.y_axis:
                for i in range(5):
                    y = rect.bottom() - rect.height() * i / 4
                    p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                    p.setPen(QColor("#46638d"))
                    p.drawText(
                        QRectF(0, y - 10, rect.left() - 7, 20),
                        Qt.AlignRight | Qt.AlignVCenter,
                        self._compact_number(peak * i / 4),
                    )
                    p.setPen(QPen(QColor(255, 255, 255, 80), 0.6))
            p.setPen(QPen(QColor(255, 255, 255, 170), 1))
            p.drawLine(rect.bottomLeft(), rect.bottomRight())
        if points:
            p.save()
            p.setClipRect(rect.adjusted(-1, -1, 1, 1))
            fill = QPainterPath(path)
            fill.lineTo(points[-1].x(), rect.bottom())
            fill.lineTo(points[0].x(), rect.bottom())
            fill.closeSubpath()
            gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            top = QColor(self.color)
            top.setAlpha(190 if not self.mini else 160)
            middle = QColor(self.color)
            middle.setAlpha(100 if not self.mini else 76)
            bottom = QColor(self.color)
            bottom.setAlpha(12 if not self.mini else 6)
            gradient.setColorAt(0, top)
            gradient.setColorAt(0.55, middle)
            gradient.setColorAt(1, bottom)
            p.fillPath(fill, gradient)
            glow = QColor(self.color)
            glow.setAlpha(24)
            p.setPen(QPen(glow, 7 if not self.mini else 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(
                QPen(QColor(255, 255, 255, 180), 3.8 if not self.mini else 2.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            )
            p.drawPath(path)
            p.setPen(QPen(self.color, 2.4 if not self.mini else 2.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            endpoint_index = min(len(self.display_values) - 1, int(math.floor(self._view_end + 1e-9)))
            endpoint_pos = indices.index(endpoint_index) if endpoint_index in indices else len(points) - 1
            if not self.mini or self.show_endpoint:
                end = points[endpoint_pos]
                p.setPen(QPen(QColor(255, 255, 255, 200), 1, Qt.DashLine))
                p.drawLine(QPointF(end.x(), rect.top()), QPointF(end.x(), rect.bottom()))
                radius = 7 + 2 * math.sin(self.phase) if self.live and self.motion else 5
                halo = QColor(self.color)
                halo.setAlpha(35)
                p.setPen(Qt.NoPen)
                p.setBrush(halo)
                p.drawEllipse(end, radius, radius)
                p.setPen(QPen(Qt.white, 1.5))
                p.setBrush(self.color)
                p.drawEllipse(end, 3.2, 3.2)
            if self.hover_index is not None and self.hover_index in indices:
                point = points[indices.index(self.hover_index)]
                p.setPen(QPen(QColor("#7095bd"), 0.8, Qt.DashLine))
                p.drawLine(QPointF(point.x(), rect.top()), QPointF(point.x(), rect.bottom()))
                p.setPen(QPen(Qt.white, 1.5))
                p.setBrush(self.color)
                p.drawEllipse(point, 3, 3)
            p.restore()
        if not self.mini:
            font = QFont("Arial")
            font.setPixelSize(self.axis_font_size)
            p.setFont(font)
            p.setPen(QColor("#46638d"))
            if self.labels:
                indexes = self._axis_indexes()
                for j, i in enumerate(indexes):
                    text = self._format_axis_label(i)
                    width = 80
                    x = rect.left() if j == 0 else (rect.right() - width if j == 2 else rect.center().x() - width / 2)
                    align = Qt.AlignLeft if j == 0 else (Qt.AlignRight if j == 2 else Qt.AlignHCenter)
                    p.drawText(QRectF(x, rect.bottom() + 5, width, 19), align | Qt.AlignVCenter, text)
            else:
                p.drawText(QRectF(0, rect.center().y() - 10, self.width(), 20), Qt.AlignCenter, "等待用量记录")

    def mouseMoveEvent(self, event):
        if not self.target_values or self.mini:
            return
        rect = self.plot_rect()
        if not rect.contains(event.pos()):
            self.hover_index = None
            self.info_popup.hide()
            self.update()
            return
        anchor = max(0.0, min(1.0, (event.x() - rect.left()) / max(1, rect.width())))
        sample = self._view_start + anchor * (self._view_end - self._view_start)
        first, last = self._viewport_indices()
        i = max(first, min(last, round(sample)))
        self.hover_index = i
        self.update()
        i = self.hover_index
        stamp = self.labels[i] if i < len(self.labels) else ""
        self.info_popup.show_info(stamp, self.target_values[i], self.unit, event.globalPos() + QPoint(12, 12))

    def leaveEvent(self, event):
        self.hover_index = None
        self.info_popup.hide()
        self.update()

    def wheelEvent(self, event):
        if self.mini or not self.plot_rect().contains(event.pos()) or not self.target_values:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        anchor = (event.x() - self.plot_rect().left()) / max(1, self.plot_rect().width())
        self.info_popup.hide()
        self.zoom_at(anchor, delta / 120.0)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if not self.mini and event.button() == Qt.LeftButton and self.plot_rect().contains(event.pos()):
            self.info_popup.hide()
            self.reset_zoom()
            event.accept()
            return
        event.ignore()
