#!/usr/bin/env python3
"""Recover stopped segmented sessions without repeating acknowledged child jobs."""
import contextlib
import csv
import json
import math
import os
import pathlib
import re
import shutil
import sys
import threading
import time

import media
from notifications import atomic_json, enqueue, lock
from publication import publish_session
from segment_capture import (Capture, CaptureError, FIELDS, LowSpace, MAX_DURATION,
                             Source, receipt_data, report_receipt, require_space,
                             state, verify_session_source)

STAGES = ('normalize', 'upload', 'encode', 'publish', 'quarantine')


class SavedSource(Source):
    """Replay saved bytes directly; never duplicate the broadcast-sized input."""
    def __init__(self, path):
        self.path = path
        self.size = path.stat().st_size
        self.done, self.error = True, None
        self.process, self.thread = None, None
        self.condition = threading.Condition()


class Replay(Capture):
    def __init__(self, job, target, known):
        super().__init__(job, [], target)
        self.known = known
        self.checked = set()

    def collect_locked(self, epoch):
        super().collect_locked(epoch)
        for index in sorted(self.sealed - self.checked):
            job = self.work / 'normalize' / self.child_path(index).name
            if index in self.known:
                require_space(self.work, (job / 'capture.bin').stat().st_size)
                tools = media.Tools(job / 'diagnostics' / 'recovery-compare')
                report = media.select_audio_report(tools.inspect(job / 'capture.bin', job), media.audio_track(job))
                if report_receipt([report], epoch['number']) != self.known[index]:
                    raise CaptureError(f'Rebuilt segment {index} differs from existing job/receipt')
                # Only generated replay output is removed; acknowledged jobs remain untouched.
                shutil.rmtree(job)
            self.checked.add(index)


def existing_children(work, session, parent, manifest):
    known, jobs = {}, {}
    for receipt in session.glob('seg*.published.json'):
        match = re.fullmatch(r'seg([1-9][0-9]*)\.published\.json', receipt.name)
        if not match:
            raise CaptureError('Invalid publication receipt identity')
        known[int(match[1])] = json.loads(receipt.read_text())
    for stage in ('active', *STAGES):
        for job in (work / stage).glob(f'recording.{parent}.seg*'):
            match = re.fullmatch(r'recording\.' + re.escape(parent) + r'\.seg([1-9][0-9]*)', job.name)
            if not match or job.is_symlink() or not job.is_dir():
                raise CaptureError('Invalid child job identity')
            index = int(match[1])
            if stage == 'active' and not (job / 'completed').exists():
                continue
            if index in jobs:
                raise CaptureError('Duplicate child queue entries; reconcile manually')
            jobs[index] = job
            if index in known:
                continue
            if (job / 'normalized.json').exists():
                known[index] = receipt_data(job)
            else:
                require_space(work, (job / 'capture.bin').stat().st_size)
                report = media.Tools(job / 'diagnostics' / 'recovery-compare').inspect(job / 'capture.bin', job)
                known[index] = report_receipt([media.select_audio_report(report, media.audio_track(job))], int((job / 'segment-epoch').read_text()))
    if any(index not in known for index in manifest['segments']):
        raise CaptureError('A sealed child has neither a job nor a publication receipt')
    return known, jobs


def prepare_job(session, destination, index, epoch, source):
    destination.mkdir(parents=True)
    for field in (*FIELDS, 'started-at'):
        shutil.copyfile(session / field, destination / field)
    if (session / 'audio-track').exists():
        shutil.copyfile(session / 'audio-track', destination / 'audio-track')
    for field, value in (('segment-index', index), ('segment-parent', session.name[10:]),
                         ('segment-epoch', epoch), ('segmenter-output', 'true')):
        state(destination / field, value)
    os.link(source, destination / 'capture.bin')
    require_space(session, source.stat().st_size)
    media.normalize(destination)
    state(destination / 'completed', 'true')


def closed_candidates(session, staging, known):
    """The common disconnect path needs validation only, with no additional remux."""
    found = set(known)
    chat_offset = 0.0
    for listing in sorted(session.glob('epoch-*.csv')):
        match = re.fullmatch(r'epoch-([1-9][0-9]*)\.csv', listing.name)
        if not match:
            raise CaptureError('Invalid epoch listing')
        for row in csv.reader(line for line in listing.read_text().splitlines(keepends=True) if line.endswith('\n')):
            if len(row) != 3:
                raise CaptureError('Incomplete segment listing')
            name, start, end = row
            match_file = re.fullmatch(r'segment-([1-9][0-9]*)\.(mp4|ts)', name)
            duration = float(end) - float(start)
            if not match_file or not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
                raise CaptureError('Invalid closed segment metadata')
            index = int(match_file[1])
            chat_start = chat_offset
            chat_offset += duration
            if index in known:
                continue
            if index in found:
                raise CaptureError('Duplicate segment in epoch listings')
            source = session / 'outputs' / name
            if source.is_symlink() or not source.is_file():
                raise CaptureError('Closed segment output is missing')
            destination = staging / 'normalize' / f'{session.name}.seg{index}'
            prepare_job(session, destination, index, int(match[1]), source)
            state(destination / 'segment-duration', duration)
            state(destination / 'chat-start', chat_start)
            state(destination / 'chat-end', chat_offset)
            found.add(index)


