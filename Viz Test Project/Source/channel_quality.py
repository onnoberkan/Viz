import base64
import dataclasses
from dataclasses import dataclass
import json
import math
import mmap
from pathlib import Path
import re
import sqlite3
import threading
import zlib

import numpy as np
from scipy import ndimage, signal
import alpha_reader as reader

FIELD = re.compile(r'^(?:c?ecog|c?lfp)(?:[ _/-]*\d|[ _/-]|$)', re.I)
FIELD_INPUT = re.compile(r'ecog|eeg|lfp|macro|strip', re.I)
WELCH_SECONDS = 256/250
FIELD_RATE, SPIKE_RATE = 1000., 8000.
FIELD_TOP = 400.
LINE_FROM = 45.
NOISY_LINE = .1
LINE_LIMIT = .5
EXPONENT_LIMIT = .5
CONNECTED_EXPONENT = 1.
SLOW_LIMIT = .7
SPIKE_SD = 5.
SPIKE_RATE_LIMIT = 1.
PERIODIC_LIMIT = 100.
RHYTHM_LIMIT = .5
CHUNK = 1 << 22
VERSION = 3
SAMPLED_RECORDINGS = 6
NEURAL, NOISY, BAD = '✓', '⚠', '✕'


def candidate(channel):
    return bool(FIELD.search(channel.name) or (
        channel.fs >= reader.MIN_FS and reader.NEURAL.search(channel.name)
        and not reader.EXCLUDE.search(channel.name)))


def kind(name, fs):
    if re.search('emg', name, re.I):return 'other'
    if FIELD_INPUT.search(name):return 'field'
    if fs >= reader.MIN_FS and reader.NEURAL.search(name) and not reader.EXCLUDE.search(name):return 'spike'
    return 'other'


@dataclass(frozen=True)
class Quality:
    selected: bool
    label: str
    detail: str
    verdict: str = ''
    spectrum: tuple | None = dataclasses.field(default=None, compare=False, repr=False)


def encode(values, dtype):
    return base64.b64encode(np.asarray(values, dtype=dtype).tobytes()).decode('ascii')


def decode(text, dtype):
    return np.frombuffer(base64.b64decode(text), dtype=dtype)


def lines(power, size, ratio):
    floor = ndimage.median_filter(power, size=size, mode='nearest')
    return floor, np.convolve(power > ratio*floor, np.ones(3), 'same') > 0


def field_measures(freq, power, top):
    band = (freq >= 1) & (freq <= top)
    f, p = freq[band], power[band]
    total = float(p.sum())
    if top < 60 or len(f) < 40 or total <= 0:return None
    step = float(freq[1]-freq[0])
    floor, peaks = lines(p, 2*round(5/step)+1, 4)
    bands = [(f >= a) & (f <= b) for a, b in ((2, 20), (min(100, top*.5), min(300, top*.9)))]
    level = [max(float(np.median(floor[band])), 1e-30) for band in bands]
    spacing = math.log10(float(np.median(f[bands[1]]))/float(np.median(f[bands[0]])))
    return dict(line=float(np.clip(p-floor, 0, None)[peaks & (f >= LINE_FROM)].sum())/total,
                slow=float(p[f < 4].sum())/total, top=float(top), exponent=math.log10(level[0]/level[1])/spacing)


def tone_share(freq, power, top, low=300, high=3000):
    keep = len(power)//2*2
    freq, power = freq[:keep].reshape(-1, 2).mean(1), power[:keep].reshape(-1, 2).sum(1)
    power = power[(freq >= low) & (freq <= min(high, top))]
    total = float(power.sum())
    if total <= 0 or len(power) < 25:return 0.
    floor, peaks = lines(power, 25, 5)
    return float(np.clip(power - floor, 0, None)[peaks].sum()) / total


def spectrum(m):
    if 'psd' not in m:return None
    power = 10.**decode(m['psd'], '<f2').astype(float)
    freq = np.arange(len(power))*m['step']
    keep = np.isfinite(power) & (freq > 0)
    return freq[keep], power[keep]


