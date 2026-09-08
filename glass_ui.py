"""Shared light-glass material, vector icons and native appearance controls."""
import ctypes
import sys
from PyQt5.QtCore import Qt, QRectF, QPoint, QPointF, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen, QLinearGradient, QFont
from PyQt5.QtWidgets import QWidget, QPushButton, QLabel, QSlider, QCheckBox, QVBoxLayout, QHBoxLayout, QComboBox
from glass_popups import GlassChoicePopup,release_owned_popup
from glass_material import liquid_material

INK='#07183e'; MUTED='#46638d'; BLUE='#159cff'; MINT='#28ceb3'
STYLE='''QWidget {font-family:"Arial","Microsoft YaHei UI";color:#07183e;background:transparent;}
QLabel {background:transparent;}
QPushButton {border:0;border-radius:12px;padding:4px;color:#17345b;background:transparent;}
QPushButton:hover {background:rgba(255,255,255,85);}
QPushButton:focus {border:1px solid #62b7ff;}
QPushButton:checked {background:rgba(255,255,255,200);border:1px solid rgba(255,255,255,235);}
QMenu {background:#edf2fb;color:#15345b;border:1px solid #c1d3e7;border-radius:12px;padding:7px;}
QMenu::item {padding:8px 20px;border-radius:6px;} QMenu::item:selected {background:#d0e9ff;}
QScrollArea {border:0;background:transparent;}
QScrollBar:vertical {background:transparent;width:5px;margin:4px 0;}
QScrollBar::handle:vertical {background:rgba(107,119,164,95);border-radius:2px;min-height:30px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:none;}
QComboBox {border:1px solid rgba(255,255,255,190);border-radius:12px;padding:3px 8px;background:rgba(255,255,255,35);}
QComboBox QAbstractItemView {background:#edf3fc;selection-background-color:#cbe6ff;color:#1b3a65;}
QToolTip {background:#edf3fc;color:#244165;border:1px solid #becfe3;padding:8px;font-size:16px;}
QSlider::groove:horizontal {height:7px;border-radius:3px;background:rgba(103,130,171,80);}
QSlider::sub-page:horizontal {background:#188fff;border-radius:3px;}
QSlider::handle:horizontal {background:white;border:1px solid #e5ebf6;width:22px;margin:-8px 0;border-radius:11px;}
'''


def font(size,bold=False):
    result=QFont('Arial');result.setPixelSize(size);result.setWeight(QFont.Bold if bold else QFont.Normal)
    result.setStyleStrategy(QFont.PreferAntialias);return result


def label(parent,text,size=12,color=INK,bold=False):
    item=QLabel(text,parent);item.setFont(font(size,bold));item.setStyleSheet('color:'+color+';background:transparent;')
    return item


def material(p,rect,transparency=65,preset='balanced',phase=0):
    return liquid_material(p,rect,transparency,preset,phase)


def native_blur(widget):
    """Keep Qt per-pixel alpha: whole-HWND DWM Acrylic leaks a square backplate.

    Frosted flowing pigment is painted by material(), not an opaque native
    backdrop or wallpaper screenshot. No OS desktop Gaussian blur is claimed.
    """
    return False


def pulse_path(rect):
    p=QPainterPath();points=[(0,.53),(.13,.53),(.25,.2),(.36,.68),(.49,.04),(.67,.92),(.81,.48),(1,.48)]
    for i,(x,y) in enumerate(points):
        point=QPointF(rect.x()+rect.width()*x,rect.y()+rect.height()*y)
        if i==0:p.moveTo(point)
        else:p.lineTo(point)
    return p


