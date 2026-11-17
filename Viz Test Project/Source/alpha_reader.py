from dataclasses import dataclass, field, replace
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import math
import mmap
import os
import re
import sqlite3
import struct
import tempfile
import threading
import zlib
import numpy as np
import library_paths

MIN_FS = 10000.
AO_NAME = re.compile(r'^(?P<side>[LR])T(?P<trajectory>\d+)D(?P<depth>[+-]?\d+(?:\.\d+)?)F(?P<file>\d+)$', re.I)
NEURAL = re.compile(r'^(?:c?spk|spike|electrode|micro(?:electrode)?|c?raw|continuous(?:chan(?:nel)?)?)(?:[_ /-]*\d|[_ /-]|$)', re.I)
EXCLUDE = re.compile(r'ecog|eeg|lfp|emg|macro|aux|analog|digital|ttl|trigger|stim', re.I)
SKIP = {'00 Viz', '00 Viz.app', 'excluded'}
FORMATS = ('.mpx', '.map', '.plx')
PLX_HEADER = 7504

def same_file(first, second):
    return first[0] == second[0] and abs(first[1] - second[1]) < 1_000_000_000

def natural(value):
    return [int(v) if v.isdigit() else v.casefold() for v in re.split(r'(\d+)', str(value))]

def parse_name(stem):
    m = AO_NAME.fullmatch(stem)
    return dict(side=m['side'].upper(), trajectory=int(m['trajectory']), depth=float(m['depth']), index=int(m['file'])) if m else None

def raw_files(root):
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in FORMATS and not root.name.startswith('.') else []
    folders = [p for p in root.iterdir() if library_paths.is_patient(p)] if library_paths.is_library(root) else [root]
    found = []
    for folder in folders:
        for base, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP]
            found += [Path(base) / name for name in files
                      if not name.startswith('.') and Path(name).suffix.lower() in FORMATS]
    return sorted(found, key=lambda p: natural(str(p)))

@dataclass
class Run:
    ticks: int
    packets: np.ndarray
    fs: float
    @property
    def count(self): return int(self.packets[:, 2].sum())

@dataclass
class Source:
    path: Path
    start: float
    end: float
    definitions: dict
    runs: dict
    size: int
    mtime_ns: int
    issue: str = ''
    ctime_ns: int = 0
    @property
    def name(self): return parse_name(library_paths.acquisition_name(self.path))

@dataclass
class Part:
    source: Source
    run: Run
    start: float
    depth: object
    channel_id: int = 0
    @property
    def end(self): return self.start + self.run.count / self.run.fs

@dataclass
class Channel:
    name: str
    fs: float
    parts: list = field(default_factory=list)
    identity: tuple | None = None
    pending: list = field(default_factory=list)
    @property
    def key(self):
        if self.identity is not None:return self.identity
        part = self.parts[0]
        definition = part.source.definitions[part.channel_id]
        return (self.name, part.channel_id, self.fs, definition['amp'], definition['gain'])

@dataclass
class Trajectory:
    label: str
    sources: list
    channels: list
    duration: float


def channel_options(view, cache=None):
    channels = {c.key: c for c in view.channels}
    defaults = set(channels)
    cursor = 0.
    for source in view.sources:
        counts = recorded_counts(source, cache)
        for ch, definition in source.definitions.items():
            fs = definition['fs']
            if not math.isfinite(fs) or fs <= 0 or counts.get(ch,0) <= 0:continue
            key = (definition['name'], ch, fs, definition['amp'], definition['gain'])
            if key in defaults:continue
            channel = channels.setdefault(key, Channel(definition['name'], fs, identity=key))
            channel.pending.append((source, cursor))
        cursor += max(0., source.end-source.start)
    extras = sorted((c for key,c in channels.items() if key not in defaults),key=lambda c:(natural(c.name),c.fs))
    return list(view.channels) + extras


def recorded_counts(source, cache=None):
    return {ch: d['sample_count'] for ch, d in source.definitions.items()}


