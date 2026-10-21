#!/usr/bin/env python3
"""Stage rclone objects and commit them before allowing scratch retirement."""
import hashlib
import os
from pathlib import Path
import re
import subprocess

from notifications import atomic_json, read_json


def validate_config(environ=os.environ):
    remote = environ.get('RCLONE_REMOTE', '')
    if remote and (remote.startswith('-') or '\n' in remote or '\r' in remote):
        raise ValueError('Invalid RCLONE_REMOTE')
    limit = environ.get('RCLONE_BWLIMIT', '')
    if limit and not re.fullmatch(r'\d+(?:\.\d+)?[kKmMgGtT]?', limit):
        raise ValueError('RCLONE_BWLIMIT must be a rate such as 8M or 500K')
    retries = environ.get('RCLONE_RETRIES', '3')
    if not re.fullmatch(r'[1-9]|10', retries):
        raise ValueError('RCLONE_RETRIES must be between 1 and 10')


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def sync_files(files, receipt, environ=os.environ, run=subprocess.run):
    remote = environ.get('RCLONE_REMOTE', '').rstrip('/')
    if not remote or not files:
        return
    validate_config(environ)
    root = Path(environ.get('STORAGE_DIR', '/storage')).resolve()
    identity = hashlib.sha256(remote.encode()).hexdigest()
    state = read_json(receipt, {})
    if state.get('remote') != identity:
        state = {'remote': identity, 'files': {}}
    flags = ['--checksum', '--immutable', '--retries', environ.get('RCLONE_RETRIES', '3'),
             '--retries-sleep', '10s', '--low-level-retries', '3']
    if environ.get('RCLONE_BWLIMIT'):
        flags += ['--bwlimit', environ['RCLONE_BWLIMIT']]
    config = environ.get('RCLONE_CONFIG') or '/config/rclone.conf'
    if Path(config).exists() or environ.get('RCLONE_CONFIG'):
        flags += ['--config', config]
    for filename in files:
        path = Path(filename).resolve()
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        previous = state['files'].get(relative, {})
        if previous.get('size') == stat.st_size and previous.get('mtime_ns') == stat.st_mtime_ns:
            continue
        checksum = digest(path)
        metadata = {'sha256': checksum, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        if previous.get('sha256') == checksum:
            state['files'][relative] = metadata
            atomic_json(receipt, state)
            continue
        target = remote + '/' + relative
        staged = remote + '/.pending/' + checksum + '/' + relative
        command = environ.get('RCLONE_BIN', 'rclone')
        # The local archive is always copied. Only a remote staging object is moved.
        try:
            run([command, 'copyto', str(path), staged, *flags], check=True)
            run([command, 'moveto', staged, target, *flags], check=True)
        except subprocess.CalledProcessError:
            # Session audits treat operational failures as retryable and continue
            # with other sessions. Avoid exposing configured remote credentials.
            raise OSError('Remote copy or commit failed; scratch retained') from None
        state['files'][relative] = metadata
        atomic_json(receipt, state)
