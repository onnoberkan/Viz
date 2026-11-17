"""Shift-wheel horizontal zoom shared by Viz Test plots."""

import math

from PySide6.QtCore import QObject, QEvent, Qt
from shiboken6 import isValid


def shift_zoom_x(plot, event, related_plots=()):
    if event.type() != QEvent.Wheel or not event.modifiers() & Qt.ShiftModifier:
        return False
    angle = event.angleDelta()
    pixels = event.pixelDelta()
    delta = angle.y() or angle.x()
    if not delta:
        delta = (pixels.y() or pixels.x()) * 1.2
    if not delta:
        return False
    delta = max(-480., min(480., float(delta)))
    factor = 1.02 ** (-delta / 8.)
    if not math.isfinite(factor):
        return False

    view = plot.getViewBox()
    x_range = view.viewRange()[0]
    scene_point = plot.mapToScene(event.position().toPoint())
    center = view.mapSceneToView(scene_point)
    center.setX(max(x_range[0], min(x_range[1], center.x())))

    held = []
    for companion in dict.fromkeys((plot, *related_plots)):
        box = companion.getViewBox()
        held.append((box, box.viewRange()[1]))
        box.disableAutoRange(axis='y')
    view.scaleBy(x=factor, center=center)
    for box, y_range in held:
        box.setYRange(*y_range, padding=0)
    event.accept()
    return True


class ShiftXWheelFilter(QObject):
    def __init__(self, plot):
        super().__init__(plot)
        self.plot = plot
        plot.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and isValid(self.plot) and watched is self.plot.viewport() and shift_zoom_x(self.plot, event):
            return True
        return super().eventFilter(watched, event)
