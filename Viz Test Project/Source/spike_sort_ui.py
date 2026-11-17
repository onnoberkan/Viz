import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox, QToolButton, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QScrollArea, QTabWidget, QWidget
import pyqtgraph as pg
from signal_units import set_voltage_axis
from unit_acceptance import UnitReview, isi_summary

SPIKE_COLOR = '#CE514A'


def colored_snippets(pieces, times, fs, span, low=-np.inf, high=np.inf):
    before,after=max(1,round(.0005*fs)),max(1,round(.001*fs))
    xs=[];ys=[]
    for piece in pieces:
        x=piece['x'];y=piece['y']
        if not len(x):continue
        start=max(span.start,float(x[0]),low-after/fs)
        end=min(span.end,float(x[-1])+1/fs,high+before/fs)
        selected=times[(times>=start)&(times<end)]
        if not len(selected):continue
        peaks=np.searchsorted(x,selected)
        peaks=np.clip(peaks,0,len(x)-1)
        peaks=peaks[np.abs(x[peaks]-selected)<.25/fs]
        if not len(peaks):continue
        offsets=np.arange(-before,after+1)
        indices=peaks[:,None]+offsets
        valid=(indices>=0)&(indices<len(x));clipped=np.clip(indices,0,len(x)-1)
        valid &= (x[clipped]>=span.start)&(x[clipped]<span.end)
        shape=(len(peaks),len(offsets)+1)
        xx=np.full(shape,np.nan);yy=np.full(shape,np.nan)
        xx[:,:-1]=np.where(valid,x[clipped],np.nan)
        yy[:,:-1]=np.where(valid,y[clipped],np.nan)
        xs.append(xx.ravel());ys.append(yy.ravel())
    return (np.concatenate(xs),np.concatenate(ys)) if xs else (np.empty(0),np.empty(0))


class SpikeOverlay:
    def __init__(self, plot):
        self.plot=plot;self.pieces=[];self.times=np.empty(0);self.fs=0;self.span=None
        self.snippets=pg.PlotCurveItem(pen=pg.mkPen(SPIKE_COLOR,width=1.5),connect='finite')
        self.raster=pg.PlotCurveItem(pen=pg.mkPen(SPIKE_COLOR,width=1.3),connect='pairs')
        for item in (self.snippets,self.raster):item.setZValue(30)
        self.attached=False;self._xrange=None
        plot.getViewBox().sigRangeChanged.connect(self.redraw)

    def clear(self):
        if self.attached:
            self.plot.removeItem(self.snippets);self.plot.removeItem(self.raster)
        self.attached=False;self.pieces=[];self.times=np.empty(0);self.span=None;self._xrange=None
        self.plot.spike_sort_applied=False

    def show(self, pieces, times, fs, span):
        self.pieces=pieces;self.times=np.asarray(times);self.fs=fs;self.span=span;self._xrange=None
        if not self.attached:
            for item in (self.snippets,self.raster):self.plot.addItem(item,ignoreBounds=True)
            self.attached=True
        self.plot.spike_sort_applied=True;self.redraw()

    def redraw(self, *args):
        if not self.attached:return
        (low,high),(bottom,top)=self.plot.getViewBox().viewRange()
        visible=self.times[(self.times>=low)&(self.times<=high)]
        x=np.repeat(visible,2)
        y=np.tile([bottom+.90*(top-bottom),bottom+.985*(top-bottom)],len(visible))
        self.raster.setData(x,y,connect='pairs')
        if self._xrange!=(low,high):
            self._xrange=(low,high)
            x,y=colored_snippets(self.pieces,self.times,self.fs,self.span,low,high)
            self.snippets.setData(x,y,connect='finite')


