from pathlib import Path
from collections import Counter, OrderedDict
import argparse
import json
import os
import sqlite3
import sys
import time
APP_STARTED = time.perf_counter()
VERSION = '3.5.3'
BUILD = '71'
import numpy as np
from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool, QTimer, QEvent
from PySide6.QtGui import QColor, QBrush, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QCheckBox, QFileDialog, QMessageBox, QToolButton, QMenu, QScrollArea, QFrame, QSizePolicy, QListWidget, QListWidgetItem, QWidgetAction
pg = None
import alpha_reader as reader
import channel_quality
from signal_units import set_voltage_axis, voltage_calibration
from shift_zoom import shift_zoom_x
import theme
import patient_targets
import hemispheres
import library_paths
from recording_flags import RecordingFlags
from crop_data import CropStore
from channel_roles import ChannelRoleStore, is_plx, role_for
from neuron_notes import NeuronNotes
from span_editor import SpanEditor
from spike_sorting import SpikeSortStore, sort_channel, source_signature
from unit_acceptance import UnitAcceptanceStore, validate_review
from spike_features import FeatureStore, calculate_features, METRICS


QUALITY_COLORS={'neural':'#25834D','noisy':'#B26B00','quiet':'#B26B00','noise':'#C0392B','unconnected':'#C0392B','flat':'#C0392B','clipped':'#C0392B'}


def page():
    w=QWidget();layout=QVBoxLayout(w);layout.setContentsMargins(12,12,12,12);layout.setSpacing(10)
    return w,layout

class Signals(QObject):
    done=Signal(str,int,object,str)

class Task(QRunnable):
    def __init__(self,key,revision,fn):
        super().__init__();self.key=key;self.revision=revision;self.fn=fn;self.signals=Signals()
    def run(self):
        try:self.signals.done.emit(self.key,self.revision,self.fn(),'')
        except Exception as e:self.signals.done.emit(self.key,self.revision,None,str(e))

class ChannelList(QListWidget):
    def mouseReleaseEvent(self,event):
        item=self.itemAt(event.position().toPoint())
        if event.button()==Qt.LeftButton and item and item.flags() & Qt.ItemIsUserCheckable:
            item.setCheckState(Qt.Unchecked if item.checkState()==Qt.Checked else Qt.Checked)
            event.accept();return
        super().mouseReleaseEvent(event)
    def keyPressEvent(self,event):
        item=self.currentItem()
        if event.key() in (Qt.Key_Space,Qt.Key_Return,Qt.Key_Enter) and item and item.flags() & Qt.ItemIsUserCheckable:
            item.setCheckState(Qt.Unchecked if item.checkState()==Qt.Checked else Qt.Checked)
            event.accept();return
        super().keyPressEvent(event)

class ChannelMenu(QMenu):
    channelToggled=Signal(object,bool)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.list=ChannelList()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setTextElideMode(Qt.ElideRight);self.list.setUniformItemSizes(True)
        self.list.setStyleSheet('QListWidget { border: none; outline: none; } QListWidget::item { padding: 7px 10px; } QListWidget::item:selected { background: #D6E4F7; color: #1C2028; }')
        action=QWidgetAction(self);action.setDefaultWidget(self.list);self.addAction(action)
        self.list.itemChanged.connect(lambda item:self.channelToggled.emit(item.data(Qt.UserRole),item.checkState()==Qt.Checked))
        self.fit_list()
    def clear(self):
        self.list.clear()
    def add_channel(self,label,key,checked,tooltip='',color=None):
        item=QListWidgetItem(label);item.setToolTip(tooltip or label);item.setData(Qt.UserRole,key)
        if color:item.setForeground(QColor(color))
        item.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked);self.list.addItem(item)
    def add_note(self,label,tooltip=''):
        item=QListWidgetItem(label);item.setFlags(Qt.NoItemFlags);item.setToolTip(tooltip or label);self.list.addItem(item)
    def available(self):
        return (self.parentWidget() or self).screen().availableGeometry()
    def fit_list(self):
        screen=self.available()
        height=self.list.sizeHintForRow(0) if self.list.count() else self.fontMetrics().height()+14
        self.list.setFixedSize(min(460,screen.width()-24),min(360,screen.height()-40,max(1,self.list.count())*height+2))
        self.setFixedSize(self.list.width()+2,self.list.height()+2)
        if self.isVisible():self.keep_on_screen()
    def keep_on_screen(self):
        screen=self.available()
        self.move(max(screen.left(),min(self.x(),screen.right()-self.width()+1)),
                  max(screen.top(),min(self.y(),screen.bottom()-self.height()+1)))
    def showEvent(self,event):
        super().showEvent(event);self.keep_on_screen();self.list.setFocus()

class HeightGrip(QWidget):
    def __init__(self,plot,changed):
        super().__init__(plot);self.plot=plot;self.changed=changed;self.origin=None;self.hover=False
        self.setCursor(Qt.SplitVCursor);self.setFixedHeight(8);self.setToolTip('Drag to change this plot’s height. Double-click to reset it.')
        plot.installEventFilter(self);self.place()
    def place(self):
        self.setGeometry(0,self.plot.height()-self.height(),self.plot.width(),self.height());self.raise_()
    def eventFilter(self,watched,event):
        if event.type()==QEvent.Resize:self.place()
        return False
    def paintEvent(self,event):
        active=self.hover or self.origin is not None;line=3 if active else 1
        QPainter(self).fillRect(0,self.height()-line,self.width(),line,QColor(theme.ACCENT if active else '#DDE0E6'))
    def enterEvent(self,event):
        self.hover=True;self.update()
    def leaveEvent(self,event):
        self.hover=False;self.update()
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:self.origin=(event.globalPosition().y(),self.plot.height());event.accept()
    def mouseMoveEvent(self,event):
        if self.origin is not None:self.changed(self.plot,max(60,round(self.origin[1]+event.globalPosition().y()-self.origin[0])))
    def mouseReleaseEvent(self,event):
        self.origin=None;self.update()
    def mouseDoubleClickEvent(self,event):
        self.origin=None;self.changed(self.plot,None)