def recover_locked(work, session):
    manifest_path = session / 'session.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest['status'] == 'completed':
        # Also finish a crash between successful capture/recovery and its final marker.
        state(session / 'broadcast-finished', 'true')
        return
    if manifest['status'] not in ('recording', 'interrupted'):
        raise CaptureError('Only interrupted/stopped recording sessions can be recovered')
    parent = session.name[10:]
    if manifest['parent'] != parent:
        raise CaptureError('Session parent identity mismatch')
    source = session / 'capture.bin'
    _, indexed = media.periods(source)
    periods = [{'start': period['start'], 'end': period['end']} for period in indexed]
    staging = session / 'recovery'
    # This directory holds generated candidates only. Originals and receipts live outside it.
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    (staging / 'normalize').mkdir()
    try:
        known, jobs = existing_children(work, session, parent, manifest)
        try:
            closed_candidates(session, staging, known)
            candidates, receipts = candidate_receipts(staging, known)
            verify_session_source(work, session, periods, receipts)
        except (CaptureError, media.UnsafeMedia, ValueError) as error:
            if isinstance(error, LowSpace):
                raise
            print(f'[Recovery] Closed output cannot cover the source ({error}); rebuilding with stream copy.', flush=True)
            shutil.rmtree(staging)
            for stage in ('active', 'normalize', 'sessions'):
                (staging / stage).mkdir(parents=True)
            job = staging / 'active' / session.name
            job.mkdir()
            for field in (*FIELDS, 'started-at'):
                shutil.copyfile(session / field, job / field)
            if (session / 'audio-track').exists():
                shutil.copyfile(session / 'audio-track', job / 'audio-track')
            target = int(manifest.get('segment_seconds', os.environ.get('YOUTUBE_SEGMENT_SECONDS', '21600')))
            replay = Replay(job, target, known)
            os.link(source, replay.session / 'capture.bin')
            if replay.run(SavedSource(source)) != 0:
                raise CaptureError('Saved source could not be segmented safely; originals retained')
            for candidate in (staging / 'normalize').glob('recording.*'):
                require_space(work, (candidate / 'capture.bin').stat().st_size)
                media.normalize(candidate)
            candidates, receipts = candidate_receipts(staging, known)
            verify_session_source(work, session, periods, receipts)
        indices = sorted(set(known) | set(candidates))
        if not indices or indices != list(range(1, len(indices) + 1)):
            raise CaptureError('Recovered segment indices are not contiguous')
        # No queue changes occur before the complete saved source passes validation.
        for index, candidate in sorted(candidates.items()):
            active = work / 'active' / candidate.name
            if active.exists():
                archive = session / 'recovered-inputs'
                archive.mkdir(exist_ok=True)
                active.rename(archive / f'{active.name}.{time.time_ns()}')
            destination = work / 'normalize' / candidate.name
            if destination.exists():
                raise CaptureError('Recovered child handoff collision')
            candidate.rename(destination)
        for job in jobs.values():
            if job.parent == work / 'active':
                job.rename(work / 'normalize' / job.name)
        manifest.update(status='completed', segments=indices, source_periods=periods, recovered_at=int(time.time()))
        state(session / 'segment-count', len(indices))
        publish_session(session, os.environ.get('STORAGE_DIR', '/storage'), os.environ.get('CHANNEL', work.parent.name))
        state(session / 'broadcast-finished', 'true')
        atomic_json(manifest_path, manifest)
        # An interrupted broadcast never emits the successful broadcast completion alert.
        if os.environ.get('DISCORD_NOTIFY_ON_ERROR', 'true') != 'false' and (
                os.environ.get('DISCORD_MONITOR_WEBHOOK_URL') or os.environ.get('DISCORD_WEBHOOK_URL') or
                os.environ.get('NOTIFICATION_PROVIDER', 'discord') != 'discord'):
            enqueue(work, 'monitor', {'embeds': [{'title': 'Interrupted recording recovered',
                'description': f'{session.name}: verified saved segments queued; original retained until publication audit.',
                'color': 15105570}]})
        print(f'[Recovery] Recovered {session.name}; {len(candidates)} additional segments queued.', flush=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def candidate_receipts(staging, known):
    candidates = {int((job / 'segment-index').read_text()): job
                  for job in (staging / 'normalize').glob('recording.*')}
    receipts = dict(known)
    receipts.update({index: receipt_data(job) for index, job in candidates.items()})
    return candidates, [receipts[index] for index in sorted(receipts)]


def recover_session(work, session):
    work, session = pathlib.Path(work).resolve(), pathlib.Path(session).resolve()
    if session.parent != work / 'sessions' or not re.fullmatch(r'recording\.[A-Za-z0-9]+', session.name):
        raise CaptureError('Session is outside this channel work directory')
    with contextlib.ExitStack() as stack:
        # Workers hold stage locks before writing publication receipts; use that same order.
        for name in (*STAGES, f'capture-{session.name}', f'session-{session.name}'):
            if not stack.enter_context(lock(work / f'{name}.lock')):
                raise CaptureError('Capture writer or pipeline worker is active; recovery deferred')
        recover_locked(work, session)


def main():
    work = pathlib.Path(sys.argv[2])
    sessions = [pathlib.Path(sys.argv[3])] if sys.argv[1] == 'recover' else sorted((work / 'sessions').glob('recording.*'))
    failed = False
    for session in sessions:
        try:
            recover_session(work, session)
        except (OSError, ValueError, KeyError, CaptureError, media.UnsafeMedia) as error:
            failed = True
            print(f'[Recovery] {session}: {error}', file=sys.stderr)
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
