import hashlib
import json
from pathlib import Path
import sqlite3
import library_paths


class RecordingFlags:
    def __init__(self, root):
        root = Path(root).resolve()
        if root.is_file():
            root = root.parent
        self.root = library_paths.library_root(root)
        self.path = library_paths.database_path(self.root)
        self.connection = None

    def identity(self, view):
        return self.paths_identity(s.path for s in view.sources)

    def paths_identity(self, paths):
        paths = sorted({str(Path(p).resolve().relative_to(self.root)) for p in paths})
        sources = json.dumps(paths, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(sources.encode('utf-8')).hexdigest(), sources

    def connect(self):
        if self.connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=1)
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS recording_flags (
                    recording_id TEXT PRIMARY KEY,
                    patient TEXT NOT NULL,
                    recording TEXT NOT NULL,
                    source_files TEXT NOT NULL,
                    good INTEGER NOT NULL CHECK (good IN (0, 1)),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS patient_channels (
                    patient_key TEXT PRIMARY KEY,
                    channel_selectors TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS patient_channel_modes (
                    patient_key TEXT PRIMARY KEY,
                    automatic INTEGER NOT NULL CHECK (automatic IN (0, 1))
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS recording_flag_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recording_id TEXT NOT NULL,
                    patient TEXT NOT NULL,
                    recording TEXT NOT NULL,
                    old_good INTEGER,
                    new_good INTEGER NOT NULL CHECK (new_good IN (0, 1)),
                    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
                    origin TEXT NOT NULL DEFAULT 'app'
                )''')
                connection.commit()
            except sqlite3.Error:
                connection.close()
                raise
            self.connection = connection
        return self.connection

    def is_good(self, view):
        if self.connection is None and not self.path.exists():
            return False
        key, _ = self.identity(view)
        row = self.connect().execute('SELECT good FROM recording_flags WHERE recording_id=?', (key,)).fetchone()
        return bool(row[0]) if row else False

    def good_recordings(self):
        if self.connection is None and not self.path.exists():
            return {}
        return {key: frozenset(json.loads(sources)) for key, sources in
                self.connect().execute('SELECT recording_id, source_files FROM recording_flags WHERE good=1')}

    def set_good(self, view, patient, good):
        key, sources = self.identity(view)
        connection = self.connect()
        with connection:
            previous = connection.execute('SELECT good FROM recording_flags WHERE recording_id=?', (key,)).fetchone()
            connection.execute('''INSERT INTO recording_flags (recording_id, patient, recording, source_files, good)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(recording_id) DO UPDATE SET
                patient=excluded.patient, recording=excluded.recording, source_files=excluded.source_files,
                good=excluded.good, updated_at=CURRENT_TIMESTAMP''',
                (key, patient or '', view.label, sources, int(good)))
            if previous is None or previous[0] != int(good):
                connection.execute('''INSERT INTO recording_flag_history
                    (recording_id,patient,recording,old_good,new_good) VALUES (?,?,?,?,?)''',
                    (key, patient or '', view.label, previous[0] if previous else None, int(good)))

    @staticmethod
    def channel_selector(key):
        name = ' '.join(key[0].split()).casefold()
        return ('name', name) if any(c.isalpha() for c in name) else ('id', key[1])

    @staticmethod
    def patient_scope(source_files):
        scopes = set()
        for relative in source_files:
            parts = library_paths.patient_parts(Path(relative).parts)
            scopes.add(str(Path(*parts)) if parts else '.')
        if len(scopes) != 1:raise ValueError('Channel preferences must belong to one patient')
        return scopes.pop()

    def channel_patient(self, view):
        _, sources = self.identity(view)
        return self.patient_scope(json.loads(sources))

    def patient_visible_channels(self, view):
        if self.connection is None and not self.path.exists():return None
        row = self.connect().execute('SELECT channel_selectors FROM patient_channels WHERE patient_key=?',
                                     (self.channel_patient(view),)).fetchone()
        if row is None:return None
        selectors = json.loads(row[0])
        if not isinstance(selectors, list) or any(not isinstance(s, list) or len(s) != 2 or s[0] not in ('name', 'id') for s in selectors):
            raise ValueError('Invalid saved patient channel selection')
        return {tuple(s) for s in selectors}

    def set_patient_visible_channels(self, view, selectors):
        patient = self.channel_patient(view)
        value = json.dumps(sorted(set(selectors)), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        with self.connect() as connection:
            connection.execute('''INSERT INTO patient_channels (patient_key,channel_selectors) VALUES (?,?)
                ON CONFLICT(patient_key) DO UPDATE SET channel_selectors=excluded.channel_selectors,
                updated_at=strftime('%Y-%m-%d %H:%M:%f','now')''', (patient, value))
            connection.execute('INSERT OR REPLACE INTO patient_channel_modes VALUES (?,0)', (patient,))

    def automatic_channels(self, view):
        if self.connection is None and not self.path.exists():return True
        row = self.connect().execute('SELECT automatic FROM patient_channel_modes WHERE patient_key=?',
                                     (self.channel_patient(view),)).fetchone()
        return bool(row[0]) if row is not None else True

    def set_automatic_channels(self, view, enabled):
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO patient_channel_modes VALUES (?,?)',
                               (self.channel_patient(view), int(enabled)))

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
