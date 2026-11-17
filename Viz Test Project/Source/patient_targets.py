from pathlib import Path
import json
import re
import library_paths

ORDER = ('STN', 'GPi', 'GPe', 'Pallidum', 'VIM', 'Thalamus', 'ZI', 'PSA', 'SNr', 'PPN')
CANONICAL = {label.casefold(): label for label in ORDER}
FILE_TARGET = re.compile(r'_(STN|GPi|GPe|VIM|ZI|PSA|SNr|PPN)_neuron_\d+$', re.I)


def file_targets(paths):
    found = {}
    for path in paths:
        match = FILE_TARGET.search(Path(path).stem)
        if match:
            found.setdefault(CANONICAL[match[1].casefold()], Path(path).name)
    return found


def evidence_path(root):
    folder = library_paths.data_dir(root)
    return folder / 'patient-target-evidence.json' if folder else None


def reviewed(root):
    try:
        data = json.loads(evidence_path(root).read_text())
        if data.get('version') != 1:return {}
        valid = {}
        for name, record in data['patients'].items():
            if (record.get('status') not in ('confirmed', 'conflicting') or
                not isinstance(record.get('targets'), list) or
                any(label not in ORDER for label in record['targets']) or
                not isinstance(record.get('evidence'), list)):continue
            if any(not isinstance(e, dict) or e.get('target') not in ORDER or
                   not isinstance(e.get('source'), str) or not isinstance(e.get('kind'), str)
                   for e in record['evidence']):continue
            valid[name] = record
        return valid
    except (OSError, ValueError, KeyError, TypeError, AttributeError):return {}


def build(root, catalog):
    reviews = reviewed(root)
    records = {}
    for name, files in catalog.items():
        review = reviews.get(name, {})
        evidence = [{'target': target, 'kind': 'file name', 'source': example}
                    for target, example in sorted(file_targets(files).items())] + review.get('evidence', [])
        if review.get('status') == 'conflicting':
            records[name] = dict(label='Unknown', note=review.get('note') or 'Conflicting target documentation.', evidence=evidence)
            continue
        labels = set(review.get('targets', [])) | {e['target'] for e in evidence}
        if 'VIM' in labels:labels.discard('Thalamus')
        if labels & {'GPi', 'GPe'}:labels.discard('Pallidum')
        records[name] = dict(label=' / '.join(label for label in ORDER if label in labels) or 'Unknown',
                             note=review.get('note', ''), evidence=evidence)
    return records


def tooltip(record):
    evidence = record.get('evidence', [])
    if not evidence:return 'Target unknown: no clear patient-specific target has been documented in the reviewed sources.'
    note = record.get('note', '')
    return (note + '\n' if note else '') + 'Target evidence:\n' + '\n'.join(
        f"{e['target']} — {e['source']}" for e in evidence)
