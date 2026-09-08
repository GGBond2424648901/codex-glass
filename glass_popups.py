"""Reusable translucent native popup surfaces for the desktop UI."""

from PyQt5.QtCore import QPoint, QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def _retain_for_owner(owner, popup):
    """Keep Python popup wrappers alive for as long as their owner exists."""
    if owner is None:
        return
    retained = getattr(owner, "_glass_popup_children", None)
    if retained is None:
        retained = []
        setattr(owner, "_glass_popup_children", retained)
    retained.append(popup)
    owner.destroyed.connect(popup.deleteLater)


def _available_geometry(point):
    app = QApplication.instance()
    screen = app.screenAt(point) if app is not None else None
    if screen is None and app is not None:
        screen = app.primaryScreen()
    return screen.availableGeometry() if screen is not None else None

def release_owned_popup(owner,popup):
    """Dispose a replaced native popup without retaining its entire widget tree."""
    popup.hide()
    retained=getattr(owner,'_glass_popup_children',[])
    if popup in retained:retained.remove(popup)
    try:owner.destroyed.disconnect(popup.deleteLater)
    except (TypeError,RuntimeError):pass
    popup.deleteLater()


def _clamped_position(widget, point):
    bounds = _available_geometry(point)
    if bounds is None:
        return point
    size = widget.size().expandedTo(widget.minimumSize())
    width = size.width()
    height = size.height()
    x = min(max(point.x(), bounds.left()), max(bounds.left(), bounds.right() - width + 1))
    y = min(max(point.y(), bounds.top()), max(bounds.top(), bounds.bottom() - height + 1))
    return QPoint(x, y)


class _ChoiceCheck(QCheckBox):
    """Checkbox whose complete painted row is an activation target."""

    def __init__(self,text):
        super().__init__(text)
        font=QFont('Microsoft YaHei UI');font.setPixelSize(18);self.setFont(font);self.setMinimumHeight(44)
    def sizeHint(self):return QSize(max(280,self.fontMetrics().horizontalAdvance(self.text())+65),44)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse() or self.hasFocus():
            p.setPen(Qt.NoPen);p.setBrush(QColor(218,237,255,180));p.drawRoundedRect(QRectF(self.rect()).adjusted(0,1,0,-1),10,10)
        box=QRectF(12,(self.height()-20)/2,20,20)
        p.setPen(QPen(QColor('#168fff' if self.isChecked() else '#8ea9ca'),1.2));p.setBrush(QColor('#168fff') if self.isChecked() else QColor(255,255,255,150));p.drawRoundedRect(box,6,6)
        if self.isChecked():
            tick=QPainterPath();tick.moveTo(box.left()+4,box.top()+10);tick.lineTo(box.left()+8,box.top()+14);tick.lineTo(box.left()+16,box.top()+6)
            p.setPen(QPen(Qt.white,2,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawPath(tick)
        p.setFont(self.font());p.setPen(QColor('#102d62'));p.drawText(QRectF(46,0,self.width()-54,self.height()),Qt.AlignVCenter,p.fontMetrics().elidedText(self.text(),Qt.ElideRight,self.width()-54))

    def hitButton(self, point):
        return self.rect().contains(point)


class GlassMenu(QMenu):
    """A QMenu-compatible popup with a deliberate glass surface."""

    def __init__(self, owner=None):
        super().__init__(None)
        _retain_for_owner(owner, self)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setFont(QFont("Microsoft YaHei UI", 12))
        self.setStyleSheet(
            """
            QMenu { background: transparent; border: none; padding: 10px; }
            QMenu::item { color: #153b70; background: transparent; border-radius: 10px;
                          padding: 10px 18px 10px 38px; margin: 2px 3px; min-height: 16px; }
            QMenu::item:selected { color: #075de6; background: rgba(224,239,255,225); }
            QMenu::item:checked { color: #075de6; font-weight: 600; }
            QMenu::indicator { width: 16px; height: 16px; left: 14px; }
            QMenu::indicator:checked { background: #0878f9; border: 2px solid white; border-radius: 5px; }
            QMenu::separator { height: 1px; background: rgba(78,126,184,55); margin: 7px 16px; }
            """
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        shadow = QPainterPath()
        shadow.addRoundedRect(QRectF(self.rect()).adjusted(5, 6, -3, -2), 17, 17)
        painter.fillPath(shadow, QColor(36, 83, 145, 40))
        surface = QPainterPath()
        surface.addRoundedRect(QRectF(self.rect()).adjusted(3, 3, -5, -5), 16, 16)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor(252, 254, 255, 246))
        gradient.setColorAt(0.55, QColor(232, 243, 255, 239))
        gradient.setColorAt(1, QColor(242, 235, 255, 241))
        painter.fillPath(surface, gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 225), 1.2))
        painter.drawPath(surface)
        painter.end()
        super().paintEvent(event)


