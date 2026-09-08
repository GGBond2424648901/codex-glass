"""Route native help events through one consistent rounded glass popup."""
from PyQt5.QtCore import QObject,QEvent,QPoint,QTimer,Qt
from PyQt5.QtWidgets import QApplication,QWidget
from glass_popups import GlassInfoPopup,_clamped_position

class GlassTooltipRouter(QObject):
    def __init__(self,app):
        super().__init__(app);self.popup=None;self.source=None
        self.timeout=QTimer(self);self.timeout.setSingleShot(True);self.timeout.timeout.connect(self.dismiss)
    def dismiss(self):
        if self.popup:self.popup.hide()
        self.source=None;self.timeout.stop()
    def eventFilter(self,watched,event):
        kind=event.type()
        if kind in (QEvent.Leave,QEvent.Hide,QEvent.MouseButtonPress) and watched is self.source:self.dismiss()
        if kind!=QEvent.ToolTip or not isinstance(watched,QWidget):return False
        root=watched
        while root is not None and not root.property('glass_tooltip_root'):root=root.parentWidget()
        if root is None or not watched.toolTip():return False
        if self.popup is None:self.popup=GlassInfoPopup(self)
        self.popup.generic_label.setTextFormat(Qt.PlainText);self.popup.generic_label.setMaximumWidth(390)
        self.popup.setText(watched.toolTip());self.popup.adjustSize()
        point=event.globalPos()+QPoint(12,18)
        if hasattr(root,'global_point'):point=root.global_point(watched,event.pos())+QPoint(12,18)
        self.popup.move(_clamped_position(self.popup,point));self.popup.show();self.popup.raise_();self.source=watched;self.timeout.start(7000)
        event.accept();return True

def install_glass_tooltips(root):
    app=QApplication.instance()
    if not hasattr(app,'glass_tooltip_router'):
        app.glass_tooltip_router=GlassTooltipRouter(app);app.installEventFilter(app.glass_tooltip_router)
    root.setProperty('glass_tooltip_root',True)
    return app.glass_tooltip_router
