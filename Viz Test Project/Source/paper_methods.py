import random
import warnings
import numpy as np
from scipy import signal
from scipy.stats import median_abs_deviation
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture
from astropy.timeseries import LombScargle
from finnpy.basic.downsampling import run as downsample
from finnpy.filters.frequency import butter as finn_butter

BANDS = ((4, 8), (8, 12), (12, 21), (21, 30))


def get_FR(spike_indices, fs):
    return fs / np.mean(np.diff(spike_indices))


def get_CV(spike_indices):
    isi = np.diff(spike_indices)
    return median_abs_deviation(isi) / np.median(isi)


def get_pISIv(spike_indices, fs):
    spiketrain = spike_indices / fs
    isi = np.diff(spiketrain)
    if (isi < 0).any():
        warnings.warn("ISI evaluated to negative values. "
                        "Please sort the input array.")
    return (sum(isi < 1 / 1000) / len(isi)) * 100


GAMMA_BANDS = tuple((low, low+6) for low in range(45, 50))
SETTINGS = dict(
    isi_units='seconds',
    burst_index='max(component mean) / min(component mean) of raw ISIs',
    gmm_components=2, gmm_covariance='spherical', gmm_seed=1,
    snr_filter_hz=[300, 3000], snr_filter_order=4, snr_window_ms=[-1, 2],
    snr_window='nearest samples; cut to the shortest window',
    snr_edge_spikes='exclude spikes within 1 ms of the segment start or 2 ms of its last sample',
    autocorrelation_bin_seconds=.01, autocorrelation_max_lag_seconds=.5,
    lomb_frequencies=[4, 30, 1000], spiketrain_rate_hz=500,
    spiketrain_binary='ceil(last spike time * 500) samples; spike sample floor(time * 500)',
    bandpass_order=4, bandpass_form='FiNNpy (b, a) Butterworth', zero_phase=True,
    spiketrain_mean_removed=True, gamma_bands=[list(band) for band in GAMMA_BANDS],
    burst_threshold='4 * mean(per-band median local envelope trough amplitude)',
    minimum_burst_seconds=.1, boundary_bursts='exclude incomplete crossings', no_burst_value=0,
    lfp_spike_removal_window_ms=[-.5, 2.5], lfp_noise_seed=0,
    lfp_lowpass_hz=125, lfp_lowpass_order=4, lfp_rate_hz=250,
    lfp_downsampling='FiNN power-of-two zero padding, FFT resample, truncate',
    lfp_normalization='z-score; Welch PSD / sum PSD from 45 through 55 Hz',
    welch_nperseg=256, lfp_bands_hz=[[12, 21], [21, 30]],
)


def isi_mixture(times):
    isi = np.diff(times)
    if len(np.unique(isi)) < 2:
        raise ValueError('Two distinct ISIs are needed to fit two Gaussian components.')
    X = isi[:, None]
    return GaussianMixture(n_components=2, covariance_type='spherical', random_state=1,
                           **published_mixture_start(X)).fit(X)


def burst_index(times):
    model = isi_mixture(times)
    return float(np.max(model.means_) / np.min(model.means_))


def published_mixture_start(X):
    rng = np.random.RandomState(1)
    mean = X[:, 0].mean()
    x = X[:, 0] - mean
    squares = x*x

    def distances(center):
        d = -2*(center*x)
        d += center*center
        d += squares
        return np.maximum(d, 0)

    first = x[rng.randint(len(x))]
    closest = distances(first)
    candidates = np.minimum(np.searchsorted(np.cumsum(closest), rng.uniform(size=2)*closest.sum()), len(x)-1)
    second = x[candidates[np.argmin([np.minimum(closest, distances(x[c])).sum() for c in candidates])]]
    labels = KMeans(n_clusters=2, init=np.array([[first], [second]]) + mean, n_init=1).fit(X).labels_
    resp = np.eye(2)[labels]
    counts = resp.sum(axis=0) + 10*np.finfo(float).eps
    means = resp.T @ X / counts[:, None]
    variances = (resp.T @ (X*X) / counts[:, None] - means**2 + 1e-6).mean(axis=1)
    return dict(weights_init=counts/len(X), means_init=means, precisions_init=1/variances)


def nearest_samples(times, count, fs):
    candidates = np.clip(np.floor(times*fs).astype(np.int64)[:, None] + np.arange(-1, 3), 0, count-1)
    return candidates[np.arange(len(times)), np.argmin(np.abs(candidates/fs - times[:, None]), axis=1)]


def snr_spikes(times, count, fs):
    return (times > .001) & (np.abs(times - (count-1)/fs) > .002)


def snr_waveforms(raw, times, fs):
    b, a = signal.iirfilter(4, [300, 3000], btype='bandpass', ftype='butter', fs=fs)
    filtered = signal.filtfilt(b, a, raw)
    starts = nearest_samples(times - .001, len(raw), fs)
    length = int(np.min(nearest_samples(times + .002, len(raw), fs) - starts))
    if length < 2:
        raise ValueError('Spike windows are too short for the SNR.')
    offset = np.median(starts - np.round(times*fs))
    return filtered[starts[:, None] + np.arange(length)], (offset + np.arange(length))/fs*1000