def resolve_channels(channels, cache=None, cancelled=lambda: False):
    resolved = {c.key: Channel(c.name,c.fs,identity=c.key) if c.pending else c for c in channels}
    sources = {}
    for channel in channels:
        for source,cursor in channel.pending:
            sources.setdefault(source.path, (source,cursor,[]))[2].append(channel.key)
    for path,(original,cursor,keys) in sources.items():
        if cancelled():raise InterruptedError('Channel selection changed')
        source = cache.source(path,include_all=True) if cache else index_file(path,include_all=True)
        if not same_file((source.size,source.mtime_ns),(original.size,original.mtime_ns)):
            raise ValueError('Recording changed on disk; refresh the patient')
        source = replace(source,runs={key[1]:source.runs[key[1]] for key in keys if key[1] in source.runs})
        depth = original.name['depth'] if original.name else None
        for key in keys:
            ch = key[1]
            for run in source.runs.get(ch,[]):
                resolved[key].parts.append(Part(source,run,cursor+run.ticks/run.fs-original.start,depth,ch))
    if cancelled():raise InterruptedError('Channel selection changed')
    return [resolved[c.key] for c in channels]


class SourceCache:
    def __init__(self, path, *, lazy=False):
        self.path = Path(path)
        self.local = threading.local()
        if not lazy:self.connection()

    def connection(self):
        if not hasattr(self.local, 'connection'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            try:
                connection.execute('PRAGMA busy_timeout=5000')
                connection.execute('CREATE TABLE IF NOT EXISTS indexed (path TEXT PRIMARY KEY, signature TEXT, payload BLOB)')
                connection.execute('CREATE TABLE IF NOT EXISTS indexed_all (path TEXT PRIMARY KEY, signature TEXT, payload BLOB)')
            except sqlite3.Error:
                connection.close()
                raise
            self.local.connection = connection
        return self.local.connection

    @staticmethod
    def signature(path):
        stat = path.stat()
        return json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino])

    @staticmethod
    def same_signature(saved, current):
        try:
            return same_file(json.loads(saved)[:2], json.loads(current)[:2])
        except (ValueError, TypeError):
            return False

    def source(self, path, include_all=False):
        path = Path(path)
        signature = self.signature(path)
        table = 'indexed_all' if include_all else 'indexed'
        try:
            row = self.connection().execute(f'SELECT signature, payload FROM {table} WHERE path=?', (str(path),)).fetchone()
            if row and self.same_signature(row[0], signature):
                data = json.loads(zlib.decompress(row[1]))
                runs = {int(ch): [Run(ticks, np.frombuffer(base64.b64decode(packets), dtype='<i8').reshape(-1, 3).copy(), fs)
                                  for ticks, fs, packets in entries] for ch, entries in data['runs'].items()}
                definitions = {int(ch): definition for ch, definition in data['definitions'].items()}
                return Source(path, data['start'], data['end'], definitions,
                              runs, data['size'], data['mtime_ns'], ctime_ns=data['ctime_ns'])
        except (sqlite3.Error, ValueError, KeyError, TypeError, zlib.error):
            pass
        source = index_file(path, include_all=include_all)
        data = dict(start=source.start, end=source.end,
                    definitions=source.definitions, size=source.size, mtime_ns=source.mtime_ns, ctime_ns=source.ctime_ns,
                    runs={ch: [(run.ticks, run.fs, base64.b64encode(run.packets.astype('<i8', copy=False).tobytes()).decode('ascii'))
                               for run in runs] for ch, runs in source.runs.items()})
        try:
            self.connection().execute(f'REPLACE INTO {table} VALUES (?,?,?)',
                                      (str(path), signature, zlib.compress(json.dumps(data).encode('utf-8'))))
        except sqlite3.Error:
            pass
        return source


class WaveformCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.mapped = {}

    def path_for(self, source, channel_id):
        key = json.dumps([str(source.path), source.size, source.mtime_ns, channel_id])
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
        return self.root / digest[:2] / (digest[2:] + '.i16')

    def contains(self, source, channel_id):
        if channel_id in source.runs:
            count = sum(run.count for run in source.runs[channel_id])
        else:
            count = source.definitions.get(channel_id, {}).get('sample_count', 0)
        path = self.path_for(source, channel_id)
        try:
            return count > 0 and path.is_file() and path.stat().st_size == count * 2
        except OSError:
            return False

    def ensure(self, source, channel_id):
        runs = source.runs[channel_id]
        count = sum(run.count for run in runs)
        path = self.path_for(source, channel_id)
        if path.is_file() and path.stat().st_size == count * 2:
            return path
        stat = source.path.stat()
        if not same_file((stat.st_size, stat.st_mtime_ns), (source.size, source.mtime_ns)):
            raise ValueError('Recording changed on disk; refresh the patient')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.stem + '-', suffix='.tmp', dir=path.parent)
        try:
            os.ftruncate(fd, count * 2)
            os.close(fd);fd = -1
            output = np.memmap(temporary, dtype='<i2', mode='r+', shape=(count,))
            cursor = 0
            with source.path.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                for run in runs:
                    for _, offset, length in run.packets:
                        length = int(length)
                        output[cursor:cursor+length] = np.frombuffer(raw, dtype='<i2', count=length, offset=int(offset))
                        cursor += length
            if cursor != count:
                raise ValueError('Incomplete waveform cache')
            output.flush();del output
            stat = source.path.stat()
            if not same_file((stat.st_size, stat.st_mtime_ns), (source.size, source.mtime_ns)):
                raise ValueError('Recording changed on disk; refresh the patient')
            os.replace(temporary, path)
        finally:
            if fd >= 0: os.close(fd)
            if os.path.exists(temporary): os.unlink(temporary)
        return path

    def samples(self, part, first, last):
        source = part.source
        path = self.ensure(source, part.channel_id)
        mapped = self.mapped.get(path)
        if mapped is None:
            count = sum(run.count for run in source.runs[part.channel_id])
            mapped = np.memmap(path, dtype='<i2', mode='r', shape=(count,))
            if len(self.mapped) >= 128:
                self.mapped.pop(next(iter(self.mapped)))
            self.mapped[path] = mapped
        offset = 0
        for run in source.runs[part.channel_id]:
            if run is part.run:
                return mapped[offset+first:offset+last]
            offset += run.count
        raise ValueError('Waveform run missing from source index')


