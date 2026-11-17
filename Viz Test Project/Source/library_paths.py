import hashlib
import json
import os
from pathlib import Path

HEMISPHERE_FOLDERS = {'left_hemisphere': 'Left', 'right_hemisphere': 'Right', 'unknown_hemisphere': None}
LOCATION = 'library-location.txt'
TEST_SUPPORT = Path(os.environ['VIZ_TEST_SUPPORT']).resolve()
TEST_WORKSPACE = TEST_SUPPORT / 'Workspace/00 Viz'
TEST_RECORDINGS = Path(os.environ['VIZ_TEST_RECORDINGS']).resolve()
CACHE_ROOT = TEST_SUPPORT / 'Cache'
_workspaces = {}
_files = {}


def open_root(root):
    root = Path(root).resolve()
    library = root
    try:
        lines = (root / '00 Viz' / LOCATION).read_text().splitlines()
        location = next(line.strip() for line in lines if line.strip() and not line.startswith('#'))
        selected = (root / location).resolve()
        if selected.is_dir():
            library = selected
            _workspaces[library] = TEST_WORKSPACE
    except (OSError, StopIteration):
        pass
    if library.is_dir() and any(is_patient(child) for child in library.iterdir() if child.is_dir()):
        _workspaces[library] = TEST_WORKSPACE
    found = workspace(library)
    if found:
        try:
            records = json.loads((found / 'Data/library-files.json').read_text())['files']
            _files.update({Path(path).name: record for path, record in records.items() if isinstance(record, dict)})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return library


def workspace(root):
    root = Path(root).resolve()
    if root in (TEST_RECORDINGS, TEST_WORKSPACE.parent) or (root / '00 Viz').is_dir():
        return TEST_WORKSPACE
    return _workspaces.get(root)


def is_library(root):
    return workspace(root) is not None


def is_patient(folder):
    return any((Path(folder) / name).is_dir() for name in HEMISPHERE_FOLDERS)


def patient_parts(parts):
    parts = tuple(parts)
    for i, part in enumerate(parts[1:], 1):
        if part in HEMISPHERE_FOLDERS:
            return parts[:i]
    return None


def hemisphere_folder(path):
    name = Path(path).parent.name
    return HEMISPHERE_FOLDERS[name] if name in HEMISPHERE_FOLDERS else False


def file_record(path):
    return _files.get(Path(path).name, {})


def acquisition_name(path):
    return file_record(path).get('acquisition_name') or Path(path).stem


def library_root(path):
    path = Path(path).resolve()
    if path.is_file():
        path = path.parent
    for parent in (path, *path.parents):
        if not is_library(parent):
            continue
        relative = path.relative_to(parent).parts
        if not relative or (is_patient(parent / relative[0]) and
                            (len(relative) == 1 or relative[1] in HEMISPHERE_FOLDERS)):
            return parent
    return path


def cache_dir(root):
    root = Path(root).resolve()
    return CACHE_ROOT / f"{root.name}-{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:12]}"


def data_dir(root):
    found = workspace(root)
    return found / 'Data' if found else None


def database_path(root):
    return TEST_WORKSPACE / 'Data/recording_flags.db'
