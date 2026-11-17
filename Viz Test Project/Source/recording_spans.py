from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RecordingSpan:
    start: float
    end: float

    def validate(self, duration):
        if (not all(math.isfinite(n) for n in (self.start, self.end, duration))
                or not 0 <= self.start < self.end <= duration):
            raise ValueError('Span times must satisfy 0 ≤ start < end ≤ recording duration')


class SpanStore:
    def __init__(self, flags):
        self.flags = flags

    def load(self, view):
        row = None
        if self.flags.connection is not None or self.flags.path.exists():
            connection = self.flags.connect()
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recording_spans'").fetchone():
                row = connection.execute('SELECT start_seconds,end_seconds FROM recording_spans WHERE recording_id=?',
                                         (self.flags.identity(view)[0],)).fetchone()
        if row is None and view.duration == 0:return None
        span = RecordingSpan(*row) if row is not None else RecordingSpan(0., view.duration)
        span.validate(view.duration)
        return span

    def save(self, view, span):
        span.validate(view.duration)
        key, sources = self.flags.identity(view)
        with self.flags.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS recording_spans (
                recording_id TEXT PRIMARY KEY,
                source_files TEXT NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
            )''')
            connection.execute('''INSERT INTO recording_spans
                (recording_id,source_files,start_seconds,end_seconds) VALUES (?,?,?,?)
                ON CONFLICT(recording_id) DO UPDATE SET start_seconds=excluded.start_seconds,
                end_seconds=excluded.end_seconds,updated_at=strftime('%Y-%m-%d %H:%M:%f','now')''',
                (key, sources, span.start, span.end))
