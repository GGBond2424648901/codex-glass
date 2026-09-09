"""Shared procedural liquid-glass material, never a wallpaper/design bitmap."""

import math
from PyQt5.QtCore import QRectF, QPointF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen, QLinearGradient, QRadialGradient


def liquid_material(p, rect, transparency=65, preset="balanced", phase=0):
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    radius = 37 if rect.height() > 400 else min(30, rect.height() / 3)
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    p.setClipPath(clip, Qt.IntersectClip)
    base_alpha = round(250 - transparency * 0.72) + (25 if preset == "frosted" else -18 if preset == "clear" else 0)
    base_alpha = max(80, min(250, base_alpha))
    base = QLinearGradient(rect.topLeft(), rect.bottomRight())
    base.setColorAt(0, QColor(255, 249, 253, base_alpha))
    base.setColorAt(0.55, QColor(224, 240, 255, base_alpha))
    base.setColorAt(1, QColor(222, 246, 255, base_alpha))
    p.fillRect(rect, base)
    w, h = rect.width(), rect.height()
    x0, y0 = rect.x(), rect.y()
    pigments = [
        (0.02, 0.05, 0.56, "#ffcbbf", 132),
        (0.8, 0.09, 0.57, "#b8b3ff", 125),
        (0.98, 0.36, 0.52, "#7adef0", 130),
        (0.72, 0.72, 0.44, "#ffd2c5", 130),
        (0.08, 0.88, 0.57, "#83dfe7", 125),
        (0.96, 0.99, 0.5, "#b9bfff", 140),
    ]
    density = (125 - transparency) / 60
    for i, (x, y, r, color, strength) in enumerate(pigments):
        x += math.sin(phase * 0.55 + i * 1.5) * 0.075
        y += math.cos(phase * 0.45 + i * 0.9) * 0.055
        g = QRadialGradient(x0 + w * x, y0 + h * y, w * r)
        c = QColor(color)
        c.setAlpha(min(180, round(strength * density * (1 + 0.055 * math.sin(phase * 0.65 + i)))))
        g.setColorAt(0, c)
        c.setAlpha(0)
        g.setColorAt(1, c)
        p.fillRect(rect, g)
    # Broad, softly graded flowing caustics. Their control points drift at
    # different periods, giving breathing depth rather than a sliding picture.
    p.save()
    p.translate(x0, y0)
    p.scale(w, h)
    drift = math.sin(phase * 0.55) * 0.035
    curl = math.cos(phase * 0.4) * 0.035
    for y, color, opacity in ((0.08, "#ffe6df", 105), (0.48, "#67bbf4", 74), (0.73, "#bdf6ed", 105)):
        path = QPainterPath(QPointF(-0.15, y + 0.08))
        path.cubicTo(0.12, y - 0.21 + drift, 0.34, y + 0.3 + curl, 0.69, y + 0.24)
        path.cubicTo(0.88, y + 0.21, 1.02, y - 0.13 + drift, 1.15, y - 0.05)
        path.lineTo(1.15, y + 0.24)
        path.cubicTo(0.9, y + 0.18, 0.8, y + 0.53, 0.52, y + 0.38)
        path.cubicTo(0.28, y + 0.29, 0.11, y - 0.04, -0.15, y + 0.31)
        path.closeSubpath()
        g = QLinearGradient(0.1, y - 0.07, 0.75, y + 0.4)
        c = QColor(color)
        c.setAlpha(round(opacity / 7))
        g.setColorAt(0, c)
        c.setAlpha(round(opacity / 14))
        g.setColorAt(0.55, c)
        c.setAlpha(0)
        g.setColorAt(1, c)
        for feather in range(-3, 4):
            p.save()
            p.translate(0, feather * 0.005)
            p.fillPath(path, g)
            p.restore()
        edge = QPainterPath(QPointF(-0.15, y + 0.08))
        edge.cubicTo(0.12, y - 0.21 + drift, 0.34, y + 0.3 + curl, 0.69, y + 0.24)
        edge.cubicTo(0.88, y + 0.21, 1.02, y - 0.13 + drift, 1.15, y - 0.05)
        for width, alpha in ((0.021, 5), (0.011, 8), (0.004, 12)):
            p.setPen(QPen(QColor(255, 255, 255, alpha), width, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(edge)
    p.restore()
    # Diffusing veil plus specular perimeter: highlights never wash out text.
    veil = QLinearGradient(rect.topLeft(), rect.bottomRight())
    veil.setColorAt(0, QColor(255, 255, 255, 43))
    veil.setColorAt(0.42, QColor(255, 255, 255, 4))
    veil.setColorAt(1, QColor(245, 250, 255, 18))
    p.fillRect(rect, veil)
    rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
    rim.setColorAt(0, QColor(255, 255, 255, 255))
    rim.setColorAt(0.35, QColor(255, 255, 255, 65))
    rim.setColorAt(0.63, QColor(171, 219, 255, 160))
    rim.setColorAt(1, QColor(255, 255, 255, 245))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(rim, 1.6))
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.setPen(QPen(QColor(255, 255, 255, 65), 1))
    p.drawRoundedRect(rect.adjusted(2, 2, -2, -2), radius - 2, radius - 2)
    p.restore()
