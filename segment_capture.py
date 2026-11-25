#!/usr/bin/env python3
"""Disk-backed, downstream stream-copy segmentation of continuous Streamlink output."""
import argparse
import contextlib
import csv
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

import media
from notifications import atomic_json, enqueue, lock
from chat_archive import ChatArchive
from content import ChapterPoller
from publication import publish_session

GIB = 1024 ** 3
MAX_DURATION = 43170  # 11h59m30s; leave headroom below YouTube's 12-hour limit.
MAX_BYTES = 240_000_000_000
FIELDS = ('title', 'stem', 'season', 'recorded-date', 'remux-required', 'upload-required', 'encode-required')


class CaptureError(Exception):
    pass


class LowSpace(CaptureError):
    pass


def state(path, value):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(str(value) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def reserve_bytes(environ=os.environ):
    # Segmented capture always keeps a safety floor, even when the legacy check is disabled.
    return max(1, int(environ.get('MIN_FREE_SPACE_GB', '0'))) * GIB


def require_space(directory, active_bytes=0, usage=shutil.disk_usage):
    required = reserve_bytes() + 2 * active_bytes
    if usage(directory).free < required:
        raise LowSpace(f'Scratch reserve reached; need {required} bytes including processing headroom')


def stop_processes(processes, grace=90):
    processes = [process for process in processes if process is not None and process.poll() is None]
    # Signal every stream writer first; all share one flush deadline.
    for process in processes:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
    deadline = time.monotonic() + grace
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def stop_process(process):
    stop_processes([process])


class Source:
    """Drain stdout to disk independently of FFmpeg; readers can tail the saved bytes."""
    def __init__(self, path, command, guard, pass_fds=()):
        self.path, self.guard = path, guard
        self.condition = threading.Condition()
        self.size, self.done, self.error = 0, False, None
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True,
                                        pass_fds=pass_fds)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            with self.path.open('xb', buffering=0) as output:
                while chunk := self.process.stdout.read1(65536):
                    self.guard()
                    if output.write(chunk) != len(chunk):
                        raise OSError('Short write to scratch; original tail retained')
                    with self.condition:
                        self.size += len(chunk)
                        self.condition.notify_all()
            if self.process.wait() != 0:
                raise CaptureError('Streamlink interrupted; original bytes retained')
        except (OSError, CaptureError) as error:
            self.error = error
        finally:
            self.process.stdout.close()
            with self.condition:
                self.done = True
                self.condition.notify_all()

    def read(self, reader, size):
        with self.condition:
            while reader.tell() >= self.size and not self.done:
                self.condition.wait(0.2)
        return reader.read(min(size, max(0, self.size - reader.tell())))

    def exact(self, reader, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.read(reader, size - len(result))
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)


