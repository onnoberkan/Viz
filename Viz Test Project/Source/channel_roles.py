"""User-assigned roles shared by matching PLX channels within a patient."""

import json
import re

import alpha_reader as reader
from recording_flags import RecordingFlags


ROLES = ('Spike', 'ECoG', 'EMG', 'Other')


def is_plx(view):
    return bool(view and view.sources and all(source.path.suffix.lower() == '.plx' for source in view.sources))


def inferred_role(channel):
    return inferred_role_name(channel.name, channel.fs)


def inferred_role_name(name, fs):
    if re.search('ecog', name, re.I):return 'ECoG'
    if re.search('emg', name, re.I):return 'EMG'
    if (fs >= reader.MIN_FS and reader.NEURAL.search(name)
            and not reader.EXCLUDE.search(name)):return 'Spike'
    return 'Other'


def role_for(channel, saved):
    return role_for_key(channel.key, saved)


def role_for_key(key, saved):
    key = tuple(key)
    if key in saved:return saved[key]
    selector = RecordingFlags.channel_selector(key)
    if selector in saved:return saved[selector]
    name, _, fs, *_ = key
    return inferred_role_name(name, fs)


class ChannelRoleStore:
    def __init__(self, flags):
        self.flags = flags

    def ensure(self, connection):
        with connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS patient_channel_labels (
            patient_key TEXT NOT NULL,
            channel_selector TEXT NOT NULL,
            label TEXT NOT NULL CHECK (label IN ('Spike','ECoG','EMG','Other')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (patient_key,channel_selector)
        )''')
            connection.execute('''CREATE TABLE IF NOT EXISTS channel_label_migrations (
                name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            if connection.execute("SELECT 1 FROM channel_label_migrations WHERE name='patient_labels_v1'").fetchone():return
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recording_channel_labels'").fetchone():
                for sources,channel,label,updated in connection.execute('''SELECT source_files,channel_key,label,updated_at
                        FROM recording_channel_labels ORDER BY updated_at,recording_id,channel_key'''):
                    patient=self.flags.patient_scope(json.loads(sources))
                    selector=self.flags.channel_selector(tuple(json.loads(channel)))
                    connection.execute('''INSERT INTO patient_channel_labels
                        (patient_key,channel_selector,label,updated_at) VALUES (?,?,?,?)
                        ON CONFLICT(patient_key,channel_selector) DO UPDATE SET
                        label=excluded.label,updated_at=excluded.updated_at''',
                        (patient,json.dumps(selector,ensure_ascii=False,separators=(',',':')),label,updated))
            connection.execute("INSERT INTO channel_label_migrations (name) VALUES ('patient_labels_v1')")

    def load(self, view):
        if not is_plx(view) or (self.flags.connection is None and not self.flags.path.exists()):return {}
        connection = self.flags.connect()
        self.ensure(connection)
        patient=self.flags.channel_patient(view)
        by_selector={tuple(json.loads(selector)):label for selector,label in connection.execute(
            'SELECT channel_selector,label FROM patient_channel_labels WHERE patient_key=?', (patient,))}
        return {channel.key:by_selector[selector] for channel in reader.channel_options(view)
                if (selector:=self.flags.channel_selector(channel.key)) in by_selector}

    def save(self, view, assignments):
        if not is_plx(view):raise ValueError('Channel labels are available for PLX recordings only')
        available = {channel.key for channel in reader.channel_options(view)}
        if any(tuple(key) not in available or label not in ROLES for key,label in assignments.items()):
            raise ValueError('The channel list changed; reopen this recording before saving labels')
        choices={}
        for key,label in assignments.items():
            selector=self.flags.channel_selector(tuple(key))
            if selector in choices and choices[selector]!=label:
                raise ValueError('Channels with the same name need the same patient-wide label')
            choices[selector]=label
        patient=self.flags.channel_patient(view)
        connection = self.flags.connect()
        self.ensure(connection)
        with connection:
            connection.executemany('''INSERT INTO patient_channel_labels
                (patient_key,channel_selector,label) VALUES (?,?,?)
                ON CONFLICT(patient_key,channel_selector) DO UPDATE SET
                label=excluded.label,updated_at=CURRENT_TIMESTAMP''',
                [(patient,json.dumps(selector,ensure_ascii=False,separators=(',',':')),label)
                 for selector,label in choices.items()])

    def all_labels(self):
        if self.flags.connection is None and not self.flags.path.exists():return {}
        connection = self.flags.connect()
        self.ensure(connection)
        result = {}
        for patient,selector,label in connection.execute(
                'SELECT patient_key,channel_selector,label FROM patient_channel_labels'):
            result.setdefault(patient,{})[tuple(json.loads(selector))] = label
        return result
