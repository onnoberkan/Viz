"""Participant review window for spike crops, EMG tremor spans and ECoG choices."""

import json
from pathlib import Path
import sqlite3

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget)

import alpha_reader as reader
from crop_data import CropStore, use_selected_cluster
from channel_roles import ChannelRoleStore, role_for
from recording_spans import SpanStore
from spike_sorting import SpikeSortStore, channel_identity
from unit_acceptance import UnitAcceptanceStore


class CropDialog(QDialog):
    def __init__(self, viewer, views):
        super().__init__(viewer)
        self.viewer=viewer;self.flags=viewer.flags;self.store=CropStore(self.flags)
        self.patient=viewer.patient;self.views=list(views)
        self.view=None;self.review=None;self.channels=[];self.sorted={};self.roles={};self.regions=[];self.emg_revision=0
        self.setWindowTitle(f'Crop Data · {self.patient}')
        self.resize(1180,760)
        layout=QVBoxLayout(self)
        intro=QLabel('Review each recording. Choose its saved spike channel, mark EMG tremor segments, and check good ECoG channels. Changes save to Viz Test automatically.')
        intro.setWordWrap(True);layout.addWidget(intro)
        split=QSplitter(Qt.Horizontal);layout.addWidget(split,1)
        left=QWidget();ll=QVBoxLayout(left);ll.addWidget(QLabel('Recordings'))
        self.recordings=QListWidget();ll.addWidget(self.recordings,1);split.addWidget(left)
        right=QWidget();rl=QVBoxLayout(right);split.addWidget(right);split.setSizes([290,890])
        crop_row=QHBoxLayout();crop_row.addWidget(QLabel('Spike channel'))
        self.spike_channel=QComboBox();crop_row.addWidget(self.spike_channel,1)
        self.crop_button=QPushButton('Crop to first 1,000 spikes in selected cluster');crop_row.addWidget(self.crop_button)
        rl.addLayout(crop_row)
        self.crop_status=QLabel();self.crop_status.setWordWrap(True);rl.addWidget(self.crop_status)
        rl.addWidget(QLabel('Good ECoG channels for this patient (shared across recordings)'))
        self.ecog=QListWidget();self.ecog.setMaximumHeight(145);rl.addWidget(self.ecog)
        emg_row=QHBoxLayout();emg_row.addWidget(QLabel('EMG channel'))
        self.emg_channel=QComboBox();emg_row.addWidget(self.emg_channel,1)
        self.add_tremor=QPushButton('Add tremor segment');emg_row.addWidget(self.add_tremor)
        rl.addLayout(emg_row)
        self.emg_plot=pg.PlotWidget(background='w');self.emg_plot.showGrid(x=True,y=True,alpha=.1)
        self.emg_plot.setLabel('bottom','Recorded time (s)');self.emg_plot.setLabel('left','EMG (native counts)')
        self.emg_plot.setDownsampling(auto=True,mode='peak');self.emg_plot.setClipToView(True)
        rl.addWidget(self.emg_plot,1)
        segments_row=QHBoxLayout();segments_row.addWidget(QLabel('Tremor segments (seconds)'),1)
        self.remove_tremor=QPushButton('Remove selected segment');segments_row.addWidget(self.remove_tremor)
        rl.addLayout(segments_row)
        self.segments=QListWidget();self.segments.setMaximumHeight(95);rl.addWidget(self.segments)
        bottom=QHBoxLayout();self.message=QLabel();self.message.setWordWrap(True);bottom.addWidget(self.message,1)
        self.export_button=QPushButton('Export good_channels.json');bottom.addWidget(self.export_button)
        rl.addLayout(bottom)
        for view in self.views:self.recordings.addItem(view.label)
        self.recordings.currentRowChanged.connect(self.select_recording)
        self.spike_channel.currentIndexChanged.connect(self.choose_spike_channel)
        self.crop_button.clicked.connect(self.compute_crop)
        self.ecog.itemChanged.connect(self.mark_ecog)
        self.emg_channel.currentIndexChanged.connect(self.show_emg)
        self.add_tremor.clicked.connect(self.add_segment)
        self.remove_tremor.clicked.connect(self.remove_segment)
        self.export_button.clicked.connect(self.export_json)
        if self.views:self.recordings.setCurrentRow(0)

    def save(self):
        if self.view is None:return False
        try:self.store.save(self.view,self.patient,self.review)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Crop review not saved',str(error));return False
        self.message.setText('Saved for this recording.')
        if self.viewer.view and self.flags.identity(self.viewer.view)[0]==self.flags.identity(self.view)[0]:
            self.viewer.apply_crop_view()
        return True

    def select_recording(self,index):
        if index<0 or index>=len(self.views):return
        self.emg_revision+=1
        self.view=self.views[index]
        try:
            self.review=self.store.load(self.view)
            self.good_selectors=self.store.load_good_ecog(self.patient)
            self.channels=reader.channel_options(self.view,self.viewer.source_cache)
            self.roles=ChannelRoleStore(self.flags).load(self.view)
            span=SpanStore(self.flags).load(self.view)
            sorts=SpikeSortStore(self.flags)
            self.spike_candidates=[c for c in self.channels if role_for(c,self.roles)=='Spike']
            self.sorted={channel_identity(c):(c,saved) for c in self.spike_candidates
                         if c.fs>=reader.MIN_FS
                         if (saved:=sorts.load(self.view,c,span)) is not None}
            self.spike_candidates.sort(key=lambda c:channel_identity(c) not in self.sorted)
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as error:
            QMessageBox.warning(self,'Cannot load crop review',str(error));return
        self.spike_channel.blockSignals(True);self.spike_channel.clear()
        for channel in self.spike_candidates:
            key=channel_identity(channel)
            saved=self.sorted.get(key)
            self.spike_channel.addItem(
                f'{channel.name} · Cluster {saved[1].selected+1}: {len(saved[1].spike_times):,} selected / {len(saved[1].times):,} detected'
                if saved else f'{channel.name} · sort needed',key)
        selected=self.spike_channel.findData(self.review.spike_channel)
        if selected>=0:self.spike_channel.setCurrentIndex(selected)
        self.spike_channel.blockSignals(False)
        self.crop_button.setEnabled(self.spike_channel.currentData() in self.sorted)
        self.update_crop_status()
        self.ecog.blockSignals(True);self.ecog.clear()
        for channel in self.channels:
            if role_for(channel,self.roles)!='ECoG':continue
            item=QListWidgetItem(channel.name);item.setData(Qt.UserRole,channel_identity(channel))
            item.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if self.flags.channel_selector(channel.key) in self.good_selectors else Qt.Unchecked)
            self.ecog.addItem(item)
        self.ecog.blockSignals(False)
        self.emg_channel.blockSignals(True);self.emg_channel.clear()
        for channel in self.channels:
            if role_for(channel,self.roles)=='EMG':
                self.emg_channel.addItem(self.emg_label(channel),channel_identity(channel))
        marked=next((i for i in range(self.emg_channel.count())
                     if self.review.tremors.get(self.emg_channel.itemData(i))),-1)
        if marked>=0:self.emg_channel.setCurrentIndex(marked)
        self.emg_channel.blockSignals(False)
        self.show_emg()

    def emg_label(self,channel):
        count=len(self.review.tremors.get(channel_identity(channel),[]))
        return f'{channel.name} · {count} saved' if count else channel.name

    def choose_spike_channel(self):
        if self.review is None:return
        key=self.spike_channel.currentData()
        if key==self.review.spike_channel:return
        self.review.spike_channel=key;self.review.crop=None;self.review.detected_spikes=0
        self.review.selected_cluster=None;self.review.selected_cluster_spikes=None
        self.review.crop_basis='selected_cluster'
        self.review.sort_updated_at=None
        self.save();self.update_crop_status()

    def update_crop_status(self):
        if self.view is None:return
        key=self.spike_channel.currentData()
        self.crop_button.setEnabled(key in self.sorted)
        if not self.sorted:self.crop_status.setText('No compatible saved spike sort for this recording. Sort spikes first (or repeat an older sort if the source file changed), then reopen Crop Data.');return
        if key not in self.sorted:self.crop_status.setText('This labeled spike channel needs a compatible saved sort before cropping.');return
        channel=self.sorted[key][0] if key in self.sorted else None
        review=UnitAcceptanceStore(self.flags).load(self.view,channel) if channel is not None else None
        decision=review.decision.replace('_',' ') if review is not None else 'needs review'
        if self.review.crop and self.store.effective_crop(self.view,self.review):
            self.crop_status.setText(f'First 1,000 spikes in selected cluster {self.review.selected_cluster+1}: {self.review.crop.start:.6f}–{self.review.crop.end:.6f} s. Recording views are restricted to this interval. Single-unit review: {decision}.')
        elif self.review.crop_basis!='selected_cluster':
            self.crop_status.setText(f'This earlier crop used all detected spikes and could not be updated automatically. Choose the saved spike channel and crop again. Single-unit review: {decision}.')
        elif self.review.selected_cluster_spikes is not None and self.review.selected_cluster_spikes<1000:
            self.crop_status.setText(f'Cluster {self.review.selected_cluster+1}: {self.review.selected_cluster_spikes:,} spikes. Fewer than 1,000; this recording remains uncropped. Single-unit review: {decision}.')
        else:self.crop_status.setText(f'Choose a saved spike channel, then calculate its first 1,000 spikes in the selected cluster. Single-unit review: {decision}.')

    def compute_crop(self):
        key=self.spike_channel.currentData()
        if key not in self.sorted:return
        channel,saved=self.sorted[key]
        self.review.spike_channel=key
        use_selected_cluster(self.review,saved,self.store.sort_revision(self.view,channel))
        if self.save():
            self.update_crop_status()
            self.zoom_emg_to_crop()
            if self.viewer.view is None or self.flags.identity(self.viewer.view)[0]!=self.flags.identity(self.view)[0]:
                self.viewer.select_view(self.view)
            else:self.viewer.apply_crop_view(force=True)

    def zoom_emg_to_crop(self):
        span=self.store.effective_crop(self.view,self.review)
        if span:self.emg_plot.setXRange(span.start,span.end,padding=0)
        else:self.emg_plot.setXRange(0,max(self.view.duration,1e-6),padding=0)

    def mark_ecog(self,_item):
        if self.view is None:return
        visible={self.flags.channel_selector(tuple(json.loads(self.ecog.item(i).data(Qt.UserRole))))
                 for i in range(self.ecog.count())}
        checked={self.flags.channel_selector(tuple(json.loads(self.ecog.item(i).data(Qt.UserRole))))
                 for i in range(self.ecog.count()) if self.ecog.item(i).checkState()==Qt.Checked}
        selectors=(self.good_selectors-visible)|checked
        try:self.store.save_good_ecog(self.patient,selectors)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Good ECoG channels not saved',str(error));return
        self.good_selectors=selectors
        self.message.setText('Good ECoG channels saved for this patient.')

    def show_emg(self):
        self.emg_revision+=1;revision=self.emg_revision
        for region in self.regions:region.blockSignals(True)
        self.emg_plot.clear();self.regions.clear();self.segments.clear()
        key=self.emg_channel.currentData()
        self.add_tremor.setEnabled(bool(key));self.remove_tremor.setEnabled(bool(key))
        if not key or self.view is None:return
        channel=next((c for c in self.channels if channel_identity(c)==key),None)
        if channel is None:return
        for start,end in self.review.tremors.get(key,[]):self.make_region(start,end)
        self.zoom_emg_to_crop()
        self.message.setText('Loading EMG trace…')
        cache=self.viewer.source_cache;wave_cache=self.viewer.wave_cache;view=self.view
        def load():
            resolved=reader.resolve_channels([channel],cache,lambda:revision!=self.emg_revision)[0]
            return reader.read_channel(resolved,0.,view.duration,wave_cache)
        def loaded(pieces):
            if revision!=self.emg_revision or self.view is not view:return
            for piece in pieces:self.emg_plot.plot(piece['x'],piece['y'],pen=pg.mkPen('#4A6FA5',width=1))
            self.zoom_emg_to_crop()
            count=len(self.review.tremors.get(key,[]))
            self.message.setText(f'{count} saved tremor segment(s) on {channel.name}.')
        self.viewer.submit('crop_emg',load,loaded)

    def make_region(self,start,end):
        key=self.emg_channel.currentData()
        region=pg.LinearRegionItem((start,end),bounds=(0.,self.view.duration),movable=True)
        region.setZValue(10)
        self.emg_plot.addItem(region);self.regions.append(region)
        item=QListWidgetItem();self.segments.addItem(item)
        region.sigRegionChangeFinished.connect(self.save_regions)
        self.update_segment_labels()

    def update_segment_labels(self):
        for i,region in enumerate(self.regions):
            start,end=region.getRegion();self.segments.item(i).setText(f'{start:.3f}–{end:.3f} s')

    def save_regions(self):
        key=self.emg_channel.currentData()
        if not key or self.review is None:return
        intervals=sorted((list(map(float,region.getRegion())) for region in self.regions),key=lambda pair:pair[0])
        self.review.tremors[key]=[pair for pair in intervals if pair[1]>pair[0]]
        self.update_segment_labels()
        if self.save():
            channel=next((c for c in self.channels if channel_identity(c)==key),None)
            if channel is not None:self.emg_channel.setItemText(self.emg_channel.currentIndex(),self.emg_label(channel))
            self.message.setText(f'{len(self.review.tremors[key])} tremor segment(s) saved for this recording.')

    def add_segment(self):
        if self.view is None or not self.emg_channel.currentData():return
        lo,hi=self.emg_plot.getViewBox().viewRange()[0]
        start=max(0.,min(self.view.duration,lo+(hi-lo)*.4))
        end=max(start+1e-6,min(self.view.duration,lo+(hi-lo)*.6))
        self.make_region(start,end);self.save_regions()

    def remove_segment(self):
        index=self.segments.currentRow()
        if index<0 or index>=len(self.regions):return
        region=self.regions.pop(index);self.emg_plot.removeItem(region)
        self.segments.takeItem(index);self.save_regions()

    def export_json(self):
        default=self.flags.path.parents[3]/'Exports/good_channels.json'
        path,_=QFileDialog.getSaveFileName(self,'Export crop review',str(default),'JSON (*.json)')
        if not path:return
        try:
            document=self.store.export_document(self.viewer.catalog,self.viewer.source_cache)
            target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name(target.name+'.tmp')
            temporary.write_text(json.dumps(document,indent=2,ensure_ascii=False)+'\n')
            temporary.replace(target)
        except (OSError,sqlite3.Error,ValueError,TypeError) as error:
            QMessageBox.warning(self,'Export failed',str(error));return
        self.message.setText(f'Exported {len(document["recordings"])} recordings to {target}')