class Capture:
    def __init__(self, job, command, target):
        self.work = job.parent.parent
        self.parent = job.name.removeprefix('recording.')
        if not re.fullmatch(r'[A-Za-z0-9]+', self.parent):
            raise CaptureError('Invalid recording parent identity')
        if not 1 <= target <= 42900:
            raise CaptureError('YOUTUBE_SEGMENT_SECONDS must be between 1 and 42900')
        self.session = self.work / 'sessions' / job.name
        self.session.parent.mkdir(exist_ok=True)
        if self.session.exists():
            raise CaptureError('Session identity collision; existing data retained')
        self.writer_lock = open(self.work / f'capture-{job.name}.lock', 'a')
        if fcntl is not None:
            try:
                fcntl.flock(self.writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                self.writer_lock.close()
                raise CaptureError('Session still has a capture writer') from error
        job.rename(self.session)
        self.output = self.session / 'outputs'
        self.output.mkdir()
        self.target, self.command = target, command
        self.source, self.error = None, None
        self.epochs = []
        self.sealed = set()
        self.current_size = 0
        self.current = 1
        self.chat_offset = 0.0
        self.ending = False
        self.success = False
        self.source_periods = []
        self.collect_lock = threading.RLock()
        self.child(1)
        state(self.work / 'active.job', self.child_path(1))
        self.save('recording')

    def child_path(self, index):
        return self.work / 'active' / f'recording.{self.parent}.seg{index}'

    def child(self, index):
        job = self.child_path(index)
        if index in self.sealed:
            return job
        if not job.exists():
            job.mkdir()
            for name in FIELDS:
                shutil.copyfile(self.session / name, job / name)
            if (self.session / 'audio-track').exists():
                shutil.copyfile(self.session / 'audio-track', job / 'audio-track')
            state(job / 'started-at', int(time.time()))
            state(job / 'segment-index', index)
            state(job / 'segment-parent', self.parent)
            state(job / 'segmenter-output', 'true')
            state(job / 'segment-incomplete', 'true')
        return job

    def guard(self):
        require_space(self.work, self.current_size)

    def start_epoch(self, container, offset):
        with self.collect_lock:
            if self.ending:
                raise CaptureError('Capture is stopping')
            return self.start_epoch_locked(container, offset)

    def start_epoch_locked(self, container, offset):
        start = max(self.sealed, default=0) + 1
        self.child(start)
        state(self.work / 'active.job', self.child_path(start))
        remux = (self.session / 'remux-required').read_text().strip() == 'true'
        extension = 'mp4' if container == 'mp4' or remux else 'ts'
        number = len(self.epochs) + 1
        listing = self.session / f'epoch-{number}.csv'
        stderr = self.session / f'epoch-{number}.stderr'
        self.guard()
        command = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'warning', '-xerror',
                   '-i', 'pipe:0', '-map', '0:v?', '-map', '0:a?', '-c', 'copy',
                   '-f', 'segment', '-segment_time', str(self.target),
                   '-segment_start_number', str(start), '-reset_timestamps', '1',
                   '-segment_list', str(listing), '-segment_list_type', 'csv',
                   '-segment_format', 'mp4' if extension == 'mp4' else 'mpegts']
        if extension == 'mp4':
            command += ['-segment_format_options', 'movflags=+faststart']
        # The relative pattern keeps percent characters in host volume paths literal.
        command += [f'segment-%d.{extension}']
        with stderr.open('wb') as log:
            process = subprocess.Popen(command, cwd=self.output, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=log, start_new_session=True,
                                       pass_fds=(self.writer_lock.fileno(),))
        epoch = {'process': process, 'list': listing, 'stderr': stderr, 'start': start,
                 'extension': extension, 'finished': False, 'allow_final': False,
                 'offset': offset, 'end': None, 'number': number}
        self.epochs.append(epoch)
        return epoch

    def finish_epoch(self, epoch, end, allow_final):
        epoch['process'].stdin.close()
        if epoch['process'].wait() != 0:
            raise CaptureError('Downstream segmenter failed; inspect retained session stderr')
        with epoch['stderr'].open(errors='replace') as messages:
            if any(media.ERRORS.search(line) for line in messages):
                raise CaptureError('Downstream segmenter reported unsafe media; originals retained')
        epoch.update(end=end, allow_final=allow_final, finished=True)
        # Before allocating the next epoch's indices, finish handing off this one.
        self.collect(epoch)
        self.source_periods.append({'start': epoch['offset'], 'end': end})

    def send(self, epoch, chunk):
        # Recheck while draining a saved backlog, even after network capture has ended.
        self.guard()
        epoch['process'].stdin.write(chunk)

    def feed(self):
        try:
            while not self.source.path.exists():
                if self.source.done:
                    raise CaptureError('No captured bytes')
                time.sleep(0.01)
            with self.source.path.open('rb', buffering=0) as reader:
                prefix = self.source.exact(reader, 8)
                if len(prefix) < 8:
                    raise CaptureError('Incomplete capture header')
                if prefix[4:8] not in (b'ftyp', b'styp'):
                    epoch = self.start_epoch('mpegts', 0)
                    self.send(epoch, prefix)
                    while chunk := self.source.read(reader, 65536):
                        self.send(epoch, chunk)
                    self.finish_epoch(epoch, reader.tell(), self.source.error is None and not self.ending)
                else:
                    # Keep MP4 initialization/codec epochs separate without touching Streamlink internals.
                    epoch, seen_moov = None, False
                    while prefix:
                        if len(prefix) != 8:
                            raise CaptureError('Truncated MP4 box header')
                        offset = reader.tell() - 8
                        size, kind = struct.unpack('>I4s', prefix)
                        header = prefix
                        if size == 1:
                            extended = self.source.exact(reader, 8)
                            if len(extended) != 8:
                                raise CaptureError('Truncated extended MP4 box')
                            size = struct.unpack('>Q', extended)[0]
                            header += extended
                        if size < len(header):
                            raise CaptureError('Open-ended or invalid MP4 box; original retained')
                        if kind == b'ftyp':
                            if epoch:
                                self.finish_epoch(epoch, offset, True)
                            epoch = self.start_epoch('mp4', offset)
                            seen_moov = False
                        if epoch is None or kind == b'moov' and seen_moov:
                            raise CaptureError('Uncertain MP4 initialization boundary')
                        seen_moov |= kind == b'moov'
                        self.send(epoch, header)
                        remaining = size - len(header)
                        while remaining:
                            chunk = self.source.read(reader, min(65536, remaining))
                            if not chunk:
                                raise CaptureError('Truncated MP4 box; original retained')
                            self.send(epoch, chunk)
                            remaining -= len(chunk)
                        prefix = self.source.exact(reader, 8)
                    self.finish_epoch(epoch, reader.tell(), self.source.error is None and not self.ending)
        except (OSError, CaptureError) as error:
            self.error = error

    def files(self, epoch):
        result = {}
        for path in self.output.glob(f'segment-*.{epoch["extension"]}'):
            index = int(path.stem.removeprefix('segment-'))
            if index >= epoch['start']:
                result[index] = path
        return result

    def collect(self, epoch):
        with self.collect_lock:
            self.collect_locked(epoch)

    def collect_locked(self, epoch):
        files = self.files(epoch)
        if files:
            index = max(files)
            if index not in self.sealed:
                job = self.child(index)
                if not (job / 'capture.bin').exists():
                    os.link(files[index], job / 'capture.bin')
                self.current, self.current_size = index, files[index].stat().st_size
                state(self.work / 'active.job', job)
                if self.current_size >= MAX_BYTES:
                    raise CaptureError('Segment size ceiling reached; original retained')
        if not epoch['list'].exists():
            return
        # Ignore a partial final line; FFmpeg writes rows only after closing files.
        lines = epoch['list'].read_text().splitlines(keepends=True)
        rows = list(csv.reader(line for line in lines if line.endswith('\n')))
        for name, start, end in rows:
            index = int(pathlib.Path(name).stem.removeprefix('segment-'))
            if index in self.sealed:
                continue
            if not (epoch['finished'] and epoch['allow_final']) and index >= max(files, default=index):
                continue
            duration = float(end) - float(start)
            if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
                raise CaptureError('Segment duration is outside the upload ceiling')
            file = self.output / pathlib.Path(name).name
            job = self.child(index)
            if not (job / 'capture.bin').exists():
                os.link(file, job / 'capture.bin')
            if file.stat().st_size >= MAX_BYTES:
                raise CaptureError('Segment size is outside the upload ceiling')
            require_space(self.work, max(self.current_size, file.stat().st_size))
            state(job / 'segment-duration', duration)
            state(job / 'chat-start', self.chat_offset)
            self.chat_offset += duration
            state(job / 'chat-end', self.chat_offset)
            state(job / 'segment-epoch', epoch['number'])
            (job / 'segment-incomplete').unlink()
            state(job / 'completed', 'true')
            # A closed segment never clears another child's live status pointer.
            destination = self.work / 'normalize' / job.name
            if destination.exists():
                raise CaptureError('Segment handoff collision; original retained')
            job.rename(destination)
            self.sealed.add(index)
            file.unlink()
            print(f'[Capture] Segment {index} closed and queued: {destination}', flush=True)
            self.save('recording')

    def save(self, status):
        atomic_json(self.session / 'session.json', {'version': 1, 'parent': self.parent,
                    'status': status, 'segments': sorted(self.sealed), 'source_periods': self.source_periods,
                    'segment_seconds': self.target})

    def run(self, source=None):
        consumer = None
        chat = ChatArchive(self.session)
        chapters = ChapterPoller(self.session)
        original_handlers = {}

        def interrupted(_signum, _frame):
            self.ending = True
            raise CaptureError('Capture stopped; unconfirmed tail retained')

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                original_handlers[sig] = signal.signal(sig, interrupted)
            self.guard()
            if source is None:
                chat.start()
                chapters.start()
            self.source = source or Source(self.session / 'capture.bin', self.command, self.guard,
                                           pass_fds=(self.writer_lock.fileno(),))
            consumer = threading.Thread(target=self.feed, daemon=True)
            consumer.start()
            while consumer.is_alive():
                self.guard()
                for epoch in list(self.epochs):
                    self.collect(epoch)
                if self.source.error:
                    raise self.source.error
                consumer.join(0.1)
            if self.source.error:
                raise self.source.error
            if self.error:
                raise self.error
            for epoch in self.epochs:
                self.collect(epoch)
            if not self.sealed:
                raise CaptureError('No complete segments produced')
            self.save('completed')
            state(self.session / 'segment-count', len(self.sealed))
            self.success = True
            return 0
        except (OSError, CaptureError) as error:
            self.ending = True
            self.save('interrupted')
            print(f'[Capture] {error}: {self.session}', file=sys.stderr)
            if isinstance(error, LowSpace):
                self.low_disk_alert()
            return 1
        finally:
            self.ending = True
            for sig in original_handlers:
                signal.signal(sig, signal.SIG_IGN)
            stop_processes(([self.source.process] if self.source else []) +
                           [epoch['process'] for epoch in self.epochs])
            if self.source:
                if self.source.thread:
                    self.source.thread.join(timeout=6)
            if consumer:
                consumer.join(timeout=6)
            chat.stop()
            chapters.stop()
            if not self.success:
                self.save('interrupted')
            (self.work / 'active.job').unlink(missing_ok=True)
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)
            try:
                if not self.success:
                    self.discard_empty_session(consumer)
            finally:
                # Children inherit this descriptor, so a killed controller cannot unlock live writers.
                self.writer_lock.close()

    def discard_empty_session(self, consumer):
        # Check actual files too: a short write can leave bytes before Source.size advances.
        if self.sealed or (self.source and self.source.size) or (consumer and consumer.is_alive()):
            return
        if self.source and self.source.thread and self.source.thread.is_alive():
            return
        source = self.session / 'capture.bin'
        if source.exists() and source.stat().st_size:
            return
        children = list((self.work / 'active').glob(f'recording.{self.parent}.seg*'))
        for job in children:
            capture = job / 'capture.bin'
            if (job / 'completed').exists() or (capture.exists() and capture.stat().st_size):
                return
        for job in children:
            shutil.rmtree(job)
        shutil.rmtree(self.session)
        # Unlink only after writers stopped, while this controller still owns the lock.
        pathlib.Path(self.writer_lock.name).unlink(missing_ok=True)
        print(f'[Capture] Removed empty aborted session: {self.session}', flush=True)

    def low_disk_alert(self):
        if os.environ.get('DISCORD_NOTIFY_ON_ERROR', 'true') == 'false':
            return
        if not (os.environ.get('DISCORD_MONITOR_WEBHOOK_URL') or os.environ.get('DISCORD_WEBHOOK_URL') or
                os.environ.get('NOTIFICATION_PROVIDER', 'discord') != 'discord'):
            return
        try:
            enqueue(self.work, 'monitor', {'embeds': [{'title': 'Low Disk Space: capture stopped safely',
                'description': 'Scratch processing reserve reached. Closed segments remain queued; any captured bytes are retained.',
                'color': 15105570}]})
        except OSError:
            print('[Capture] Unable to queue disk-space alert.', file=sys.stderr)