class Viewer(QMainWindow):
    def __init__(self,root):
        super().__init__();self.root=Path(root);self.pool=QThreadPool(self);self.pool.setMaxThreadCount(2)
        self.quality_pool=QThreadPool(self);self.quality_pool.setMaxThreadCount(1)
        self.revisions={};self.callbacks={};self.catalog={};self.views=[];self.view=None;self.patient=None;self.loaded=[];self.ready=False;self.error='';self.issues=[]
        self.source_cache=None;self.wave_cache=None;self.trace_cache=OrderedDict();self.trace_cache_bytes=0;self.catalog_loaded=False
        self.flags=None;self.good_saved=False;self.rating_unlocked=False;self.filtered_records={};self.filtered_files={};self.targets={};self.hemispheres={};self.hemisphere_files={}
        self.catalog_sources={}
        self.progress_revision=None
        self.available_channels=[];self.selected_channels=[];self.loaded_by_channel={};self.plot_keys=[];self.plot_heights={};self.channel_roles={}
        self.active_spike_channel=None;self.sort_dialog=None;self.recording_windows={};self.spike_results={};self.spike_store_error='';self.sort_progress={}
        self.feature_dialog=None;self.saved_note=''
        self.sorted_color=QColor('#25834D')
        marker=QPixmap(12,12);marker.fill(Qt.transparent)
        painter=QPainter(marker);painter.setRenderHint(QPainter.Antialiasing);painter.setPen(Qt.NoPen);painter.setBrush(self.sorted_color);painter.drawEllipse(2,2,8,8);painter.end()
        self.sorted_icon=QIcon(marker)
        self.channels_complete=False;self.channel_error='';self.pending_channel_selection=None;self.channel_preferences=None
        self.auto_channels=True;self.quality_pending=False;self.channel_quality={};self.quality_cache=OrderedDict();self.channel_votes={}
        self.quality_store=None;self.labels_pending=False;self.label_error='';self.spectrum_dialog=None;self.crop_dialog=None;self.crop_span=None
        self.trace_timer=QTimer(self,interval=120,singleShot=True);self.trace_timer.timeout.connect(self.load_trace)
        self.setWindowTitle('Viz Test — Read-only recordings');self.resize(1240,800);self.setMinimumSize(850,560)
        outer,layout=page();self.setCentralWidget(outer);split=QSplitter(Qt.Horizontal);layout.addWidget(split)
        left,ll=page();left.setMinimumWidth(190);heading=QLabel('Patients');heading.setObjectName('heading');ll.addWidget(heading)
        types=QHBoxLayout();types.setSpacing(12);self.type_boxes={}
        for suffix,label,tip in (('.map','MAP','Alpha Omega .map recordings (Electrode channels, 12 or 24 kHz).'),
                                 ('.mpx','MPX','Alpha Omega .mpx recordings (SPK and RAW channels, 44 kHz).'),
                                 ('.plx','PLX','Plexon-format .plx copies from the lab server (UCSF_141 onward). They were converted from APM recordings (20 kHz) or Alpha Omega MAP recordings (24 kHz), not recorded on a Plexon system.')):
            box=QCheckBox(label);box.setChecked(True);box.setToolTip(tip+' Untick to hide these recordings and patients who only have them.');box.toggled.connect(self.format_changed);types.addWidget(box);self.type_boxes[suffix]=box
        types.addStretch(1);ll.addLayout(types)
        self.only_good=QCheckBox('Only good recordings');self.only_good.setToolTip('Show flagged recordings and patients with at least one matching recording.');self.only_good.toggled.connect(self.format_changed);ll.addWidget(self.only_good)
        self.search=QLineEdit(placeholderText='Search patients…',clearButtonEnabled=True);self.search.textChanged.connect(self.filter);ll.addWidget(self.search)
        self.tree=QTreeWidget(headerHidden=True,indentation=12);self.tree.currentItemChanged.connect(self.choose);self.tree.itemExpanded.connect(self.expanded);ll.addWidget(self.tree,1)
        self.sort_total=QLabel('0 patients · 0 sorted neurons');self.sort_total.setObjectName('muted');self.sort_total.setWordWrap(False)
        self.sort_total_help='Totals for the patients listed under the file-type and Only good recordings filters; searching does not change them. Each saved selected cluster counts as one neuron per recording/channel. Historical, cleared and mismatched-span results are excluded.'
        self.sort_total.setToolTip(self.sort_total_help)
        ll.addWidget(self.sort_total);split.addWidget(left)
        right,rl=page();header=QHBoxLayout();self.title=QLabel('Choose a patient');self.title.setObjectName('heading');self.title.setWordWrap(True);header.addWidget(self.title,1)
        self.good_recording=QCheckBox('Good recording');self.good_recording.hide();self.good_recording.toggled.connect(self.mark_good);header.addWidget(self.good_recording)
        self.rating_lock=QToolButton(text='Unlock rating');self.rating_lock.setToolTip('Unlock one rating change for this recording. Ratings lock again after the change or when you switch recordings.');self.rating_lock.hide();self.rating_lock.clicked.connect(self.toggle_rating_lock);header.addWidget(self.rating_lock)
        rl.addLayout(header)
        subheader=QHBoxLayout();self.subtitle=QLabel('');self.subtitle.setObjectName('muted');subheader.addWidget(self.subtitle,1)
        self.channel_button=QToolButton(text='Channels');self.channel_button.setPopupMode(QToolButton.InstantPopup)
        self.channel_menu=ChannelMenu(self.channel_button);self.channel_menu.aboutToShow.connect(self.load_available_channels);self.channel_button.setMenu(self.channel_menu)
        self.channel_menu.channelToggled.connect(self.toggle_channel)
        self.sort_button=QToolButton(text='Sort spikes');self.sort_button.hide();self.sort_button.clicked.connect(self.open_spike_sorting);subheader.addWidget(self.sort_button)
        self.feature_button=QToolButton(text='Calculate features');self.feature_button.hide();self.feature_button.setEnabled(False)
        self.feature_button.clicked.connect(self.open_features);subheader.addWidget(self.feature_button)
        self.span_editor=SpanEditor(self);self.span_editor.changed.connect(self.refresh_spike_sorts);self.span_editor.changed.connect(self.refresh_sort_progress);subheader.addWidget(self.span_editor)
        self.spectra_button=QToolButton(text='Spectra');self.spectra_button.setToolTip('Power spectra of this recording\'s inputs over the entire recording, colored by channel quality.')
        self.spectra_button.clicked.connect(self.open_spectra);self.spectra_button.hide();subheader.addWidget(self.spectra_button)
        self.crop_button=QToolButton(text='Crop Data');self.crop_button.clicked.connect(self.open_crop_data)
        self.crop_button.hide();subheader.addWidget(self.crop_button)
        self.channel_button.hide();subheader.addWidget(self.channel_button);rl.addLayout(subheader)
        self.trace_area=QScrollArea();self.trace_area.setWidgetResizable(True);self.trace_area.setFrameShape(QFrame.NoFrame);self.trace_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);trace_holder=QWidget()
        plots_layout=QVBoxLayout(trace_holder);plots_layout.setContentsMargins(0,0,0,0);plots_layout.setSpacing(12);self.plots_layout=plots_layout
        self.trace_layout=QVBoxLayout();self.trace_layout.setSpacing(12);plots_layout.addLayout(self.trace_layout,1)
        self.trace_area.setWidget(trace_holder);rl.addWidget(self.trace_area,1)
        notes_header=QHBoxLayout();self.notes_title=QLabel('Neuron notes');self.notes_title.setObjectName('heading');notes_header.addWidget(self.notes_title)
        self.notes_status=QLabel('');self.notes_status.setObjectName('muted');notes_header.addWidget(self.notes_status,1)
        self.notes_save=QPushButton('Save note');self.notes_save.clicked.connect(self.save_neuron_note);notes_header.addWidget(self.notes_save)
        rl.addLayout(notes_header)
        self.notes_edit=QPlainTextEdit();self.notes_edit.setPlaceholderText('Notes about this neuron and recording…')
        self.notes_edit.setFixedHeight(94);self.notes_edit.textChanged.connect(self.note_edited);rl.addWidget(self.notes_edit)
        self.notes_edit.setEnabled(False);self.notes_save.setEnabled(False)
        self.trace=None;self.depth=None;self.traces=[]
        self.trace_area.viewport().installEventFilter(self)
        self.note=QLabel('');self.note.setObjectName('muted');rl.addWidget(self.note);split.addWidget(right);split.setSizes([270,970])
        menu=self.menuBar().addMenu('File')
        for title,fn in [('Choose folder…',self.choose_folder),('Refresh',self.scan),('Reset view',self.reset_view),('Save image…',self.save_image),('Recording details…',self.details)]:menu.addAction(title).triggered.connect(fn)
        QTimer.singleShot(0,self.scan)
    def eventFilter(self,watched,event):
        if event.type()==QEvent.Wheel and watched is not self.trace_area.viewport():
            plot=next((trace for trace in self.traces if watched is trace.viewport()),None)
            if plot is not None and shift_zoom_x(plot,event,self.traces):return True
        if event.type()==QEvent.MouseButtonDblClick:
            plot=next((trace for trace in self.traces if watched is trace.viewport()),None)
            if plot is not None and event.button()==Qt.LeftButton:
                point=plot.mapToScene(event.position().toPoint())
                if plot.getViewBox().sceneBoundingRect().contains(point) and self.open_recording_from_trace(plot):
                    return True
        if watched is self.trace_area.viewport() and event.type()==QEvent.Resize:
            self.size_trace_panels()
        elif event.type()==QEvent.Wheel and watched is not self.trace_area.viewport():
            bar=self.trace_area.verticalScrollBar()
            if bar.maximum()>0 and not event.modifiers() & (Qt.ControlModifier|Qt.MetaModifier):
                delta=event.pixelDelta().y() or event.angleDelta().y()/120*bar.singleStep()*3
                if delta:
                    bar.setValue(bar.value()-round(delta));event.accept();return True
        return super().eventFilter(watched,event)
    def size_trace_panels(self):
        height=max(120,(self.trace_area.viewport().height()-4*12)//5)
        for plot in self.traces+([self.depth] if self.depth is not None else []):
            custom=self.plot_heights.get(plot.height_key)
            if custom:plot.setFixedHeight(custom)
            else:plot.setMinimumHeight(height);plot.setMaximumHeight(16777215)
        self.trace_area.verticalScrollBar().setSingleStep(max(20,height//3))
    def set_plot_height(self,plot,height):
        if plot.height_key is None:return
        if height is None:self.plot_heights.pop(plot.height_key,None)
        else:self.plot_heights[plot.height_key]=height
        self.size_trace_panels()
    def plot(self):
        global pg
        if pg is None:
            import pyqtgraph as pg
            pg.setConfigOptions(antialias=False,background='w',foreground=theme.MUTED)
        p=pg.PlotWidget(background='w');p.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Ignored);p.setMinimumHeight(50);p.showGrid(x=True,y=True,alpha=.1);p.setMenuEnabled(False);p.getAxis('left').setWidth(70)
        p.viewport().installEventFilter(self);p.height_key=None;p.height_grip=HeightGrip(p,self.set_plot_height)
        p.setToolTip('Scroll to see more channels. Hold Control or Command while scrolling to zoom; hold Shift to zoom only in time. Double-click a non-electrode trace to inspect it in a larger window.')
        for a in ('left','bottom'):p.getAxis(a).enableAutoSIPrefix(False)
        p.scene().sigMouseClicked.connect(lambda event,plot=p:self.select_spike_channel(plot) if event.button()==Qt.LeftButton else None)
        return p
    def ensure_plots(self):
        if self.trace is not None:return
        self.trace=self.plot();self.trace.setLabel('left','Amplitude');self.trace.setLabel('bottom','Recorded time (s)');self.trace_layout.addWidget(self.trace,1);self.traces=[self.trace]
        self.depth=self.plot();self.depth.setLabel('left','Depth (mm)');self.depth.setLabel('bottom','Recorded time (s)');self.depth.setXLink(self.trace);self.depth.setMouseEnabled(x=False,y=False);self.depth.height_key=('depth',);self.plots_layout.addWidget(self.depth,1);self.plots_layout.addStretch(0);self.depth.hide()
    def submit(self,key,fn,callback,pool=None):
        revision=self.revisions.get(key,0)+1;self.revisions[key]=revision;self.callbacks[key]=callback
        task=Task(key,revision,fn);task.signals.done.connect(self.done);(pool or self.pool).start(task)
    def done(self,key,revision,result,error):
        if revision!=self.revisions.get(key):return
        if key=='sort' and error:
            if self.sort_dialog:self.sort_dialog.failed(error)
            return
        if key=='sort_cardiac' and error:
            if self.sort_dialog:self.sort_dialog.cardiac_status.setText(f'Could not load the cardiac reference: {error}')
            return
        if key=='features' and error:
            if self.feature_dialog:self.feature_dialog.failed(error)
            return
        if key=='notch' and error:
            if self.spectrum_dialog:self.spectrum_dialog.notch_failed(error)
            return
        if key=='crop_emg' and error:
            if self.crop_dialog:self.crop_dialog.message.setText(f'Could not load EMG: {error}')
            return
        if key=='labels' and error:
            self.labels_pending=False;self.label_error=error;self.update_channel_menu();return
        if key=='quality' and error:
            self.quality_pending=False;self.channel_error=error;self.update_channel_menu()
            self.rebuild_traces();self.note.setText('Automatic channel check failed — open Channels to retry.')
            return
        if error:self.error=error;self.note.setText(error);return
        self.callbacks[key](result)
    def clear(self):
        if not self.save_neuron_note():return False
        self.revisions['notch']=self.revisions.get('notch',0)+1
        for window in list(self.recording_windows.values()):window.close()
        self.recording_windows.clear()
        self.cancel_features();self.feature_button.hide();self.feature_button.setEnabled(False)
        self.cancel_spike_sorting();self.spike_results={};self.spike_store_error='';self.active_spike_channel=None;self.sort_button.hide()
        self.span_editor.clear()
        self.trace_timer.stop()
        self.revisions['trace']=self.revisions.get('trace',0)+1;self.view=None;self.ready=False;self.error='';self.loaded=[]
        self.revisions['quality']=self.revisions.get('quality',0)+1
        self.revisions['labels']=self.revisions.get('labels',0)+1
        self.auto_channels=True;self.quality_pending=False;self.channel_quality={};self.labels_pending=False;self.label_error=''
        self.available_channels=[];self.selected_channels=[];self.loaded_by_channel={};self.channels_complete=False;self.channel_error='';self.pending_channel_selection=None;self.channel_preferences=None;self.channel_roles={}
        self.channel_button.hide();self.channel_menu.clear();self.spectra_button.hide();self.crop_button.hide();self.crop_span=None
        if self.spectrum_dialog is not None:self.spectrum_dialog.close();self.spectrum_dialog=None
        self.lock_rating();self.rating_lock.hide()
        self.good_recording.blockSignals(True);self.good_recording.setChecked(False);self.good_recording.blockSignals(False);self.good_recording.hide();self.good_saved=False
        self.clear_traces()
        if self.depth is not None:self.depth.clear();self.depth.hide()
        self.note.clear()
        self.notes_edit.blockSignals(True);self.notes_edit.clear();self.notes_edit.blockSignals(False)
        self.notes_edit.setEnabled(False);self.notes_save.setEnabled(False);self.notes_status.clear()
        return True
    def note_edited(self):
        self.notes_save.setEnabled(self.view is not None and self.notes_edit.toPlainText()!=self.saved_note)
        self.notes_status.setText('Unsaved changes' if self.notes_save.isEnabled() else 'Saved')
    def load_neuron_note(self):
        try:self.saved_note=NeuronNotes(self.flags).load(self.view)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            self.saved_note='';self.notes_status.setText(f'Could not load note: {error}')
            self.notes_edit.setEnabled(False);self.notes_save.setEnabled(False);return
        self.notes_edit.blockSignals(True);self.notes_edit.setPlainText(self.saved_note);self.notes_edit.blockSignals(False)
        self.notes_edit.setEnabled(True);self.notes_save.setEnabled(False)
        self.notes_status.setText('Saved' if self.saved_note else '')
    def save_neuron_note(self):
        if self.view is None or not self.notes_save.isEnabled():return True
        value=self.notes_edit.toPlainText()
        try:NeuronNotes(self.flags).save(self.view,value)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Cannot save neuron note',f'Could not save this note to {self.flags.path}:\n{error}')
            self.notes_status.setText('Save failed — note is still unsaved')
            return False
        self.saved_note=value;self.notes_save.setEnabled(False);self.notes_status.setText('Saved')
        return True
    def clear_traces(self):
        if self.trace is None:return
        self.span_editor.set_plots([])
        for trace in self.traces:
            if hasattr(trace,'spike_overlay'):trace.spike_overlay.clear()
        for trace in self.traces[1:]:
            trace.setXLink(None);self.trace_layout.removeWidget(trace);trace.close();trace.deleteLater()
        self.traces=[self.trace];self.plot_keys=[];self.trace.clear();self.trace.recording_pieces=None;self.trace.setTitle('');self.trace.showAxis('bottom');self.trace.height_key=None
    def type_shown(self,path):
        box=self.type_boxes.get(Path(path).suffix.lower());return box is None or box.isChecked()
    def scan(self):
        if not self.save_neuron_note():return
        if self.crop_dialog:self.crop_dialog.close()
        self.catalog_loaded=False;self.sort_progress={}
        self.sort_total.setText('Finding patients…')
        if not self.clear():return
        self.catalog={};self.patient=None;self.tree.clear()
        if self.flags:self.flags.close()
        self.flags=RecordingFlags(self.root)
        self.trace_cache.clear();self.trace_cache_bytes=0
        try:
            cache_root=library_paths.cache_dir(self.root) if library_paths.is_library(self.root) else None
            self.source_cache=reader.SourceCache(cache_root/'reader-cache.sqlite3',lazy=True) if cache_root else None
            self.wave_cache=reader.WaveformCache(cache_root/'waveforms') if cache_root else None
            self.quality_store=channel_quality.Store(cache_root/'channel-quality.sqlite3',self.root) if cache_root else None
        except (OSError, sqlite3.Error):
            self.source_cache=None;self.wave_cache=None;self.quality_store=None
        def find():
            if library_paths.is_library(self.root):
                groups={p.name:reader.raw_files(p) for p in self.root.iterdir() if library_paths.is_patient(p)}
            else:groups={self.root.name:reader.raw_files(self.root)}
            return {name:files for name,files in groups.items() if files}
        def found(catalog):
            self.targets=patient_targets.build(self.root,catalog);self.set_hemispheres(hemispheres.build(self.root,catalog))
            self.list_patients(catalog)
        self.submit('library',find,found)
    def set_hemispheres(self,patients):
        self.hemispheres=patients
        self.hemisphere_files={path:entry for patient in patients.values() for path,entry in patient['files'].items()}
    def recording_hemisphere(self,view):
        return hemispheres.record(self.hemispheres,self.patient,self.root,view.sources[0].path)
    def recording_text(self,view):
        mark=hemispheres.mark(self.recording_hemisphere(view))
        return (f'{mark} · ' if mark else '')+view.label
    def show_hemisphere(self,view):
        entry=self.recording_hemisphere(view)
        self.subtitle.setText(hemispheres.text(entry) if entry else '')
        self.subtitle.setToolTip(hemispheres.tooltip(entry) if entry else '')
    def patient_label(self,name):
        kinds='/'.join(s[1:].upper() for s in sorted({p.suffix.lower() for p in self.catalog.get(name,[])}))
        return f"{name} ({self.targets.get(name,{}).get('label','Unknown')})"+(f' · {kinds}' if kinds else '')
    def label_patient(self,item,name):
        available=self.available_sources(name)
        count=sum(bool(entry['sources']) and entry['sources']<=available for entry in self.sort_progress.values())
        item.setText(0,self.patient_label(name)+(f' · {count} sorted' if count else ''))
        tooltip=patient_targets.tooltip(self.targets.get(name,{}))
        sides=Counter(entry.get('side') or 'unknown' for entry in self.hemispheres.get(name,{}).get('files',{}).values())
        if sides:tooltip+='\n\nRecorded hemisphere: '+', '.join(f'{sides[s]} {s.lower()}' for s in ('Left','Right','unknown') if sides[s])+' recordings.'
        if count:tooltip+=f'\n\n{count} spike-sorted recording'+('s' if count!=1 else '')+' in the current filters (at least one channel saved per recording).'
        item.setToolTip(0,tooltip);self.color_sorted_item(item,count>0)
    def color_sorted_item(self,item,sorted):
        item.setForeground(0,QBrush(self.sorted_color) if sorted else QBrush())
        item.setIcon(0,self.sorted_icon if sorted else QIcon())
    def label_recording(self,item,view):
        entry=self.sort_progress.get(self.flags.identity(view)[0]);count=len(entry['channels']) if entry else 0
        side=self.recording_hemisphere(view)
        item.setText(0,self.recording_text(view));self.color_sorted_item(item,count>0)
        tooltip='\n'.join(str(s.path) for s in view.sources)
        if side:tooltip+='\n\n'+hemispheres.tooltip(side)
        if count:tooltip+=f'\n\nSpike sorted · {count} channel'+('s' if count!=1 else '')+' saved'
        item.setToolTip(0,tooltip)
    def load_sort_progress(self):
        try:
            self.sort_progress=SpikeSortStore(self.flags).progress(self.views)
            self.progress_revision=self.database_revision()
            self.update_sort_total()
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            self.sort_total.setText(f'{len(self.listed_patients()):,} patients · neuron total unavailable')
            self.sort_progress={};self.note.setText(f'Could not read spike-sorting progress: {error}')
    def listed_patients(self):
        return [name for name in self.catalog if self.patient_files(name)]
    def update_sort_total(self):
        listed=self.listed_patients();available=set().union(*map(self.available_sources,listed))
        entries=[entry for entry in self.sort_progress.values() if entry['sources'] and entry['sources']<=available]
        patients=len(listed);neurons=sum(len(entry['channels']) for entry in entries)
        self.sort_total.setText(f'{patients:,} patient'+('s' if patients!=1 else '')+f' · {neurons:,} sorted neuron'+('s' if neurons!=1 else ''))
        sides=Counter()
        for entry in entries:
            found={(self.hemisphere_files.get(source) or {}).get('side') for source in entry['sources']}
            sides[found.pop() if len(found)==1 and None not in found else 'Unknown']+=len(entry['channels'])
        self.sort_total.setToolTip(self.sort_total_help+(f"\n\nSorted neurons by hemisphere: {sides['Left']:,} left, {sides['Right']:,} right, {sides['Unknown']:,} unknown." if neurons else ''))
    def refresh_sort_progress(self):
        if not self.flags:return
        self.load_sort_progress()
        for i in range(self.tree.topLevelItemCount()):
            parent=self.tree.topLevelItem(i);data=parent.data(0,Qt.UserRole)
            if not data or data[0]!='patient':continue
            self.label_patient(parent,data[1])
            if data[1]!=self.patient:continue
            for j in range(parent.childCount()):
                item=parent.child(j);child=item.data(0,Qt.UserRole)
                if child and child[0]=='view' and child[1]<len(self.views):self.label_recording(item,self.views[child[1]])
    def patient_files(self,name):
        if self.only_good.isChecked():return self.filtered_files.get(name,[])
        return [p for p in self.catalog[name] if self.type_shown(p)]
    def index_catalog_sources(self,catalog):
        self.catalog_sources={}
        prefix=str(self.flags.root).rstrip('/')+'/'
        for name,paths in catalog.items():
            available={}
            for path in paths:
                text=str(path)
                relative=text[len(prefix):] if text.startswith(prefix) else str(path.resolve().relative_to(self.flags.root))
                available[relative]=path
            self.catalog_sources[name]=available
    def available_sources(self,name):
        sources=self.catalog_sources.get(name,{})
        if self.only_good.isChecked():
            wanted=set(self.filtered_files.get(name,[]))
            return {relative for relative,path in sources.items() if path in wanted}
        return {relative for relative,path in sources.items()
                if self.type_shown(path)}
    def load_recording_filter(self,catalog):
        self.filtered_records={};self.filtered_files={}
        if not self.only_good.isChecked():return
        try:
            self.filtered_records=self.flags.good_recordings()
            for name in catalog:
                available={relative:path for relative,path in self.catalog_sources[name].items()
                           if self.type_shown(path)}
                wanted=set()
                for sources in self.filtered_records.values():
                    if sources and sources<=available.keys():wanted.update(sources)
                if wanted:self.filtered_files[name]=[path for relative,path in available.items() if relative in wanted]
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            self.only_good.blockSignals(True);self.only_good.setChecked(False);self.only_good.blockSignals(False)
            QMessageBox.warning(self,'Cannot filter recordings',f'Could not read {self.flags.path}:\n{error}')
    def format_changed(self):
        if self.catalog:
            self.list_patients(self.catalog,refresh_progress=self.database_revision()!=self.progress_revision)
    def database_revision(self):
        connection=self.flags.connection if self.flags else None
        if connection is None:return None
        return connection.total_changes,connection.execute('PRAGMA data_version').fetchone()[0]
    def list_patients(self,catalog,refresh_progress=True):
        if not self.save_neuron_note():return
        if catalog is not self.catalog or not self.catalog_sources:
            self.index_catalog_sources(catalog)
        self.load_recording_filter(catalog)
        self.revisions['library']=self.revisions.get('library',0)+1
        if not self.clear():return
        self.patient=None;self.views=[];self.issues=[];self.title.setText('Choose a patient');self.subtitle.clear();self.subtitle.setToolTip('')
        self.catalog=catalog
        if refresh_progress:self.load_sort_progress()
        else:self.update_sort_total()
        self.tree.blockSignals(True);self.tree.clear()
        for name in sorted(catalog,key=reader.natural):
            if not self.patient_files(name):continue
            item=QTreeWidgetItem(self.tree);item.setData(0,Qt.UserRole,('patient',name));self.label_patient(item,name);QTreeWidgetItem(item,[''])
        self.tree.blockSignals(False);self.filter()
        if not self.tree.topLevelItemCount():self.note.setText('No good recordings match these filters.' if self.only_good.isChecked() else 'No recordings match the ticked file types.')
        self.catalog_loaded=True
    def expanded(self,item):
        data=item.data(0,Qt.UserRole)
        if data and data[0]=='patient' and data[1]!=self.patient:self.open_patient(data[1])
    def choose(self,item,previous=None):
        if item is None:return
        data=item.data(0,Qt.UserRole)
        if not data:return
        if data[0]=='patient' and data[1]!=self.patient:self.open_patient(data[1])
        elif data[0]=='view':self.select_view(self.views[data[1]])
    def open_patient(self,name):
        if self.crop_dialog and self.crop_dialog.patient!=name:self.crop_dialog.close()
        if not self.clear():return
        self.patient=name;self.title.setText(self.patient_label(name));self.subtitle.setText('Loading original recordings…');self.subtitle.setToolTip('');self.views=[];self.crop_button.show()
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item=self.tree.topLevelItem(i);matches=item.data(0,Qt.UserRole)==('patient',name);item.takeChildren();QTreeWidgetItem(item,['Loading…' if matches else '']);item.setExpanded(matches)
        self.tree.blockSignals(False)
        files=self.patient_files(name);cache=self.source_cache
        self.submit('library',lambda:reader.recordings(files,cache),self.list_views)
    def list_views(self,result):
        self.views,self.issues=result
        if self.only_good.isChecked():self.views=[view for view in self.views if self.flags.identity(view)[0] in self.filtered_records]
        self.load_sort_progress()
        self.tree.blockSignals(True)
        parent=next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()) if self.tree.topLevelItem(i).data(0,Qt.UserRole)==('patient',self.patient)),None)
        if parent is None:self.tree.blockSignals(False);return
        parent.takeChildren();first=None
        for i,v in enumerate(self.views):
            item=QTreeWidgetItem(parent);item.setData(0,Qt.UserRole,('view',i));self.label_recording(item,v);first=first or item
        self.label_patient(parent,self.patient)
        self.tree.blockSignals(False);self.filter()
        if first:self.tree.setCurrentItem(first)
        else:self.subtitle.setText('No recordings found.')
    def select_view(self,view):
        self.ensure_plots()
        if not self.clear():return
        self.view=view;self.title.setText(f'{self.patient_label(self.patient)} · {self.recording_text(view)}');self.show_hemisphere(view)
        try:self.channel_roles=ChannelRoleStore(self.flags).load(view)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Cannot load channel labels',str(error));self.channel_roles={}
        try:self.crop_span=CropStore(self.flags).effective_crop(view)
        except (OSError,sqlite3.Error,ValueError,TypeError):self.crop_span=None
        self.load_neuron_note()
        self.restore_good()
        self.span_editor.bind(self.flags,view)
        saved=None
        try:
            saved=self.flags.patient_visible_channels(view)
            self.auto_channels=self.flags.automatic_channels(view)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            self.auto_channels=False
            QMessageBox.warning(self,'Cannot load channel selection',f'Could not read {self.flags.path}:\n{error}')
        self.channel_preferences=saved
        self.quality_pending=self.auto_channels
        self.channel_quality=self.quality_cache.get(self.quality_signature(view),{})
        self.available_channels=list(view.channels)
        self.selected_channels=[c for c in view.channels if saved is None or self.flags.channel_selector(c.key) in saved]
        if not self.auto_channels and saved is not None and saved-{self.flags.channel_selector(c.key) for c in view.channels}:self.pending_channel_selection=saved
        self.channel_button.show();self.spectra_button.show();self.crop_button.show();self.load_labels();self.update_channel_menu()
        if self.auto_channels:
            self.trace.setXRange(0,max(view.duration,1e-9),padding=0)
            self.note.setText('Choosing channels once for this patient…');self.load_available_channels()
        else:
            self.rebuild_traces(reset=True)
            if self.pending_channel_selection is not None:self.ready=False;self.load_available_channels()
    def update_channel_menu(self):
        selected={c.key for c in self.selected_channels};self.channel_menu.clear()
        labels={}
        for channel in self.available_channels:
            label=f'{channel.name} · {channel.fs/1000:g} kHz'
            labels[label]=labels.get(label,0)+1
        for channel in self.available_channels:
            label=f'{channel.name} · {channel.fs/1000:g} kHz'
            if labels[label]>1:label+=f' · channel {channel.key[1]} · gain {channel.key[4]:g} · amp {channel.key[3]:g}'
            quality=self.channel_quality.get(channel.key)
            tooltip=label
            if quality:
                tooltip+='\n'+quality.detail
                label+=' · '+quality.label
            votes=self.channel_votes.get(self.patient,{}).get(self.flags.channel_selector(channel.key))
            if votes:tooltip+=f'\nOne-off choice: looked neural in {votes[0]}/{votes[1]} sampled recordings of this patient.'
            self.channel_menu.add_channel(label,channel.key,channel.key in selected,tooltip,QUALITY_COLORS.get(quality.verdict) if quality else None)
        if self.labels_pending:self.channel_menu.add_note('Checking every channel over the entire recording…')
        elif self.label_error:self.channel_menu.add_note('Channel check failed — reopen the recording to retry',self.label_error)
        if self.quality_pending:self.channel_menu.add_note('Choosing channels once for this patient…')
        elif self.channels_complete and self.channel_error:self.channel_menu.add_note('Automatic check failed — reopen to retry',self.channel_error)
        if not self.channels_complete:
            self.channel_menu.add_note('Other channels load when opened')
        elif not self.available_channels:
            self.channel_menu.add_note('No continuous signals in this recording')
        self.channel_menu.fit_list()
    def load_available_channels(self):
        if not self.view:return
        if self.channels_complete:
            if self.auto_channels and self.channel_error:self.check_channel_quality()
            return
        self.channel_error=''
        resolved={c.key:c for c in self.selected_channels}
        self.available_channels=[resolved.get(c.key,c) for c in reader.channel_options(self.view)]
        restoring=self.pending_channel_selection is not None
        if restoring:
            self.selected_channels=[c for c in self.available_channels if self.flags.channel_selector(c.key) in self.pending_channel_selection]
            self.pending_channel_selection=None
        self.channels_complete=True;self.update_channel_menu();self.update_spectra()
        if restoring:self.rebuild_traces()
        if self.auto_channels:self.check_channel_quality()
    @staticmethod
    def quality_signature(view):
        return tuple((str(s.path),s.size,s.mtime_ns,s.ctime_ns) for s in view.sources)
    def load_labels(self):
        view=self.view;cache=self.source_cache;store=self.quality_store;signature=self.quality_signature(view)
        self.labels_pending=True;self.label_error=''
        revision=self.revisions.get('labels',0)+1
        def loaded(result):
            self.labels_pending=False
            self.quality_cache[signature]={**self.quality_cache.get(signature,{}),**result};self.quality_cache.move_to_end(signature)
            while len(self.quality_cache)>256:self.quality_cache.popitem(last=False)
            if self.view is not view:return
            self.channel_quality=self.quality_cache[signature];self.update_channel_menu();self.update_spectra()
        self.submit('labels',lambda:channel_quality.label(view,cache,lambda:revision!=self.revisions.get('labels'),store),loaded,self.quality_pool)
    def open_spectra(self):
        if not self.view:return
        self.load_available_channels()
        if self.spectrum_dialog is None:
            from spectrum_ui import SpectrumDialog
            self.spectrum_dialog=SpectrumDialog(self)
            self.spectrum_dialog.requestNotched.connect(self.prepare_notched_spectra)
            self.spectrum_dialog.saveLabels.connect(self.save_channel_labels)
            def finished(_):
                self.revisions['notch']=self.revisions.get('notch',0)+1
                self.spectrum_dialog=None
            self.spectrum_dialog.finished.connect(finished)
        self.update_spectra();self.spectrum_dialog.show();self.spectrum_dialog.raise_()
    def save_channel_labels(self,assignments):
        if self.view is None or not is_plx(self.view):return
        try:ChannelRoleStore(self.flags).save(self.view,assignments)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self.spectrum_dialog or self,'Channel labels not saved',str(error));return
        self.channel_roles=dict(assignments)
        self.update_spectra();self.refresh_spike_sorts();self.apply_crop_view()
        if self.crop_dialog is not None and self.crop_dialog.view is not None:
            self.crop_dialog.select_recording(self.crop_dialog.recordings.currentRow())
    def open_crop_data(self):
        if not self.patient:return
        if self.crop_dialog:
            self.crop_dialog.raise_();self.crop_dialog.activateWindow();return
        from crop_ui import CropDialog
        try:views,_=reader.recordings(self.catalog[self.patient],self.source_cache)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Cannot open Crop Data',str(error));return
        dialog=CropDialog(self,views);self.crop_dialog=dialog
        def finished(_):
            dialog.emg_revision+=1
            self.revisions['crop_emg']=self.revisions.get('crop_emg',0)+1
            if self.crop_dialog is dialog:self.crop_dialog=None
            dialog.deleteLater()
        dialog.finished.connect(finished);dialog.show()
    def apply_crop_view(self,force=False):
        if self.view is None:return
        try:span=CropStore(self.flags).effective_crop(self.view)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Cannot load crop',str(error));return
        if span==self.crop_span:
            if force:self.reset_view()
            return
        self.crop_span=span
        for window in list(self.recording_windows.values()):window.close()
        self.recording_windows.clear()
        self.reset_view()
    def update_spectra(self):
        if self.spectrum_dialog is None or not self.view:return
        self.spectrum_dialog.set_recording(f'{self.patient_label(self.patient)} · {self.recording_text(self.view)}',
            self.available_channels or list(self.view.channels),self.channel_quality,{c.key for c in self.selected_channels},
            self.labels_pending,self.quality_signature(self.view),self.channel_roles,is_plx(self.view))
    def prepare_notched_spectra(self,keys):
        dialog=self.spectrum_dialog;view=self.view
        if dialog is None or view is None:return
        channels={c.key:c for c in self.available_channels or view.channels}
        selected=[channels[key] for key in keys if key in channels]
        cache=self.source_cache
        revision=self.revisions.get('notch',0)+1
        def calculate():
            results,errors={},{}
            for channel in selected:
                if revision!=self.revisions.get('notch'):raise InterruptedError('Spectrum selection changed')
                try:results[channel.key]=channel_quality.notched_spectrum(channel,cache,
                    lambda:revision!=self.revisions.get('notch'))
                except (OSError,ValueError,TypeError,KeyError) as error:errors[channel.key]=str(error)
            return results,errors
        def complete(result):
            if self.view is view and self.spectrum_dialog is dialog:dialog.set_notched(*result)
        self.submit('notch',calculate,complete)
    def check_channel_quality(self):
        view=self.view;views=list(self.views) or [view];cache=self.source_cache;store=self.quality_store;saved=self.channel_preferences;patient=self.patient
        self.quality_pending=True;self.ready=False;self.channel_error='';self.update_channel_menu()
        def loaded(choice):
            self.quality_pending=False
            for screened,result in choice.screened:
                signature=self.quality_signature(screened)
                self.quality_cache[signature]={**self.quality_cache.get(signature,{}),**result};self.quality_cache.move_to_end(signature)
            while len(self.quality_cache)>256:self.quality_cache.popitem(last=False)
            self.channel_quality=self.quality_cache.get(self.quality_signature(view),{});self.channel_votes[patient]=choice.votes
            self.update_spectra()
            if not self.auto_channels:return
            self.selected_channels=[c for c in self.available_channels if self.flags.channel_selector(c.key) in choice.selectors]
            self.update_channel_menu();self.rebuild_traces()
            try:self.flags.set_patient_visible_channels(view,choice.selectors)
            except (OSError,sqlite3.Error,ValueError,TypeError) as error:
                self.note.setText(f'Automatic channel choice not saved: {error}');return
            self.channel_preferences=set(choice.selectors);self.auto_channels=False
        revision=self.revisions.get('quality',0)+1
        self.submit('quality',lambda:channel_quality.choose(views,view,saved,self.flags.channel_selector,cache,lambda:revision!=self.revisions.get('quality'),store),loaded,self.quality_pool)
    def toggle_channel(self,key,checked):
        if not self.view:return
        selected=set(self.channel_preferences) if self.channel_preferences is not None and not self.auto_channels else {self.flags.channel_selector(c.key) for c in self.selected_channels}
        if self.auto_channels and self.channel_preferences is not None:
            selected.update(self.channel_preferences-{self.flags.channel_selector(c.key) for c in self.available_channels})
        selector=self.flags.channel_selector(key)
        if checked:selected.add(selector)
        else:selected.discard(selector)
        try:self.flags.set_patient_visible_channels(self.view,selected)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            self.update_channel_menu()
            QMessageBox.warning(self,'Channel selection not saved',f'Could not save to {self.flags.path}:\n{error}');return
        self.channel_preferences=selected
        was_pending=self.quality_pending
        self.auto_channels=False;self.quality_pending=False;self.revisions['quality']=self.revisions.get('quality',0)+1
        if self.pending_channel_selection is not None:self.pending_channel_selection=selected
        self.selected_channels=[c for c in self.available_channels if self.flags.channel_selector(c.key) in selected]
        if was_pending:self.update_channel_menu()
        else:
            self.channel_menu.list.blockSignals(True)
            keys={c.key for c in self.selected_channels}
            for i in range(self.channel_menu.list.count()):
                item=self.channel_menu.list.item(i)
                if item.flags() & Qt.ItemIsUserCheckable:item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in keys else Qt.Unchecked)
            self.channel_menu.list.blockSignals(False)
        self.rebuild_traces()
    def rebuild_traces(self,reset=False):
        self.ensure_plots()
        self.trace_timer.stop()
        self.span_editor.set_plots([])
        x_range=self.trace.getViewBox().viewRange()[0]
        self.revisions['trace']=self.revisions.get('trace',0)+1;self.ready=False;self.error=''
        self.sort_button.setEnabled(False)
        self.cancel_features();self.feature_button.setEnabled(False)
        self.plots_layout.setStretchFactor(self.trace_layout,max(1,len(self.selected_channels)))
        self.trace_layout.setSpacing(12)
        if reset:self.trace_area.verticalScrollBar().setValue(0)
        if reset:self.trace.setXRange(0,max(self.view.duration,1e-9),padding=0)
        if not self.selected_channels:
            self.clear_traces();self.depth.clear();self.depth.hide()
            self.trace.setMinimumHeight(0);self.trace.setMaximumHeight(16777215)
            self.loaded=[];self.loaded_by_channel={};self.ready=self.pending_channel_selection is None and not self.quality_pending
            self.span_editor.set_plots(self.traces)
            self.refresh_spike_sorts()
            self.note.setText(self.view.sources[0].issue or ('No signals selected automatically. Open Channels to review.' if self.auto_channels else 'Choose channels above to show a signal.'));return
        previous=dict(zip(self.plot_keys,self.traces));blank=self.trace if not self.plot_keys else None
        traces=[];created=[]
        for i,channel in enumerate(self.selected_channels):
            trace=previous.pop(channel.key,None)
            if trace is None:
                trace=blank or self.plot();blank=None;created.append(trace)
                trace.setLabel('bottom','Recorded time (s)')
            traces.append(trace);self.trace_layout.insertWidget(i,trace,1);trace.show();trace.height_key=channel.name
            trace.setTitle(f'{channel.name} · {channel.fs/1000:g} kHz',color=theme.MUTED,size='11pt')
            set_voltage_axis(trace,channel)
        for trace in traces:trace.setXLink(None)
        self.depth.setXLink(None);self.trace=traces[0];self.traces=traces
        self.plot_keys=[c.key for c in self.selected_channels]
        for trace,channel in zip(self.traces,self.selected_channels):trace.spike_channel_key=channel.key
        for trace in previous.values():
            trace.setXLink(None);self.trace_layout.removeWidget(trace);trace.close();trace.deleteLater()
        for trace in traces[1:]:trace.setXLink(self.trace)
        self.depth.setXLink(self.trace)
        self.update_depth()
        if reset:self.reset_view()
        else:self.trace.setXRange(*x_range,padding=0)
        for trace in created:trace.enableAutoRange(axis='y');trace.getViewBox().setAutoVisible(y=True)
        self.span_editor.set_plots(self.traces)
        if any(c.pending for c in self.selected_channels):
            self.note.setText(self.trace_loading_message(self.selected_channels));self.trace_timer.start()
        else:self.load_trace()
    def update_depth(self):
        self.depth.clear();x,y=[],[]
        intervals=sorted({(p.start,p.end,p.depth) for c in self.selected_channels for p in c.parts if p.depth is not None})
        for start,end,depth in intervals:x.extend([start,end,np.nan]);y.extend([depth,depth,np.nan])
        self.depth.setVisible(bool(x))
        if x:self.depth.plot(x,y,connect='finite',pen=pg.mkPen('#B57431',width=2));self.depth.enableAutoRange(axis='y')
        for trace in self.traces:trace.showAxis('bottom')
        self.size_trace_panels()
    def restore_good(self):
        self.good_recording.show();self.rating_lock.show();self.lock_rating()
        try:
            self.good_saved=self.flags.is_good(self.view)
        except (OSError,sqlite3.Error,ValueError) as error:
            QMessageBox.warning(self,'Cannot load recording flag',f'Could not read {self.flags.path}:\n{error}');return
        self.good_recording.blockSignals(True);self.good_recording.setChecked(self.good_saved);self.good_recording.blockSignals(False)
    def lock_rating(self):
        self.rating_unlocked=False;self.good_recording.setEnabled(False);self.rating_lock.setText('Unlock rating')
    def toggle_rating_lock(self):
        if self.view is None:return
        if self.rating_unlocked:self.lock_rating()
        else:
            self.rating_unlocked=True;self.good_recording.setEnabled(True);self.rating_lock.setText('Lock rating')
    def mark_good(self,good):
        if self.view is None:return
        if not self.rating_unlocked:
            self.good_recording.blockSignals(True);self.good_recording.setChecked(self.good_saved);self.good_recording.blockSignals(False)
            return
        try:self.flags.set_good(self.view,self.patient,good)
        except (OSError,sqlite3.Error,ValueError) as error:
            self.good_recording.blockSignals(True);self.good_recording.setChecked(self.good_saved);self.good_recording.blockSignals(False)
            self.lock_rating()
            QMessageBox.warning(self,'Recording flag not saved',f'Could not save to {self.flags.path}:\n{error}');return
        self.good_saved=good;self.lock_rating()
        if self.only_good.isChecked() and not good:self.list_patients(self.catalog)
    def reset_view(self):
        if self.view and self.selected_channels:
            start=self.crop_span.start if self.crop_span else 0.
            end=self.crop_span.end if self.crop_span else max(self.view.duration,1/min(c.fs for c in self.selected_channels))
            for trace in self.traces:trace.getViewBox().setLimits(
                xMin=start if self.crop_span else None,xMax=end if self.crop_span else None)
            self.trace.setXRange(start,end,padding=0)
            for trace in self.traces:trace.enableAutoRange(axis='y');trace.getViewBox().setAutoVisible(y=True)
    def trace_loading_message(self,channels):
        parts=[(p.source,p.channel_id) for c in channels for p in c.parts]
        parts.extend((s,c.key[1]) for c in channels for s,_ in c.pending)
        if self.wave_cache and parts and all(self.wave_cache.contains(s,ch) for s,ch in parts):
            return 'Opening cached traces…'
        return 'Preparing traces…'
    def load_trace(self):
        if not self.view or not self.selected_channels:return
        channels=list(self.selected_channels);duration=self.view.duration
        try:
            key=tuple((str(s.path),s.size,s.mtime_ns,s.ctime_ns) for s in self.view.sources)
            for source in self.view.sources:
                stat=source.path.stat()
                if not reader.same_file((stat.st_size,stat.st_mtime_ns),(source.size,source.mtime_ns)):
                    raise ValueError('Recording changed on disk; refresh the patient')
        except (OSError,ValueError) as error:
            self.error=str(error);self.note.setText(self.error);return
        keys=[(key,c.key) for c in channels]
        cached={}
        for channel,cache_key in zip(channels,keys):
            if channel.key in self.loaded_by_channel:cached[cache_key]=self.loaded_by_channel[channel.key]
            elif cache_key in self.trace_cache:
                self.trace_cache.move_to_end(cache_key);cached[cache_key]=self.trace_cache[cache_key]
        for trace,cache_key in zip(self.traces,keys):
            if cache_key in cached:self.draw_trace(trace,cached[cache_key])
        def loaded(pieces):
            resolved={c.key:reader.Channel(c.name,c.fs,[p['part'] for p in data],identity=c.key) for c,data in zip(channels,pieces)}
            self.selected_channels=[resolved[c.key] for c in channels]
            self.available_channels=[resolved.get(c.key,c) for c in self.available_channels]
            self.update_depth()
            self.loaded_by_channel={c.key:p for c,p in zip(channels,pieces)}
            self.draw(pieces)
            for cache_key,channel_pieces in zip(keys,pieces):
                size=sum(p['x'].nbytes+p['y'].nbytes for p in channel_pieces)
                if cache_key in self.trace_cache:self.trace_cache.move_to_end(cache_key)
                elif size<=1_000_000_000:self.trace_cache[cache_key]=channel_pieces;self.trace_cache_bytes+=size
            while self.trace_cache_bytes>1_000_000_000:
                _,old=self.trace_cache.popitem(last=False)
                self.trace_cache_bytes-=sum(p['x'].nbytes+p['y'].nbytes for p in old)
        if len(cached)==len(channels):loaded([cached[k] for k in keys]);return
        self.note.setText(self.trace_loading_message([c for c,k in zip(channels,keys) if k not in cached]))
        wave_cache=self.wave_cache;source_cache=self.source_cache;revision=self.revisions.get('trace',0)+1
        def cancelled():return revision!=self.revisions.get('trace')
        def load():
            missing=[c for c,k in zip(channels,keys) if k not in cached]
            resolved=reader.resolve_channels(missing,source_cache,cancelled)
            fresh={}
            for channel in resolved:
                if cancelled():raise InterruptedError('Channel selection changed')
                fresh[channel.key]=reader.read_channel(channel,0.,duration,wave_cache)
            return [cached[k] if k in cached else fresh[c.key] for c,k in zip(channels,keys)]
        self.submit('trace',load,loaded)
    def draw(self,channel_pieces):
        for trace,pieces in zip(self.traces,channel_pieces):self.draw_trace(trace,pieces)
        self.loaded=[p for pieces in channel_pieces for p in pieces];self.ready=self.pending_channel_selection is None and not self.quality_pending;self.note.setText('No samples in this recording.' if not self.loaded else ('Depth unavailable' if self.depth.isHidden() else ''))
        self.refresh_spike_sorts()
        if self.crop_span:self.reset_view()
    def draw_trace(self,trace,pieces):
        if getattr(trace,'recording_pieces',None) is pieces:return
        if not pieces:trace.setTitle(trace.getPlotItem().titleLabel.text+' · no recorded samples',color=theme.MUTED,size='11pt')
        for item in list(trace.listDataItems()):trace.removeItem(item)
        trace.setClipToView(True);trace.setDownsampling(auto=True,mode='peak')
        for p in pieces:
            item=trace.plot(pen=pg.mkPen(theme.ACCENT,width=1),connect='all',skipFiniteCheck=True)
            item.setData(p['x'],p['y'])
        trace.recording_pieces=pieces
    def spike_channels(self):
        if is_plx(self.view):
            return [c for c in self.selected_channels if c.fs>=reader.MIN_FS and role_for(c,self.channel_roles)=='Spike']
        return [c for c in self.selected_channels if c.fs>=reader.MIN_FS and reader.NEURAL.search(c.name) and not reader.EXCLUDE.search(c.name)]
    def open_recording_from_trace(self,plot):
        if plot not in self.traces or not self.ready:return False
        channel=self.selected_channels[self.traces.index(plot)]
        if channel.key in {c.key for c in self.spike_channels()}:return False
        self.open_recording_window(plot,channel)
        return True
    def open_recording_window(self,plot,channel):
        from recording_window import RecordingWindow
        if self.view is None or not self.ready:return
        pieces=self.loaded_by_channel.get(channel.key)
        if not pieces:return
        existing=self.recording_windows.get(channel.key)
        if existing:
            existing.raise_();existing.activateWindow();return
        window=RecordingWindow(self.view.label,channel,pieces,self.view.duration,plot,self,crop_span=self.crop_span)
        self.recording_windows[channel.key]=window
        def finished(_):
            if self.recording_windows.get(channel.key) is window:self.recording_windows.pop(channel.key,None)
            window.deleteLater()
        window.finished.connect(finished)
        window.show()
    def select_spike_channel(self,plot):
        if plot not in self.traces:return
        key=getattr(plot,'spike_channel_key',None)
        if key in {c.key for c in self.spike_channels()}:
            self.active_spike_channel=key;self.update_sort_button()
    def update_sort_button(self):
        channels=self.spike_channels()
        if self.active_spike_channel not in {c.key for c in channels}:self.active_spike_channel=channels[0].key if channels else None
        active=next((c for c in channels if c.key==self.active_spike_channel),None)
        self.sort_button.setVisible(self.view is not None)
        enabled=bool(active and self.ready and self.span_editor.locked and self.span_editor.span and not self.span_editor.load_error)
        self.sort_button.setEnabled(enabled)
        self.feature_button.setVisible(self.view is not None)
        self.feature_button.setEnabled(enabled and self.active_spike_channel in self.spike_results)
        self.feature_button.setToolTip(f'View features for the saved neuron in {active.name}.'
                                      if self.feature_button.isEnabled() else 'Apply spike sorting for this channel and locked span first.')
        self.sort_button.setToolTip(f'Sort {active.name} within the locked span. Click another spike plot to select it.' if enabled else 'Select a spike channel and lock the span before sorting.')
        for plot,channel in zip(self.traces,self.selected_channels):
            color=theme.ACCENT if channel.key==self.active_spike_channel else theme.MUTED
            plot.setTitle(f'{channel.name} · {channel.fs/1000:g} kHz',color=color,size='11pt')
    def refresh_spike_sorts(self):
        from spike_sort_ui import SpikeOverlay
        self.cancel_features();self.spike_results={};self.update_sort_button()
        for plot in self.traces:
            overlay=getattr(plot,'spike_overlay',None)
            if overlay:overlay.clear()
            plot.spike_sort_applied=False
        if not self.view or not self.ready or not self.span_editor.locked or self.span_editor.span is None:
            self.span_editor.refresh();return
        store=SpikeSortStore(self.flags)
        try:
            for plot,channel in zip(self.traces,self.selected_channels):
                saved=store.load(self.view,channel,self.span_editor.span)
                if saved is None:continue
                self.spike_results[channel.key]=saved
                if not hasattr(plot,'spike_overlay'):plot.spike_overlay=SpikeOverlay(plot)
                plot.spike_overlay.show(self.loaded_by_channel.get(channel.key,[]),saved.spike_times,channel.fs,saved.span)
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError) as error:
            message=f'Could not load saved spike sorting: {error}'
            if message!=self.spike_store_error:self.spike_store_error=message;self.note.setText(message)
        self.span_editor.refresh()
        self.update_sort_button()
    def open_features(self):
        from feature_ui import FeatureDialog
        self.update_sort_button()
        if not self.feature_button.isEnabled():return
        if self.feature_dialog:self.feature_dialog.raise_();return
        channel=next(c for c in self.spike_channels() if c.key==self.active_spike_channel)
        view=self.view;span=self.span_editor.span;pieces=self.loaded_by_channel[channel.key]
        store=FeatureStore(self.flags)
        try:
            token=store.token(view,channel,span)
            cached=store.load(view,channel,span,token)
            saved=SpikeSortStore(self.flags).load(view,channel,span,preview=[])
            if saved is None:raise ValueError('Apply spike sorting before calculating features.')
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as error:
            QMessageBox.warning(self,'Cannot calculate features',str(error));return
        dialog=FeatureDialog(channel,span,self,pieces=pieces,saved_sort=saved);self.feature_dialog=dialog
        dialog.set_hemisphere(self.recording_hemisphere(view))
        def finished(_):
            self.revisions['features']=self.revisions.get('features',0)+1
            if self.feature_dialog is dialog:self.feature_dialog=None
            dialog.deleteLater()
        def complete(result):
            if self.feature_dialog is not dialog or self.view is not view or self.span_editor.span!=span:return
            result.update(recording=view.label,channel=channel.name,sort_token=token)
            try:store.save(view,channel,span,token,result)
            except (OSError,sqlite3.Error,ValueError,TypeError) as error:
                dialog.set_result(result,saved=False);dialog.failed(f'Calculated, but could not save: {error}');return
            dialog.set_result(result)
        dialog.finished.connect(finished);dialog.show()
        if cached is not None:dialog.set_result(cached)
        else:
            revision=self.revisions.get('features',0)+1
            self.submit('features',lambda:calculate_features(pieces,channel.fs,span,saved,
                        lambda:revision!=self.revisions.get('features')),complete)
    def cancel_features(self):
        self.revisions['features']=self.revisions.get('features',0)+1
        if self.feature_dialog:self.feature_dialog.reject();self.feature_dialog=None
    def mark_sorted_good(self,view):
        if self.good_saved:return
        try:self.flags.set_good(view,self.patient,True)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Could not mark recording good',str(error));return
        self.good_saved=True;self.good_recording.blockSignals(True);self.good_recording.setChecked(True);self.good_recording.blockSignals(False)
    def open_spike_sorting(self):
        from spike_sort_ui import SortDialog
        self.update_sort_button()
        if not self.sort_button.isEnabled():return
        if self.sort_dialog:self.sort_dialog.raise_();return
        channel=next(c for c in self.spike_channels() if c.key==self.active_spike_channel)
        view=self.view;span=self.span_editor.span;pieces=self.loaded_by_channel[channel.key]
        try:
            source_signature(view,self.flags,verify=True)
            saved=SpikeSortStore(self.flags).load(view,channel,span,preview=pieces)
            acceptance=UnitAcceptanceStore(self.flags)
            saved_revision=acceptance.sort_revision(view,channel) if saved is not None else None
            saved_review=acceptance.load(view,channel) if saved is not None else None
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Cannot sort spikes',str(error));return
        dialog=SortDialog(channel,span,self);self.sort_dialog=dialog
        def run(filtering=False):
            revision=self.revisions.get('sort',0)+1;threshold_sd=dialog.threshold.value()
            self.submit('sort',lambda:sort_channel(pieces,channel.fs,span,lambda:revision!=self.revisions.get('sort'),filtering=filtering,threshold_sd=threshold_sd),dialog.set_result)
        def apply(result,selected,review):
            if self.view is not view or self.span_editor.span!=span or not self.span_editor.locked:
                dialog.failed('The recording or span changed. Close this preview and sort the current span.');return
            try:
                validate_review(review,int(np.count_nonzero(result.labels==selected)))
                applied=SpikeSortStore(self.flags).save(view,channel,span,result,selected)
                UnitAcceptanceStore(self.flags).save(view,channel,applied,review)
            except (OSError,sqlite3.Error,ValueError,TypeError) as error:
                QMessageBox.warning(dialog,'Spike sorting not saved',str(error));return
            self.mark_sorted_good(view);self.refresh_spike_sorts();self.refresh_sort_progress();self.apply_crop_view();dialog.accept()
        def save_review(review):
            if self.view is not view or self.span_editor.span!=span or saved is None:return
            try:UnitAcceptanceStore(self.flags).save(view,channel,saved,review,expected_revision=saved_revision)
            except (OSError,sqlite3.Error,ValueError,TypeError) as error:
                QMessageBox.warning(dialog,'Unit review not saved',str(error));return
            dialog.set_review(review)
            dialog.status.setText('Single-unit review saved for this cluster.')
        def clear():
            if self.sort_dialog is not dialog or self.view is not view:return
            try:SpikeSortStore(self.flags).clear(view,channel)
            except (OSError,sqlite3.Error,ValueError,TypeError) as error:
                QMessageBox.warning(dialog,'Spike sorting not cleared',str(error));return
            self.refresh_spike_sorts();self.refresh_sort_progress();self.apply_crop_view();dialog.accept()
        def finished(_):
            self.revisions['sort']=self.revisions.get('sort',0)+1
            self.revisions['sort_cardiac']=self.revisions.get('sort_cardiac',0)+1
            if self.sort_dialog is dialog:self.sort_dialog=None
            dialog.deleteLater()
        dialog.rerun.connect(run);dialog.applyRequested.connect(apply);dialog.reviewRequested.connect(save_review)
        dialog.clearRequested.connect(clear);dialog.finished.connect(finished)
        if saved is None:dialog.set_unsorted(pieces)
        else:
            dialog.filtering.blockSignals(True);dialog.filtering.setChecked(bool(saved.parameters.get('filtering',False)));dialog.filtering.blockSignals(False)
            dialog.set_result(saved,saved=True);dialog.set_review(saved_review);dialog.tabs.setCurrentIndex(1)
        dialog.show()
        cardiac_channels=[candidate for candidate in self.available_channels or view.channels
                          if any(label in candidate.name.lower() for label in ('ecg','ekg','pulse','cardiac'))]
        if cardiac_channels:
            cardiac=cardiac_channels[0]
            cache=self.source_cache;wave_cache=self.wave_cache
            def load_cardiac():
                resolved=reader.resolve_channels([cardiac],cache)[0]
                return reader.read_channel(resolved,span.start,span.end,wave_cache)
            def show_cardiac(pieces):
                if self.sort_dialog is dialog and self.view is view:dialog.set_cardiac_trace(cardiac.name,pieces)
            dialog.cardiac_status.setText(f'Loading cardiac reference {cardiac.name}…')
            self.submit('sort_cardiac',load_cardiac,show_cardiac)
    def cancel_spike_sorting(self):
        self.revisions['sort']=self.revisions.get('sort',0)+1
        if self.sort_dialog:self.sort_dialog.reject();self.sort_dialog=None
    def filter(self):
        query=self.search.text().casefold()
        def visit(item,ancestor=''):
            text=ancestor+' '+item.text(0).casefold();children=[visit(item.child(i),text) for i in range(item.childCount())];show=query in text or any(children);item.setHidden(not show);return show
        for i in range(self.tree.topLevelItemCount()):visit(self.tree.topLevelItem(i))
    def choose_folder(self):
        path=QFileDialog.getExistingDirectory(self,'Choose recordings folder',str(self.root))
        if path:self.root=library_paths.open_root(path);self.scan()
    def save_image(self):
        path,_=QFileDialog.getSaveFileName(self,'Save image','recording.png','PNG (*.png)')
        if path:self.centralWidget().grab().save(path)
    def details(self):
        files='\n'.join(str(s.path) for s in self.view.sources) if self.view else ''
        units='\n'.join(f'{c.name}: {voltage_calibration(c).detail}' for c in self.selected_channels)
        box=QMessageBox(self);box.setWindowTitle('Recording details');box.setText('Original MAP, MPX or PLX files. Calibrated voltage axes use µV. Uncalibrated axes require acquisition calibration. File gaps removed; within-file gaps retained. Depth uses the signed filename value in millimeters.');box.setDetailedText(files+'\n\n'+units+'\n\n'+'\n'.join(self.issues));box.exec()
    def closeEvent(self,event):
        if not self.save_neuron_note():event.ignore();return
        if self.crop_dialog:self.crop_dialog.close()
        for window in list(self.recording_windows.values()):window.close()
        self.cancel_features()
        self.cancel_spike_sorting()
        self.trace_timer.stop();self.revisions['trace']=self.revisions.get('trace',0)+1
        self.revisions['quality']=self.revisions.get('quality',0)+1
        self.pool.clear();self.pool.waitForDone()
        if self.flags:self.flags.close()
        super().closeEvent(event)

