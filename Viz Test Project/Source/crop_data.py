"""Per-recording crop review annotations in Viz Test's separate database."""

from dataclasses import dataclass, field
import json
import math
import re
import struct

import alpha_reader as reader
from channel_roles import ChannelRoleStore, is_plx, role_for_key
from recording_spans import RecordingSpan
from recording_spans import SpanStore
from spike_sorting import SpikeSortStore, channel_identity


def first_thousand(times):
    """Return the first and thousandth times from one selected cluster, if available."""
    if len(times) < 1000:
        return None
    start, end = float(times[0]), float(times[999])
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError('Saved selected-cluster spike times are invalid')
    return RecordingSpan(start, end)


def use_selected_cluster(review, saved, revision):
    """Refresh a crop from the cluster selected in the saved spike sort."""
    selected_times = saved.spike_times
    review.detected_spikes = len(saved.times)
    review.selected_cluster = int(saved.selected)
    review.selected_cluster_spikes = len(selected_times)
    review.crop = first_thousand(selected_times)
    review.sort_updated_at = revision
    review.crop_basis = 'selected_cluster'


def clipped_intervals(intervals, span):
    if span is None:
        return [list(pair) for pair in intervals]
    return [[max(float(a), span.start), min(float(b), span.end)] for a, b in intervals
            if max(float(a), span.start) < min(float(b), span.end)]


@dataclass
class CropReview:
    spike_channel: str | None = None
    crop: RecordingSpan | None = None
    detected_spikes: int = 0
    selected_cluster: int | None = None
    selected_cluster_spikes: int | None = None
    crop_basis: str = 'selected_cluster'
    sort_updated_at: str | None = None
    tremors: dict = field(default_factory=dict)
    good_ecog: list = field(default_factory=list)


