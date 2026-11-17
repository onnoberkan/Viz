import csv
import hashlib
import json
import warnings

import numpy as np

from spike_sorting import SpikeSortStore, channel_identity, same_source, source_signature, span_pieces

METHOD = 'neuronal-spike-features-v3'
UPSTREAM = 'https://github.com/Toronto-TNBS/spooky-spikes/tree/d2887bce8107b77950bccb0cfdbf7eb02ae3c259'
REFERENCE_CODE = {
    'analysis/old/old codes_/extract_neuronal_features_ver4.py':
        'e6d8ab85249dda327bd24295c5e0c177714fa61f829ed5193cf50645c45c26e5',
    'analysis/old/EXTRACT/EXTRACT_SPIKETRAIN_FEATURES_BURST_AUTOMATIC.py':
        '66b5d7e22c7a58df5d515923775036e252948258abca001cc3c3064ad716cdab',
}
BANDS = ('Theta', 'Alpha', 'Low_Beta', 'High_Beta')
METRICS = [
    ('Firing_Rate', 'Firing rate', 'Hz'),
    ('Burst_Index', 'Burst index', 'ratio'),
    ('CV', 'Robust coefficient of variation', 'ratio'),
    ('SNR', 'Signal-to-noise ratio', 'ratio'),
    ('ISI_Violations', 'ISI violations (<1 ms)', '%'),
    ('Silhouette', 'Silhouette (all saved clusters)', 'score'),
] + [(f'Spiketrain_{b}_Power', f'Spike-train {b.replace("_", " ").lower()} power', 'dB') for b in BANDS
] + [(f'{b}_Mean_Burst_Duration', f'Spike-train {b.replace("_", " ").lower()} mean burst', 's') for b in BANDS
]


def calculate_features(pieces, fs, span, saved, cancelled=lambda: False):
    import paper_methods as paper
    from sklearn.metrics import silhouette_score
    from threadpoolctl import threadpool_limits
    import scipy
    import sklearn
    import astropy

    def check():
        if cancelled():
            raise InterruptedError('Feature calculation cancelled')

    SpikeSortStore.validate(saved.times, saved.labels, saved.selected, span)
    if fs != saved.fs or not np.isfinite(fs) or fs < 10000:
        raise ValueError('Saved sorting does not match this spike channel.')
    if (saved.features.shape != (len(saved.times), 2)
            or not np.isfinite(saved.features).all()
            or saved.run_indices.shape != saved.times.shape
            or saved.sample_indices.shape != saved.times.shape
            or not np.issubdtype(saved.run_indices.dtype, np.integer)
            or not np.issubdtype(saved.sample_indices.dtype, np.integer)):
        raise ValueError('Saved sorting has invalid PCA or native sample locations.')
    selected = saved.labels == saved.selected
    check()
    silhouette = None
    labels = np.unique(saved.labels)
    if 1 < len(labels) < len(saved.labels):
        with threadpool_limits(limits=1):
            silhouette = float(silhouette_score(saved.features, saved.labels))
    reports = []
    mapped = 0
    for piece in span_pieces(pieces, span):
        check()
        raw = np.asarray(piece['y'], dtype=np.float64)
        if not np.isfinite(raw).all():
            raise ValueError('The selected span contains non-finite recording samples.')
        mask = selected & (saved.run_indices == piece['run_index'])
        indices = saved.sample_indices[mask] - round((piece['x'][0] - piece['run_origin']) * fs)
        if (np.any(indices < 0) or np.any(indices >= len(raw))
                or not np.allclose(saved.times[mask], piece['x'][0] + indices / fs, rtol=0, atol=.25/fs)):
            raise ValueError('Saved spikes do not match the native samples. Refresh the recording.')
        times = saved.times[mask] - piece['x'][0]
        mapped += len(indices)
        values = {key: None for key, _, _ in METRICS}
        notes = {}
        report = dict(run=piece['run_index'] + 1, start_seconds=float(piece['x'][0]),
                      end_seconds=float(piece['x'][0] + len(raw)/fs), sample_count=len(raw),
                      spike_count=len(indices), values=values, notes=notes)

        def compute(keys, fn):
            check()
            try:
                with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
                    warnings.simplefilter('always')
                    result = fn()
                result = [result] if len(keys) == 1 else result
                for key, value in zip(keys, result):
                    value = float(value)
                    if np.isfinite(value):
                        values[key] = value
                    else:
                        notes[key] = 'Calculation is undefined for this segment (insufficient variation or numerical degeneracy).'
                if caught:
                    report.setdefault('reference_warnings', []).extend(sorted({str(w.message) for w in caught}))
            except (ValueError, IndexError, ZeroDivisionError, FloatingPointError) as error:
                for key in keys:
                    notes[key] = f'Calculation unavailable: {error}'

        values['Silhouette'] = silhouette
        if silhouette is None:
            notes['Silhouette'] = 'Silhouette requires at least two saved clusters and more spikes than clusters.'
        if len(indices) >= 2:
            compute(['Firing_Rate'], lambda: paper.get_FR(indices, fs))
            compute(['CV'], lambda: paper.get_CV(indices))
            compute(['ISI_Violations'], lambda: paper.get_pISIv(indices, fs))
            if len(indices) >= 3:
                compute(['Burst_Index'], lambda: paper.burst_index(times))
            else:
                notes['Burst_Index'] = 'At least two inter-spike intervals are needed for the two-component mixture.'
            compute([f'Spiketrain_{b}_Power' for b in BANDS], lambda: paper.spiketrain_power(times))
            compute([f'{b}_Mean_Burst_Duration' for b in BANDS], lambda: paper.spiketrain_bursts(times))
        else:
            for key, _, _ in METRICS:
                if key not in ('SNR', 'Silhouette'):
                    notes[key] = 'At least two spikes are needed in this continuous segment.'
        snr_times = times[paper.snr_spikes(times, len(raw), fs)]
        report['snr_spike_count'] = len(snr_times)
        if len(snr_times) >= 2:
            compute(['SNR'], lambda: paper.snr(raw, snr_times, fs))
        else:
            notes['SNR'] = 'At least two spikes with complete -1/+2 ms windows are needed.'

        report['study_quality'] = dict(
            duration_at_least_5s=len(raw)/fs >= 5,
            snr_above_4=values['SNR'] > 4 if values['SNR'] is not None else None,
            isi_violations_below_1pct=values['ISI_Violations'] < 1 if values['ISI_Violations'] is not None else None)
        reports.append(report)
    if not reports or mapped != int(selected.sum()):
        raise ValueError('Some saved spikes have no matching continuous recording segment.')
    check()
    return dict(method=METHOD, settings={k: v for k, v in paper.SETTINGS.items()
                                       if not k.startswith('lfp_') and k != 'welch_nperseg'}, upstream=UPSTREAM,
                published_database='NEURONAL_DATABASE.xlsx', reference_code=REFERENCE_CODE,
                selected_cluster=int(saved.selected),
                sampling_rate=fs, span_start=span.start, span_end=span.end,
                versions=dict(numpy=np.__version__, scipy=scipy.__version__, sklearn=sklearn.__version__,
                              astropy=astropy.__version__, finnpy='2.0.0'),
                sort_parameters=saved.parameters, runs=reports)


