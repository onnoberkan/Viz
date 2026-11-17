"""Isolated launcher for the Viz test build."""

import builtins
import io
import os
from pathlib import Path
import sys


def delivery_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parents[3]
    return Path(__file__).resolve().parents[2]


delivery = delivery_directory()
support = delivery / 'Viz Test Support'
recordings = Path(os.environ.get('VIZ_TEST_RECORDINGS',
    '/Users/apple/Desktop/Others/UCSF/raw_UCSF')).resolve()
os.environ['VIZ_TEST_SUPPORT'] = str(support)
os.environ['VIZ_TEST_RECORDINGS'] = str(recordings)


def in_recordings(path):
    if isinstance(path, int):
        return False
    try:
        resolved = Path(os.fspath(path)).resolve()
    except (TypeError, ValueError, OSError):
        return False
    return (resolved == recordings or recordings in resolved.parents or
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

if not any(arg == '--root' or arg.startswith('--root=') for arg in sys.argv[1:]):
    sys.argv.extend(['--root', str(support / 'Workspace')])

from alpha_viewer import main  # noqa: E402


if __name__ == '__main__':
    main()
