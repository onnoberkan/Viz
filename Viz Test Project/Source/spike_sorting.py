from dataclasses import dataclass
import io
import json
import math
import zipfile

import numpy as np

from alpha_reader import same_file
from recording_spans import RecordingSpan

DEFAULTS = dict(algorithm='pca-kmeans-v1', filter_hz=[300.,3000.], filter_order=3,
                filtering=False,
                threshold_sd=2., refractory_ms=2., before_ms=.5, after_ms=1.,
                max_clusters=6, random_seed=0)


@dataclass
class SortResult:
    times: np.ndarray
    labels: np.ndarray
    features: np.ndarray
    waveforms: np.ndarray
    preview: list
    fs: float
    selected: int
    parameters: dict
    run_indices: np.ndarray
    sample_indices: np.ndarray

    @property
    def spike_times(self):
        return self.times[self.labels == self.selected]


@dataclass
class SavedSort:
    times: np.ndarray
    labels: np.ndarray
    selected: int
    fs: float
    span: RecordingSpan
    parameters: dict

    @property
    def spike_times(self):return self.times[self.labels == self.selected]


def source_signature(view, flags, verify=False):
    rows = []
    for source in view.sources:
        if verify:
            stat = source.path.stat()
            if not same_file((stat.st_size,stat.st_mtime_ns),(source.size,source.mtime_ns)):
                raise ValueError('Recording changed on disk; refresh it before sorting.')
        rows.append((str(source.path.resolve().relative_to(flags.root)),source.size,source.mtime_ns,source.ctime_ns))
    return json.dumps(sorted(rows),separators=(',',':'))


def same_source(saved, current):
    try:
        before, after = json.loads(saved), json.loads(current)
        if len(before) != len(after):return False
        for old, new in zip(before, after):
            path, size, mtime = old[:3]
            if path != new[0] or size != new[1]:return False
            # Downloaded PLX copies can carry a two-second filesystem timestamp.
            tolerance = 2_000_000_000 if path.lower().endswith('.plx') else 1_000_000_000
            if abs(mtime-new[2]) >= tolerance:return False
        return True
    except (ValueError, TypeError):
        return False


