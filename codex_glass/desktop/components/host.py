"""Transparent proportional host for live canonical-size Qt surfaces."""

from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QPainter, QPalette, QTransform
from PyQt5.QtWidgets import QApplication, QFrame, QGraphicsScene, QGraphicsView


class ScaledSurfaceHost(QGraphicsView):
    """Display an actual QWidget at a uniformly scaled physical size.

    The surface remains at its canonical dimensions inside a graphics proxy,
    so layouts, painting, and hit testing all share one coordinate system.
    """

    scaleChanged = pyqtSignal(float)
    EDGE_MARGIN = 8

    def __init__(self, surface, scale=1.0, min_scale=0.7, max_scale=1.5):
        initial_position = surface.pos()
        flags = surface.windowFlags()
        title = surface.windowTitle()
        icon = surface.windowIcon()
        super().__init__()
        self.surface = surface
        self.min_scale = float(min_scale)
        self.max_scale = float(max_scale)
        if self.min_scale > self.max_scale:
            self.min_scale, self.max_scale = self.max_scale, self.min_scale
        self.scale = self._clamp(scale)
        self._resize_edges = ""
        self._resize_start_geometry = None
        self._syncing_visibility = False

        self.setWindowFlags(flags | Qt.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setWindowIcon(icon)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setStyleSheet("QGraphicsView { background: transparent; border: 0; }")
        self.setAutoFillBackground(False)
        palette = self.palette()
        palette.setColor(QPalette.Base, QColor(0, 0, 0, 0))
        palette.setColor(QPalette.Window, QColor(0, 0, 0, 0))
        self.setPalette(palette)
        self.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self.viewport().setAttribute(Qt.WA_NoSystemBackground, True)
        self.viewport().setAutoFillBackground(False)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self.scene_surface = QGraphicsScene(self)
        self.scene_surface.setBackgroundBrush(QColor(0, 0, 0, 0))
        self.setScene(self.scene_surface)
        self.proxy = self.scene_surface.addWidget(surface)
        self.proxy.setPos(0, 0)
        surface.scaled_host = self
        self.sync_size()
        self.move(initial_position)
        self.clamp_to_screen()
        surface.hide()

    def _clamp(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        return max(self.min_scale, min(self.max_scale, value))

    def set_scale(self, value):
        new_scale = self._clamp(value)
        if abs(new_scale - self.scale) < 1e-9:
            return
        self.scale = new_scale
        self.sync_size()
        self.scaleChanged.emit(float(self.scale))

    def sync_size(self):
        size = self.surface.size()
        width = max(1, size.width())
        height = max(1, size.height())
        self.scene_surface.setSceneRect(QRectF(0, 0, width, height))
        self.proxy.resize(width, height)
        self.setTransform(QTransform.fromScale(self.scale, self.scale))
        self.setFixedSize(max(1, round(width * self.scale)), max(1, round(height * self.scale)))
        self.viewport().update()

    def to_global(self, widget, point):
        canonical = widget.mapTo(self.surface, point)
        viewport_point = self.mapFromScene(QPointF(canonical))
        return self.viewport().mapToGlobal(viewport_point)

    def clamp_to_screen(self):
        app = QApplication.instance()
        if app is None:
            return
        screen = app.screenAt(self.frameGeometry().center()) or app.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        max_x = max(area.left(), area.right() - self.width() + 1)
        max_y = max(area.top(), area.bottom() - self.height() + 1)
        self.move(
            min(max(self.x(), area.left()), max_x),
            min(max(self.y(), area.top()), max_y),
        )

    def _edges_at(self, point):
        margin = self.EDGE_MARGIN
        edges = ""
        if point.x() < margin:
            edges += "w"
        elif point.x() >= self.width() - margin:
            edges += "e"
        if point.y() < margin:
            edges += "n"
        elif point.y() >= self.height() - margin:
            edges += "s"
        return edges

    @staticmethod
    def _cursor_for(edges):
        if edges in ("wn", "es"):
            return Qt.SizeFDiagCursor
        if edges in ("en", "ws"):
            return Qt.SizeBDiagCursor
        if edges in ("w", "e"):
            return Qt.SizeHorCursor
        if edges in ("n", "s"):
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._edges_at(event.pos())
            if edges:
                self._resize_edges = edges
                self._resize_start_geometry = self.geometry()
                event.accept()
                return
        super().mousePressEvent(event)

    def _drag_scale(self, global_position):
        geometry = self._resize_start_geometry
        if geometry is None:
            return self.scale
        base_width = max(1, self.surface.width())
        base_height = max(1, self.surface.height())
        edges = self._resize_edges
        boundary_left = geometry.x()
        boundary_top = geometry.y()
        boundary_right = geometry.x() + geometry.width()
        boundary_bottom = geometry.y() + geometry.height()
        x = global_position.x()
        y = global_position.y()
        if len(edges) == 1:
            if edges == "e":
                return (x - boundary_left) / base_width
            if edges == "w":
                return (boundary_right - x) / base_width
            if edges == "s":
                return (y - boundary_top) / base_height
            return (boundary_bottom - y) / base_height

        anchor_x = boundary_right if "w" in edges else boundary_left
        anchor_y = boundary_bottom if "n" in edges else boundary_top
        direction_x = -base_width if "w" in edges else base_width
        direction_y = -base_height if "n" in edges else base_height
        target_x = x - anchor_x
        target_y = y - anchor_y
        return (target_x * direction_x + target_y * direction_y) / (
            direction_x * direction_x + direction_y * direction_y
        )

    def mouseMoveEvent(self, event):
        if self._resize_edges and self._resize_start_geometry is not None:
            self._apply_resize(event.globalPos())
            event.accept()
            return
        edges = self._edges_at(event.pos())
        self.viewport().setCursor(QCursor(self._cursor_for(edges)))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_edges:
            self._apply_resize(event.globalPos())
            self._resize_edges = ""
            self._resize_start_geometry = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _apply_resize(self, global_position):
        start = self._resize_start_geometry
        if start is None:
            return
        edges = self._resize_edges
        fixed_right = start.x() + start.width()
        fixed_bottom = start.y() + start.height()
        new_scale = self._clamp(self._drag_scale(global_position))
        changed = abs(new_scale - self.scale) >= 1e-9
        if changed:
            self.scale = new_scale
            self.sync_size()
        x = fixed_right - self.width() if "w" in edges else start.x()
        y = fixed_bottom - self.height() if "n" in edges else start.y()
        self.move(x, y)
        if changed:
            self.scaleChanged.emit(float(self.scale))

    def showEvent(self, event):
        super().showEvent(event)
        if not self._syncing_visibility and not self.surface.isVisible():
            self._syncing_visibility = True
            self.surface.show()
            self._syncing_visibility = False

    def hideEvent(self, event):
        if not self._syncing_visibility and self.surface.isVisible():
            self._syncing_visibility = True
            self.surface.hide()
            self._syncing_visibility = False
        super().hideEvent(event)

    def closeEvent(self, event):
        self.surface.close()
        event.ignore()