def published_receipt(job):
    """Called only after every final part has been checksummed by the existing pipeline."""
    parent = (job / 'segment-parent').read_text().strip()
    index = int((job / 'segment-index').read_text())
    if not re.fullmatch('[A-Za-z0-9]+', parent) or index < 1:
        raise CaptureError('Invalid segment receipt identity')
    work = job.parent.parent
    session = work / 'sessions' / ('recording.' + parent)
    with lock(work / f'session-recording.{parent}.lock') as acquired:
        if not acquired:
            raise CaptureError('Source audit in progress; child cleanup deferred')
        if not session.exists():
            # A retry after session retirement still has independently verified final files.
            return
        published_receipt_locked(job, session, index)


def published_receipt_locked(job, session, index):
    atomic_json(session / f'seg{index}.published.json', receipt_data(job))


def receipt_data(job):
    manifest = json.loads((job / 'normalized.json').read_text())
    epoch = int((job / 'segment-epoch').read_text())
    return report_receipt([part['validation'] for part in manifest['parts']], epoch)


def report_receipt(reports, epoch):
    totals = {'video': 0, 'audio': 0}
    video = []
    for report in reports:
        track = 0
        for stream, (kind, count) in enumerate(zip(report['stream_types'], report['packets'])):
            totals[kind] += count
            if kind == 'video':
                if len(video) <= track:
                    video.append([])
                video[track].append({'frames': report['frames'][stream], 'sha256': report['frame_hashes'][stream]})
                track += 1
    return {'packets': totals, 'video': video, 'epoch': epoch}