class GlassChoicePopup(QWidget):
    """Reusable staged single- or multi-choice popup.

    Choice changes are drafts until the Apply button is activated. Hiding,
    pressing Escape, or clicking outside discards the draft.
    """

    def __init__(self, options, selected, callback, owner=None, multi=False):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        _retain_for_owner(owner, self)
        self.options = [str(option) for option in options]
        self.multi = bool(multi)
        self.callback = callback
        self.selection = self._normalise_selection(selected)
        self._draft = self._copy_selection(self.selection)
        self._applying = False
        self._focus_index = self._initial_focus_index()
        self.choices = []
        self.choice_for = {}

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(330)
        popup_font=QFont('Microsoft YaHei UI');popup_font.setPixelSize(16);self.setFont(popup_font)
        self.setStyleSheet(
            """
            QLabel { color: #173d73; font-family: 'Microsoft YaHei UI'; font-size: 16px; }
            QLineEdit { color: #315b91; background: rgba(255,255,255,170);
                        border: 1px solid rgba(255,255,255,220); border-radius: 14px;
                        padding: 8px 13px; font-size: 16px; }
            QCheckBox { color: #102d62; spacing: 12px; padding: 8px 10px;
                        min-height: 24px; border-radius: 10px; font-size: 18px; }
            QCheckBox:hover, QCheckBox:focus { background: rgba(217,237,255,205); color: #075de6; }
            QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px;
                                   border: 1px solid #6387bd; background: rgba(255,255,255,150); }
            QCheckBox::indicator:checked { border: 2px solid white; background: #0878f9; }
            QPushButton { color: #075de6; background: rgba(255,255,255,115); border: none;
                          border-radius: 12px; padding: 8px 15px; font-size: 16px; }
            QPushButton:hover, QPushButton:focus { background: rgba(224,239,255,230); }
            QPushButton#apply { color: white; font-weight: 600;
                                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                                stop:0 #49a8ff, stop:1 #0874ee); min-width: 82px; }
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(17, 17, 17, 17)
        outer.setSpacing(8)
        self.search = None
        self.all_button = None
        self.clear_button = None
        if self.multi:
            controls = QHBoxLayout()
            title = QLabel("选择项目")
            title_font = QFont("Microsoft YaHei UI")
            title_font.setPixelSize(18)
            title_font.setBold(True)
            title.setFont(title_font)
            controls.addWidget(title)
            controls.addStretch(1)
            self.all_button = QPushButton("全选")
            self.clear_button = QPushButton("清空")
            controls.addWidget(self.all_button)
            controls.addWidget(self.clear_button)
            outer.addLayout(controls)
            self.search = QLineEdit()
            self.search.setPlaceholderText("搜索")
            self.search.textChanged.connect(self._filter_choices)
            outer.addWidget(self.search)

        self.choice_scroll = QScrollArea()
        self.choice_scroll.setWidgetResizable(True)
        self.choice_scroll.setFrameShape(QFrame.NoFrame)
        self.choice_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.choice_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.choice_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { width: 8px; background: transparent; margin: 2px; }"
            "QScrollBar::handle:vertical { background: rgba(82,137,200,110); border-radius: 4px; min-height: 28px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.choice_container = QWidget()
        self.choice_container.setStyleSheet("background: transparent;")
        choice_layout = QVBoxLayout(self.choice_container)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        choice_layout.setSpacing(0)
        for index, option in enumerate(self.options):
            choice = _ChoiceCheck(option)
            choice.setFocusPolicy(Qt.StrongFocus)
            choice.clicked.connect(lambda checked, i=index: self._choice_clicked(i, checked))
            self.choices.append(choice)
            self.choice_for[option] = choice
            choice_layout.addWidget(choice)
        self._choice_content_height = max(44, len(self.choices)*44)
        self.choice_container.setMinimumHeight(self._choice_content_height)
        self.choice_scroll.setWidget(self.choice_container)
        self.choice_scroll.setFixedHeight(min(360, self._choice_content_height))
        outer.addWidget(self.choice_scroll)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet("color: rgba(78,126,184,55); background: rgba(78,126,184,55);")
        outer.addWidget(divider)
        footer = QHBoxLayout()
        self.count_label = QLabel()
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        self.apply_button = QPushButton("应用")
        self.apply_button.setObjectName("apply")
        self.apply_button.clicked.connect(self.apply)
        footer.addWidget(self.apply_button)
        outer.addLayout(footer)

        if self.multi:
            self.all_button.clicked.connect(self.select_all)
            self.clear_button.clicked.connect(self.clear_selection)
        self._sync_choices()
        self.adjustSize()

    def _normalise_selection(self, selected):
        if self.multi:
            values = set(selected or [])
            return {value for value in self.options if value in values}
        value = str(selected) if selected is not None else ""
        return value if value in self.options else (self.options[0] if self.options else "")

    def _copy_selection(self, selection):
        return set(selection) if self.multi else selection

    def _initial_focus_index(self):
        if not self.options:
            return -1
        if self.multi:
            for index, option in enumerate(self.options):
                if option in self.selection:
                    return index
        elif self.selection in self.options:
            return self.options.index(self.selection)
        return 0

    def _choice_clicked(self, index, checked):
        option = self.options[index]
        self._focus_index = index
        if self.multi:
            if checked:
                self._draft.add(option)
            else:
                self._draft.discard(option)
        else:
            self._draft = option
        self._sync_choices()

    def _sync_choices(self):
        for option, choice in zip(self.options, self.choices):
            should_check = option in self._draft if self.multi else option == self._draft
            old = choice.blockSignals(True)
            choice.setChecked(should_check)
            choice.blockSignals(old)
        count = len(self._draft) if self.multi else (1 if self._draft else 0)
        self.count_label.setText(f"已选 {count} 项")

    def _filter_choices(self, text):
        needle = text.strip().casefold()
        for option, choice in zip(self.options, self.choices):
            choice.setVisible(not needle or needle in option.casefold())

    def select_all(self):
        self._draft = set(self.options)
        self._sync_choices()

    def clear_selection(self):
        self._draft = set()
        self._sync_choices()

    def apply(self):
        self.selection = self._copy_selection(self._draft)
        result = self._copy_selection(self.selection)
        self._applying = True
        self.hide()
        self._applying = False
        if callable(self.callback):
            self.callback(result)

    def popup(self, global_point):
        bounds = _available_geometry(global_point)
        if bounds is not None:
            self.setMaximumSize(16777215, 16777215)
            self.setMinimumWidth(min(330, max(120, bounds.width() - 16)))
            self.choice_scroll.setFixedHeight(min(360, self._choice_content_height))
            self.adjustSize()
            overhead = max(0, self.sizeHint().height() - self.choice_scroll.height())
            max_height = max(120, bounds.height() - 16)
            scroll_height = max(44, min(360, self._choice_content_height, max_height - overhead))
            self.choice_scroll.setFixedHeight(scroll_height)
            self.setMaximumSize(max(120, bounds.width() - 16), max_height)
        self.adjustSize()
        if bounds is not None:
            self.resize(min(self.width(), self.maximumWidth()), min(self.height(), self.maximumHeight()))
        self.move(_clamped_position(self, global_point))
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.PopupFocusReason)

    def showEvent(self, event):
        self._draft = self._copy_selection(self.selection)
        self._focus_index = self._initial_focus_index()
        self._sync_choices()
        if self.search is not None:
            self.search.clear()
        super().showEvent(event)

    def hideEvent(self, event):
        if not self._applying:
            self._draft = self._copy_selection(self.selection)
            self._sync_choices()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        if event.key() in (Qt.Key_Down, Qt.Key_Up) and self.choices:
            direction = 1 if event.key() == Qt.Key_Down else -1
            start = self._focus_index
            for offset in range(1, len(self.choices) + 1):
                index = (start + direction * offset) % len(self.choices)
                if self.choices[index].isVisible():
                    self._focus_index = index
                    self.choices[index].setFocus(Qt.TabFocusReason)
                    break
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        shadow = QPainterPath()
        shadow.addRoundedRect(QRectF(self.rect()).adjusted(5, 7, -3, -2), 22, 22)
        painter.fillPath(shadow, QColor(31, 75, 137, 44))
        surface = QPainterPath()
        surface.addRoundedRect(QRectF(self.rect()).adjusted(3, 3, -5, -5), 20, 20)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor(253, 254, 255, 245))
        gradient.setColorAt(0.55, QColor(228, 241, 255, 239))
        gradient.setColorAt(1, QColor(241, 235, 255, 240))
        painter.fillPath(surface, gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1.4))
        painter.drawPath(surface)


class GlassInfoPopup(QWidget):
    """Translucent structured chart tooltip and readable generic info popup."""

    def __init__(self, owner=None):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        _retain_for_owner(owner, self)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(170)
        self._generic_mode = False
        self.setStyleSheet("QLabel { color: #173d73; background: transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(1)
        self.time_label = QLabel()
        self.value_label = QLabel()
        self.unit_label = QLabel()
        self.generic_label = QLabel()
        self.generic_label.setWordWrap(True)
        for label, size, bold in (
            (self.time_label, 18, False),
            (self.value_label, 30, True),
            (self.unit_label, 16, False),
            (self.generic_label, 16, False),
        ):
            font = QFont("Microsoft YaHei UI")
            font.setPixelSize(size)
            font.setBold(bold)
            label.setFont(font)
            layout.addWidget(label)
        self.generic_label.hide()

    @staticmethod
    def _formatted_value(value):
        number = float(value)
        return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}".rstrip("0").rstrip(".")

    def show_info(self, stamp, value, unit, global_pos):
        self._generic_mode = False
        self.generic_label.hide()
        self.time_label.setText(str(stamp))
        self.value_label.setText(self._formatted_value(value))
        self.unit_label.setText(str(unit))
        self.time_label.show()
        self.value_label.show()
        self.unit_label.show()
        self.adjustSize()
        self.move(_clamped_position(self, global_pos))
        self.show()
        self.raise_()

    def setText(self, text):
        self._generic_mode = True
        self.time_label.hide()
        self.value_label.hide()
        self.unit_label.hide()
        self.generic_label.setText(str(text))
        self.generic_label.show()

    def text(self):
        if self._generic_mode:
            return self.generic_label.text()
        return "\n".join((self.time_label.text(), self.value_label.text(), self.unit_label.text()))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        shadow = QPainterPath()
        shadow.addRoundedRect(QRectF(self.rect()).adjusted(5, 6, -3, -2), 19, 19)
        painter.fillPath(shadow, QColor(33, 73, 132, 42))
        surface = QPainterPath()
        surface.addRoundedRect(QRectF(self.rect()).adjusted(3, 3, -5, -5), 17, 17)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor(255, 255, 255, 246))
        gradient.setColorAt(1, QColor(224, 239, 255, 241))
        painter.fillPath(surface, gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1.3))
        painter.drawPath(surface)