def index_file(path, include_all=False):
    path = Path(path)
    if path.suffix.lower() not in FORMATS:
        raise ValueError('Only MPX, MAP and PLX files are supported')
    if path.suffix.lower() == '.plx': return index_plx(path, include_all)
    stat = path.stat()
    if stat.st_size < 40: raise ValueError('Truncated Alpha Omega header')
    definitions, packets = {}, defaultdict(list)
    digital_inputs = set()
    with path.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        hlen, kind = struct.unpack_from('<Hc', raw)
        if kind != b'h' or hlen not in (40, 60): raise ValueError('Unsupported Alpha Omega header')
        version = 4 if hlen == 60 else 3
        if version == 4 and (len(raw) < 60 or raw[40] != 4): raise ValueError('Unsupported Alpha Omega format version')
        p = 0
        while p < len(raw):
            if p + 3 > len(raw): raise ValueError(f'Truncated block at byte {p}')
            length, kind = struct.unpack_from('<Hc', raw, p)
            if length == 65535:
                if raw[p:] != b'\xff'*5: raise ValueError('Invalid end marker')
                break
            end = p + length
            if length < 3 or end > len(raw): raise ValueError(f'Invalid block bounds at byte {p}')
            if kind == b'2':
                if length < 18: raise ValueError('Truncated channel definition')
                analog, input_, ch = struct.unpack_from('<hhh', raw, p+8)
                if ch in definitions or ch in digital_inputs:
                    raise ValueError('Changing channel definitions need separate interpretation')
                if analog and input_:
                    if length < 32: raise ValueError('Truncated analog definition')
                    mode, amp, rate, count, spike_mode = struct.unpack_from('<hffhh', raw, p+18)
                    if mode == 0:
                        if length < 38: raise ValueError('Truncated continuous definition')
                        name = raw[p+38:end].split(b'\0')[0].decode('latin1').strip()
                        definitions[ch] = dict(name=name, fs=float(rate)*1000, count=count, amp=amp, format_version=version,
                                               gain=struct.unpack_from('<h',raw,p+36)[0], configuration=bytes(raw[p+18:p+38]).hex())
                elif analog == 0 and input_ == 1:
                    if length < 30: raise ValueError('Truncated digital input definition')
                    digital_inputs.add(ch)
            elif kind == b'5' and version == 4:
                if length < 10: raise ValueError('Truncated MPX sample packet')
                unit, ch = struct.unpack_from('<Bh', raw, p+3)
                if ch in definitions:
                    if (length-10) % 2: raise ValueError('Unaligned MPX samples')
                    ticks = struct.unpack_from('<I', raw, end-4)[0]
                    packets[ch].append((ticks, p+6, (length-10)//2))
            elif kind == b'7' and version == 3:
                if length < 6 or struct.unpack_from('<h', raw, p+4)[0] != 1:
                    raise ValueError('Unsupported MAP multi-channel packet header')
                q = p+6
                while q < end:
                    if q+2 > end: raise ValueError('Truncated MAP channel marker')
                    ch = struct.unpack_from('<h', raw, q)[0]
                    if ch == -111:
                        if q+4 > end: raise ValueError('Truncated MAP event marker')
                        q += 4
                        continue
                    if ch in digital_inputs:
                        if q+8 > end: raise ValueError('Truncated MAP digital input event')
                        if struct.unpack_from('<h', raw, q+6)[0] not in (0, 1):
                            raise ValueError(f'Invalid MAP digital input state at byte {q}')
                        q += 8
                        continue
                    if ch not in definitions: raise ValueError(f'Unsupported MAP channel {ch} at byte {q}')
                    words = definitions[ch]['count']; stop = q+2+words*2
                    if words < 2 or stop > end: raise ValueError('Invalid MAP frame bounds')
                    ticks = struct.unpack_from('<I', raw, stop-4)[0]
                    packets[ch].append((ticks, q+2, words-2))
                    q = stop
            p = end
    return finished_source(path, stat, definitions, packets, include_all)


def index_plx(path, include_all=False):
    stat = path.stat()
    if stat.st_size < PLX_HEADER: raise ValueError('Truncated PLX header')
    definitions, packets, stops = {}, defaultdict(list), {}
    with path.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if raw[:4] != b'PLEX': raise ValueError('Unsupported PLX header')
        clock, spikes, events, slow = struct.unpack_from('<4i', raw, 136)
        p = PLX_HEADER + spikes*1020 + events*296
        if clock <= 0 or min(spikes, events, slow) < 0 or p + slow*296 > len(raw):
            raise ValueError('Invalid PLX header')
        for q in range(p, p + slow*296, 296):
            ch, rate, gain = struct.unpack_from('<3i', raw, q+32)
            definitions[ch] = dict(name=raw[q:q+32].split(b'\0')[0].decode('latin1').strip(), fs=float(rate), count=0, amp=0.,
                                   format_version='plx', gain=gain, configuration=bytes(raw[q+32:q+56]).hex())
        p += slow*296
        while p + 16 <= len(raw):
            kind, upper, lower, ch, _, waves, words = struct.unpack_from('<hHIhhhh', raw, p)
            count = waves*words
            if kind not in (1, 4, 5) or count < 0: raise ValueError(f'Invalid PLX block at byte {p}')
            end = p + 16 + count*2
            if end > len(raw): break
            if kind == 5 and count > 0 and ch in definitions:
                ticks = round(((upper << 32) | lower)*definitions[ch]['fs']/clock)
                start = ticks
                if ch in stops:
                    stop, stamped = stops[ch]
                    start = stop if abs(ticks-stamped) <= 1 else max(ticks, stop)
                packets[ch].append((start, p+16, count))
                stops[ch] = (start+count, ticks+count)
            p = end
    return finished_source(path, stat, definitions, packets, include_all)


def finished_source(path, stat, definitions, packets, include_all):
    runs = {}
    starts, ends = [], []
    for ch,definition in definitions.items():
        definition['sample_count'] = sum(row[2] for row in packets.get(ch,[]) if row[2]>0)
    for ch, rows in packets.items():
        fs = definitions[ch]['fs']
        if not math.isfinite(fs) or fs <= 0: continue
        rows = [r for r in rows if r[2] > 0]
        if not rows: continue
        starts.extend(r[0]/fs for r in rows); ends.extend((r[0]+r[2])/fs for r in rows)
        grouped, current, previous = [], [], None
        for row in rows:
            if previous is not None and row[0] != previous:
                grouped.append(current); current = []
            current.append(row); previous = row[0]+row[2]
        if current: grouped.append(current)
        name = definitions[ch]['name']
        if include_all or (fs >= MIN_FS and NEURAL.search(name) and not EXCLUDE.search(name)):
            runs[ch] = [Run(g[0][0], np.asarray(g, dtype=np.int64), fs) for g in grouped]
    return Source(path, min(starts, default=0.), max(ends, default=0.), definitions, runs, stat.st_size, stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)


def indexed(path, cache, include_all):
    try: return cache.source(path, include_all=include_all) if cache else index_file(path, include_all=include_all), None
    except (OSError, ValueError, struct.error) as error: return None, error

def recordings(files, cache=None, include_all=False):
    views, issues = [], []
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda path: indexed(path, cache, include_all), files))
    for path, (source, error) in zip(files, found):
        if error is not None:
            issues.append(f'{path.name}: {error}')
            source = Source(path, 0., 0., {}, {}, path.stat().st_size, path.stat().st_mtime_ns, str(error))
        depth = source.name['depth'] if source.name else None
        channels = {}
        for ch, runs in source.runs.items():
            definition = source.definitions[ch]
            key = (definition['name'], ch, definition['fs'], definition['amp'], definition['gain'])
            channel = channels.setdefault(key, Channel(definition['name'], definition['fs']))
            for run in runs:
                channel.parts.append(Part(source, run, run.ticks/run.fs-source.start, depth, ch))
        ordered = sorted(channels.values(), key=lambda c: (not bool(re.match(r'c?spk', c.name, re.I)), natural(c.name), c.fs))
        views.append(Trajectory(path.name, [source], ordered, max(0., source.end-source.start)))
    views.sort(key=lambda v: natural(str(v.sources[0].path)))
    return views, issues