def verify_source_frames(report, receipts):
    """Match each child boundary against the original decoded video sequence."""
    tracks = []
    for receipt in receipts:
        for index, chunks in enumerate(receipt['video']):
            if len(tracks) <= index:
                tracks.append([])
            tracks[index].extend(chunks)
    if len(tracks) != report['stream_types'].count('video') or any(not chunks for chunks in tracks):
        raise CaptureError('Missing source-video fingerprint receipts')
    positions = [0] * len(tracks)
    counts = [0] * len(tracks)
    hashes = [hashlib.sha256() for _ in tracks]
    with open(report['_frame_file']) as frames:
        for line in frames:
            if not line.strip() or line.startswith('#'):
                continue
            fields = [field.strip() for field in line.split(',')]
            track = int(fields[0])
            # Tools.inspect maps every video track before audio tracks.
            if track >= len(tracks):
                continue
            position = positions[track]
            if position >= len(tracks[track]):
                raise CaptureError('Source video contains frames absent from published segments')
            hashes[track].update((fields[-1] + '\n').encode())
            counts[track] += 1
            expected = tracks[track][position]
            if counts[track] == expected['frames']:
                if hashes[track].hexdigest() != expected['sha256']:
                    raise CaptureError('Segment frame content differs from the original video sequence')
                positions[track] += 1
                hashes[track] = hashlib.sha256()
                counts[track] = 0
    if any(positions[index] != len(chunks) or counts[index] for index, chunks in enumerate(tracks)):
        raise CaptureError('Published video frame accounting is incomplete')