class CropStore:
    def __init__(self, flags):
        self.flags = flags

    @staticmethod
    def ensure(connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS crop_reviews (
            recording_id TEXT PRIMARY KEY,
            source_files TEXT NOT NULL,
            patient TEXT NOT NULL,
            spike_channel TEXT,
            crop_start REAL,
            crop_end REAL,
            detected_spikes INTEGER NOT NULL DEFAULT 0,
            selected_cluster INTEGER,
            selected_cluster_spikes INTEGER,
            crop_basis TEXT NOT NULL DEFAULT 'all_detected',
            sort_updated_at TEXT,
            tremors TEXT NOT NULL DEFAULT '{}',
            good_ecog TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        columns={row[1] for row in connection.execute('PRAGMA table_info(crop_reviews)')}
        for name,definition in (
            ('selected_cluster','INTEGER'),('selected_cluster_spikes','INTEGER'),
            ('crop_basis',"TEXT NOT NULL DEFAULT 'all_detected'")):
            if name not in columns:
                connection.execute(f'ALTER TABLE crop_reviews ADD COLUMN {name} {definition}')
        connection.execute('''CREATE TABLE IF NOT EXISTS patient_good_ecog (
            patient TEXT PRIMARY KEY,
            channel_selectors TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')

    def load_good_ecog(self, patient):
        connection=self.flags.connect()
        self.ensure(connection)
        row=connection.execute('SELECT channel_selectors FROM patient_good_ecog WHERE patient=?',
                               (patient,)).fetchone()
        if row is not None:return {tuple(selector) for selector in json.loads(row[0])}
        # Carry forward ECoG marks made with the earlier per-recording controls.
        selectors=set()
        for (value,) in connection.execute('SELECT good_ecog FROM crop_reviews WHERE patient=?',(patient,)):
            for encoded in json.loads(value):
                selectors.add(self.flags.channel_selector(tuple(json.loads(encoded))))
        if selectors:self.save_good_ecog(patient,selectors)
        return selectors

    def save_good_ecog(self, patient, selectors):
        value=json.dumps(sorted(set(map(tuple,selectors))),ensure_ascii=False)
        with self.flags.connect() as connection:
            self.ensure(connection)
            connection.execute('''INSERT INTO patient_good_ecog (patient,channel_selectors)
                VALUES (?,?) ON CONFLICT(patient) DO UPDATE SET
                channel_selectors=excluded.channel_selectors,updated_at=CURRENT_TIMESTAMP''',
                (patient,value))

    def load(self, view):
        if self.flags.connection is None and not self.flags.path.exists():
            return CropReview()
        connection = self.flags.connect()
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crop_reviews'").fetchone():
            return CropReview()
        self.ensure(connection)
        row = connection.execute('''SELECT patient,spike_channel,crop_start,crop_end,detected_spikes,
            selected_cluster,selected_cluster_spikes,crop_basis,sort_updated_at,tremors,good_ecog
            FROM crop_reviews WHERE recording_id=?''',
            (self.flags.identity(view)[0],)).fetchone()
        if row is None:return CropReview()
        patient,channel,start,end,count,selected,selected_count,basis,revision,tremors,good = row
        span = RecordingSpan(start, end) if start is not None and end is not None else None
        if span is not None:span.validate(view.duration)
        review=CropReview(channel,span,int(count),selected,selected_count,basis,revision,
                          json.loads(tremors),json.loads(good))
        if basis!='selected_cluster' and channel:
            try:
                spike_channel=next((candidate for candidate in reader.channel_options(view)
                                    if channel_identity(candidate)==channel),None)
                sort_span=SpanStore(self.flags).load(view)
                saved=(SpikeSortStore(self.flags).load(view,spike_channel,sort_span)
                       if spike_channel is not None and sort_span is not None else None)
                if saved is not None:
                    use_selected_cluster(review,saved,self.sort_revision(view,spike_channel))
                    self.save(view,patient,review)
            except (OSError,ValueError,KeyError,TypeError,struct.error):
                pass
        return review

    def save(self, view, patient, review):
        if review.crop is not None:review.crop.validate(view.duration)
        for intervals in review.tremors.values():
            for start, end in intervals:
                RecordingSpan(float(start), float(end)).validate(view.duration)
        key, sources = self.flags.identity(view)
        values=(key,sources,patient,review.spike_channel,
                review.crop.start if review.crop else None,review.crop.end if review.crop else None,
                review.detected_spikes,review.selected_cluster,review.selected_cluster_spikes,
                review.crop_basis,review.sort_updated_at,
                json.dumps(review.tremors,ensure_ascii=False),json.dumps(review.good_ecog,ensure_ascii=False))
        with self.flags.connect() as connection:
            self.ensure(connection)
            connection.execute('''INSERT INTO crop_reviews
                (recording_id,source_files,patient,spike_channel,crop_start,crop_end,
                 detected_spikes,selected_cluster,selected_cluster_spikes,crop_basis,
                 sort_updated_at,tremors,good_ecog)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(recording_id) DO UPDATE SET
                source_files=excluded.source_files,patient=excluded.patient,
                spike_channel=excluded.spike_channel,crop_start=excluded.crop_start,
                crop_end=excluded.crop_end,detected_spikes=excluded.detected_spikes,
                selected_cluster=excluded.selected_cluster,
                selected_cluster_spikes=excluded.selected_cluster_spikes,
                crop_basis=excluded.crop_basis,
                sort_updated_at=excluded.sort_updated_at,tremors=excluded.tremors,
                good_ecog=excluded.good_ecog,updated_at=CURRENT_TIMESTAMP''',values)

    def sort_revision(self, view, channel):
        if self.flags.connection is None and not self.flags.path.exists():return None
        row=self.flags.connect().execute('''SELECT updated_at FROM spike_sorts
            WHERE recording_id=? AND channel_key=?''',
            (self.flags.identity(view)[0],channel_identity(channel))).fetchone()
        return row[0] if row else None

    def effective_crop(self, view, review=None):
        review = review or self.load(view)
        if (review.crop is None or review.spike_channel is None or
                review.crop_basis!='selected_cluster' or review.selected_cluster is None):return None
        if is_plx(view) and role_for_key(json.loads(review.spike_channel),
                ChannelRoleStore(self.flags).load(view))!='Spike':return None
        connection=self.flags.connect()
        row=connection.execute('''SELECT updated_at,selected_cluster FROM spike_sorts
            WHERE recording_id=? AND channel_key=?''',
            (self.flags.identity(view)[0],review.spike_channel)).fetchone()
        return review.crop if row and row==(review.sort_updated_at,review.selected_cluster) else None

    def export_document(self, catalog, source_cache=None):
        connection=self.flags.connect()
        self.ensure(connection)
        labels_by_patient=ChannelRoleStore(self.flags).all_labels()
        row_query='''SELECT spike_channel,crop_start,crop_end,detected_spikes,
            selected_cluster,selected_cluster_spikes,crop_basis,sort_updated_at,tremors,good_ecog
            FROM crop_reviews WHERE recording_id=?'''
        rows={row[0]:row[1:] for row in connection.execute('''SELECT recording_id,
            spike_channel,crop_start,crop_end,detected_spikes,selected_cluster,
            selected_cluster_spikes,crop_basis,sort_updated_at,tremors,good_ecog
            FROM crop_reviews''')}
        sorted_revisions={}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spike_sorts'").fetchone():
            sorted_revisions={(key,channel):(updated,selected) for key,channel,updated,selected in connection.execute(
                'SELECT recording_id,channel_key,updated_at,selected_cluster FROM spike_sorts')}
        sorted_channels={}
        for key,channel in sorted_revisions:
            sorted_channels.setdefault(key,[]).append(json.loads(channel))
        unit_reviews={}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unit_acceptance'").fetchone():
            unit_reviews={(key,channel):(revision,cluster,decision,bool(distinct),bool(refractory),
                cardiac,evidence,notes,reviewed_at) for key,channel,revision,cluster,decision,
                distinct,refractory,cardiac,evidence,notes,reviewed_at in connection.execute('''
                SELECT recording_id,channel_key,sort_updated_at,selected_cluster,decision,
                distinct_waveforms,clear_refractory,cardiac,cardiac_evidence,notes,reviewed_at
                FROM unit_acceptance''')}
        recordings=[]
        for patient,paths in sorted(catalog.items()):
            good_selectors=self.load_good_ecog(patient)
            for path in sorted(paths,key=str):
                key,sources=self.flags.paths_identity([path])
                patient_key=self.flags.patient_scope(json.loads(sources))
                saved_labels=labels_by_patient.get(patient_key,{}) if path.suffix.lower()=='.plx' else {}
                row=rows.get(key)
                if row and row[6]!='selected_cluster' and row[0]:
                    try:
                        views,_=reader.recordings([path],source_cache)
                        if views:
                            self.load(views[0])
                            row=connection.execute(row_query,(key,)).fetchone()
                    except (OSError,ValueError,KeyError,TypeError,struct.error):
                        pass
                entry={'patient':patient,'recording_path':str(path.resolve()),
                       'recording_id':key,'status':'unreviewed','spike_channel':None,
                       'spike_channels':[],
                       'channel_labels':[],
                       'detected_spikes':None,'selected_cluster':None,
                       'selected_cluster_spikes':None,'crop_basis':None,
                       'first_1000_spikes_seconds':None,
                       'single_unit_review':None,'single_unit_analysis_ready':False,
                       'emg_tremor_segments':[],'good_ecog_channels':[]}
                if path.suffix.lower()=='.plx':
                    entry['channel_labels']=[{'channel':channel[0],'channel_key':channel,
                        'label':saved_labels[self.flags.channel_selector(tuple(channel))]}
                        for channel in sorted_channels.get(key,[])
                        if self.flags.channel_selector(tuple(channel)) in saved_labels]
                    entry['spike_channels']=[{'channel':channel[0],'channel_key':channel}
                        for channel in sorted_channels.get(key,[])
                        if role_for_key(channel,saved_labels)=='Spike']
                    if entry['spike_channels']:
                        entry['spike_channel']=entry['spike_channels'][0]['channel_key']
                if row:
                    channel,start,end,count,selected,selected_count,basis,revision,tremors,good=row
                    relabeled=(path.suffix.lower()=='.plx' and channel is not None and
                        role_for_key(json.loads(channel),saved_labels)!='Spike')
                    valid=(basis=='selected_cluster' and selected_count is not None and
                           selected_count>=1000 and start is not None and end is not None and
                           sorted_revisions.get((key,channel))==(revision,selected) and not relabeled)
                    span=RecordingSpan(float(start),float(end)) if valid else None
                    entry['spike_channel']=json.loads(channel) if channel else None
                    entry['detected_spikes']=int(count)
                    entry['selected_cluster']=selected+1 if selected is not None else None
                    entry['selected_cluster_spikes']=selected_count
                    entry['crop_basis']=basis
                    entry['status']=('crop_ready' if valid else
                                     'legacy_crop' if start is not None and basis!='selected_cluster' else
                                     'stale_channel_label' if relabeled and start is not None else
                                     'stale_sort' if start is not None else
                                     'fewer_than_1000_spikes' if selected_count is not None and selected_count<1000 else
                                     'needs_spike_sort')
                    if span:entry['first_1000_spikes_seconds']={'start':span.start,'end':span.end}
                    unit=unit_reviews.get((key,channel))
                    if unit and sorted_revisions.get((key,channel))==unit[:2]:
                        _,_,decision,distinct,refractory,cardiac,evidence,notes,reviewed_at=unit
                        entry['single_unit_review']={'decision':decision,
                            'distinct_waveforms':distinct,'clear_3_ms_refractory':refractory,
                            'cardiac_assessment':cardiac,'cardiac_evidence':evidence,
                            'notes':notes,'reviewed_at':reviewed_at}
                        entry['single_unit_analysis_ready']=bool(valid and decision=='accepted')
                    for encoded,intervals in json.loads(tremors).items():
                        identity=json.loads(encoded)
                        entry['emg_tremor_segments'].append({'channel':identity[0],
                            'channel_key':identity,'intervals_seconds':clipped_intervals(intervals,span)})
                if path.suffix.lower() in ('.mpx','.plx') or good_selectors:
                    try:
                        source=(source_cache.source(path) if source_cache else reader.index_file(path))
                        def channel_entry(channel_id, definition):
                            return {'channel':definition['name'],
                                    'channel_key':[definition['name'],channel_id,definition['fs'],
                                                   definition['amp'],definition['gain']]}
                        if path.suffix.lower()=='.plx':
                            entry['channel_labels']=[{**channel_entry(channel_id,definition),
                                'label':saved_labels[selector]}
                                for channel_id,definition in source.definitions.items()
                                if definition.get('sample_count',0)>0
                                if (selector:=self.flags.channel_selector((definition['name'],channel_id,
                                    definition['fs'],definition['amp'],definition['gain']))) in saved_labels]
                        if path.suffix.lower() in ('.mpx','.plx'):
                            entry['spike_channels']=[channel_entry(channel_id,definition)
                                for channel_id,definition in source.definitions.items()
                                if ((path.suffix.lower()=='.mpx' and re.match(r'^c?spk(?:[_ /-]*\d|[_ /-]|$)',definition['name'],re.I))
                                    or (path.suffix.lower()=='.plx' and role_for_key(
                                        (definition['name'],channel_id,definition['fs'],definition['amp'],definition['gain']),
                                        saved_labels)=='Spike'))
                                and definition['fs']>=reader.MIN_FS
                                and definition.get('sample_count',0)>0]
                            if entry['spike_channel'] is None and entry['spike_channels']:
                                entry['spike_channel']=entry['spike_channels'][0]['channel_key']
                        if good_selectors:
                            entry['good_ecog_channels']=[channel_entry(channel_id,definition)
                                for channel_id,definition in source.definitions.items()
                                if ((path.suffix.lower()=='.plx' and role_for_key(
                                        (definition['name'],channel_id,definition['fs'],definition['amp'],definition['gain']),
                                        saved_labels)=='ECoG') or
                                    (path.suffix.lower()!='.plx' and re.search('ecog',definition['name'],re.I)))
                                and definition.get('sample_count',0)>0
                                and self.flags.channel_selector((definition['name'],channel_id,
                                    definition['fs'],definition['amp'],definition['gain'])) in good_selectors]
                    except (OSError,ValueError,KeyError,TypeError,struct.error) as error:
                        entry['channel_index_error']=str(error)
                recordings.append(entry)
        return {'format':'viz-test-good-channels-v1','recordings':recordings}
