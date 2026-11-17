from collections import defaultdict
from pathlib import Path
import re
import library_paths

SIDES = {'L': 'Left', 'R': 'Right'}
NEURO_OMEGA = re.compile(r'([LR])T\d+D[+-]?\d+(?:\.\d+)?F\d+', re.I)
PASS = re.compile(r'([LR])\d{2}(?=x[LR]?\d|d[bn]?\d|[EIS]?[SA][PM][FC]|SPF)')
LFP = re.compile(r'([LR])(?:lfp|LFP|flp)')
ECOG = re.compile(r'([LR])(?:ecog|ECOG)')
KINDS = {'Neuro Omega name': 'Neuro Omega file name', 'MER pass code': 'MER pass code',
         'LFP lead side': 'LFP lead side', 'ECoG strip side': 'ECoG strip side'}


def name_sides(path):
    stem = library_paths.acquisition_name(path)
    found = defaultdict(set)
    match = NEURO_OMEGA.fullmatch(stem)
    if match:
        found['Neuro Omega name'].add(match[1].upper())
    else:
        for kind, pattern in (('MER pass code', PASS), ('LFP lead side', LFP), ('ECoG strip side', ECOG)):
            found[kind].update(m[1] for m in pattern.finditer(stem))
    return {kind: sides for kind, sides in found.items() if sides}


def file_record(path):
    side = library_paths.hemisphere_folder(path)
    if side is False:
        return dict(side=None, basis='not in a hemisphere folder', inferred=False, evidence=[])
    saved = library_paths.file_record(path)
    basis = saved.get('hemisphere_basis') or ''
    inferred = basis.endswith('(inferred)')
    basis = basis.removesuffix('(inferred)').strip()
    labelled = side is None and saved.get('hemisphere') in SIDES.values()
    if labelled:
        side = saved['hemisphere']
    named = [f"{KINDS[kind]} in {library_paths.acquisition_name(path)}: {' and '.join(SIDES[s] for s in sorted(sides))}"
             for kind, sides in name_sides(path).items()]
    filed = [] if labelled else [f'Filed in {Path(path).parent.name}/' + (f' ({basis}).' if basis else '.')]
    return dict(side=side, basis=basis or 'hemisphere folder', inferred=inferred and bool(side), evidence=filed + named)


def summary(records):
    sides = {r['side'] for r in records.values() if r.get('side')}
    return 'Bilateral' if len(sides) > 1 else next(iter(sides), '')


def relative(root, path):
    try:return str(Path(path).relative_to(root))
    except ValueError:return str(path)


def build(root, catalog):
    patients = {}
    for name, paths in catalog.items():
        records = {relative(root, path): file_record(path) for path in paths}
        patients[name] = dict(label=summary(records), files=records)
    return patients


def record(patients, name, root, path):
    return patients.get(name, {}).get('files', {}).get(relative(root, path))


def mark(entry):
    return {'Left': 'L', 'Right': 'R'}.get((entry or {}).get('side'), '')


def text(entry):
    if not entry or not entry.get('side'):return 'Hemisphere unknown'
    return f"{entry['side']} hemisphere" + (' (inferred)' if entry.get('inferred') else '')


def export_fields(entry):
    if not entry:return {'hemisphere': '', 'hemisphere_basis': ''}
    return {'hemisphere': entry.get('side') or 'Unknown',
            'hemisphere_basis': (entry.get('basis') or '') + (' (inferred)' if entry.get('inferred') else '')}


def tooltip(entry):
    if not entry:return 'Hemisphere not yet determined.'
    lines = entry.get('evidence', [])
    heading = text(entry) + (f" — {entry['basis']}" if entry.get('basis') else '')
    return heading + ('\n' + '\n'.join(lines) if lines else '')