class FeatureStore:
    def __init__(self, flags):
        self.flags = flags

    def token(self, view, channel, span):
        source_signature(view, self.flags, verify=True)
        conn = self.flags.connect()
        row = conn.execute('''SELECT source_signature,span_start,span_end,sampling_rate,
            selected_cluster,parameters,events FROM spike_sorts WHERE recording_id=? AND channel_key=?''',
            (self.flags.identity(view)[0], channel_identity(channel))).fetchone()
        if (row is None or not same_source(row[0], source_signature(view, self.flags))
                or row[1:3] != (span.start, span.end) or row[3] != channel.fs):
            raise ValueError('Apply spike sorting for this channel and span before calculating features.')
        digest = hashlib.sha256(json.dumps(row[:-1], separators=(',', ':')).encode())
        digest.update(row[-1])
        return digest.hexdigest()

    def load(self, view, channel, span, token):
        conn = self.flags.connect()
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spike_features'").fetchone():
            return None
        row = conn.execute('''SELECT results FROM spike_features WHERE recording_id=? AND channel_key=?
            AND sort_token=? AND method=?''', (self.flags.identity(view)[0], channel_identity(channel), token, METHOD)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, view, channel, span, token, result):
        payload = json.dumps(result, allow_nan=False, ensure_ascii=False)
        with self.flags.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.token(view, channel, span) != token:
                raise ValueError('Saved sorting changed during calculation. Calculate features again.')
            conn.execute('''CREATE TABLE IF NOT EXISTS spike_features (
                recording_id TEXT NOT NULL, channel_key TEXT NOT NULL, sort_token TEXT NOT NULL,
                method TEXT NOT NULL, results TEXT NOT NULL,
                calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(recording_id,channel_key,sort_token,method))''')
            conn.execute('''INSERT OR REPLACE INTO spike_features
                (recording_id,channel_key,sort_token,method,results) VALUES (?,?,?,?,?)''',
                (self.flags.identity(view)[0], channel_identity(channel), token, METHOD, payload))


def export_csv(path, result):
    metadata = ['Recording', 'Channel', 'Hemisphere', 'Hemisphere_basis', 'Cluster', 'Run', 'Start_s', 'End_s', 'Spikes', 'SNR_spikes', 'Method', 'Sort_SHA256']
    with open(path, 'w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(metadata + [key for key, _, _ in METRICS] + ['Unavailable_reasons'])
        for run in result['runs']:
            writer.writerow([result.get('recording', ''), result.get('channel', ''), result.get('hemisphere', ''),
                             result.get('hemisphere_basis', ''), result['selected_cluster'] + 1,
                             run['run'], run['start_seconds'], run['end_seconds'], run['spike_count'], run['snr_spike_count'],
                             result['method'], result.get('sort_token', '')]
                            + [run['values'][key] for key, _, _ in METRICS]
                            + [json.dumps(run['notes'], ensure_ascii=False)])