def default_root():
    if getattr(sys,'frozen',False):
        parent=Path(sys.executable).resolve().parents[3]
        try:return Path((parent/'data-root.txt').read_text().strip())
        except OSError:return parent
    return Path(__file__).resolve().parents[2]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path);parser.add_argument('--smoke-test',type=Path);parser.add_argument('--smoke-patient');parser.add_argument('--smoke-channel',action='append',default=[]);parser.add_argument('--smoke-enlarge',action='store_true',help='Verify double-click inspection of a non-electrode trace');parser.add_argument('--smoke-enlarge-channel',help='Channel name fragment for double-click verification');parser.add_argument('--smoke-notes',action='store_true',help='Verify note save and reload in the configured support database');parser.add_argument('--smoke-crop',action='store_true',help='Verify crop review and JSON export in an isolated support folder');parser.add_argument('--smoke-sort',action='store_true',help='Apply one default sort to a smoke-test library');parser.add_argument('--smoke-features',action='store_true',help='Calculate features of the saved neuron in a smoke-test library');parser.add_argument('--smoke-feature-plot',choices=[m[0] for m in METRICS]);parser.add_argument('--smoke-spectra',action='store_true',help='Open Spectra and save its image');parser.add_argument('--smoke-notch',action='store_true',help='Verify the 60 Hz notch changes a sample spectrum');parser.add_argument('--smoke-spectra-interaction',action='store_true',help='Verify zoom preservation, curve click selection and channel summary');parser.add_argument('--smoke-shift-zoom',action='store_true',help='Verify Shift-wheel changes only x on all three plot types');parser.add_argument('--startup-test',type=Path);parser.add_argument('--launch-start-seconds',type=float);args=parser.parse_args()
    imports_ready=time.perf_counter()
    app=QApplication(sys.argv[:1]);theme.apply(app)
    application_ready=time.perf_counter()
    root=args.root or default_root()
    if not args.root and os.environ.get('VIZ_TEST_SUPPORT'):
        from recording_locator import find_library, save_library, valid_library
        support=Path(os.environ['VIZ_TEST_SUPPORT'])
        root=find_library(default_root(),support)
        if root is None:
            while True:
                chosen=QFileDialog.getExistingDirectory(None,'Choose your raw_UCSF recordings folder',str(Path.home()))
                if not chosen:return
                if valid_library(chosen):
                    root=save_library(support,chosen)
                    break
                QMessageBox.warning(None,'Choose recordings folder',
                                    'Choose the raw_UCSF folder containing patient folders.')
        os.environ['VIZ_TEST_RECORDINGS']=str(root)
        library_paths.TEST_RECORDINGS=Path(root)
    w=Viewer(library_paths.open_root(root));w.show()
    window_ready=time.perf_counter()
    if args.startup_test:
        import cProfile
        startup_profile=cProfile.Profile();startup_profile.enable()
        def startup_check():
            if not w.catalog_loaded and not w.error:return
            startup_timer.stop()
            startup_profile.disable();startup_profile.dump_stats(str(args.startup_test.with_suffix('.prof')))
            args.startup_test.write_text(json.dumps({'patients':len(w.catalog),'ready_seconds':time.perf_counter()-APP_STARTED,'import_seconds':imports_ready-APP_STARTED,'application_seconds':application_ready-imports_ready,'window_seconds':window_ready-application_ready,'catalog_seconds':time.perf_counter()-window_ready,'launch_seconds':time.monotonic()-args.launch_start_seconds if args.launch_start_seconds is not None else None,'error':w.error}))
            w.close();app.exit(1 if w.error else 0)
        startup_timer=QTimer(app,interval=10);startup_timer.timeout.connect(startup_check);startup_timer.start()
        QTimer.singleShot(60000,lambda:app.exit(2))
    if args.smoke_test:
        smoke_channels_applied=False;smoke_sort_applied=False;smoke_enlarge_report=None;smoke_notes_report=None;smoke_notch_report=None;smoke_spectra_report=None;smoke_shift_report=None;smoke_crop_report=None;smoke_crop_stage=0
        def check():
            nonlocal smoke_channels_applied,smoke_sort_applied,smoke_enlarge_report,smoke_notes_report,smoke_notch_report,smoke_spectra_report,smoke_shift_report,smoke_crop_report,smoke_crop_stage
            if w.catalog and not w.patient:
                name=next((p for p in w.catalog if args.smoke_patient and p.startswith(args.smoke_patient)),next(iter(w.catalog)));w.open_patient(name);return
            if not w.ready and not w.error:return
            if w.labels_pending and not w.error:return
            if args.smoke_channel and not smoke_channels_applied and not w.error:
                if not w.channels_complete:
                    if w.channel_error:w.error=w.channel_error
                    else:w.load_available_channels();return
                if not w.error:
                    smoke_channels_applied=True
                    for name in args.smoke_channel:
                        channel=next((c for c in w.available_channels if name.casefold() in c.name.casefold()),None)
                        if channel is None:w.error=f'Channel not found: {name}';break
                        if channel.key not in {c.key for c in w.selected_channels}:w.toggle_channel(channel.key,True)
                    if not w.error:return
            if args.smoke_crop and smoke_crop_stage<3 and not w.error:
                from crop_data import CropStore
                if smoke_crop_stage==0:
                    w.open_crop_data();dialog=w.crop_dialog
                    if dialog is None:w.error='Crop Data did not open'
                    else:
                        for index in range(dialog.recordings.count()):
                            dialog.recordings.setCurrentRow(index)
                            if dialog.sorted and dialog.emg_channel.count() and dialog.ecog.count():break
                        else:w.error='No recording with a valid sort, ECoG and EMG for crop verification'
                        if not w.error:
                            dialog.crop_button.click()
                            dialog.ecog.item(0).setCheckState(Qt.Checked)
                            smoke_crop_stage=1;return
                if smoke_crop_stage==1 and not w.error:
                    dialog=w.crop_dialog
                    if not dialog.emg_plot.listDataItems():return
                    span=dialog.review.crop
                    if span is None:w.error='Sample sort has fewer than 1,000 detected spikes'
                    else:
                        start=span.start+(span.end-span.start)*.5
                        end=span.end+(dialog.view.duration-span.end)*.5
                        dialog.make_region(start,end);dialog.save_regions()
                        dialog.emg_plot.setXRange(0,dialog.view.duration,padding=0)
                        dialog.crop_button.click()
                        emg_zoomed=bool(np.allclose(dialog.emg_plot.getViewBox().viewRange()[0],
                            [span.start,span.end],rtol=0,atol=1e-6))
                        selected_sort=dialog.sorted[dialog.spike_channel.currentData()][1]
                        selected_times=selected_sort.spike_times
                        selected_crop=bool(len(selected_times)>=1000 and
                            np.allclose([span.start,span.end],
                                [selected_times[0],selected_times[999]],rtol=0,atol=1e-9))
                        document=CropStore(w.flags).export_document(w.catalog,w.source_cache)
                        mpx_entries=[entry for entry in document['recordings']
                                     if entry['recording_path'].lower().endswith('.mpx')]
                        spk_exported=bool(mpx_entries and any(entry['spike_channels'] for entry in mpx_entries)
                            and all(entry['spike_channel'] is not None for entry in mpx_entries
                                    if entry['spike_channels']))
                        record=next((entry for entry in document['recordings']
                                     if entry['recording_path']==str(dialog.view.sources[0].path.resolve())),None)
                        cropped=(record and record['status']=='crop_ready' and
                                 record['selected_cluster_spikes']==len(selected_times) and
                                 record['selected_cluster']==selected_sort.selected+1)
                        clipped=bool(record and record['emg_tremor_segments'] and
                                     np.isclose(record['emg_tremor_segments'][0]['intervals_seconds'][0][1],span.end))
                        good=bool(record and record['good_ecog_channels'])
                        shared=any(entry['recording_path']!=record['recording_path'] and
                                   entry['patient']==dialog.patient and entry['good_ecog_channels']
                                   for entry in document['recordings']) if record else False
                        smoke_crop_report={'recording':dialog.view.label,'crop_saved':bool(cropped),
                            'tremor_clipped_to_crop':clipped,'good_ecog_saved':good,
                            'good_ecog_shared_in_export':shared,'crop_button_zooms_emg':emg_zoomed,
                            'mpx_spk_exported':spk_exported,'selected_cluster_crop':selected_crop,
                            'exported_recordings':len(document['recordings'])}
                        if not all((cropped,clipped,good,shared,emg_zoomed,spk_exported,selected_crop)):
                            w.error=f'Crop review verification failed: {smoke_crop_report}'
                        args.smoke_test.with_name(args.smoke_test.stem+'-good_channels.json').write_text(
                            json.dumps(document,indent=2,ensure_ascii=False)+'\n')
                        dialog.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-crop.png')))
                        if w.view is not dialog.view:w.select_view(dialog.view)
                        smoke_crop_stage=2;return
                if smoke_crop_stage==2 and not w.error:
                    span=w.crop_span;visible=w.trace.getViewBox().viewRange()[0]
                    shown=bool(span and np.allclose(visible,[span.start,span.end],rtol=0,atol=1e-6))
                    smoke_crop_report['main_view_cropped']=shown
                    dialog=w.crop_dialog
                    dialog_index=dialog.recordings.currentRow()
                    other=next((i for i in range(dialog.recordings.count()) if i!=dialog_index),None)
                    if other is not None:
                        dialog.recordings.setCurrentRow(other)
                        propagated=dialog.ecog.count()>0 and dialog.ecog.item(0).checkState()==Qt.Checked
                        dialog.recordings.setCurrentRow(dialog_index)
                    else:propagated=False
                    smoke_crop_report['good_ecog_shared_in_ui']=propagated
                    if not shown or not propagated:w.error=f'Main recording crop verification failed: {smoke_crop_report}'
                    if not w.error:
                        marked_key=dialog.emg_channel.currentData()
                        expected=list(dialog.review.tremors.get(marked_key,[]))
                        dialog.close();app.processEvents()
                        w.open_crop_data();reopened=w.crop_dialog
                        if reopened is not None:
                            reopened.recordings.setCurrentRow(dialog_index)
                        restored=bool(reopened and expected and
                            reopened.emg_channel.currentData()==marked_key and
                            len(reopened.regions)==len(expected) and
                            all(np.allclose(region.getRegion(),interval,rtol=0,atol=1e-6)
                                for region,interval in zip(reopened.regions,expected)))
                        smoke_crop_report['tremors_restored_after_reopen']=restored
                        if not restored:w.error=f'Saved tremor regions were not restored: {smoke_crop_report}'
                    smoke_crop_stage=3
            if args.smoke_shift_zoom and smoke_shift_report is None and not w.error:
                from PySide6.QtCore import QPoint, QPointF
                from PySide6.QtGui import QWheelEvent
                def wheel(plot, angle, pixel):
                    viewport=plot.viewport();position=viewport.rect().center()
                    event=QWheelEvent(QPointF(position),QPointF(viewport.mapToGlobal(position)),
                        pixel,angle,Qt.NoButton,Qt.ShiftModifier,Qt.NoScrollPhase,False)
                    app.sendEvent(viewport,event);app.processEvents()
                    return event.isAccepted()
                def smaller(before,after):return after[0][1]-after[0][0]<before[0][1]-before[0][0]
                def same_y(before,after):return bool(np.allclose(before[1],after[1],rtol=0,atol=1e-6))
                main=w.traces[0];main.setXRange(10,20,padding=0)
                before_main=main.getViewBox().viewRange()
                main_ys=[plot.getViewBox().viewRange()[1] for plot in w.traces]
                main_accepted=wheel(main,QPoint(0,120),QPoint())
                after_main=main.getViewBox().viewRange()
                main_ok=(main_accepted and smaller(before_main,after_main) and same_y(before_main,after_main)
                         and all(np.allclose(y,plot.getViewBox().viewRange()[1],rtol=0,atol=1e-6)
                                 for y,plot in zip(main_ys,w.traces)))
                w.open_spectra();app.processEvents()
                spectra=w.spectrum_dialog.plot;spectra.setXRange(30,90,padding=0);spectra.setYRange(-20,40,padding=0)
                before_spectra=spectra.getViewBox().viewRange()
                spectra_accepted=wheel(spectra,QPoint(120,0),QPoint())
                after_spectra=spectra.getViewBox().viewRange()
                spectra_ok=spectra_accepted and smaller(before_spectra,after_spectra) and same_y(before_spectra,after_spectra)
                channel=next((c for c in w.selected_channels if c.key not in {s.key for s in w.spike_channels()}),None)
                if channel is None:w.error='No non-electrode channel for enlarged Shift-zoom verification'
                else:
                    w.open_recording_window(w.traces[w.selected_channels.index(channel)],channel)
                    enlarged=w.recording_windows[channel.key];app.processEvents()
                    enlarged.plot.setXRange(10,20,padding=0)
                    before_enlarged=enlarged.plot.getViewBox().viewRange()
                    enlarged_accepted=wheel(enlarged.plot,QPoint(),QPoint(0,100))
                    after_enlarged=enlarged.plot.getViewBox().viewRange()
                    enlarged_ok=(enlarged_accepted and smaller(before_enlarged,after_enlarged)
                                 and same_y(before_enlarged,after_enlarged))
                    smoke_shift_report={'main_x_only':main_ok,'all_main_voltage_ranges_held':main_ok,
                        'spectra_x_only':spectra_ok,'enlarged_x_only':enlarged_ok,
                        'mouse_wheel_angle_used_in_main':main_accepted,
                        'shift_translated_horizontal_wheel_used_in_spectra':spectra_accepted,
                        'trackpad_pixel_delta_used_in_enlarged':enlarged_accepted}
                    if not all((main_ok,spectra_ok,enlarged_ok)):
                        w.error=f'Shift-wheel zoom verification failed: {smoke_shift_report}'
                    w.spectrum_dialog.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-spectra.png')))
                    enlarged.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-enlarged.png')))
            if args.smoke_notes and smoke_notes_report is None and not w.error:
                original=w.notes_edit.toPlainText();marker='Viz Test note save/reload verification'
                w.notes_edit.setPlainText(marker)
                enabled=w.notes_save.isEnabled()
                w.notes_save.click()
                stored=NeuronNotes(w.flags).load(w.view)==marker
                w.load_neuron_note()
                reloaded=w.notes_edit.toPlainText()==marker and not w.notes_save.isEnabled()
                w.notes_edit.setPlainText(original);w.notes_save.click()
                restored=NeuronNotes(w.flags).load(w.view)==original
                smoke_notes_report={'save_enabled_after_edit':enabled,'stored':stored,'reloaded':reloaded,'restored_prior_text':restored}
                if not all(smoke_notes_report.values()):w.error=f'Neuron note verification failed: {smoke_notes_report}'
            if args.smoke_enlarge and smoke_enlarge_report is None and not w.error:
                from PySide6.QtTest import QTest
                wanted=args.smoke_enlarge_channel
                channel=next((c for c in w.selected_channels if wanted.casefold() in c.name.casefold()),None) if wanted else next((c for c in w.selected_channels if any(kind in c.name.upper() for kind in ('ECOG','EMG','LFP'))),None)
                if channel is None:w.error='No ECoG, EMG or LFP trace available for double-click test'
                else:
                    plot=w.traces[w.selected_channels.index(channel)]
                    source_range=plot.getViewBox().viewRange()
                    source_scale=plot.getAxis('left').scale
                    point=plot.mapFromScene(plot.getViewBox().sceneBoundingRect().center())
                    QTest.mouseClick(plot.viewport(),Qt.LeftButton,pos=point)
                    QTest.mouseDClick(plot.viewport(),Qt.LeftButton,pos=point)
                    app.processEvents()
                    enlarged=w.recording_windows.get(channel.key)
                    if enlarged is None:w.error='Double-click did not open the recording window'
                    else:
                        enlarged_range=enlarged.plot.getViewBox().viewRange()
                        same_range=all(np.allclose(a,b,rtol=1e-6,atol=1e-6) for a,b in zip(source_range,enlarged_range))
                        same_scale=np.isclose(source_scale,enlarged.plot.getAxis('left').scale)
                        enlarged.plot.setXRange(source_range[0][0],(source_range[0][0]+source_range[0][1])/2,padding=0)
                        enlarged.restore_button.click()
                        restored=all(np.allclose(a,b,rtol=1e-6,atol=1e-6) for a,b in zip(source_range,enlarged.plot.getViewBox().viewRange()))
                        enlarged.full_button.click()
                        full_range=enlarged.plot.getViewBox().viewRange()[0]
                        full_recording=np.allclose(full_range,[0,w.view.duration],rtol=1e-6,atol=1e-6)
                        enlarged.restore_button.click()
                        enlarged.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-enlarged.png')))
                        electrode=next((c for c in w.spike_channels() if c.key!=channel.key),None)
                        electrode_unchanged=None
                        if electrode:
                            electrode_plot=w.traces[w.selected_channels.index(electrode)]
                            electrode_point=electrode_plot.mapFromScene(electrode_plot.getViewBox().sceneBoundingRect().center())
                            count=len(w.recording_windows)
                            sort_enabled=w.sort_button.isEnabled()
                            QTest.mouseDClick(electrode_plot.viewport(),Qt.LeftButton,pos=electrode_point)
                            app.processEvents()
                            electrode_unchanged=(len(w.recording_windows)==count and w.active_spike_channel==electrode.key and w.sort_button.isEnabled()==sort_enabled)
                        smoke_enlarge_report={'channel':channel.name,'opened':True,'same_time_and_voltage_range':same_range,'same_voltage_axis_scale':bool(same_scale),'restore_view_works':restored,'full_recording_works':bool(full_recording),'electrode_double_click_keeps_sort_selection':electrode_unchanged}
                        if not all((same_range,same_scale,restored,full_recording)) or electrode_unchanged is False:w.error=f'Enlarged recording verification failed: {smoke_enlarge_report}'
            if args.smoke_sort and not smoke_sort_applied and not w.error:
                if w.sort_dialog is None:
                    w.open_spike_sorting()
                    if w.sort_dialog is None:w.error='No eligible spike channel for smoke sorting'
                    else:return
                if w.sort_dialog is not None:
                    dialog=w.sort_dialog
                    if dialog.error:w.error=dialog.error
                    elif dialog.unsorted:dialog.apply_button.click();return
                    elif dialog.result is None:return
                    elif not len(dialog.result.times):w.error='No spikes detected in the smoke-test recording'
                    else:
                        dialog.apply_button.click();smoke_sort_applied=True
                        if not w.spike_results:w.error='Smoke sorting did not save a result'
                        else:return
            if (args.smoke_features or args.smoke_feature_plot) and not w.error:
                if w.feature_dialog is None:
                    if not w.feature_button.isEnabled():w.error='No saved neuron for smoke feature calculation'
                    else:w.feature_button.click();return
                if w.feature_dialog is not None:
                    if w.feature_dialog.error:w.error=w.feature_dialog.error
                    elif w.feature_dialog.result is None:return
                if args.smoke_feature_plot and not w.error:
                    if w.feature_dialog.plot_dialog is None:
                        row=next(i for i,m in enumerate(METRICS) if m[0]==args.smoke_feature_plot)
                        w.feature_dialog.open_plot(row,2);return
                    plot=w.feature_dialog.plot_dialog
                    if plot.error:w.error=plot.error
                    elif plot.data is None:return
            if (args.smoke_spectra or args.smoke_notch or args.smoke_spectra_interaction) and not w.error:
                if w.spectrum_dialog is None:w.open_spectra();return
                if args.smoke_spectra_interaction and smoke_spectra_report is None:
                    from PySide6.QtCore import QPointF
                    from PySide6.QtTest import QTest
                    dialog=w.spectrum_dialog
                    items=[dialog.list.item(i) for i in range(dialog.list.count())
                           if dialog.list.item(i).checkState()==Qt.Checked and
                           w.channel_quality.get(dialog.list.item(i).data(Qt.UserRole)) and
                           w.channel_quality[dialog.list.item(i).data(Qt.UserRole)].spectrum is not None]
                    if len(items)<2:w.error='Need two plotted spectra for interaction verification'
                    else:
                        original_count=len(dialog.checked_keys())
                        summary_ok=(dialog.selection_summary.text().startswith(f'{original_count} channels selected:')
                                    and items[0].data(Qt.UserRole)[0] in dialog.selection_summary.text())
                        dialog.plot.setXRange(45,75,padding=0);dialog.plot.setYRange(-20,20,padding=0)
                        before=dialog.plot.getViewBox().viewRange()
                        dialog.list.setCurrentItem(items[1]);app.processEvents()
                        after=dialog.plot.getViewBox().viewRange()
                        zoom_ok=all(np.allclose(a,b,rtol=0,atol=1e-6) for a,b in zip(before,after))
                        dialog.check_all(False);items[0].setCheckState(Qt.Checked)
                        dialog.list.setCurrentItem(items[1]);app.processEvents()
                        curve=dialog.plot.listDataItems()[0];x,y=curve.getData()
                        index=int(np.argmin(np.abs(x-60)))
                        dialog.plot.setXRange(50,70,padding=0)
                        dialog.plot.setYRange(float(y[index]-10),float(y[index]+10),padding=0)
                        app.processEvents()
                        point=dialog.plot.mapFromScene(dialog.plot.getViewBox().mapViewToScene(
                            QPointF(float(x[index]),float(y[index]))))
                        before_click=dialog.plot.getViewBox().viewRange()
                        QTest.mouseClick(dialog.plot.viewport(),Qt.LeftButton,pos=point);app.processEvents()
                        click_ok=(dialog.list.currentItem() is items[0] and
                                  items[0].data(Qt.UserRole)[0] in dialog.highlight_label.text())
                        click_zoom_ok=all(np.allclose(a,b,rtol=0,atol=1e-6) for a,b in zip(
                            before_click,dialog.plot.getViewBox().viewRange()))
                        one_selected=(dialog.selection_summary.text().startswith('1 channel selected:') and
                                      items[0].data(Qt.UserRole)[0] in dialog.selection_summary.text())
                        smoke_spectra_report={'zoom_preserved_on_list_selection':zoom_ok,
                            'summary_count_and_names':summary_ok,'curve_click_highlights_channel':click_ok,
                            'zoom_preserved_on_curve_click':click_zoom_ok,
                            'summary_updates_with_selection':one_selected,
                            'clicked_channel':items[0].data(Qt.UserRole)[0]}
                        if not all((zoom_ok,summary_ok,click_ok,click_zoom_ok,one_selected)):
                            w.error=f'Spectra interaction verification failed: {smoke_spectra_report}'
                if args.smoke_notch and smoke_notch_report is None:
                    dialog=w.spectrum_dialog
                    if not dialog.notch.isChecked():dialog.notch.setChecked(True);return
                    if dialog.notch_pending:return
                    key=next((key for key in dialog.checked_keys() if key in dialog.notched and dialog.notched[key] is not None),None)
                    if key is None:w.error=f'No notched spectrum available: {dialog.notch_errors}'
                    else:
                        original=w.channel_quality[key].spectrum;filtered=dialog.notched[key]
                        ratio=float(np.interp(60.,filtered[0],filtered[1])/np.interp(60.,original[0],original[1]))
                        baseline=float(np.interp(30.,filtered[0],filtered[1])/np.interp(30.,original[0],original[1]))
                        smoke_notch_report={'channel':next(c.name for c in dialog.channels if c.key==key),
                            'notch_checked':dialog.notch.isChecked(),'power_ratio_at_60_hz':ratio,
                            'power_ratio_at_30_hz':baseline,'notch_reduces_60_hz_power':ratio<.8,
                            '30_hz_largely_preserved':.8<baseline<1.2}
                        if not smoke_notch_report['notch_reduces_60_hz_power'] or not smoke_notch_report['30_hz_largely_preserved']:
                            w.error=f'Notch verification failed: {smoke_notch_report}'
                w.spectrum_dialog.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-spectra.png')))
            timer.stop();report={'version':VERSION,'frozen':bool(getattr(sys,'frozen',False)),'direct_raw_reader':True,'patients':len(w.catalog),'views':len(w.views),'selected':w.view.label if w.view else None,'source_files':len(w.view.sources) if w.view else 0,'source_formats':sorted({s.path.suffix for s in w.view.sources}) if w.view else [],'duration_seconds':w.view.duration if w.view else 0,'plotted_pieces':len(w.loaded),'electrode_plots':len(w.selected_channels),'channel_names':[c.name for c in w.selected_channels],'x_ranges':[p.getViewBox().viewRange()[0] for p in w.traces],'depth_visible':bool(w.depth and w.depth.isVisible()),'error':w.error,'issues':w.issues}
            span=w.span_editor.span
            report.update(plot_heights=[p.height() for p in w.traces],scroll_maximum=w.trace_area.verticalScrollBar().maximum(),
                          voltage_axes=[{'label':p.getAxis('left').labelText,'unit':p.getAxis('left').labelUnits,'scale':p.getAxis('left').scale} for p in w.traces],
                          automatic_channels=w.auto_channels,channel_check_error=w.channel_error,
                          channel_screening={str(key):{'selected':quality.selected,'label':quality.label,'verdict':quality.verdict} for key,quality in w.channel_quality.items()},
                          channel_label_error=w.label_error,spectra_curves=len(w.spectrum_dialog.plot.listDataItems()) if w.spectrum_dialog else None)
            report.update(span_locked=w.span_editor.locked,span_regions=len(w.span_editor.regions),span={'start':span.start,'end':span.end} if span else None)
            report.update(sorting_total=w.sort_total.text(),sorted_recordings=len(w.sort_progress),patient_rows=[w.tree.topLevelItem(i).text(0) for i in range(w.tree.topLevelItemCount())],sorted_channels=len(w.spike_results),sorted_spikes=sum(len(r.spike_times) for r in w.spike_results.values()),all_detected_spikes=sum(len(r.times) for r in w.spike_results.values()),enlarged_recording=smoke_enlarge_report,neuron_notes=smoke_notes_report,notch=smoke_notch_report,spectra_interaction=smoke_spectra_report,shift_zoom=smoke_shift_report,crop_review=smoke_crop_report)
            report['feature_button_enabled']=w.feature_button.isEnabled()
            report['hemisphere']=hemispheres.export_fields(w.recording_hemisphere(w.view)) if w.view else None
            parent=next((w.tree.topLevelItem(i) for i in range(w.tree.topLevelItemCount()) if w.tree.topLevelItem(i).data(0,Qt.UserRole)==('patient',w.patient)),None)
            report['recording_rows']=[parent.child(j).text(0) for j in range(parent.childCount())] if parent else []
            if w.feature_dialog and w.feature_dialog.result:
                report['features']=w.feature_dialog.result
                w.feature_dialog.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-features.png')))
                if w.feature_dialog.plot_dialog:
                    report['feature_plot']=w.feature_dialog.plot_dialog.key
                    report['feature_plot_ready']=w.feature_dialog.plot_dialog.data is not None
                    w.feature_dialog.plot_dialog.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem+'-plot.png')))
            args.smoke_test.write_text(json.dumps(report,indent=2));w.grab().save(str(args.smoke_test.with_suffix('.png')));w.close();app.exit(1 if w.error else 0)
        timer=QTimer(app,interval=200);timer.timeout.connect(check);timer.start();QTimer.singleShot(240000,lambda:app.exit(2))
    sys.exit(app.exec())
if __name__=='__main__':main()