def audit_sessions(work):
    """Release session sources only after complete packet accounting and verified publication."""
    with lock(work / 'sessions.lock') as acquired:
        if acquired:
            audit_sessions_locked(work)


def audit_sessions_locked(work):
    for retired in (work / 'sessions').glob('.completed.recording.*'):
        shutil.rmtree(retired)
    for session in sorted((work / 'sessions').glob('recording.*')):
        with lock(work / f'session-{session.name}.lock') as acquired:
            if acquired:
                audit_session(work, session)


def verify_session_source(work, session, periods, receipts):
    """Validate full source coverage before retirement or recovery handoff."""
    source = session / 'capture.bin'
    position = 0
    for period in periods:
        if period['start'] != position or period['end'] <= position:
            raise CaptureError('Incomplete source epoch accounting')
        position = period['end']
    if position != source.stat().st_size:
        raise CaptureError('Source epochs do not cover the entire original capture')
    expected = {kind: sum(receipt['packets'][kind] for receipt in receipts) for kind in ('video', 'audio')}
    original = {'video': 0, 'audio': 0}
    for number, period in enumerate(periods, 1):
        whole = period['start'] == 0 and period['end'] == source.stat().st_size
        candidate = source if whole else session / 'audit-input.bin'
        if not whole:
            candidate.unlink(missing_ok=True)
        require_space(work, 0 if whole else period['end'] - period['start'])
        if not whole:
            media.copy_range(source, candidate, period['start'], period['end'])
        tools = media.Tools(session / 'audit' / str(number))
        report = media.select_audio_report(tools.inspect(candidate, session), media.audio_track(session))
        verify_source_frames(report, [receipt for receipt in receipts if receipt['epoch'] == number])
        for kind, count in zip(report['stream_types'], report['packets']):
            original[kind] += count
        if not whole:
            candidate.unlink()
        for output in tools.directory.glob('*.stdout'):
            output.unlink()
    if original != expected:
        raise CaptureError(f'Source and published packet counts differ: source={original}, published={expected}')