class SortDialog(QDialog):
    rerun = Signal(bool)
    applyRequested = Signal(object,int,object)
    reviewRequested = Signal(object)
    clearRequested = Signal()

    def __init__(self, channel, span, parent=None):
        super().__init__(parent)
        self.channel=channel;self.span=span;self.result=None;self.error='';self.unsorted=True;self.saved_cluster=None;self.persisted_review=None
        self.setWindowTitle(f'Sort spikes · {channel.name}')
        self.setWindowModality(Qt.WindowModal)
        screen=QApplication.primaryScreen().availableGeometry()
        self.resize(min(1100,screen.width()-40),min(850,screen.height()-60))
        layout=QVBoxLayout(self)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        sorting_page=QWidget();sorting_layout=QVBoxLayout(sorting_page)
        self.tabs.addTab(sorting_page,'Sorting')
        review_page=QWidget();review_layout=QVBoxLayout(review_page)
        review_scroll=QScrollArea();review_scroll.setWidgetResizable(True);review_scroll.setWidget(review_page)
        self.tabs.addTab(review_scroll,'Single-unit review · 3 ms ISI')
        row=QHBoxLayout()
        row.addWidget(QLabel(f'{channel.name} · {span.start:.6g}–{span.end:.6g} s'),1)
        row.addWidget(QLabel('Threshold'))
        self.threshold=QDoubleSpinBox();self.threshold.setRange(.1,10.);self.threshold.setDecimals(1)
        self.threshold.setSingleStep(.1);self.threshold.setValue(2.);self.threshold.setSuffix(' SD')
        self.threshold.setKeyboardTracking(False);self.threshold.setMinimumWidth(100)
        self.threshold.setToolTip('Multiples of the signal standard deviation within the span. Lower values include smaller peaks and may include more noise.')
        self.threshold.valueChanged.connect(lambda _: self.request_sort(self.filtering.isChecked()) if not self.unsorted else None)
        row.addWidget(self.threshold)
        self.filtering=QCheckBox('Bandpass filter');self.filtering.setChecked(False)
        self.filtering.setToolTip('Optional 300–3000 Hz filter. Leave off for signals already filtered.')
        self.filtering.toggled.connect(lambda checked: self.request_sort(checked) if not self.unsorted else None);row.addWidget(self.filtering);sorting_layout.addLayout(row)
        self.trace=pg.PlotWidget(background='w');self.trace.setLabel('bottom','Recorded time (s)');set_voltage_axis(self.trace,channel)
        self.trace.setMenuEnabled(False);self.trace.setClipToView(True);self.trace.setDownsampling(auto=True,mode='peak')
        self.overlay=SpikeOverlay(self.trace);self.trace.setMinimumHeight(210);sorting_layout.addWidget(self.trace,2)
        plots=QHBoxLayout()
        self.waveforms=pg.PlotWidget(background='w');self.waveforms.setLabel('bottom','Spike time (ms)');set_voltage_axis(self.waveforms,channel)
        self.pca=pg.PlotWidget(background='w');self.pca.setLabel('bottom','PC1');self.pca.setLabel('left','PC2')
        for plot in (self.waveforms,self.pca):plot.setMenuEnabled(False);plot.setMinimumHeight(145);plots.addWidget(plot)
        sorting_layout.addLayout(plots,1)
        review_plots=QHBoxLayout()
        self.isi_plot=pg.PlotWidget(background='w');self.isi_plot.setLabel('bottom','Interspike interval (ms)')
        self.isi_plot.setLabel('left','Count');self.isi_plot.setMenuEnabled(False)
        self.isi_plot.addItem(pg.LinearRegionItem((0,3),movable=False,brush=pg.mkBrush(220,85,60,55),pen=pg.mkPen(220,85,60)))
        self.stability_plot=pg.PlotWidget(background='w');self.stability_plot.setLabel('bottom','Recorded time (s)')
        self.stability_plot.setLabel('left','Spike peak-to-peak (native counts)');self.stability_plot.setMenuEnabled(False)
        self.cardiac_plot=pg.PlotWidget(background='w');self.cardiac_plot.setLabel('bottom','Recorded time (s)')
        self.cardiac_plot.setLabel('left','Cardiac reference');self.cardiac_plot.setMenuEnabled(False)
        self.cardiac_plot.setXLink(self.stability_plot);self.cardiac_plot.hide()
        temporal=QVBoxLayout();temporal.addWidget(self.stability_plot);temporal.addWidget(self.cardiac_plot)
        self.isi_plot.setMinimumHeight(155);self.stability_plot.setMinimumHeight(155)
        review_plots.addWidget(self.isi_plot);review_plots.addLayout(temporal);review_layout.addLayout(review_plots,1)
        self.isi_summary=QLabel('Select a cluster to review.');review_layout.addWidget(self.isi_summary)
        self.cardiac_status=QLabel('No ECG or pulse channel was loaded. Use an external cardiac reference if available, and document it below.')
        self.cardiac_status.setWordWrap(True);review_layout.addWidget(self.cardiac_status)
        checks=QHBoxLayout()
        self.distinct_waveforms=QCheckBox('One clearly distinguished spike population')
        self.clear_refractory=QCheckBox('Clear refractory period of at least 3 ms')
        checks.addWidget(self.distinct_waveforms);checks.addWidget(self.clear_refractory);review_layout.addLayout(checks)
        cardiac_row=QHBoxLayout();cardiac_row.addWidget(QLabel('Cardiac-cycle shape change'))
        self.cardiac=QComboBox()
        for label,value in (('Not assessed','not_assessed'),('No linked change seen','no_change'),('Linked change seen','change_seen')):
            self.cardiac.addItem(label,value)
        cardiac_row.addWidget(self.cardiac)
        self.cardiac_evidence=QLineEdit();self.cardiac_evidence.setPlaceholderText('Evidence reviewed: ECG/pulse trace, clinical record, etc.')
        self.cardiac_evidence.setToolTip('The amplitude-over-time plot helps locate shape changes, but cannot establish cardiac timing by itself. Document the cardiac reference used.')
        cardiac_row.addWidget(self.cardiac_evidence,1);review_layout.addLayout(cardiac_row)
        decision_row=QHBoxLayout();decision_row.addWidget(QLabel('Single-unit decision'))
        self.decision=QComboBox()
        for label,value in (('Needs review','needs_review'),('Accepted','accepted'),('Rejected','rejected')):
            self.decision.addItem(label,value)
        decision_row.addWidget(self.decision);decision_row.addStretch(1);review_layout.addLayout(decision_row)
        self.review_notes=QPlainTextEdit();self.review_notes.setPlaceholderText('Review notes, including waveform or cardiac-cycle concerns')
        self.review_notes.setMaximumHeight(65);review_layout.addWidget(self.review_notes)
        footer=QHBoxLayout();self.status=QLabel('Sorting…');self.status.setWordWrap(True);footer.addWidget(self.status,1)
        self.cluster=QComboBox();self.cluster.setMinimumWidth(230);self.cluster.setSizeAdjustPolicy(QComboBox.AdjustToContents);self.cluster.setEnabled(False);self.cluster.currentIndexChanged.connect(self.update_selection);footer.addWidget(self.cluster)
        self.clear_button=QToolButton(text='Clear')
        self.clear_button.setToolTip('Remove saved sorting for this channel and return to the recording.')
        self.clear_button.clicked.connect(self.clearRequested.emit);footer.addWidget(self.clear_button)
        self.apply_button=QToolButton(text='Apply');self.apply_button.setEnabled(False)
        self.apply_button.setToolTip('Save this cluster and show its spike train in the recording.')
        self.save_review_button=QToolButton(text='Save unit review');self.save_review_button.setEnabled(False)
        self.save_review_button.setToolTip('Save this decision for the already saved selected cluster.')
        self.save_review_button.clicked.connect(self.save_review)
        footer.addWidget(self.save_review_button)
        self.apply_button.clicked.connect(self.apply_selected);footer.addWidget(self.apply_button);layout.addLayout(footer)

    def review_data(self):
        return UnitReview(self.decision.currentData(),self.distinct_waveforms.isChecked(),
                          self.clear_refractory.isChecked(),self.cardiac.currentData(),
                          self.cardiac_evidence.text(),self.review_notes.toPlainText())

    def set_review(self,review=None,remember=True):
        if remember:self.persisted_review=review
        review=review or UnitReview()
        self.decision.setCurrentIndex(self.decision.findData(review.decision))
        self.distinct_waveforms.setChecked(review.distinct_waveforms)
        self.clear_refractory.setChecked(review.clear_refractory)
        self.cardiac.setCurrentIndex(self.cardiac.findData(review.cardiac))
        self.cardiac_evidence.setText(review.cardiac_evidence)
        self.review_notes.setPlainText(review.notes)

    def save_review(self):
        if self.result is not None and self.cluster.currentData()==self.saved_cluster:
            self.reviewRequested.emit(self.review_data())

    def set_cardiac_trace(self,name,pieces):
        self.cardiac_plot.clear()
        for piece in pieces:
            self.cardiac_plot.plot(piece['x'],piece['y'],pen=pg.mkPen('#637593',width=1))
        self.cardiac_plot.setVisible(bool(pieces))
        if pieces:
            self.cardiac_plot.setLabel('left',name)
            self.cardiac_status.setText(f'{name} is aligned with spike amplitude over recorded time. Inspect for repeated cardiac-linked changes in spike shape; document the evidence used.')
        else:
            self.cardiac_status.setText('No usable ECG or pulse trace was loaded. Use an external cardiac reference if available, and document it below.')

    def request_sort(self, filtering=False):
        self.unsorted=False
        self.result=None;self.error='';self.saved_cluster=None;self.persisted_review=None;self.apply_button.setEnabled(False);self.cluster.setEnabled(False)
        self.save_review_button.setEnabled(False);self.set_review()
        self.apply_button.setText('Apply');self.apply_button.setToolTip('Save this cluster and show its spike train in the recording.')
        self.clear_button.setEnabled(True)
        self.status.setText('Sorting…');self.overlay.clear()
        self.trace.clear();self.waveforms.clear();self.pca.clear()
        self.rerun.emit(bool(filtering))

    def set_unsorted(self, pieces):
        self.unsorted=True;self.result=None;self.error='';self.saved_cluster=None;self.persisted_review=None;self.overlay.clear()
        self.save_review_button.setEnabled(False);self.set_review()
        self.trace.clear();self.waveforms.clear();self.pca.clear()
        for piece in pieces:self.trace.plot(piece['x'],piece['y'],pen=pg.mkPen('#555B64',width=1))
        self.trace.setXRange(self.span.start,self.span.end,padding=0);self.trace.enableAutoRange(axis='y')
        self.cluster.clear();self.cluster.setEnabled(False);self.clear_button.setEnabled(False)
        self.status.setText('No sorting saved.')
        self.apply_button.setText('Sort');self.apply_button.setEnabled(True)
        self.apply_button.setToolTip('Sort this channel within the selected span.')

    def failed(self, message):
        self.result=None;self.error=message;self.status.setText(message);self.apply_button.setEnabled(False);self.cluster.setEnabled(False)

    def set_result(self, result, saved=False):
        self.unsorted=False;self.result=result;self.overlay.clear();self.trace.clear()
        self.saved_cluster=result.selected if saved else None;self.persisted_review=None
        self.threshold.blockSignals(True);self.threshold.setValue(float(result.parameters.get('threshold_sd',2.)));self.threshold.blockSignals(False)
        self.apply_button.setText('Apply');self.clear_button.setEnabled(True)
        for piece in result.preview:self.trace.plot(piece['x'],piece['y'],pen=pg.mkPen('#555B64',width=1))
        self.trace.setXRange(self.span.start,self.span.end,padding=0);self.trace.enableAutoRange(axis='y')
        self.cluster.blockSignals(True);self.cluster.clear()
        for label in np.unique(result.labels):
            self.cluster.addItem(f'Cluster {int(label)+1} · {int(np.sum(result.labels==label))} spikes',int(label))
        self.cluster.setCurrentIndex(self.cluster.findData(result.selected));self.cluster.blockSignals(False)
        self.cluster.setEnabled(bool(len(result.times)));self.apply_button.setEnabled(bool(len(result.times)))
        if not len(result.times):self.status.setText('No spikes detected within this span.');return
        self.update_selection()

    def update_selection(self):
        if self.result is None or self.cluster.currentIndex()<0:return
        result=self.result;selected=self.cluster.currentData();mask=result.labels==selected
        self.save_review_button.setEnabled(self.saved_cluster==selected)
        self.set_review(self.persisted_review if selected==self.saved_cluster else UnitReview(),remember=False)
        self.status.setText(f'{len(result.times):,} detected · {int(mask.sum()):,} in selected cluster. Open Single-unit review for the 3 ms ISI check.')
        self.overlay.show(result.preview,result.times[mask],result.fs,self.span)
        self.waveforms.clear();self.pca.clear()
        time=(np.arange(result.waveforms.shape[1])-max(1,round(.0005*result.fs)))/result.fs*1000
        for label in np.unique(result.labels):
            waveforms=result.waveforms[result.labels==label]
            color=SPIKE_COLOR if label==selected else '#A2A7AF'
            sample=waveforms[np.linspace(0,len(waveforms)-1,min(100,len(waveforms)),dtype=int)]
            x=np.tile(np.r_[time,np.nan],len(sample));y=np.column_stack((sample,np.full(len(sample),np.nan))).ravel()
            self.waveforms.plot(x,y,connect='finite',pen=pg.mkPen(color,width=.5))
            self.waveforms.plot(time,waveforms.mean(axis=0),pen=pg.mkPen(color,width=2))
            features=result.features[result.labels==label]
            features=features[np.linspace(0,len(features)-1,min(2000,len(features)),dtype=int)]
            self.pca.addItem(pg.ScatterPlotItem(features[:,0],features[:,1],size=4,pen=None,brush=color))
        selected_times=result.times[mask]
        summary=isi_summary(selected_times)
        self.isi_summary.setText(f'{summary["spikes"]:,} selected spikes · {summary["under_3_ms"]:,} of '
                                 f'{summary["intervals"]:,} intervals below 3 ms '
                                 f'({summary["percent_under_3_ms"]:.2f}%). Inspect the shaded 0–3 ms region.')
        self.isi_plot.clear()
        self.isi_plot.addItem(pg.LinearRegionItem((0,3),movable=False,brush=pg.mkBrush(220,85,60,55),pen=pg.mkPen(220,85,60)))
        if len(selected_times)>1:
            intervals=np.diff(selected_times)*1000
            edges=np.arange(0,max(10.5,min(100.5,float(np.ceil(np.percentile(intervals,99)*2)/2)+.5)),.5)
            counts,_=np.histogram(intervals,bins=edges)
            self.isi_plot.addItem(pg.BarGraphItem(x=edges[:-1]+.25,height=counts,width=.48,brush=pg.mkBrush('#647C98')))
            self.isi_plot.setXRange(0,max(10,min(100,float(np.percentile(intervals,99)))),padding=0)
        self.stability_plot.clear()
        waves=result.waveforms[mask]
        if len(waves):
            keep=np.linspace(0,len(waves)-1,min(len(waves),5000),dtype=int)
            amplitude=np.ptp(waves[keep],axis=1)
            self.stability_plot.addItem(pg.ScatterPlotItem(selected_times[keep],amplitude,size=3,pen=None,brush=pg.mkBrush('#596F8F')))

    def apply_selected(self):
        if self.unsorted:
            self.request_sort(self.filtering.isChecked());return
        if self.result is not None and self.cluster.currentIndex()>=0:
            self.applyRequested.emit(self.result,int(self.cluster.currentData()),self.review_data())
