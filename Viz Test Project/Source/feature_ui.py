import json
from pathlib import Path
from PySide6.QtCore import Qt, QObject, Signal, Slot, QRunnable, QThreadPool
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QToolButton,
                              QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox, QComboBox)
import pyqtgraph as pg
from spike_features import METRICS, export_csv
from signal_units import set_voltage_axis
import hemispheres


class PlotSignals(QObject):
    ready = Signal(int, object, str)


class PlotTask(QRunnable):
    def __init__(self, revision, args):
        super().__init__()
        self.revision, self.args = revision, args
        self.signals = PlotSignals()

    def run(self):
        try:
            from feature_plots import feature_plot_data
            data = feature_plot_data(*self.args)
        except Exception as error:
            self.signals.ready.emit(self.revision, None, str(error))
        else:
            self.signals.ready.emit(self.revision, data, '')


class FeaturePlotDialog(QDialog):
    def __init__(self, feature, result, run_index, pieces, span, saved_sort, parent):
        super().__init__(parent)
        self.key, self.name, self.unit = feature
        self.result, self.pieces, self.span, self.saved_sort = result, pieces, span, saved_sort
        self.revision = 0
        self.data = None
        self.error = ''
        self.cache = {}
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(self.name)
        self.setWindowModality(Qt.WindowModal)
        self.resize(860, 540)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.heading = QLabel()
        header.addWidget(self.heading, 1)
        self.segment = QComboBox()
        for run in result['runs']:
            self.segment.addItem(f'Segment {run["run"]} · {run["start_seconds"]:.4g}–{run["end_seconds"]:.4g} s')
        self.segment.setCurrentIndex(run_index)
        self.segment.setVisible(len(result['runs']) > 1 and self.key != 'Silhouette')
        self.segment.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.segment)
        layout.addLayout(header)
        self.plot = pg.PlotWidget(background='w')
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=.12)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode='peak')
        self.plot.addLegend(offset=(12, 12), brush=pg.mkBrush(255, 255, 255, 230))
        for axis in ('left', 'bottom'):
            self.plot.getAxis(axis).enableAutoSIPrefix(False)
        layout.addWidget(self.plot, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch()
        reset = QToolButton(text='Reset view')
        reset.clicked.connect(lambda: self.plot.autoRange())
        footer.addWidget(reset)
        close = QToolButton(text='Close')
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.finished.connect(self.cancel)
        self.refresh()

    def cancel(self, _):
        self.revision += 1

    def refresh(self, *_):
        self.revision += 1
        self.data = None
        self.error = ''
        self.plot.clear()
        self.plot.plotItem.legend.clear()
        run_index = self.segment.currentIndex()
        run = self.result['runs'][run_index]
        value = run['values'].get(self.key)
        self.heading.setText(f'{self.name} · ' + ('N/A' if value is None else f'{value:.6g} {self.unit}'))
        self.status.setText('Loading plot…')
        self.status.show()
        if run_index in self.cache:
            self.render(self.revision, self.cache[run_index], '')
            return
        task = PlotTask(self.revision, (self.key, run, self.pieces, self.span, self.saved_sort))
        task.signals.ready.connect(self.render)
        QThreadPool.globalInstance().start(task)

    @Slot(int, object, str)
    def render(self, revision, data, error):
        if revision != self.revision:
            return
        self.error = error
        if error:
            self.status.setText(error)
            return
        self.data = data
        self.cache[self.segment.currentIndex()] = data
        self.plot.setLabel('bottom', data['xlabel'])
        self.plot.setLabel('left', data['ylabel'])
        if self.key == 'SNR':
            set_voltage_axis(self.plot, getattr(self.parent(), 'channel', None), filtered=True)
        for low, high in data['regions']:
            region = pg.LinearRegionItem((low, high), movable=False,
                                         brush=pg.mkBrush(74, 111, 165, 25), pen=pg.mkPen(None))
            region.setZValue(-5)
            self.plot.addItem(region)
        for bar in data['bars']:
            self.plot.addItem(pg.BarGraphItem(**bar, brush='#8CA5C8', pen=None))
        for line in data['lines']:
            if line.get('scatter'):
                self.plot.plot(line['x'], line['y'], pen=None, symbol='o', symbolSize=4,
                               symbolPen=None, symbolBrush=line['color'], name=line['name'])
            else:
                self.plot.plot(line['x'], line['y'], connect='finite', name=line['name'],
                               pen=pg.mkPen(line['color'], width=.7 if line.get('thin') else 2))
        for direction, value, label in data['markers']:
            self.plot.addItem(pg.InfiniteLine(value, angle=90 if direction == 'v' else 0,
                pen=pg.mkPen('#CE514A', width=1.5, style=Qt.DashLine), label=label,
                labelOpts={'position': .85, 'color': '#CE514A'}))
        caption = data.get('caption', '')
        self.status.setText(caption)
        self.status.setVisible(bool(caption))
        self.plot.autoRange()


class FeatureDialog(QDialog):
    def __init__(self, channel, span, parent=None, *, pieces=None, saved_sort=None):
        super().__init__(parent)
        self.channel = channel
        self.result = None
        self.error = ''
        self.pieces, self.span, self.saved_sort = pieces, span, saved_sort
        self.plot_dialog = None
        self.hemisphere = None
        self.cluster_text = ''
        self.setWindowTitle(f'Neuron features · {channel.name}')
        self.setWindowModality(Qt.WindowModal)
        self.resize(820, 530)
        layout = QVBoxLayout(self)
        self.heading_text = f'{channel.name} · {span.start:.6g}–{span.end:.6g} s'
        self.heading = QLabel(self.heading_text)
        layout.addWidget(self.heading)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setCursor(Qt.PointingHandCursor)
        self.table.cellClicked.connect(self.open_plot)
        self.table.cellActivated.connect(self.open_plot)
        layout.addWidget(self.table, 1)
        self.status = QLabel('Calculating features from saved spikes…')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch()
        self.export_button = QToolButton(text='Export…')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export)
        footer.addWidget(self.export_button)
        close = QToolButton(text='Close')
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.finished.connect(self.close_plot)

    def close_plot(self, *_):
        if self.plot_dialog is not None:
            self.plot_dialog.reject()
            self.plot_dialog = None

    def open_plot(self, row, col):
        if self.result is None or self.saved_sort is None or not 0 <= row < len(METRICS):
            return
        self.close_plot()
        self.plot_dialog = FeaturePlotDialog(METRICS[row], self.result, max(0, col-2),
                                             self.pieces, self.span, self.saved_sort, self)
        self.plot_dialog.finished.connect(self.plot_closed)
        self.plot_dialog.show()

    def plot_closed(self, _):
        self.plot_dialog = None

    def set_hemisphere(self, entry):
        self.hemisphere = entry
        self.update_heading()

    def update_heading(self):
        side = f' · {hemispheres.text(self.hemisphere)}' if self.hemisphere else ''
        self.heading.setText(self.heading_text + self.cluster_text + side)
        self.heading.setToolTip(hemispheres.tooltip(self.hemisphere) if self.hemisphere else '')

    def failed(self, message):
        self.error = message
        self.status.setText(message)
        self.status.show()

    def set_result(self, result, saved=True):
        self.result = result
        runs = result['runs']
        self.cluster_text = f' · Cluster {result["selected_cluster"] + 1}'
        self.update_heading()
        self.table.setColumnCount(2 + len(runs))
        self.table.setHorizontalHeaderLabels(['Feature', 'Unit'] +
            [f'Segment {r["run"]}\n{r["start_seconds"]:.4g}–{r["end_seconds"]:.4g} s\n{r["spike_count"]} spikes' for r in runs])
        self.table.setRowCount(len(METRICS))
        for row, (key, name, unit) in enumerate(METRICS):
            label = QTableWidgetItem(name)
            label.setForeground(pg.mkColor('#4A6FA5'))
            label.setToolTip('Click to view plot')
            self.table.setItem(row, 0, label)
            self.table.setItem(row, 1, QTableWidgetItem(unit))
            for col, run in enumerate(runs, 2):
                value = run['values'][key]
                item = QTableWidgetItem('N/A' if value is None else f'{value:.6g}')
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(run['notes'].get(key, f'{value!r} {unit}'))
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.status.setText('' if saved else 'Could not save features.')
        self.status.setVisible(not saved)
        self.export_button.setEnabled(True)

    def export(self):
        path, selected = QFileDialog.getSaveFileName(self, 'Export neuron features', 'neuron-features.csv',
                                                    'CSV (*.csv);;JSON with provenance (*.json)')
        if not path:
            return
        result = dict(self.result, **hemispheres.export_fields(self.hemisphere))
        try:
            if Path(path).suffix.lower() == '.json' or selected.startswith('JSON'):
                Path(path).write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            else:
                export_csv(path, result)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Features not exported', str(error))