class Downsampler:
    def __init__(self, fs, rate):
        self.k = max(1, int(fs // rate));self.fs = fs/self.k
        self.sos = signal.cheby1(8, .05, .8/self.k, output='sos') if self.k > 1 else None

    def run(self, chunks):
        out, state, phase = [], None, 0
        for chunk in chunks:
            x = chunk.astype(np.float64)
            if self.sos is not None:
                if state is None:state = signal.sosfilt_zi(self.sos)*x[0]
                x, state = signal.sosfilt(self.sos, x, zi=state)
            out.append(x[phase::self.k].astype(np.float32));phase = (phase - len(x)) % self.k
        return np.concatenate(out) if out else np.zeros(0, np.float32)


def welch_runs(runs, fs, seconds=WELCH_SECONDS, shortest=.5):
    width = max(16, round(fs*seconds));longest = max((len(y) for y in runs), default=0)
    if longest < width:
        if longest < fs*shortest:return None, None
        width = longest
    step = width - width//2;power = None;count = 0
    for y in runs:
        if len(y) < width:continue
        freq, p = signal.welch(y, fs, nperseg=width, noverlap=width//2, nfft=width)
        n = (len(y)-width)//step + 1
        power = p*n if power is None else power + p*n;count += n
    return freq, power/count


def notched_spectrum(channel, cache=None, cancelled=lambda: False):
    """Welch spectrum after filtering continuous, downsampled recording runs."""
    resolved = reader.resolve_channels([channel], cache, cancelled)[0]
    spike = kind(resolved.name, resolved.fs) == 'spike'
    down = Downsampler(resolved.fs, SPIKE_RATE if spike else FIELD_RATE)
    if down.fs <= 120.:
        raise ValueError(f'{resolved.name} is sampled too slowly for a 60 Hz notch.')
    sos = signal.tf2sos(*signal.iirnotch(60., 30., fs=down.fs))
    filtered = []
    for part in resolved.parts:
        if cancelled():raise InterruptedError('Spectrum selection changed')
        source = part.source
        stat = source.path.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (source.size, source.mtime_ns, source.ctime_ns):
            raise ValueError('Recording changed on disk; refresh the patient')
        with source.path.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            samples = reader.samples(part, 0, part.run.count, raw)
        y = down.run(chunks(samples))
        if len(y):
            state = signal.sosfilt_zi(sos) * float(y[0])
            y, _ = signal.sosfilt(sos, y, zi=state)
            filtered.append(y)
    if cancelled():raise InterruptedError('Spectrum selection changed')
    freq, power = welch_runs(filtered, down.fs)
    if freq is None:return None
    top = min(FIELD_TOP if not spike else SPIKE_RATE*.4, down.fs*(.4 if down.k > 1 else .45))
    shown = (freq > 0) & (freq <= top) & np.isfinite(power)
    return freq[shown], power[shown]


def chunks(y, size=CHUNK):
    for first in range(0, len(y), size):yield y[first:first+size]


def highpassed(y, sos, size):
    state = None
    for chunk in chunks(y, size):
        x = chunk.astype(np.float64)
        if state is None:state = signal.sosfilt_zi(sos)*x[0]
        x, state = signal.sosfilt(sos, x, zi=state)
        yield x.astype(np.float32)


def measure(source, channel_id, raw, cancelled=lambda: False):
    definition = source.definitions[channel_id]
    runs = [run for run in source.runs.get(channel_id, []) if run.count > 0]
    def arrays():
        for run in runs:
            if cancelled():raise InterruptedError('Channel selection changed')
            yield reader.samples(reader.Part(source, run, 0., None, channel_id), 0, run.count, raw)
    return measure_runs(definition['name'], definition['fs'], arrays)


def measure_runs(name, fs, arrays):
    k = kind(name, fs);spike = k == 'spike'
    down = Downsampler(fs, SPIKE_RATE if spike else FIELD_RATE)
    total = rails = plateau = 0;low, high = 32767, -32768;decimated = []
    highpass = signal.butter(4, 300, 'highpass', fs=fs, output='sos') if spike and fs >= 1000 else None
    bin_size = max(1, round(fs*.01));size = bin_size*max(1, CHUNK//bin_size)
    deviations, envelopes, seconds = [], [], 0.
    for y in arrays():
        total += len(y)
        low, high = min(low, int(y.min())), max(high, int(y.max()))
        rails += int(np.count_nonzero((y >= 32760) | (y <= -32760)))
        decimated.append(down.run(chunks(y)))
        if highpass is not None and len(y) >= 2*bin_size:
            envelope = []
            for x in highpassed(y, highpass, size):
                deviations.append(float(np.median(np.abs(x))))
                n = len(x)//bin_size
                envelope.append(np.square(x[:n*bin_size].astype(np.float64)).reshape(n, bin_size).mean(1))
            envelopes.append(np.concatenate(envelope));seconds += len(y)/fs
    sd = float(np.median(deviations))/.6745 if deviations else 0.
    events = 0
    for y in arrays():
        plateau += int(np.count_nonzero((y == low) | (y == high)))
        if highpass is not None and len(y) >= 2*bin_size and sd > 0:
            for x in highpassed(y, highpass, size):
                over = np.flatnonzero(np.abs(x) > SPIKE_SD*sd)
                if len(over):events += 1 + int(np.count_nonzero(np.diff(over) > fs*1e-3))
    m = dict(name=name, fs=fs, n=total, low=low, high=high, rails=rails/max(total, 1), plateau=plateau/max(total, 1))
    freq, power = welch_runs(decimated, down.fs)
    if freq is not None:
        top = min(FIELD_TOP if not spike else SPIKE_RATE*.4, down.fs*(.4 if down.k > 1 else .45))
        shown = freq <= top
        with np.errstate(divide='ignore'):
            m.update(step=float(freq[1]-freq[0]), psd=encode(np.log10(power[shown]), '<f2'), rate=down.fs, top=float(top))
        if spike:m['tone'] = tone_share(freq, power, top)
        elif (measured := field_measures(freq, power, min(top, FIELD_TOP))):m.update(measured)
    if highpass is not None and seconds:
        m['events'] = events/seconds
        m['periodic'] = periodicity(envelopes)
        m['rhythm'] = rhythm(envelopes)
    return m


def rhythm(envelopes, seconds=10):
    peaks = []
    for envelope in envelopes:
        if len(envelope) < 200:continue
        envelope = envelope - ndimage.uniform_filter1d(envelope, 200, mode='nearest')
        for first in range(0, len(envelope)-199, seconds*100):
            e = envelope[first:first+seconds*100]
            if len(e) < 200:break
            e = e - e.mean();energy = float(np.dot(e, e));high = min(170, int(len(e)*.6))
            if energy <= 0 or high < 36:continue
            ac = np.array([np.dot(e[:-lag], e[lag:]) for lag in range(32, high+2)])/energy
            i = int(np.argmax(ac[1:-1])) + 1
            peaks.append(float(ac[i]) if ac[i] >= ac[i-1] and ac[i] >= ac[i+1] else 0.)
    return float(np.median(peaks)) if peaks else None


def periodicity(envelopes):
    usable = [e - e.mean() for e in envelopes if len(e) >= 200]
    freq, power = welch_runs(usable, 100., seconds=20.48, shortest=2.)
    if freq is None:return None
    band, reference = (freq >= .6) & (freq <= 3), (freq >= .3) & (freq <= 10)
    floor = float(np.median(power[reference]))
    return float(power[band].max()/floor) if floor > 0 and band.any() else None


def classify(m):
    k = kind(m['name'], m['fs']);seconds = m['n']/m['fs']
    shape = spectrum(m)
    made = lambda selected, mark, text, detail, verdict:Quality(selected, f'{mark} {text}'.strip(), detail, verdict, shape)
    if not m['n']:
        return Quality(False, 'No signal', 'No recorded samples.', 'flat')
    if m['high'] - m['low'] <= 1:
        return Quality(False, f'{BAD} Flat / unused', f'All {seconds:.1f} s lie within one acquisition count.', 'flat')
    if m['rails'] >= .1 or (k == 'field' and m['high']-m['low'] >= 100 and m['plateau'] >= .2):
        return made(False, BAD, 'Clipped', f'{max(m["rails"], m["plateau"]):.0%} of {seconds:.1f} s sit at the extremes.', 'clipped')
    if shape is None:
        return Quality(True, 'Too short to check', f'Recorded runs total {seconds:.2f} s; half a second is needed.', 'unchecked')
    if k == 'spike':
        tone, events, periodic, rhythmic = m.get('tone') if m['top'] >= 2500 else None, m.get('events'), m.get('periodic'), m.get('rhythm')
        parts = ([f'{tone:.0%} of 300–3000 Hz power in narrow lines'] if tone is not None else []) + (
            [f'{events:.1f} spike events/s beyond {SPIKE_SD:g} SD'] if events is not None else []) + (
            [f'envelope periodicity {periodic:.0f}× (0.6–3 Hz)'] if periodic is not None else []) + (
            [f'envelope rhythm {rhythmic:.2f} (0.33–1.7 s)'] if rhythmic is not None else [])
        detail = f'Entire recording ({seconds:.1f} s): ' + '; '.join(parts or ['too slow for spike-band checks']) + '.'
        if tone is not None and tone >= LINE_LIMIT:return made(False, BAD, 'Line noise', detail, 'unconnected')
        if periodic is not None and periodic >= PERIODIC_LIMIT:return made(False, BAD, 'Periodic artifact', detail, 'noise')
        if events is not None and events < SPIKE_RATE_LIMIT:return made(False, NOISY, 'No spikes', detail, 'quiet')
        if rhythmic is not None and rhythmic >= RHYTHM_LIMIT:return made(True, NOISY, 'Rhythmic artifact', detail, 'noisy')
        return made(True, NEURAL, 'Neural', detail, 'neural')
    if 'line' not in m:
        return made(True, '', 'Too slow to check' if k == 'field' else 'Signal', f'Entire recording ({seconds:.1f} s); sampled too slowly for a 1/f check.', 'unchecked')
    detail = (f'Entire recording ({seconds:.1f} s): {m["line"]:.0%} of 1–{m["top"]:g} Hz power in lines ≥{LINE_FROM:g} Hz; '
              f'1/f exponent {m["exponent"]:.2f}; {m["slow"]:.0%} below 4 Hz.')
    if k == 'other':
        if m['line'] >= LINE_LIMIT:return made(False, BAD, 'Line noise', detail, 'unconnected')
        return made(True, '', 'Signal', detail, 'signal')
    if m['line'] >= LINE_LIMIT:
        if m['exponent'] >= CONNECTED_EXPONENT:return made(False, BAD, 'Mostly line noise', detail, 'noise')
        return made(False, BAD, 'Unconnected? Line noise', detail, 'unconnected')
    if m['exponent'] < EXPONENT_LIMIT:return made(False, BAD, 'Unconnected? Flat spectrum', detail, 'unconnected')
    if m['line'] >= NOISY_LINE:return made(True, NOISY, 'Neural + line noise', detail, 'noisy')
    if m['slow'] >= SLOW_LIMIT:return made(True, NOISY, 'Neural + slow artifact', detail, 'noisy')
    return made(True, NEURAL, 'Neural', detail, 'neural')


class Store:
    def __init__(self, path, root=None):
        self.path, self.root = Path(path), Path(root) if root else None
        self.local = threading.local()

    def connection(self):
        if not hasattr(self.local, 'connection'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('CREATE TABLE IF NOT EXISTS measured (path TEXT PRIMARY KEY, signature TEXT, version INTEGER, payload BLOB)')
            self.local.connection = connection
        return self.local.connection

    def key(self, path):
        try:return str(Path(path).relative_to(self.root)) if self.root else str(path)
        except ValueError:return str(path)

    @staticmethod
    def signature(path):
        stat = Path(path).stat()
        return json.dumps([stat.st_size, stat.st_mtime_ns])

    def get(self, path):
        try:
            row = self.connection().execute('SELECT signature, version, payload FROM measured WHERE path=?', (self.key(path),)).fetchone()
            if row and row[1] == VERSION and reader.same_file(json.loads(row[0]), json.loads(self.signature(path))):
                return {int(ch):m for ch, m in json.loads(zlib.decompress(row[2])).items()}
        except (sqlite3.Error, ValueError, TypeError, zlib.error):pass
        return None

    def put(self, path, signature, measured):
        try:
            self.connection().execute('REPLACE INTO measured VALUES (?,?,?,?)', (self.key(path), signature, VERSION,
                                      zlib.compress(json.dumps(measured, separators=(',', ':')).encode('utf-8'))))
        except sqlite3.Error:pass


def prefetch(path, cancelled=lambda: False):
    buffer = bytearray(1 << 24)
    with Path(path).open('rb', buffering=0) as handle:
        while handle.readinto(buffer):
            if cancelled():raise InterruptedError('Channel selection changed')


def measurements(path, store=None, cache=None, cancelled=lambda: False):
    path = Path(path)
    if store is not None and (saved := store.get(path)) is not None:return saved
    signature = Store.signature(path)
    prefetch(path, cancelled)
    source = cache.source(path, include_all=True) if cache else reader.index_file(path, include_all=True)
    if not reader.same_file([source.size, source.mtime_ns], json.loads(signature)):
        raise ValueError('Recording changed on disk; refresh the patient')
    result = {}
    with path.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        for ch in sorted(source.runs):
            if any(run.count > 0 for run in source.runs[ch]):result[ch] = measure(source, ch, raw, cancelled)
    if not reader.same_file(json.loads(Store.signature(path)), json.loads(signature)):raise ValueError('Recording changed on disk; refresh the patient')
    if store is not None:store.put(path, signature, result)
    return result


def qualities(channels, cache=None, cancelled=lambda: False, store=None):
    measured, result = {}, {}
    for channel in channels:
        paths = {p.source.path for p in channel.parts} | {s.path for s, _ in channel.pending}
        found = []
        for path in sorted(paths, key=str):
            if cancelled():raise InterruptedError('Channel selection changed')
            if path not in measured:measured[path] = measurements(path, store, cache, cancelled)
            m = measured[path].get(channel.key[1])
            if m and m['name'] == channel.name and m['fs'] == channel.fs:found.append(m)
        result[channel.key] = classify(max(found, key=lambda m:m['n'])) if found else Quality(False, 'No signal', 'No recorded samples.', 'flat')
    return result


def screen(channels, cache=None, cancelled=lambda: False, store=None):
    return qualities([c for c in channels if candidate(c)], cache, cancelled, store)


def label(view, cache=None, cancelled=lambda: False, store=None):
    return qualities(reader.channel_options(view, cache), cache, cancelled, store)


@dataclass
class Choice:
    selectors: set
    screened: list
    votes: dict


def choose(views, current, saved, selector, cache=None, cancelled=lambda: False, store=None):
    others = [v for v in views if v is not current]
    picks = sorted({round(i*(len(others)-1)/max(1, SAMPLED_RECORDINGS-2)) for i in range(SAMPLED_RECORDINGS-1)}) if others else []
    votes, fields, screened = {}, set(), []
    for view in [current] + [others[i] for i in picks]:
        channels = reader.channel_options(view, cache)
        result = screen(channels, cache, cancelled, store=store)
        screened.append((view, result))
        for channel in channels:
            if channel.key not in result:continue
            key = selector(channel.key)
            passed, total = votes.get(key, (0, 0))
            votes[key] = (passed + result[channel.key].selected, total + 1)
            if FIELD.search(channel.name):fields.add(key)
    chosen = {key for key,(passed,total) in votes.items()
              if (passed*2 > total if key in fields else passed > 0) and (key in fields or saved is None or key in saved)}
    if saved is not None:chosen |= saved - set(votes)
    return Choice(chosen, screened, votes)