def audit_session(work, session):
    manifest_file = session / 'session.json'
    try:
        manifest = json.loads(manifest_file.read_text())
        if manifest['status'] != 'completed' or not (session / 'broadcast-finished').exists():
            return
        if not manifest['segments'] or manifest['segments'] != list(range(1, len(manifest['segments']) + 1)):
            raise CaptureError('Incomplete segment index accounting')
        receipts = []
        for index in manifest['segments']:
            receipt = session / f'seg{index}.published.json'
            if not receipt.exists():
                break
            receipt = json.loads(receipt.read_text())
            receipts.append(receipt)
        else:
            publish_session(session, os.environ.get('STORAGE_DIR', '/storage'), os.environ.get('CHANNEL', work.parent.name))
            verify_session_source(work, session, manifest['source_periods'], receipts)
            # Raw input is retained through the broadcast and until all child publications are verified.
            retired = session.with_name('.completed.' + session.name)
            session.rename(retired)
            shutil.rmtree(retired)
    except LowSpace as error:
        print(f'[Capture] Source audit waiting for scratch space: {session}: {error}', file=sys.stderr)
    except (CaptureError, media.UnsafeMedia) as error:
        manifest['status'] = 'audit-failed'
        manifest['reason'] = str(error)
        atomic_json(manifest_file, manifest)
        print(f'[Capture] Source audit failed; original retained: {session}: {error}', file=sys.stderr)
        if os.environ.get('DISCORD_NOTIFY_ON_ERROR', 'true') != 'false' and (
                os.environ.get('DISCORD_MONITOR_WEBHOOK_URL') or os.environ.get('DISCORD_WEBHOOK_URL') or
                os.environ.get('NOTIFICATION_PROVIDER', 'discord') != 'discord'):
            enqueue(work, 'monitor', {'embeds': [{'title': 'Recording source audit failed',
                'description': 'Original capture retained for inspection: ' + session.name,
                'color': 15158332}]})
    except (OSError, ValueError, KeyError) as error:
        print(f'[Capture] Session cleanup deferred: {session}: {error}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('capture', 'receipt', 'audit', 'space'))
    parser.add_argument('path', type=pathlib.Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.operation == 'space':
        require_space(args.path, (args.path / 'capture.bin').stat().st_size)
        return 0
    if args.operation == 'receipt':
        published_receipt(args.path)
        return 0
    if args.operation == 'audit':
        audit_sessions(args.path)
        return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    return Capture(args.path, command, int(os.environ.get('YOUTUBE_SEGMENT_SECONDS', '21600'))).run()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, CaptureError) as error:
        print(f'[Capture] {error}', file=sys.stderr)
        sys.exit(1)
