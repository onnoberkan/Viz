"""Large, read-only view of one non-electrode recording channel."""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QToolButton, QVBoxLayout
import pyqtgraph as pg

import theme
from signal_units import set_voltage_axis
from shift_zoom import ShiftXWheelFilter


class RecordingWindow(QDialog):
    def __init__(self, recording, channel, pieces, duration, source_plot, parent=None, crop_span=None):
        super().__init__(parent)
        self.channel = channel
        self.duration = duration
        self.crop_span = crop_span
        self.initial_range = source_plot.getViewBox().viewRange()
        self.setWindowTitle(f'Inspect recording · {channel.name} · {recording}')
        self.resize(1100, 650)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel(f'{channel.name} · {channel.fs / 1000:g} kHz'), 1)
        self.restore_button = QToolButton(text='Restore view')
        self.restore_button.clicked.connect(self.restore_view)
        header.addWidget(self.restore_button)
        self.full_button = QToolButton(text='Full crop' if crop_span else 'Full recording')
        self.full_button.clicked.connect(self.show_full_recording)
        header.addWidget(self.full_button)
        layout.addLayout(header)

        self.plot = pg.PlotWidget(background='w')
        self.shift_zoom_filter = ShiftXWheelFilter(self.plot)
        self.plot.showGrid(x=True, y=True, alpha=.1)
        self.plot.setMenuEnabled(False)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode='peak')
        self.plot.getAxis('left').setWidth(70)
        self.plot.setLabel('bottom', 'Recorded time (s)')
        for axis in ('left', 'bottom'):
            self.plot.getAxis(axis).enableAutoSIPrefix(False)
        set_voltage_axis(self.plot, channel)
        for piece in pieces:
            item = self.plot.plot(pen=pg.mkPen(theme.ACCENT, width=1), connect='all', skipFiniteCheck=True)
            item.setData(piece['x'], piece['y'])
        self.plot.getViewBox().disableAutoRange()
        if crop_span:self.plot.getViewBox().setLimits(xMin=crop_span.start,xMax=crop_span.end)
        layout.addWidget(self.plot, 1)

        help_text = QLabel('Drag to navigate; scroll or use a trackpad gesture to zoom. Hold Shift while scrolling to zoom only in time.')
        help_text.setStyleSheet(f'color: {theme.MUTED};')
        layout.addWidget(help_text)
        self.restore_view()

    def restore_view(self):
        x_range, y_range = self.initial_range
        self.plot.getViewBox().disableAutoRange()
        self.plot.getViewBox().setRange(xRange=x_range, yRange=y_range, padding=0)

    def show_full_recording(self):
        start=self.crop_span.start if self.crop_span else 0
        end=self.crop_span.end if self.crop_span else max(self.duration, 1 / self.channel.fs)
        self.plot.setXRange(start,end,padding=0)
        self.plot.enableAutoRange(axis='y')
