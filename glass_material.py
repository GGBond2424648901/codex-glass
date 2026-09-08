"""Shared procedural liquid-glass material, never a wallpaper/design bitmap."""
import math
from PyQt5.QtCore import QRectF,QPointF,Qt
from PyQt5.QtGui import QColor,QPainter,QPainterPath,QPen,QLinearGradient,QRadialGradient

def liquid_material(p,rect,transparency=65,preset='balanced',phase=0):
    p.save();p.setRenderHint(QPainter.Antialiasing)
    radius=37 if rect.height()>400 else min(30,rect.height()/3)
    clip=QPainterPath();clip.addRoundedRect(rect,radius,radius);p.setClipPath(clip,Qt.IntersectClip)
    base_alpha=round(250-transparency*.72)+(25 if preset=='frosted' else -18 if preset=='clear' else 0)
    base_alpha=max(80,min(250,base_alpha));base=QLinearGradient(rect.topLeft(),rect.bottomRight())
    base.setColorAt(0,QColor(255,249,253,base_alpha));base.setColorAt(.55,QColor(224,240,255,base_alpha));base.setColorAt(1,QColor(222,246,255,base_alpha));p.fillRect(rect,base)
    w,h=rect.width(),rect.height();x0,y0=rect.x(),rect.y()
    pigments=[(.02,.05,.56,'#ffcbbf',132),(.8,.09,.57,'#b8b3ff',125),(.98,.36,.52,'#7adef0',130),(.72,.72,.44,'#ffd2c5',130),(.08,.88,.57,'#83dfe7',125),(.96,.99,.5,'#b9bfff',140)]
    density=(125-transparency)/60
    for i,(x,y,r,color,strength) in enumerate(pigments):
        x+=math.sin(phase*.55+i*1.5)*.075;y+=math.cos(phase*.45+i*.9)*.055
        g=QRadialGradient(x0+w*x,y0+h*y,w*r);c=QColor(color);c.setAlpha(min(180,round(strength*density*(1+.055*math.sin(phase*.65+i)))))
        g.setColorAt(0,c);c.setAlpha(0);g.setColorAt(1,c);p.fillRect(rect,g)
    # Broad, softly graded flowing caustics. Their control points drift at
    # different periods, giving breathing depth rather than a sliding picture.
    p.save();p.translate(x0,y0);p.scale(w,h)
    drift=math.sin(phase*.55)*.035;curl=math.cos(phase*.4)*.035
    for y,color,opacity in ((.08,'#ffe6df',105),(.48,'#67bbf4',74),(.73,'#bdf6ed',105)):
        path=QPainterPath(QPointF(-.15,y+.08));path.cubicTo(.12,y-.21+drift,.34,y+.3+curl,.69,y+.24);path.cubicTo(.88,y+.21,1.02,y-.13+drift,1.15,y-.05);path.lineTo(1.15,y+.24);path.cubicTo(.9,y+.18,.8,y+.53,.52,y+.38);path.cubicTo(.28,y+.29,.11,y-.04,-.15,y+.31);path.closeSubpath()
        g=QLinearGradient(.1,y-.07,.75,y+.4);c=QColor(color);c.setAlpha(round(opacity/7));g.setColorAt(0,c);c.setAlpha(round(opacity/14));g.setColorAt(.55,c);c.setAlpha(0);g.setColorAt(1,c)
        for feather in range(-3,4):
            p.save();p.translate(0,feather*.005);p.fillPath(path,g);p.restore()
        edge=QPainterPath(QPointF(-.15,y+.08));edge.cubicTo(.12,y-.21+drift,.34,y+.3+curl,.69,y+.24);edge.cubicTo(.88,y+.21,1.02,y-.13+drift,1.15,y-.05)
        for width,alpha in ((.021,5),(.011,8),(.004,12)):
            p.setPen(QPen(QColor(255,255,255,alpha),width,Qt.SolidLine,Qt.RoundCap));p.drawPath(edge)
    p.restore()
    # Diffusing veil plus specular perimeter: highlights never wash out text.
    veil=QLinearGradient(rect.topLeft(),rect.bottomRight());veil.setColorAt(0,QColor(255,255,255,43));veil.setColorAt(.42,QColor(255,255,255,4));veil.setColorAt(1,QColor(245,250,255,18));p.fillRect(rect,veil)
    rim=QLinearGradient(rect.topLeft(),rect.bottomRight());rim.setColorAt(0,QColor(255,255,255,255));rim.setColorAt(.35,QColor(255,255,255,65));rim.setColorAt(.63,QColor(171,219,255,160));rim.setColorAt(1,QColor(255,255,255,245))
    p.setBrush(Qt.NoBrush);p.setPen(QPen(rim,1.6));p.drawRoundedRect(rect.adjusted(.5,.5,-.5,-.5),radius,radius)
    p.setPen(QPen(QColor(255,255,255,65),1));p.drawRoundedRect(rect.adjusted(2,2,-2,-2),radius-2,radius-2)
    p.restore()
