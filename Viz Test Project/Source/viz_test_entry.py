"""Isolated launcher for the Viz test build."""

import builtins
import io
import os
from pathlib import Path
import sys

from recording_locator import find_library, save_library


def delivery_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parents[3]
    return Path(__file__).resolve().parents[2]


delivery = delivery_directory()
support = delivery / 'Viz Test Support'
recordings = find_library(delivery, support)
if recordings is not None:
    location = support / 'Workspace/00 Viz/library-location.txt'
    if not location.is_file() or location.read_text().strip() != str(recordings):
        save_library(support, recordings)
os.environ['VIZ_TEST_SUPPORT'] = str(support)
os.environ['VIZ_TEST_RECORDINGS'] = str(recordings or support / 'Workspace')


def in_recordings(path):
    if isinstance(path, int):
        return False
    try:
        resolved = Path(os.fspath(path)).resolve()
    except (TypeError, ValueError, OSError):
        return False
    return ((recordings is not None and (resolved == recordings or recordings in resolved.parents)) or
            resolved.suffix.lower() in ('.map','.mpx','.plx'))


_builtin_open = builtins.open
_io_open = io.open
_os_open = os.open


def read_only_open(file, mode='r', *args, **kwargs):
    if in_recordings(file) and any(flag in mode for flag in 'wax+'):
        raise PermissionError('Viz Test opens original recordings read-only')
    return _builtin_open(file, mode, *args, **kwargs)


def read_only_io_open(file, mode='r', *args, **kwargs):
    if in_recordings(file) and any(flag in mode for flag in 'wax+'):
        raise PermissionError('Viz Test opens original recordings read-only')
    return _io_open(file, mode, *args, **kwargs)


def read_only_os_open(file, flags, *args, **kwargs):
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    if in_recordings(file) and flags & write_flags:
        raise PermissionError('Viz Test opens original recordings read-only')
    return _os_open(file, flags, *args, **kwargs)


builtins.open = read_only_open
io.open = read_only_io_open
os.open = read_only_os_open

if recordings is not None and not any(arg == '--root' or arg.startswith('--root=') for arg in sys.argv[1:]):
    sys.argv.extend(['--root', str(recordings)])

from alpha_viewer import main  # noqa: E402


if __name__ == '__main__':
    main()
