#!/usr/bin/env python3
"""Small operational helpers shared by the shell workers."""
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import sys
import time
from zoneinfo import ZoneInfo

from notifications import lock

RETAINED_MEDIA = {'.mp4', '.ts', '.mkv'}
MEDIA_EXTENSIONS = RETAINED_MEDIA | {
    '.mov', '.m4v', '.avi', '.webm', '.m2ts', '.mts', '.mpg', '.mpeg', '.m2v',
    '.m2t', '.vob', '.flv', '.asf', '.wmv', '.ogv', '.ogg', '.3gp', '.3g2', '.mxf',
    '.mp3', '.aac', '.flac', '.m4a', '.opus', '.wav', '.oga', '.wma', '.ac3', '.eac3', '.mka',
}


def next_quota_reset(now):
    pacific = ZoneInfo('America/Los_Angeles')
    local = datetime.datetime.fromtimestamp(now, pacific)
    tomorrow = local.date() + datetime.timedelta(days=1)
    return int(datetime.datetime.combine(tomorrow, datetime.time(), pacific).timestamp())


def quota_key(secrets):
    path = pathlib.Path(secrets)
    try:
        credentials = json.loads(path.read_text())
        identity = (credentials.get('installed') or credentials.get('web') or {})['client_id']
        if not isinstance(identity, str) or not identity:
            raise ValueError('Missing client ID')
    except (OSError, ValueError, KeyError, TypeError):
        # Offline tests and incomplete configurations still share a stable scope.
        identity = str(path.resolve())
    return hashlib.sha256(identity.encode()).hexdigest()


def prune_quarantine(work, days, now=None):
    """Expire whole, closed quarantine jobs, independently of storage publication."""
    if days <= 0:
        return
    work = pathlib.Path(work)
    cutoff = (time.time() if now is None else now) - days * 86400
    with lock(work / 'quarantine.lock') as acquired:
        if not acquired:
            return
        directory = work / 'quarantine'
        for job in sorted(directory.iterdir()):
            if job.is_symlink() or not job.is_dir():
                continue
            if job.name.startswith('.expired.recording.'):
                shutil.rmtree(job)
                continue
            if not job.name.startswith('recording.'):
                continue
            marker = job / 'quarantined-at'
            # Legacy jobs have no entry timestamp: use the directory's age.
            entered = marker.stat().st_mtime if marker.exists() else job.stat().st_mtime
            if entered > cutoff:
                continue
            retired = job.with_name('.expired.' + job.name)
            job.rename(retired)
            shutil.rmtree(retired)
            print(f'[Retention] Expired quarantine job: {job}', flush=True)


def prune_recordings(directory, days, now=None):
    """Expire media and its literal stem.* companions, keeping live sibling versions."""
    directory = pathlib.Path(directory)
    if days <= 0 or directory.is_symlink() or not directory.is_dir():
        return
    # Preserve find -mtime +N's whole-day rounding used by the existing policy.
    cutoff = (time.time() if now is None else now) - (days + 1) * 86400
    with lock(directory / '.retention.lock') as acquired:
        if not acquired:
            return
        visited = []
        for current, subdirs, filenames in os.walk(directory, followlinks=False):
            subdirs[:] = [name for name in subdirs if not name.startswith('.publish.')]
            folder = pathlib.Path(current)
            visited.append(folder)
            files = [folder / name for name in filenames
                     if name != '.retention.lock' and not (folder / name).is_symlink()
                     and (folder / name).is_file()]
            videos = {}
            for path in files:
                if path.suffix.lower() in MEDIA_EXTENSIONS:
                    videos.setdefault(path.stem, []).append(path)
            removed = set()
            for paths in videos.values():
                for path in paths:
                    try:
                        if path.suffix.lower() in RETAINED_MEDIA and path.stat().st_mtime <= cutoff:
                            path.unlink()
                            removed.add(path)
                    except OSError as error:
                        print(f'[Retention] Media retained: {path}: {error}', file=sys.stderr)
            expired_stems = {stem for stem, paths in videos.items() if all(path in removed for path in paths)}
            for path in files:
                if path.suffix.lower() in MEDIA_EXTENSIONS:
                    continue
                # The longest matching stem owns a sidecar: episode.part1.jpg belongs
                # to episode.part1.mp4 rather than an older episode.mp4.
                owners = [stem for stem in videos if path.name.startswith(stem + '.')]
                if not owners or max(owners, key=len) not in expired_stems:
                    continue
                try:
                    if path.is_file():
                        path.unlink()
                except OSError as error:
                    print(f'[Retention] Sidecar cleanup deferred: {path}: {error}', file=sys.stderr)
        for folder in reversed(visited):
            if folder != directory:
                try:
                    folder.rmdir()
                except OSError:
                    pass  # Nonempty folders, including unrelated metadata, are retained.


if __name__ == '__main__':
    if sys.argv[1] == 'quota-reset':
        print(next_quota_reset(int(sys.argv[2])))
    elif sys.argv[1] == 'quota-key':
        print(quota_key(sys.argv[2]))
    elif sys.argv[1] == 'prune-quarantine':
        prune_quarantine(sys.argv[2], int(sys.argv[3]))
    elif sys.argv[1] == 'prune-recordings':
        prune_recordings(sys.argv[2], int(sys.argv[3]))
    else:
        raise SystemExit('Unknown operation')