def channel_identity(channel):
    return json.dumps(channel.key,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def span_pieces(pieces, span):
    for run_index,piece in enumerate(pieces):
        x = piece['x'];a,b = np.searchsorted(x,[span.start,span.end],side='left')
        if b>a:yield dict(x=x[a:b],y=piece['y'][a:b],run_index=run_index,run_origin=float(x[0]))


def sort_channel(pieces, fs, span, cancelled=lambda:False, filtering=False, threshold_sd=2.):
    from scipy import __version__ as scipy_version
    from sklearn import __version__ as sklearn_version
    from scipy.signal import butter, sosfiltfilt, find_peaks
    from sklearn.decomposition import PCA
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from threadpoolctl import threadpool_limits

    def check():
        if cancelled():raise InterruptedError('Spike sorting cancelled')
    if not math.isfinite(fs) or fs < 10000:raise ValueError('Select a spike channel sampled at 10 kHz or higher.')
    if not 0 <= span.start < span.end:raise ValueError('Select a non-empty span first.')
    if not math.isfinite(threshold_sd) or threshold_sd<=0:raise ValueError('The threshold must be a positive, finite SD multiplier.')
    check();sos = butter(3,[300,3000],btype='bandpass',fs=fs,output='sos')
    before,after = max(1,round(.0005*fs)),max(1,round(.001*fs))
    window = before+after+1;filtered = []
    minimum = max(window,3*(2*len(sos)+1)+1) if filtering else window
    for piece in span_pieces(pieces,span):
        check()
        if len(piece['y'])<minimum:continue
        values = np.asarray(piece['y'],dtype=np.float64)
        if not np.isfinite(values).all():raise ValueError('The selected span contains non-finite samples.')
        trace = sosfiltfilt(sos,values) if filtering else values
        filtered.append({**piece,'y':trace})
    if not filtered:raise ValueError('The span is too short to extract complete spike waveforms.')
    total = sum(len(p['y']) for p in filtered)
    mean = sum(p['y'].sum() for p in filtered)/total
    direction = -1 if min(p['y'].min()-mean for p in filtered) < -max(p['y'].max()-mean for p in filtered) else 1
    variance = sum(np.sum((p['y']-mean)**2) for p in filtered)/total
    threshold = float(threshold_sd)*math.sqrt(variance)
    events=[];waves=[];preview=[];run_indices=[];sample_indices=[]
    for piece in filtered:
        check();trace=piece['y'];x=piece['x']
        peaks = find_peaks(direction*(trace-mean),height=threshold,distance=max(1,round(.002*fs)))[0] if threshold>0 else np.empty(0,dtype=int)
        peaks=peaks[(peaks>=before)&(peaks+after<len(trace))]
        events.append(x[peaks])
        run_indices.append(np.full(len(peaks),piece['run_index'],dtype=np.int32))
        sample_indices.append(np.rint((x[peaks]-piece['run_origin'])*fs).astype(np.int64))
        waves.append(trace[peaks[:,None]+np.arange(-before,after+1)])
        preview.append(dict(x=x,y=trace))
    times=np.concatenate(events);waveforms=np.concatenate(waves,axis=0)
    run_indices=np.concatenate(run_indices);sample_indices=np.concatenate(sample_indices)
    if len(times)>1:
        order=np.argsort(times,kind='stable');times=times[order];waveforms=waveforms[order]
        run_indices=run_indices[order];sample_indices=sample_indices[order]
    parameters={**DEFAULTS,'filtering':bool(filtering),'threshold_sd':float(threshold_sd),'polarity':direction,'threshold_counts':threshold,'fs':fs}
    parameters['software_versions']=dict(numpy=np.__version__,scipy=scipy_version,scikit_learn=sklearn_version)
    parameters['runs']=[dict(start_seconds=float(p['x'][0]) if len(p['x']) else None,sample_count=len(p['x']),
                            source_start_ticks=int(p['part'].run.ticks) if 'part' in p else None) for p in pieces]
    if not len(times):return SortResult(times,np.empty(0,dtype=np.int32),np.empty((0,2)),waveforms,preview,fs,0,parameters,run_indices,sample_indices)
    check();centered=waveforms-waveforms.mean(axis=1,keepdims=True)
    features=np.zeros((len(times),2));labels=np.zeros(len(times),dtype=np.int32)
    rng=np.random.default_rng(0);fit_indices=np.sort(rng.choice(len(times),min(10000,len(times)),replace=False))
    with threadpool_limits(limits=1):
        if len(times)>=3 and np.any(np.ptp(centered,axis=0)>0):
            pca=PCA(n_components=2,svd_solver='full');pca.fit(centered[fit_indices]);features=pca.transform(centered)
            sample=features[fit_indices];best_score=-np.inf;best=None
            score_indices=np.sort(rng.choice(len(sample),min(500,len(sample)),replace=False))
            for count in range(2,min(6,len(sample)-1,len(np.unique(sample,axis=0)))+1):
                check();model=KMeans(n_clusters=count,random_state=0,n_init=10).fit(sample)
                trial=model.labels_[score_indices]
                if not 1<len(np.unique(trial))<len(trial):continue
                score=silhouette_score(sample[score_indices],trial)
                if score>best_score:best_score=score;best=model
            if best is not None:labels=best.predict(features).astype(np.int32)
    check()
    clusters=np.unique(labels)
    selected=int(max(clusters,key=lambda label:float(np.mean(direction*waveforms[labels==label,before]))))
    return SortResult(times,labels,features,waveforms,preview,fs,selected,parameters,run_indices,sample_indices)


class SpikeSortStore:
    def __init__(self, flags):self.flags=flags

    def progress(self, views=()):
        if self.flags.connection is None and not self.flags.path.exists():return {}
        conn=self.flags.connect()
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'spike_sorts' not in tables:return {}
        self.ensure_progress_index(conn)
        query='''SELECT s.recording_id,s.source_files,s.source_signature,s.channel_key,
            s.span_start,s.span_end,'''+('p.recording_id' if 'recording_spans' in tables else 'NULL')+''',s.selected_spike_count
            FROM spike_sorts s'''
        if 'recording_spans' in tables:
            query+=''' LEFT JOIN recording_spans p ON p.recording_id=s.recording_id
                WHERE p.recording_id IS NULL OR (s.span_start=p.start_seconds AND s.span_end=p.end_seconds)'''
        indexed={self.flags.identity(view)[0]:(source_signature(view,self.flags),view.duration) for view in views}
        result={}
        for key,sources,signature,channel,start,end,annotation,count in conn.execute(query):
            if key in indexed:
                expected,duration=indexed[key]
                if not same_source(signature,expected) or not 0<=start<end<=duration:continue
                if annotation is None and (start,end)!=(0.,duration):continue
            entry=result.setdefault(key,dict(sources=frozenset(json.loads(sources)),channels=set(),spike_count=0))
            entry['channels'].add(channel)
            entry['spike_count']+=int(count)
        return result

    @staticmethod
    def ensure_progress_index(conn):
        conn.execute('''CREATE INDEX IF NOT EXISTS spike_sorts_progress ON spike_sorts
            (recording_id,source_files,source_signature,channel_key,span_start,span_end,selected_spike_count)''')

    def save(self, view, channel, span, result, selected):
        span.validate(view.duration)
        signature=source_signature(view,self.flags,verify=True)
        times=np.asarray(result.times,dtype='<f8');labels=np.asarray(result.labels,dtype='<i4')
        self.validate(times,labels,selected,span)
        if result.fs != channel.fs:raise ValueError('The channel sampling rate changed.')
        if (result.waveforms.ndim!=2 or len(result.waveforms)!=len(times)
                or result.features.shape!=(len(times),2) or not np.isfinite(result.waveforms).all()
                or not np.isfinite(result.features).all()
                or result.run_indices.shape!=times.shape or result.sample_indices.shape!=times.shape
                or not np.issubdtype(result.run_indices.dtype,np.integer)
                or not np.issubdtype(result.sample_indices.dtype,np.integer)
                or (result.run_indices<0).any() or (result.sample_indices<0).any()):
            raise ValueError('Incomplete spike sorting results')
        runs=result.parameters['runs']
        if (result.run_indices>=len(runs)).any():raise ValueError('Invalid spike run identities')
        for index in np.unique(result.run_indices):
            mask=result.run_indices==index;run=runs[index];samples=result.sample_indices[mask]
            if ((samples>=run['sample_count']).any() or run['start_seconds'] is None
                    or not np.allclose(times[mask],run['start_seconds']+samples/channel.fs,rtol=0,atol=.25/channel.fs)):
                raise ValueError('Spike times do not match native sample locations')
        payload=io.BytesIO();np.savez_compressed(payload,times=times,labels=labels,
            waveforms=result.waveforms,features=result.features,
            run_indices=result.run_indices,sample_indices=result.sample_indices)
        key,sources=self.flags.identity(view)
        values=(key,channel_identity(channel),sources,signature,span.start,span.end,channel.fs,int(selected),
                json.dumps(result.parameters,allow_nan=False),payload.getvalue())
        with self.flags.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS spike_sorts (
                recording_id TEXT NOT NULL, channel_key TEXT NOT NULL, source_files TEXT NOT NULL,
                source_signature TEXT NOT NULL, span_start REAL NOT NULL, span_end REAL NOT NULL,
                sampling_rate REAL NOT NULL, selected_cluster INTEGER NOT NULL,
                parameters TEXT NOT NULL, events BLOB NOT NULL,
                selected_spike_count INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (recording_id,channel_key))''')
            self.ensure_progress_index(conn)
            conn.execute('''CREATE TABLE IF NOT EXISTS spike_sort_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id TEXT NOT NULL, channel_key TEXT NOT NULL, source_files TEXT NOT NULL,
                source_signature TEXT NOT NULL, span_start REAL NOT NULL, span_end REAL NOT NULL,
                sampling_rate REAL NOT NULL, selected_cluster INTEGER NOT NULL,
                parameters TEXT NOT NULL, events BLOB NOT NULL,
                action TEXT NOT NULL DEFAULT 'apply',
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')))''')
            conn.execute('''INSERT INTO spike_sort_history (recording_id,channel_key,source_files,source_signature,
                span_start,span_end,sampling_rate,selected_cluster,parameters,events) VALUES (?,?,?,?,?,?,?,?,?,?)''',values)
            conn.execute('''INSERT INTO spike_sorts (recording_id,channel_key,source_files,source_signature,
                span_start,span_end,sampling_rate,selected_cluster,parameters,events,selected_spike_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(recording_id,channel_key) DO UPDATE SET source_signature=excluded.source_signature,
                span_start=excluded.span_start,span_end=excluded.span_end,sampling_rate=excluded.sampling_rate,
                selected_cluster=excluded.selected_cluster,parameters=excluded.parameters,events=excluded.events,
                selected_spike_count=excluded.selected_spike_count,
                updated_at=strftime('%Y-%m-%d %H:%M:%f','now')''',
                (*values,int(np.count_nonzero(labels==selected))))
        return SavedSort(times,labels,int(selected),channel.fs,span,result.parameters)

    def clear(self, view, channel):
        if self.flags.connection is None and not self.flags.path.exists():return False
        conn=self.flags.connect()
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spike_sorts'").fetchone():return False
        key=(self.flags.identity(view)[0],channel_identity(channel))
        with conn:
            if not conn.execute('SELECT 1 FROM spike_sorts WHERE recording_id=? AND channel_key=?',key).fetchone():return False
            if 'action' not in {row[1] for row in conn.execute('PRAGMA table_info(spike_sort_history)')}:
                conn.execute("ALTER TABLE spike_sort_history ADD COLUMN action TEXT NOT NULL DEFAULT 'apply'")
            conn.execute('''INSERT INTO spike_sort_history (recording_id,channel_key,source_files,source_signature,
                span_start,span_end,sampling_rate,selected_cluster,parameters,events,action)
                SELECT recording_id,channel_key,source_files,source_signature,
                span_start,span_end,sampling_rate,selected_cluster,parameters,events,'clear'
                FROM spike_sorts WHERE recording_id=? AND channel_key=?''',key)
            conn.execute('DELETE FROM spike_sorts WHERE recording_id=? AND channel_key=?',key)
        return True

    def load(self, view, channel, span, preview=None):
        if self.flags.connection is None and not self.flags.path.exists():return None
        conn=self.flags.connect()
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spike_sorts'").fetchone():return None
        row=conn.execute('''SELECT source_signature,span_start,span_end,sampling_rate,selected_cluster,parameters,events
            FROM spike_sorts WHERE recording_id=? AND channel_key=?''',
            (self.flags.identity(view)[0],channel_identity(channel))).fetchone()
        if row is None:return None
        signature,start,end,fs,selected,parameters,events=row
        if not same_source(signature,source_signature(view,self.flags)) or (start,end)!=(span.start,span.end) or fs!=channel.fs:return None
        try:
            with np.load(io.BytesIO(events),allow_pickle=False) as data:
                times=data['times'];labels=data['labels']
                if preview is not None:
                    waveforms=data['waveforms'];features=data['features']
                    run_indices=data['run_indices'];sample_indices=data['sample_indices']
        except (zipfile.BadZipFile,EOFError) as error:
            raise ValueError('Saved spike sorting is corrupt') from error
        self.validate(times,labels,selected,span)
        parameters=json.loads(parameters)
        if preview is not None:
            pieces=list(span_pieces(preview,span))
            if parameters.get('filtering',False):
                from scipy.signal import butter, sosfiltfilt
                sos=butter(3,[300,3000],btype='bandpass',fs=fs,output='sos')
                minimum=max(1+round(.0005*fs)+round(.001*fs),3*(2*len(sos)+1)+1)
                pieces=[{**p,'y':sosfiltfilt(sos,np.asarray(p['y'],dtype=np.float64))} for p in pieces if len(p['y'])>=minimum]
            return SortResult(times,labels,features,waveforms,pieces,fs,selected,parameters,run_indices,sample_indices)
        return SavedSort(times,labels,selected,fs,span,parameters)

    @staticmethod
    def validate(times,labels,selected,span):
        if (times.ndim!=1 or labels.shape!=times.shape or not np.issubdtype(labels.dtype,np.integer)
                or not len(times) or not np.isfinite(times).all() or (np.diff(times)<=0).any()
                or (times<span.start).any() or (times>=span.end).any() or (labels<0).any()
                or selected not in labels):
            raise ValueError('Invalid spike sorting results')
