from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSplitter, QToolButton, QWidget, QCheckBox, QScrollArea, QFrame, QComboBox, QDialogButtonBox
import numpy as np
import pyqtgraph as pg
import channel_quality
from channel_roles import ROLES, inferred_role, role_for
from shift_zoom import ShiftXWheelFilter

COLORS = {'neural': '#25834D', 'noisy': '#B26B00', 'quiet': '#B26B00', 'noise': '#C0392B', 'unconnected': '#C0392B', 'flat': '#C0392B', 'clipped': '#C0392B'}
OTHER = '#5F6672'


class ChannelLabelDialog(QDialog):
    def __init__(self, channels, saved, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Label Channels')
        self.resize(570, 520)
        self.combos = {}
        layout = QVBoxLayout(self)
        note = QLabel('Assign a role to each channel. Save applies each label to matching channel names in all PLX recordings for this patient, including Crop Data and JSON exports.')
        note.setWordWrap(True);layout.addWidget(note)
        scroll = QScrollArea();scroll.setWidgetResizable(True)
        content = QWidget();rows = QVBoxLayout(content)
        for channel in channels:
            row = QHBoxLayout()
            name = QLabel(f'{channel.name} · channel {channel.key[1]} · {channel.fs/1000:g} kHz')
            name.setToolTip(str(channel.key));row.addWidget(name, 1)
            choice = QComboBox();choice.addItems(ROLES)
            choice.setCurrentText(role_for(channel, saved))
            row.addWidget(choice);rows.addLayout(row)
            self.combos[channel.key] = choice
        rows.addStretch(1);scroll.setWidget(content);layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def assignments(self):
        return {key:combo.currentText() for key,combo in self.combos.items()}


class SpectrumDialog(QDialog):
    requestNotched = Signal(object)
    saveLabels = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Spectra')
        self.resize(980, 600)
        self.channels, self.qualities, self.checked = [], {}, None
        self.saved_roles = {};self.can_label = False
        self.recording_id = None
        self.notched, self.notch_pending, self.notch_errors = {}, set(), {}
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.note = QLabel()
        self.note.setObjectName('muted')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.filter_status = QLabel()
        self.filter_status.setObjectName('muted')
        self.filter_status.setWordWrap(True)
        layout.addWidget(self.filter_status)
        split = QSplitter(Qt.Horizontal)
        left = QVBoxLayout()
        buttons = QHBoxLayout()
        for text, fn in (('Field inputs', self.check_fields), ('All', lambda: self.check_all(True)), ('None', lambda: self.check_all(False))):
            button = QToolButton(text=text)
            button.clicked.connect(fn)
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.label_button = QToolButton(text='Label Channels')
        self.label_button.setToolTip('Assign Spike, ECoG, EMG, or Other to matching channels across this patient’s PLX recordings.')
        self.label_button.clicked.connect(self.edit_labels)
        self.label_button.hide();buttons.addWidget(self.label_button)
        self.notch = QCheckBox('60 Hz notch')
        self.notch.setToolTip('Filter each continuous recording run at 60 Hz before calculating its displayed spectrum. Original recordings and channel quality labels are unchanged.')
        self.notch.toggled.connect(self.redraw)
        buttons.addWidget(self.notch)
        self.db = QCheckBox('dB')
        self.db.setToolTip('Show 10·log₁₀ power.')
        self.db.setChecked(True)
        self.db.toggled.connect(self.redraw)
        buttons.addWidget(self.db)
        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.itemChanged.connect(self.redraw)
        self.list.currentItemChanged.connect(self.redraw)
        panel = QWidget()
        panel.setLayout(left)
        left.setContentsMargins(0, 0, 0, 0)
        left.addLayout(buttons)
        left.addWidget(self.list, 1)
        split.addWidget(panel)
        graph_panel = QWidget()
        graph_layout = QVBoxLayout(graph_panel)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        self.highlight_label = QLabel('Highlighted channel: none')
        self.highlight_label.setObjectName('muted')
        self.highlight_label.setWordWrap(True)
        graph_layout.addWidget(self.highlight_label)
        self.plot = pg.PlotWidget(background='w')
        self.shift_zoom_filter = ShiftXWheelFilter(self.plot)
        self.plot.setToolTip('Hold Shift while scrolling to zoom only in frequency, keeping the power range fixed.')
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=.15)
        self.plot.setLabel('bottom', 'Frequency (Hz)')
        for axis in ('left', 'bottom'):
            self.plot.getAxis(axis).enableAutoSIPrefix(False)
        graph_layout.addWidget(self.plot, 1)
        self.selection_summary = QLabel('0 channels selected')
        self.selection_summary.setWordWrap(True)
        self.selection_summary.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.selection_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary_scroll = QScrollArea()
        summary_scroll.setWidgetResizable(True)
        summary_scroll.setFrameShape(QFrame.NoFrame)
        summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        summary_scroll.setWidget(self.selection_summary)
        summary_scroll.setMinimumHeight(48)
        summary_scroll.setMaximumHeight(90)
        graph_layout.addWidget(summary_scroll)
        split.addWidget(graph_panel)
        split.setSizes([330, 650])
        layout.addWidget(split, 1)

    def set_recording(self, title, channels, qualities, selected, pending=False, recording_id=None,
                      saved_roles=None, can_label=False):
        new_recording = recording_id != self.recording_id
        if new_recording:
            self.recording_id = recording_id
            self.notched.clear();self.notch_pending.clear();self.notch_errors.clear()
        same = [c.key for c in channels] == [c.key for c in self.channels]
        checked = self.checked_keys() if same and self.checked is not None else None
        current = self.list.currentItem().data(Qt.UserRole) if same and self.list.currentItem() else None
        self.channels, self.qualities = list(channels), dict(qualities)
        self.saved_roles=dict(saved_roles or {});self.can_label=can_label
        self.label_button.setVisible(can_label)
        self.heading.setText(title)
        missing = sum(1 for c in self.channels if c.key not in self.qualities)
        self.note.setText('Every recorded sample, anti-alias filtered and downsampled (ECoG, LFP and other inputs to about 1 kHz, '
                          'shown to 400 Hz; microelectrodes to about 8 kHz, shown to 3.2 kHz), then a Welch spectrum: '
                          'Hann windows of 256 samples per 250 Hz (about 1 s), 50% overlap, none spanning a recording gap. '
                          'Green: neural; amber: neural with line noise or slow artifact, or no spikes; red: unconnected, artifact, flat or clipped.'
                          + (f' Measuring {missing} input{"s" * (missing != 1)}…' if pending and missing else ''))
        if checked is None:
            fields = self.field_keys()
            checked = fields or set(selected)
        self.list.blockSignals(True)
        self.list.clear()
        for channel in self.channels:
            quality = self.qualities.get(channel.key)
            text = f'{channel.name} · {channel.fs/1000:g} kHz' + (f' · {quality.label}' if quality else '')
            if can_label:text += f' · {role_for(channel,self.saved_roles)}'
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, channel.key)
            item.setToolTip(quality.detail if quality else 'Not measured yet.')
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if channel.key in checked else Qt.Unchecked)
            item.setForeground(QColor(COLORS.get(quality.verdict, OTHER) if quality else OTHER))
            self.list.addItem(item)
            if channel.key == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.checked = checked
        self.redraw(reset_view=new_recording)

    def checked_keys(self):
        return {self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked}

    def field_keys(self):
        return {c.key for c in self.channels if
                (self.can_label and role_for(c,self.saved_roles)=='ECoG') or
                (channel_quality.kind(c.name,c.fs)=='field' and
                 (not self.can_label or role_for(c,self.saved_roles)=='Other'))}

    def edit_labels(self):
        if not self.can_label:return
        editor=ChannelLabelDialog(self.channels,self.saved_roles,self)
        if editor.exec()==QDialog.Accepted:self.saveLabels.emit(editor.assignments())

    def check_all(self, state):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Checked if state else Qt.Unchecked)
        self.list.blockSignals(False)
        self.redraw()

    def check_fields(self):
        fields = self.field_keys()
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in fields else Qt.Unchecked)
        self.list.blockSignals(False)
        self.redraw()

    def set_notched(self, results, errors=None):
        self.notch_pending.difference_update(results)
        self.notch_pending.difference_update(errors or {})
        self.notched.update(results)
        self.notch_errors.update(errors or {})
        self.redraw()

    def notch_failed(self, error):
        for key in self.notch_pending:self.notch_errors[key] = error
        self.notch_pending.clear()
        self.redraw()

    def highlight_curve(self, key):
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.UserRole) == key:
                self.list.setCurrentItem(item)
                self.list.scrollToItem(item)
                return

    def redraw(self, *_, reset_view=False):
        view = self.plot.getViewBox()
        had_curves = bool(self.plot.listDataItems())
        previous_range = view.viewRange() if had_curves and not reset_view else None
        previous_auto = view.autoRangeEnabled() if previous_range is not None else None
        self.checked = self.checked_keys()
        selected = [c.name for c in self.channels if c.key in self.checked]
        self.selection_summary.setText(f'{len(selected)} channel{"s" if len(selected) != 1 else ""} selected'
                                       + (': ' + ', '.join(selected) if selected else ''))
        filtered = self.notch.isChecked()
        needed = {key for key in self.checked if self.qualities.get(key) and self.qualities[key].spectrum is not None}
        missing = needed - self.notched.keys() - self.notch_pending - self.notch_errors.keys()
        if filtered and missing and not self.notch_pending:
            self.notch_pending.update(missing)
            self.requestNotched.emit(missing)
        if filtered:
            waiting = len(needed & self.notch_pending)
            failed = len(needed & self.notch_errors.keys())
            self.filter_status.setText((f'Calculating 60 Hz notch spectra for {waiting} channel(s)…' if waiting else
                                        f'60 Hz notch applied to displayed spectra.' if needed and not failed else
                                        '60 Hz notch selected.') +
                                       (f' {failed} channel(s) could not be filtered: {next(iter(self.notch_errors.values()))}' if failed else ''))
        else:self.filter_status.clear()
        decibels = self.db.isChecked()
        self.plot.setLabel('left', 'Power (dB, native counts²/Hz)' if decibels else 'Power (native counts²/Hz)')
        scale = (lambda power: 10*np.log10(power)) if decibels else (lambda power: power)
        current = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
        highlighted = next((c.name for c in self.channels if c.key == current), None)
        self.highlight_label.setText(f'Highlighted channel: {highlighted}' if highlighted else 'Highlighted channel: none')
        self.plot.clear()
        names = {c.key: c.name for c in self.channels}
        top = None
        for key in [k for k in names if k in self.checked]:
            quality = self.qualities.get(key)
            if not quality or quality.spectrum is None:
                continue
            spectrum = self.notched.get(key) if filtered else quality.spectrum
            if spectrum is None:continue
            freq, power = spectrum
            power = scale(power)
            color = QColor(COLORS.get(quality.verdict, OTHER))
            if key == current:
                top = (key, freq, power, color)
                continue
            color.setAlpha(150)
            self.draw_curve(key, freq, power, color, 1)
        if top:
            self.draw_curve(top[0], top[1], top[2], top[3], 3)
        if previous_range is None:
            self.plot.enableAutoRange()
        else:
            for axis in (0, 1):
                if previous_auto[axis]:view.enableAutoRange(axis=axis)
                else:
                    kwargs = {'xRange' if axis == 0 else 'yRange': previous_range[axis]}
                    view.setRange(padding=0, **kwargs)

    def draw_curve(self, key, freq, power, color, width):
        item = self.plot.plot(freq, power, pen=pg.mkPen(color, width=width))
        item.setCurveClickable(True, width=12)
        item.setToolTip(next(c.name for c in self.channels if c.key == key))
        item.sigClicked.connect(lambda _curve, _event, channel_key=key: self.highlight_curve(channel_key))
