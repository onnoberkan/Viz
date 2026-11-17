"""Find a local recording library without sharing machine-specific paths."""

import os
from pathlib import Path
import subprocess


LOCATION = Path('Workspace/00 Viz/library-location.txt')
HEMISPHERES = ('left_hemisphere', 'right_hemisphere', 'unknown_hemisphere')


def valid_library(path):
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        return False
    try:
        return any(child.is_dir() and any((child / side).is_dir() for side in HEMISPHERES)
                   for child in path.iterdir())
    except OSError:
        return False


def find_library(delivery, support):
    delivery, support = Path(delivery), Path(support)
    saved = support / LOCATION
    candidates = []
    override = os.environ.get('VIZ_TEST_RECORDINGS')
    if override:
        candidates.append(override)
    try:
        candidates.append(saved.read_text().strip())
    except OSError:
        pass
    candidates.extend((delivery / 'raw_UCSF', delivery.parent / 'raw_UCSF',
                       Path.home() / 'Desktop/raw_UCSF', Path.home() / 'Documents/raw_UCSF'))
    for candidate in candidates:
        if candidate and valid_library(candidate):
            return Path(candidate).expanduser().resolve()
    if os.name == 'posix' and Path('/usr/bin/mdfind').is_file():
        try:
            result = subprocess.run(['/usr/bin/mdfind', '-name', 'raw_UCSF'],
                                    capture_output=True, text=True, timeout=3, check=False)
            for candidate in result.stdout.splitlines():
                if Path(candidate).name == 'raw_UCSF' and valid_library(candidate):
                    return Path(candidate).resolve()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def save_library(support, path):
    path = Path(path).expanduser().resolve()
    if not valid_library(path):
        raise ValueError('Choose the raw_UCSF folder containing patient folders.')
    location = Path(support) / LOCATION
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(str(path) + '\n')
    return path