class IconButton(QPushButton):
    def __init__(self,kind,tooltip,parent=None):
        super().__init__(parent);self.kind=kind;self.setToolTip(tooltip);self.setAccessibleName(tooltip)
        self.setFixedSize(28,28);self.setCursor(Qt.PointingHandCursor)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse() or self.hasFocus():
            p.setBrush(QColor(255,255,255,100));p.setPen(Qt.NoPen);p.drawRoundedRect(self.rect(),9,9)
        p.setPen(QPen(QColor('#375782' if self.isChecked() else MUTED),1.7,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        if self.kind=='more':
            p.setBrush(QColor(MUTED));p.setPen(Qt.NoPen)
            for x in (7,14,21):p.drawEllipse(QPointF(x,14),1.5,1.5)
        elif self.kind=='pin':
            path=QPainterPath();path.moveTo(10,6);path.lineTo(18,6);path.lineTo(17,13);path.lineTo(20,16);path.lineTo(8,16);path.lineTo(11,13);path.closeSubpath();p.drawPath(path);p.drawLine(14,16,14,23)
        elif self.kind=='down':
            p.drawLine(8,11,14,17);p.drawLine(14,17,20,11)
        elif self.kind=='up':
            p.drawLine(8,17,14,11);p.drawLine(14,11,20,17)
        elif self.kind=='close':
            p.drawLine(9,9,19,19);p.drawLine(19,9,9,19)


class Appearance(QWidget):
    def __init__(self,owner):
        super().__init__(None,Qt.Popup|Qt.FramelessWindowHint)
        self.owner=owner;self.setAttribute(Qt.WA_TranslucentBackground);self.setFixedSize(330,323);self.setStyleSheet(STYLE)
        retained=getattr(owner,'_glass_popup_children',None)
        if retained is None:retained=[];owner._glass_popup_children=retained
        retained.append(self);owner.destroyed.connect(self.deleteLater)
        label(self,'外观',21,bold=True).setGeometry(28,25,160,30)
        label(self,'透明度',14,color=MUTED).setGeometry(28,65,150,22)
        self.value=label(self,'65%',14);self.value.setAlignment(Qt.AlignRight);self.value.setGeometry(242,65,60,22)
        self.opacity_slider=QSlider(Qt.Horizontal,self);self.opacity_slider.setRange(15,90);self.opacity_slider.setValue(owner.transparency)
        self.opacity_slider.setGeometry(28,88,274,28);self.opacity_slider.setAccessibleName('玻璃透明度')
        self.opacity_slider.valueChanged.connect(self.change_opacity)
        self.presets={}
        for i,(text,key) in enumerate((('通透','clear'),('平衡','balanced'),('磨砂','frosted'))):
            button=GlassPreset(text,key,self);button.setCheckable(True);button.setChecked(key==owner.glass_preset)
            button.setGeometry(28+i*94,143,86,99);button.setFont(font(14));button.clicked.connect(lambda checked=False,k=key:self.select_preset(k));self.presets[key]=button
        label(self,'始终置顶',14).setGeometry(28,278,190,25)
        self.top=GlassSwitch(self);self.top.setGeometry(252,277,50,27);self.top.setChecked(True)
        self.top.toggled.connect(owner.top_action.setChecked)

    def change_opacity(self,value):
        self.value.setText(f'{value}%');self.owner.set_transparency(value);self.update()

    def select_preset(self,key):
        self.owner.glass_preset=key
        for name,button in self.presets.items():button.setChecked(name==key)
        self.opacity_slider.setValue({'clear':80,'balanced':65,'frosted':35}[key])
        self.owner.save_settings();self.owner.update();self.update()

    def showEvent(self,event):
        super().showEvent(event);native_blur(self);self.change_opacity(self.owner.transparency)

    def paintEvent(self,event):
        p=QPainter(self);material(p,QRectF(5,5,self.width()-10,self.height()-10),self.owner.transparency,self.owner.glass_preset,self.owner.flow_phase)
        p.setPen(QColor(255,255,255,150));p.drawLine(28,125,302,125);p.drawLine(28,258,302,258)


class GlassSwitch(QCheckBox):
    def __init__(self,parent=None):
        super().__init__(parent);self.setCursor(Qt.PointingHandCursor);self.setAccessibleName('始终置顶')
    def hitButton(self,pos):return self.rect().contains(pos)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setPen(Qt.NoPen)
        p.setBrush(QColor('#159cff' if self.isChecked() else '#a6b8d4'));p.drawRoundedRect(QRectF(1,1,48,25),12.5,12.5)
        p.setBrush(QColor(255,255,255,245));p.drawEllipse(QRectF(25 if self.isChecked() else 3,3,21,21))


class GlassRange(QComboBox):
    def __init__(self,parent=None):
        super().__init__(parent);self.popup_surface=None
    def showPopup(self):
        options=[self.itemText(i) for i in range(self.count())]
        if self.popup_surface is None or self.popup_surface.options!=options:
            if self.popup_surface is not None:release_owned_popup(self,self.popup_surface)
            self.popup_surface=GlassChoicePopup(options,self.currentText(),self._choose,self)
        else:self.popup_surface.selection=self.currentText()
        surface=self.window();point=QPoint(0,self.height()+6)
        global_point=surface.global_point(self,point) if hasattr(surface,'global_point') else self.mapToGlobal(point)
        self.popup_surface.popup(global_point)
    def hidePopup(self):
        if self.popup_surface is not None:self.popup_surface.hide()
    def _choose(self,value):
        index=self.findText(value)
        if index>=0:self.setCurrentIndex(index)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(255,255,255,60));p.setPen(QPen(QColor(255,255,255,150),1))
        p.drawRoundedRect(QRectF(.5,.5,self.width()-1,self.height()-1),17,17)
        p.setFont(font(14));p.setPen(QColor('#274f82'))
        p.drawText(QRectF(12,0,self.width()-35,self.height()),Qt.AlignVCenter,self.currentText())
        p.setPen(QPen(QColor('#527eb0'),1.8,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        x=self.width()-17;y=self.height()/2;p.drawLine(QPointF(x-4,y-2),QPointF(x,y+2));p.drawLine(QPointF(x,y+2),QPointF(x+4,y-2))


class GlassPreset(QPushButton):
    def __init__(self,text,key,parent=None):
        super().__init__(text,parent);self.key=key;self.setAccessibleName(text+'玻璃');self.setCursor(Qt.PointingHandCursor)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);rect=QRectF(3,2,80,71)
        gradient=QLinearGradient(rect.topLeft(),rect.bottomRight())
        colors=['#f1cfd2','#bfd7f7','#a3dce9'] if self.key!='frosted' else ['#dddce9','#d5e1ef','#cde8ec']
        for t,c in zip((0,.5,1),colors):gradient.setColorAt(t,QColor(c))
        p.setBrush(gradient);p.setPen(QPen(QColor(BLUE if self.isChecked() else '#f3f5ff'),1.5));p.drawRoundedRect(rect,12,12)
        p.setPen(QColor(INK));p.setFont(font(13));p.drawText(QRectF(0,79,86,20),Qt.AlignCenter,self.text())