def samples(part, first, last, raw):
    packets = part.run.packets
    stops = np.cumsum(packets[:,2]); starts = stops-packets[:,2]
    out = np.empty(last-first, dtype='<i2')
    for i in range(int(np.searchsorted(stops, first, side='right')), len(packets)):
        a, b = max(first, int(starts[i])), min(last, int(stops[i]))
        if b <= a: break
        offset = int(packets[i,1]) + 2*(a-int(starts[i]))
        out[a-first:b-first] = np.frombuffer(raw, dtype='<i2', count=b-a, offset=offset)
        if b == last: break
    return out


def read_channel(channel, lo, hi, wave_cache=None):
    result = []
    for part in channel.parts:
        a = max(0, min(part.run.count, math.ceil((lo-part.start)*part.run.fs-1e-7)))
        b = max(a, min(part.run.count, math.ceil((hi-part.start)*part.run.fs-1e-7)))
        if b <= a: continue
        source = part.source; st = source.path.stat()
        if not same_file((st.st_size, st.st_mtime_ns), (source.size, source.mtime_ns)): raise ValueError('Recording changed on disk; refresh the patient')
        try:
            y = wave_cache.samples(part,a,b) if wave_cache else None
        except OSError:
            y = None
        if y is None:
            with source.path.open('rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                y = samples(part,a,b,raw)
        x = np.arange(a,b,dtype=np.float64)
        x /= part.run.fs
        x += part.start
        result.append(dict(part=part,x=x,y=y))
    return result
