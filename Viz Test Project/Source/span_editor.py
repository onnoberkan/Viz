import sqlite3

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QToolButton, QMessageBox

from recording_spans import RecordingSpan, SpanStore


class SpanEditor(QToolButton):
    changed = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = None
        self.store = None
        self.span = None
        self.saved = None
        self.locked = True
        self.load_error = False
        self.plots = []
        self.regions = []
        self.clicked.connect(self.toggle_lock)
        self.hide()

    def clear(self):
        self.set_plots([])
        self.view = None;self.store = None;self.span = None;self.saved = None
        self.locked = True;self.load_error = False;self.hide()

    def bind(self, flags, view):
        self.clear();self.view = view;self.store = SpanStore(flags)
        try:
            self.span = self.store.load(view);self.saved = self.span
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            self.load_error = True
            QMessageBox.warning(self, 'Cannot load span', f'Could not read the saved span from {flags.path}:\n{error}')
        self.refresh();self.show()

    def refresh(self):
        if self.regions:
            import pyqtgraph as pg
        self.setEnabled(self.span is not None and not self.load_error)
        self.setText('Unlock span' if self.locked else 'Lock span')
        self.setToolTip('Span unavailable — reopen the recording to retry.' if self.load_error else
            'Unlock to drag the span or its edges. Lock it when finished. The span is saved for this file.')
        for plot, region in self.regions:
            color=(206,81,74) if getattr(plot,'spike_sort_applied',False) else (44,151,102)
            region.setMovable(not self.locked)
            region.setBrush(pg.mkBrush(*color, 6 if self.locked else 28))
            region.setHoverBrush(pg.mkBrush(*color, 6 if self.locked else 36))
            region.setMouseHover(False)
            for line in region.lines:
                line.setPen(pg.mkPen((*color, 18 if self.locked else 180), width=1))
                line.setHoverPen(pg.mkPen((*color, 18 if self.locked else 230), width=1 if self.locked else 2))
            region.update()

    def toggle_lock(self):
        if self.span is None or self.load_error:return
        if not self.locked and self.span != self.saved:self.save()
        self.locked = not self.locked;self.refresh()
        self.changed.emit()

    def set_plots(self, plots):
        for plot, region in self.regions:plot.removeItem(region)
        self.regions = [];self.plots = list(plots)
        if self.span is None:return
        import pyqtgraph as pg
        for plot in self.plots:
            region = pg.LinearRegionItem((self.span.start, self.span.end), bounds=(0., self.view.duration),
                                         movable=False, swapMode='sort')
            region.setZValue(10)
            region.sigRegionChanged.connect(self.region_changed)
            region.sigRegionChangeFinished.connect(self.region_finished)
            plot.addItem(region, ignoreBounds=True);self.regions.append((plot, region))
        self.refresh()

    def sync_regions(self):
        for _, region in self.regions:
            region.blockSignals(True);region.setRegion((self.span.start, self.span.end));region.blockSignals(False)

    def region_changed(self, region):
        if not self.locked:self.span = RecordingSpan(*region.getRegion())
        self.sync_regions()

    def region_finished(self, region):
        self.region_changed(region)
        if not self.locked:self.save()

    def save(self):
        if self.locked or self.load_error or self.span is None or self.span == self.saved:return
        try:
            self.store.save(self.view, self.span)
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            self.span = self.saved;self.sync_regions()
            QMessageBox.warning(self, 'Span not saved', f'The last saved span was restored.\n{error}')
            return
        self.saved = self.span
        self.changed.emit()
