"""Paper-specific, reviewer-signed single-unit decisions for saved spike sorts."""

from dataclasses import dataclass

import numpy as np

from spike_sorting import channel_identity


REFRACTORY_SECONDS = .003


def isi_summary(times):
    """Measure the selected cluster without discarding short intervals."""
    intervals = np.diff(np.asarray(times, dtype=np.float64))
    short = int(np.count_nonzero(intervals < REFRACTORY_SECONDS))
    return dict(spikes=len(times), intervals=len(intervals), under_3_ms=short,
                percent_under_3_ms=100 * short / len(intervals) if len(intervals) else 0.,
                minimum_ms=float(intervals.min() * 1000) if len(intervals) else None)


@dataclass
class UnitReview:
    decision: str = 'needs_review'
    distinct_waveforms: bool = False
    clear_refractory: bool = False
    cardiac: str = 'not_assessed'
    cardiac_evidence: str = ''
    notes: str = ''
    reviewed_at: str | None = None


def validate_review(review, selected_spikes):
    if review.decision not in ('needs_review', 'accepted', 'rejected'):
        raise ValueError('Choose a valid review decision.')
    if review.cardiac not in ('not_assessed', 'no_change', 'change_seen'):
        raise ValueError('Choose a valid cardiac-cycle assessment.')
    if review.decision == 'accepted':
        if selected_spikes < 1000:
            raise ValueError('The selected cluster has fewer than 1,000 spikes.')
        if not review.distinct_waveforms or not review.clear_refractory:
            raise ValueError('Confirm waveform separation and a clear 3 ms refractory period.')
        if review.cardiac != 'no_change' or not review.cardiac_evidence.strip():
            raise ValueError('Document how cardiac-linked shape changes were assessed before accepting.')


class UnitAcceptanceStore:
    def __init__(self, flags):
        self.flags = flags

    @staticmethod
    def ensure(connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS unit_acceptance (
            recording_id TEXT NOT NULL, channel_key TEXT NOT NULL,
            sort_updated_at TEXT NOT NULL, selected_cluster INTEGER NOT NULL,
            decision TEXT NOT NULL, distinct_waveforms INTEGER NOT NULL,
            clear_refractory INTEGER NOT NULL, cardiac TEXT NOT NULL,
            cardiac_evidence TEXT NOT NULL, notes TEXT NOT NULL,
            reviewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (recording_id, channel_key))''')

    def sort_revision(self, view, channel):
        connection = self.flags.connect()
        return connection.execute('''SELECT updated_at, selected_cluster FROM spike_sorts
            WHERE recording_id=? AND channel_key=?''',
            (self.flags.identity(view)[0], channel_identity(channel))).fetchone()

    def load(self, view, channel):
        if self.flags.connection is None and not self.flags.path.exists():
            return None
        connection = self.flags.connect()
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unit_acceptance'").fetchone():
            return None
        row = connection.execute('''SELECT sort_updated_at, selected_cluster, decision,
            distinct_waveforms, clear_refractory, cardiac, cardiac_evidence, notes, reviewed_at
            FROM unit_acceptance WHERE recording_id=? AND channel_key=?''',
            (self.flags.identity(view)[0], channel_identity(channel))).fetchone()
        if row is None or self.sort_revision(view, channel) != row[:2]:
            return None
        return UnitReview(row[2], bool(row[3]), bool(row[4]), row[5], row[6], row[7], row[8])

    def save(self, view, channel, saved, review, expected_revision=None):
        revision = self.sort_revision(view, channel)
        if revision is None or revision[1] != saved.selected:
            raise ValueError('The saved cluster changed. Reopen Sort spikes before reviewing it.')
        if expected_revision is not None and revision != expected_revision:
            raise ValueError('The saved sort changed. Reopen Sort spikes before reviewing it.')
        validate_review(review, len(saved.spike_times))
        values = (self.flags.identity(view)[0], channel_identity(channel), revision[0],
                  revision[1], review.decision, int(review.distinct_waveforms),
                  int(review.clear_refractory), review.cardiac,
                  review.cardiac_evidence.strip(), review.notes.strip())
        with self.flags.connect() as connection:
            self.ensure(connection)
            connection.execute('''INSERT INTO unit_acceptance
                (recording_id,channel_key,sort_updated_at,selected_cluster,decision,
                 distinct_waveforms,clear_refractory,cardiac,cardiac_evidence,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(recording_id,channel_key) DO UPDATE SET
                sort_updated_at=excluded.sort_updated_at,
                selected_cluster=excluded.selected_cluster,decision=excluded.decision,
                distinct_waveforms=excluded.distinct_waveforms,
                clear_refractory=excluded.clear_refractory,cardiac=excluded.cardiac,
                cardiac_evidence=excluded.cardiac_evidence,notes=excluded.notes,
                reviewed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''', values)
