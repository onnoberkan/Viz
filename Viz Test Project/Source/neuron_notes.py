"""User notes for recordings, stored only in Viz Test's annotation database."""

import sqlite3


class NeuronNotes:
    def __init__(self, flags):
        self.flags = flags

    def load(self, view):
        if self.flags.connection is None and not self.flags.path.exists():
            return ''
        connection = self.flags.connect()
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='neuron_notes'").fetchone():
            return ''
        row = connection.execute('SELECT note FROM neuron_notes WHERE recording_id=?',
                                 (self.flags.identity(view)[0],)).fetchone()
        return row[0] if row else ''

    def save(self, view, note):
        if not isinstance(note, str):
            raise TypeError('Neuron note must be text')
        key, sources = self.flags.identity(view)
        with self.flags.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS neuron_notes (
                recording_id TEXT PRIMARY KEY,
                source_files TEXT NOT NULL,
                note TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            connection.execute('''INSERT INTO neuron_notes (recording_id,source_files,note)
                VALUES (?,?,?) ON CONFLICT(recording_id) DO UPDATE SET
                source_files=excluded.source_files, note=excluded.note, updated_at=CURRENT_TIMESTAMP''',
                (key, sources, note))