def snr(raw, times, fs):
    waves = snr_waveforms(raw, times, fs)[0]
    mean = waves.mean(axis=0)
    return float((mean.max() - mean.min()) / (2*waves.std(axis=0).mean()))


def bandpass(data, low, high, fs):
    return finn_butter(data, low, high, fs, order=4, zero_phase=True)


def spiketrain_binary(times):
    count = int(np.ceil(np.max(times)*500))
    binary = np.zeros(count)
    samples = (times*500).astype(int)
    binary[samples[samples < count]] = 1
    return binary


def spiketrain_wave(times, low, high):
    wave = bandpass(spiketrain_binary(times), low, high, 500)
    return wave - np.mean(wave)


def gamma_threshold(waves):
    medians = []
    for wave in waves:
        envelope = np.abs(signal.hilbert(wave))
        troughs = signal.argrelextrema(envelope, np.less)[0]
        if not len(troughs):
            raise ValueError('No gamma-envelope troughs for the burst threshold.')
        medians.append(np.median(envelope[troughs]))
    return float(4 * np.mean(medians))


def burst_duration(wave, fs, threshold):
    durations = burst_intervals(wave, fs, threshold)[2]
    return float(np.mean(durations)) if len(durations) else 0.


def burst_intervals(wave, fs, threshold):
    envelope = np.abs(signal.hilbert(wave))
    if not np.isfinite(envelope).all() or not np.isfinite(threshold):
        raise ValueError('The burst envelope or gamma threshold is non-finite.')
    above = envelope > threshold
    crossings = np.flatnonzero(np.diff(above))
    if len(crossings) and not above[crossings[0]+1]:
        crossings = crossings[1:]
    starts, stops = crossings[:len(crossings)//2*2:2], crossings[1::2]
    durations = stops/fs - starts/fs
    keep = durations > .1
    return envelope, np.column_stack((starts[keep]+1, stops[keep]+1)), durations[keep]


def spiketrain_bursts(times):
    threshold = gamma_threshold([spiketrain_wave(times, low, high) for low, high in GAMMA_BANDS])
    return [burst_duration(spiketrain_wave(times, low, high), 500, threshold) for low, high in BANDS]


def spiketrain_power(times):
    frequencies, power = spiketrain_spectrum(times)
    result = []
    for low, high in BANDS:
        band = power[(frequencies >= low) & (frequencies < high)]
        peaks = signal.find_peaks(band)[0]
        result.append(10*np.log10(np.max(band[peaks] if len(peaks) else band)))
    return result


def spiketrain_spectrum(times):
    bins = np.arange(0, np.max(times) + .01, .01)
    counts = np.histogram(times, bins=bins)[0]
    lags = np.arange(min(len(counts), 51))
    ac = np.array([np.dot(counts[:len(counts)-lag], counts[lag:]) for lag in lags])
    frequencies = np.linspace(4, 30, 1000)
    power = LombScargle(lags * .01, ac).power(frequencies)
    return frequencies, power


def remove_spikes(raw, indices, fs):
    data = np.array(raw, dtype=float, copy=True)
    before, after = int(.0005*fs), int(.0025*fs)
    rng = random.Random(0)
    contaminated = np.zeros(len(data), dtype=bool)
    for index in indices:
        contaminated[max(0, index-before):min(len(data), index+after)] = True
    prefix = np.concatenate(([0], np.cumsum(contaminated)))
    donors = np.arange(before, len(data)-after+1)
    donors = donors[(prefix[donors+after]-prefix[donors-before]) == 0]
    if len(indices) and not len(donors):
        raise ValueError('No spike-free noise windows are available for LFP spike removal.')
    for index in indices:
        if index-before < 3 or index+after > len(data):
            raise ValueError('A spike window crosses the span boundary; LFP spike removal is incomplete.')
        count = int(np.searchsorted(donors, index-before, side='right')) or len(donors)
        donor = donors[rng.randrange(count)]
        noise = raw[donor-before:donor+after]
        data[index-before:index+after] = noise - np.mean(noise) + np.mean(data[index-before-3:index-before])
    return data


def lfp_metrics(raw, indices, fs):
    cleaned = remove_spikes(raw, indices, fs)
    sos = signal.butter(4, 125, btype='lowpass', fs=fs, output='sos')
    lfp = downsample(signal.sosfiltfilt(sos, cleaned), fs, 250)
    if len(lfp) < 256:
        raise ValueError('At least 256 samples at 250 Hz are needed for the paper’s Welch window.')
    scale = np.std(lfp)
    if scale <= 0 or not np.isfinite(scale):
        raise ValueError('LFP has no finite variation after spike removal.')
    lfp = (lfp - np.mean(lfp))/scale
    frequencies, psd = signal.welch(lfp, 250, nperseg=256)
    baseline = np.sum(psd[(frequencies >= 45) & (frequencies <= 55)])
    if baseline <= 0:
        raise ValueError('No gamma-band power for LFP normalization.')
    powers = [10*np.log10(np.sum(psd[(frequencies >= low) & (frequencies <= high)])/baseline)
              for low, high in BANDS[2:]]
    threshold = gamma_threshold([bandpass(lfp, low, high, 250) for low, high in GAMMA_BANDS])
    durations = [burst_duration(bandpass(lfp, low, high, 250), 250, threshold) for low, high in BANDS[2:]]
    return powers + durations
