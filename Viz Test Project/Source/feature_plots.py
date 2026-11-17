import numpy as np
from scipy.stats import median_abs_deviation
from threadpoolctl import threadpool_limits

import paper_methods as paper
from spike_features import BANDS
from spike_sorting import span_pieces

COLORS = ('#4A6FA5', '#CE514A', '#548A67', '#9B70AF', '#C68A39', '#448C99')


def feature_plot_data(key, run, pieces, span, saved):
    data = dict(lines=[], bars=[], regions=[], markers=[], xlabel='', ylabel='')

    def line(x, y, name, color=COLORS[0], **kwargs):
        data['lines'].append(dict(x=np.asarray(x), y=np.asarray(y), name=name, color=color, **kwargs))

    def histogram(values, xlabel):
        counts, edges = np.histogram(values, bins=min(60, max(8, int(np.sqrt(len(values))))))
        data['bars'].append(dict(x=(edges[:-1]+edges[1:])/2, height=counts, width=np.diff(edges)))
        data.update(xlabel=xlabel, ylabel='Intervals')

    if key == 'Silhouette':
        for label in np.unique(saved.labels):
            points = saved.features[saved.labels == label]
            sample = points[np.linspace(0, len(points)-1, min(5000, len(points)), dtype=int)]
            line(sample[:, 0], sample[:, 1], f'Cluster {label+1}', COLORS[int(label) % len(COLORS)], scatter=True)
        data.update(xlabel='PC1', ylabel='PC2', caption='All saved clusters · full span')
        return data

    piece = next((p for p in span_pieces(pieces, span) if p['run_index']+1 == run['run']), None)
    if piece is None:
        raise ValueError('This segment is no longer loaded. Reopen features.')
    fs = saved.fs
    start = float(piece['x'][0])
    raw = np.asarray(piece['y'], dtype=float)
    mask = (saved.labels == saved.selected) & (saved.run_indices == piece['run_index'])
    indices = saved.sample_indices[mask] - round((start-piece['run_origin'])*fs)
    if (np.any(indices < 0) or np.any(indices >= len(raw)) or
            not np.allclose(saved.times[mask], start+indices/fs, rtol=0, atol=.25/fs)):
        raise ValueError('Saved spikes do not match this segment. Refresh the recording.')
    if not len(indices):
        raise ValueError('No saved spikes in this segment.')
    times = saved.times[mask] - start
    if key == 'Firing_Rate':
        duration = len(raw)/fs
        bins = np.linspace(0, duration, min(120, max(1, int(np.ceil(duration)))))
        if len(bins) < 2:
            bins = np.array([0., duration])
        counts, edges = np.histogram(indices/fs, bins=bins)
        data['bars'].append(dict(x=start+(edges[:-1]+edges[1:])/2,
                                 height=counts/np.diff(edges), width=np.diff(edges)*.9))
        data.update(xlabel='Recorded time (s)', ylabel='Binned firing rate (Hz)')
        value = run['values'].get(key)
        if value is not None:
            data['markers'].append(('h', value, '1 / mean ISI'))
        return data
    if len(indices) < 2:
        raise ValueError(run['notes'].get(key, 'At least two spikes are needed.'))
    isi = np.diff(indices)/fs
    if key == 'Burst_Index':
        fitted = np.diff(times)
        histogram(fitted*1000, 'Inter-spike interval (ms)')
        if len(fitted) >= 2 and len(np.unique(fitted)) >= 2:
            with threadpool_limits(limits=1):
                model = paper.isi_mixture(times)
            x = np.linspace(0, fitted.max()*1000, 400)
            width = data['bars'][0]['width'][0]
            for i, (mean, variance, weight) in enumerate(zip(model.means_.ravel()*1000, model.covariances_*1e6,
                                                             model.weights_)):
                density = weight*np.exp(-.5*(x-mean)**2/variance)/np.sqrt(2*np.pi*variance)
                line(x, density*len(fitted)*width, f'Component {i+1}', COLORS[i+1])
                data['markers'].append(('v', mean, f'Mean {i+1}'))
        return data
    if key in ('CV', 'ISI_Violations'):
        histogram(isi*1000, 'Inter-spike interval (ms)')
        if key == 'CV':
            median = np.median(isi)*1000
            mad = median_abs_deviation(isi)*1000
            data['markers'].append(('v', median, 'Median'))
            data['regions'].append((max(0, median-mad), median+mad))
        else:
            data['regions'].append((0, 1))
            data['markers'].append(('v', 1, '1 ms'))
        return data
    if key == 'SNR':
        valid = times[paper.snr_spikes(times, len(raw), fs)]
        if len(valid) < 2:
            raise ValueError(run['notes'].get(key, 'At least two complete spike windows are needed.'))
        waves, x = paper.snr_waveforms(raw, valid, fs)
        mean, sd = waves.mean(axis=0), waves.std(axis=0)
        sample = waves[np.linspace(0, len(waves)-1, min(60, len(waves)), dtype=int)]
        line(np.tile(np.r_[x, np.nan], len(sample)),
             np.column_stack((sample, np.full(len(sample), np.nan))).ravel(), 'Spikes', '#CDD7E6', thin=True)
        line(x, mean-sd, 'Mean − SD', '#92A6C1')
        line(x, mean+sd, 'Mean + SD', '#92A6C1')
        line(x, mean, 'Mean waveform')
        data.update(xlabel='Time from spike (ms)', ylabel='Filtered counts')
        return data
    if key.startswith('Spiketrain_'):
        band = key.removeprefix('Spiketrain_').removesuffix('_Power')
        low, high = paper.BANDS[BANDS.index(band)]
        frequencies, power = paper.spiketrain_spectrum(times)
        with np.errstate(divide='ignore', invalid='ignore'):
            db = np.where(power > 0, 10*np.log10(power), np.nan)
        if not np.isfinite(db).any():
            raise ValueError(run['notes'].get(key, 'Spectrum unavailable for this segment.'))
        line(frequencies, db, 'Spike-train spectrum')
        data['regions'].append((low, high))
        data.update(xlabel='Frequency (Hz)', ylabel='Power (dB)')
        return data
    if key.endswith('_Mean_Burst_Duration'):
        band = key.removesuffix('_Mean_Burst_Duration')
        low, high = paper.BANDS[BANDS.index(band)]
        threshold = paper.gamma_threshold([paper.spiketrain_wave(times, *gamma) for gamma in paper.GAMMA_BANDS])
        envelope, intervals, _ = paper.burst_intervals(paper.spiketrain_wave(times, low, high), 500, threshold)
        line(start+np.arange(len(envelope))/500, envelope, f'{low}–{high} Hz envelope')
        data['markers'].append(('h', threshold, 'Burst threshold'))
        data['regions'] = [(start+a/500, start+b/500) for a, b in intervals]
        data.update(xlabel='Recorded time (s)', ylabel='Envelope amplitude')
        if not len(intervals):
            data['caption'] = 'No completed bursts longer than 100 ms; recorded as 0 s'
        return data
    raise ValueError('Plot unavailable for this feature.')
