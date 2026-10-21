"""Real downstream stream-copy segmentation, live handoff, recovery and disk guards."""
import json
import functools
import http.server
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import media
import segment_capture as segmentation
import session_recovery as recovery


class SegmentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures_temp = tempfile.TemporaryDirectory()
        cls.fixtures = pathlib.Path(cls.fixtures_temp.name)
        for name, fragmented in (('ts', False), ('mp4', True)):
            command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                       'testsrc2=size=160x90:rate=10:duration=12', '-f', 'lavfi', '-i',
                       'sine=frequency=800:duration=12', '-c:v', 'libx264', '-g', '10',
                       '-bf', '2', '-c:a', 'aac']
            if fragmented:
                command += ['-movflags', 'empty_moov+frag_keyframe+default_base_moof']
            command += ['-f', 'mp4' if fragmented else 'mpegts', str(cls.fixtures / ('source.' + name))]
            subprocess.run(command, check=True, capture_output=True)
        subprocess.run([sys.executable, str(ROOT / 'tests/generate_media_fixtures.py'),
                        '--intro-seconds', '3', '--main-seconds', '4', '--output', str(cls.fixtures / 'periods')],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures_temp.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='segmentation-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = pathlib.Path(self.temporary.name)
        self.env = dict(os.environ, MODE='twitch', CHANNEL='segments',
                        SCRATCH_ROOT=str(self.directory / 'scratch'), STORAGE_DIR=str(self.directory / 'storage'),
                        REMUX='false', UPLOAD='false', ENCODE='false', YOUTUBE_SPLITTING='true',
                        YOUTUBE_SEGMENT_SECONDS='2', MIN_FREE_SPACE_GB='0',
                        DISCORD_WEBHOOK_URL='https://example.invalid/public',
                        DISCORD_MONITOR_WEBHOOK_URL='https://example.invalid/monitor',
                        DISCORD_NOTIFY_ON_ERROR='true', UPLOAD_BOT_NAME='test')
        self.work = pathlib.Path(self.env['SCRATCH_ROOT']) / 'twitch/segments/queue-v2'
        self.log = self.directory / 'capture.log'
        self.bin = self.directory / 'bin'
        self.bin.mkdir()
        streamlink = self.bin / 'streamlink'
        streamlink.write_text('''#!/usr/bin/env python3
import os, pathlib, sys, time
with open(os.environ['CALLS'], 'a') as log: log.write(' '.join(sys.argv[1:]) + '\\n')
assert '--stdout' in sys.argv and '--hls-duration' not in sys.argv
if os.environ.get('EMPTY_SOURCE'): sys.exit(int(os.environ.get('EMPTY_EXIT', '1')))
sys.stdout.buffer.write(pathlib.Path(os.environ['FIXTURE']).read_bytes())
sys.stdout.buffer.flush()
while os.environ.get('HOLD') and not pathlib.Path(os.environ['HOLD']).exists(): time.sleep(0.05)
if os.environ.get('FAIL_SOURCE'): sys.exit(1)
''')
        streamlink.chmod(0o755)
        ffmpeg = self.bin / 'ffmpeg'
        ffmpeg.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
            'with open(os.environ["FFMPEG_CALLS"], "a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            f'os.execv({shutil.which("ffmpeg")!r}, [{shutil.which("ffmpeg")!r}, *sys.argv[1:]])\n')
        ffmpeg.chmod(0o755)
        self.env.update(PATH=str(self.bin) + ':' + os.environ['PATH'], CALLS=str(self.directory / 'calls'))
        self.env['FFMPEG_CALLS'] = str(self.directory / 'ffmpeg-calls')
        self.env['FIXTURE'] = str(self.fixtures / 'source.ts')

    def tearDown(self):
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        if failed and self.log.exists():
            print(self.log.read_text(errors='replace')[-4000:])
        if failed and os.getenv('TEST_ARTIFACT_DIR'):
            target = pathlib.Path(os.environ['TEST_ARTIFACT_DIR']) / self._testMethodName
            shutil.copytree(self.directory, target, dirs_exist_ok=True)

    def start(self, **environment):
        self.env.update(environment)
        output = self.log.open('wb')
        self.addCleanup(output.close)
        process = subprocess.Popen(['bash', '-c', 'source "$1/download.sh"; pipeline_init; '
                                    'record_stream "Live segment title" Author 123', 'test', str(ROOT)],
                                   env=self.env, stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process

    def wait(self, process):
        result = process.wait(timeout=60)
        self.assertEqual(0, result, self.log.read_text(errors='replace'))

    def shell(self, script, **environment):
        result = subprocess.run(['bash', '-c', 'set -euo pipefail; source "$1/download.sh"; '
                                 'source "$1/upload.sh"; source "$1/encode.sh"; pipeline_init; ' + script,
                                 'test', str(ROOT)], env=dict(self.env, **environment),
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def publish(self):
        self.shell('''
invoke_uploader() {
    local title='' file=''
    while (($#)); do
        case "$1" in -title) title="$2" ;; -filename) file="$2" ;; esac
        shift
    done
    local index
    read -r index < "${file%/parts/*}/segment-index"
    printf -v index '%04d' "$index"
    [[ "$title" == *"part-$index"* ]]
    printf '%s\\n' "$title" >> "$SCRATCH_ROOT/uploads"
}
run_queue_once normalize normalize_job
run_queue_once upload upload_job
run_queue_once encode encode_job
run_queue_once publish publish_recording
''')

    def test_publish_before_broadcast_ends_with_current_child_status(self):
        marker = self.directory / 'finish'
        process = self.start(HOLD=str(marker))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not list((self.work / 'normalize').glob('recording.*')):
            if process.poll() is not None:
                self.fail(self.log.read_text())
            time.sleep(0.1)
        self.assertTrue(list((self.work / 'normalize').glob('recording.*')), self.log.read_text())
        self.assertIsNone(process.poll())
        active = pathlib.Path((self.work / 'active.job').read_text().strip())
        self.assertTrue(active.is_dir())
        self.assertTrue((active / 'capture.bin').exists())
        self.assertRegex(active.name, r'^recording\.[A-Za-z0-9]+\.seg\d+$')
        self.assertFalse(any('Recording Completed' in path.read_text()
                             for path in (self.work / 'notifications').glob('event.*.json')))
        self.publish()
        self.assertTrue(list(pathlib.Path(self.env['STORAGE_DIR']).rglob('*part-0001.ts')))
        self.assertIsNone(process.poll())
        marker.touch()
        self.wait(process)
        self.publish()
        self.assertEqual(1, len((self.directory / 'calls').read_text().splitlines()))
        alerts = [json.loads(path.read_text()) for path in (self.work / 'notifications').glob('event.*.json')]
        self.assertEqual(1, sum('Recording Completed' in item['payload'].get('embeds', [{}])[0].get('title', '') for item in alerts))
        segmentation.audit_sessions(self.work)
        self.assertFalse(list((self.work / 'sessions').glob('recording.*')))

    def test_selected_second_audio_track_passes_segment_source_audit(self):
        dual = self.directory / 'dual.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.fixtures / 'source.ts'),
            '-f', 'lavfi', '-i', 'sine=frequency=1600:duration=12', '-map', '0:v', '-map', '0:a', '-map', '1:a',
            '-c:v', 'copy', '-c:a', 'aac', '-movflags', 'empty_moov+frag_keyframe+default_base_moof', str(dual)],
            check=True, capture_output=True)
        process = self.start(FIXTURE=str(dual), TWITCH_AUDIO_TRACK='2', YOUTUBE_SEGMENT_SECONDS='4')
        self.wait(process)
        self.publish()
        files = list(pathlib.Path(self.env['STORAGE_DIR']).rglob('*.mp4'))
        self.assertTrue(files)
        for file in files:
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(file)]))
            self.assertEqual(1, sum(s['codec_type'] == 'audio' for s in probe['streams']))
        segmentation.audit_sessions(self.work)
        self.assertFalse(list((self.work / 'sessions').glob('recording.*')))

    def test_all_processing_routes_preserve_frames_without_extra_remux(self):
        for container in ('ts', 'mp4'):
            for remux, upload, encode in ((r, u, e) for r in (False, True)
                                          for u in (False, True) for e in (False, True)):
                with self.subTest(container=container, remux=remux, upload=upload, encode=encode):
                    channel = f'{container}{int(remux)}{int(upload)}{int(encode)}'
                    self.env.update(CHANNEL=channel, REMUX=str(remux).lower(), UPLOAD=str(upload).lower(), ENCODE=str(encode).lower())
                    self.work = pathlib.Path(self.env['SCRATCH_ROOT']) / 'twitch' / channel / 'queue-v2'
                    (self.directory / 'ffmpeg-calls').write_text('')
                    process = self.start(FIXTURE=str(self.fixtures / ('source.' + container)), YOUTUBE_SEGMENT_SECONDS='4')
                    self.wait(process)
                    jobs = sorted((self.work / 'normalize').glob('recording.*'))
                    self.assertGreaterEqual(len(jobs), 3)
                    reports = [media.Tools(self.directory / 'source-checks').inspect(job / 'capture.bin', job)
                               for job in jobs]
                    self.assertEqual(120, sum(report['frames'][0] for report in reports))
                    self.publish()
                    extension = 'mp4' if container == 'mp4' or remux or encode else 'ts'
                    outputs = list((pathlib.Path(self.env['STORAGE_DIR']) / channel).rglob('*.' + extension))
                    self.assertEqual(len(jobs), len(outputs))
                    self.assertEqual(120, sum(media.Tools(self.directory / 'checks').inspect(file, file.parent)['frames'][0]
                                              for file in outputs))
                    commands = [json.loads(line) for line in (self.directory / 'ffmpeg-calls').read_text().splitlines()]
                    self.assertEqual(1, sum('copy' in command for command in commands), commands)
                    self.assertEqual(len(jobs) if encode else 0, sum('libx264' in command for command in commands), commands)
                    # Complete accounting permits raw-source cleanup only after every child is committed.
                    segmentation.audit_sessions(self.work)
                    self.assertFalse(list((self.work / 'sessions').glob('recording.*')))

    def test_changed_mp4_initialization_and_codec_are_separate_epochs(self):
        self.env['MODE'] = 'kick'
        self.work = pathlib.Path(self.env['SCRATCH_ROOT']) / 'kick/segments/queue-v2'
        process = self.start(FIXTURE=str(self.fixtures / 'periods/duplicate-changed-codec.mp4'))
        self.wait(process)
        jobs = list((self.work / 'normalize').glob('recording.*'))
        self.assertGreaterEqual(len(jobs), 2)
        self.assertEqual(70, sum(media.Tools(self.directory / 'codec-checks').inspect(job / 'capture.bin', job)['frames'][0]
                                 for job in jobs))
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(list((self.work / 'sessions').glob('recording.*')))

    @unittest.skipUnless(shutil.which('streamlink'), 'Requires Streamlink for local live HLS capture')
    def test_real_live_hls_stdout_publishes_before_endlist(self):
        web = self.directory / 'hls'
        web.mkdir()
        playlist = web / 'live.m3u8'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.fixtures / 'source.ts'),
                        '-c', 'copy', '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0',
                        str(playlist)], check=True, capture_output=True)
        full = playlist.read_text()
        lines = full.splitlines()
        entries = [index for index, line in enumerate(lines) if line.startswith('#EXTINF:')]
        playlist.write_text('\n'.join(lines[:entries[-2]]) + '\n')

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(web)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        real = shutil.which('streamlink')
        (self.bin / 'streamlink').write_text('#!/usr/bin/env python3\nimport os, sys\n'
            f'sys.argv[-2] = "hls://http://127.0.0.1:{server.server_port}/live.m3u8"\n'
            f'os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n')
        # This fixture intentionally pauses playlist updates during publication.
        # Reload attempts cover HTTP failures, not Streamlink's implicit EOF deadline.
        # Keep the read timeout above the 30s queue wait plus the 90s publish timeout.
        process = self.start(REMUX='true', STREAMLINK_EXTRA_ARGS=(
            '--hls-live-restart --hls-live-edge 99 --stream-segmented-queue-deadline 0 '
            '--stream-timeout 180'))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not list((self.work / 'normalize').glob('recording.*')):
            if process.poll() is not None:
                self.fail(self.log.read_text())
            time.sleep(0.1)
        self.assertTrue(list((self.work / 'normalize').glob('recording.*')), self.log.read_text())
        self.assertIsNone(process.poll(), self.log.read_text())
        self.publish()
        self.assertTrue(list(pathlib.Path(self.env['STORAGE_DIR']).rglob('*part-0001.mp4')))
        self.assertIsNone(process.poll(), self.log.read_text())
        temporary = playlist.with_suffix('.tmp')
        temporary.write_text(full)
        temporary.replace(playlist)
        self.wait(process)
        self.publish()
        outputs = list(pathlib.Path(self.env['STORAGE_DIR']).rglob('*.mp4'))
        self.assertEqual(120, sum(media.Tools(self.directory / 'checks').inspect(file, file.parent)['frames'][0]
                                  for file in outputs))
        segmentation.audit_sessions(self.work)
        self.assertFalse(list((self.work / 'sessions').glob('recording.*')))

    def test_failed_source_retains_unconfirmed_tail_and_raw_capture(self):
        process = self.start(FAIL_SOURCE='true')
        self.assertNotEqual(0, process.wait(timeout=30))
        self.assertTrue(list((self.work / 'sessions').glob('recording.*/capture.bin')))
        self.assertFalse((self.work / 'active.job').exists())
        self.assertTrue(list((self.work / 'active').glob('recording.*.seg*')))
        before = list((self.work / 'active').glob('recording.*'))
        self.shell('AUTO_RECOVER_INTERRUPTED=false recover_completed_recordings')
        self.assertEqual(before, list((self.work / 'active').glob('recording.*')))
        self.shell('AUTO_RECOVER_INTERRUPTED=true recover_completed_recordings')
        self.assertFalse(list((self.work / 'active').glob('recording.*')))
        session = next((self.work / 'sessions').glob('recording.*'))
        self.assertEqual('completed', json.loads((session / 'session.json').read_text())['status'])
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(session.exists())

    def test_empty_source_aborts_remove_session_children_pointer_and_capture_lock(self):
        for status in ('0', '1'):
            with self.subTest(status=status):
                process = self.start(EMPTY_SOURCE='true', EMPTY_EXIT=status)
                self.assertNotEqual(0, process.wait(timeout=30))
                self.assertFalse((self.work / 'active.job').exists())
                self.assertFalse(list((self.work / 'active').glob('recording.*')))
                self.assertFalse(list((self.work / 'sessions').glob('recording.*')))
                self.assertFalse(list(self.work.glob('capture-recording.*.lock')))
                self.assertFalse(list((self.work / 'normalize').glob('recording.*')))

    def test_empty_source_launch_failure_removes_job_but_short_capture_is_retained(self):
        self.shell('new_recording_job missing Author 123')
        parent = next((self.work / 'active').glob('recording.*'))
        with patch.dict(os.environ, self.env):
            capture = segmentation.Capture(parent, [str(self.bin / 'missing-streamlink')], 2)
            self.assertEqual(1, capture.run())
        self.assertFalse(capture.session.exists())
        self.assertFalse(capture.child_path(1).exists())
        self.assertFalse(pathlib.Path(capture.writer_lock.name).exists())
        fixture = self.directory / 'short.bin'
        fixture.write_bytes(b'partial')
        process = self.start(FIXTURE=str(fixture), FAIL_SOURCE='true')
        self.assertNotEqual(0, process.wait(timeout=30))
        session = next((self.work / 'sessions').glob('recording.*'))
        self.assertEqual(b'partial', (session / 'capture.bin').read_bytes())
        self.assertTrue(list((self.work / 'active').glob('recording.*')))
        self.assertTrue((self.work / f'capture-{session.name}.lock').exists())

    def test_empty_source_stopped_before_first_byte_cleans_up(self):
        self.shell('new_recording_job stopped Author 123')
        parent = next((self.work / 'active').glob('recording.*'))
        output = self.log.open('wb')
        self.addCleanup(output.close)
        process = subprocess.Popen([sys.executable, str(ROOT / 'segment_capture.py'), 'capture',
                                    str(parent), '--', sys.executable, '-c', 'import time; time.sleep(60)'],
                                   env=self.env, stdout=output, stderr=output)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        session = self.work / 'sessions' / parent.name
        deadline = time.monotonic() + 15
        while not (session / 'capture.bin').exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(self.log.read_text())
            time.sleep(0.05)
        self.assertTrue((session / 'capture.bin').exists(), self.log.read_text())
        self.assertEqual(0, (session / 'capture.bin').stat().st_size)
        process.terminate()
        self.assertNotEqual(0, process.wait(timeout=20))
        self.assertFalse(session.exists(), self.log.read_text())
        self.assertFalse(list((self.work / 'active').glob('recording.*')))
        self.assertFalse((self.work / 'active.job').exists())
        self.assertFalse((self.work / f'capture-{session.name}.lock').exists())

    def interrupted_tail(self, fixture='source.mp4'):
        self.wait(self.start(FIXTURE=str(self.fixtures / fixture), YOUTUBE_SEGMENT_SECONDS='4'))
        session = next((self.work / 'sessions').glob('recording.*'))
        jobs = sorted((self.work / 'normalize').glob('recording.*'),
                      key=lambda job: int((job / 'segment-index').read_text()))
        tail = jobs[-1]
        index = int((tail / 'segment-index').read_text())
        active = self.work / 'active' / tail.name
        tail.rename(active)
        (active / 'completed').unlink()
        (active / 'segment-incomplete').touch()
        extension = 'mp4' if fixture.endswith('mp4') else 'ts'
        os.link(active / 'capture.bin', session / 'outputs' / f'segment-{index}.{extension}')
        manifest = json.loads((session / 'session.json').read_text())
        manifest.update(status='interrupted', segments=list(range(1, index)), source_periods=[])
        (session / 'session.json').write_text(json.dumps(manifest))
        (session / 'broadcast-finished').unlink()
        self.publish()  # Already published children must never be queued/uploaded twice.
        return session, active, index

    def test_offline_recovery_reuses_closed_tail_without_remux_and_is_idempotent(self):
        session, active, index = self.interrupted_tail()
        original = media.digest(session / 'capture.bin')
        calls = [json.loads(line) for line in (self.directory / 'ffmpeg-calls').read_text().splitlines()]
        self.shell('bash "$1/recover.sh" "$WORK_DIR/sessions/' + session.name + '"')
        after = [json.loads(line) for line in (self.directory / 'ffmpeg-calls').read_text().splitlines()]
        self.assertFalse(any('copy' in command or 'libx264' in command for command in after[len(calls):]))
        self.assertEqual(original, media.digest(session / 'capture.bin'))
        self.assertFalse(active.exists())
        recovered = list((self.work / 'normalize').glob('recording.*'))
        self.assertEqual([index], [int((job / 'segment-index').read_text()) for job in recovered])
        self.shell('bash "$1/recover.sh" "$WORK_DIR/sessions/' + session.name + '"')
        self.assertEqual(recovered, list((self.work / 'normalize').glob('recording.*')))
        events = [json.loads(path.read_text())['payload'] for path in (self.work / 'notifications').glob('event.*.json')]
        self.assertEqual(1, sum('Interrupted recording recovered' in json.dumps(event) for event in events))
        self.assertEqual(1, sum('Recording Completed' in json.dumps(event) for event in events))  # Original successful fixture only.
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(session.exists())

    def test_recovery_rebuilds_unfinished_mp4_from_raw_with_one_stream_copy(self):
        session, active, index = self.interrupted_tail()
        original = media.digest(session / 'capture.bin')
        # Simulate a killed muxer that never wrote its MP4 trailer/listing entry.
        (active / 'capture.bin').write_bytes(b'no MP4 trailer')
        for listing in session.glob('epoch-*.csv'):
            rows = listing.read_text().splitlines(keepends=True)
            listing.write_text(''.join(rows[:-1]))
        before = [json.loads(line) for line in (self.directory / 'ffmpeg-calls').read_text().splitlines()]
        self.shell('AUTO_RECOVER_INTERRUPTED=true recover_completed_recordings', YOUTUBE_SEGMENT_SECONDS='2')
        # Saved segment duration wins over the current environment.
        self.assertEqual('completed', json.loads((session / 'session.json').read_text())['status'])
        after = [json.loads(line) for line in (self.directory / 'ffmpeg-calls').read_text().splitlines()]
        self.assertEqual(1, sum('copy' in command for command in after[len(before):]))
        self.assertFalse(any('libx264' in command for command in after[len(before):]))
        self.assertEqual(original, media.digest(session / 'capture.bin'))
        self.assertEqual([index], [int((job / 'segment-index').read_text())
                                  for job in (self.work / 'normalize').glob('recording.*')])
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(session.exists())

    def test_recovery_refuses_live_writer_and_retains_original_on_low_space_or_bad_receipt(self):
        session, active, _ = self.interrupted_tail('source.ts')
        original = media.digest(session / 'capture.bin')
        with segmentation.lock(self.work / f'capture-{session.name}.lock') as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(recovery.CaptureError):
                recovery.recover_session(self.work, session)
        with patch.object(recovery, 'require_space', side_effect=segmentation.LowSpace('injected')):
            with self.assertRaises(segmentation.LowSpace):
                recovery.recover_session(self.work, session)
        receipt = next(session.glob('seg*.published.json'))
        value = json.loads(receipt.read_text())
        value['video'][0][0]['sha256'] = '0' * 64
        receipt.write_text(json.dumps(value))
        with patch.dict(os.environ, self.env):
            with self.assertRaises(recovery.CaptureError):
                recovery.recover_session(self.work, session)
        self.assertEqual(original, media.digest(session / 'capture.bin'))
        self.assertTrue(active.exists())
        self.assertFalse(list((self.work / 'normalize').glob('recording.*')))
        self.assertEqual('interrupted', json.loads((session / 'session.json').read_text())['status'])

    def test_recovery_of_multiple_mp4_epochs(self):
        session, _, _ = self.interrupted_tail('periods/duplicate-changed-codec.mp4')
        self.shell('AUTO_RECOVER_INTERRUPTED=true recover_completed_recordings')
        self.assertEqual('completed', json.loads((session / 'session.json').read_text())['status'])
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(session.exists())

    def test_killed_controller_keeps_writer_lock_until_orphan_source_exits(self):
        self.shell('new_recording_job crashed Author 123')
        parent = next((self.work / 'active').glob('recording.*'))
        marker = self.directory / 'release-orphan'
        self.addCleanup(marker.touch)
        output = self.log.open('wb')
        self.addCleanup(output.close)
        process = subprocess.Popen([sys.executable, str(ROOT / 'segment_capture.py'), 'capture',
                                    str(parent), '--', str(self.bin / 'streamlink'), '--stdout'],
                                   env=dict(self.env, HOLD=str(marker)), stdout=output, stderr=output)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        session = self.work / 'sessions' / parent.name
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            source = session / 'capture.bin'
            if source.exists() and source.stat().st_size == pathlib.Path(self.env['FIXTURE']).stat().st_size:
                break
            if process.poll() is not None:
                self.fail(self.log.read_text())
            time.sleep(0.05)
        else:
            self.fail('Capture did not drain the source fixture')
        process.kill()
        process.wait(timeout=10)
        with self.assertRaises(recovery.CaptureError):
            recovery.recover_session(self.work, session)
        marker.touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with segmentation.lock(self.work / f'capture-{session.name}.lock') as acquired:
                if acquired:
                    break
            time.sleep(0.05)
        else:
            self.fail('Orphan writer did not exit')
        self.shell('AUTO_RECOVER_INTERRUPTED=true recover_completed_recordings')
        self.assertEqual('completed', json.loads((session / 'session.json').read_text())['status'])
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(session.exists())

    def test_scratch_guard_includes_active_segment_processing_headroom(self):
        with patch.dict(os.environ, MIN_FREE_SPACE_GB='0'):
            with self.assertRaises(segmentation.LowSpace):
                segmentation.require_space(self.directory, 100, lambda _: SimpleNamespace(free=segmentation.GIB + 199))
            segmentation.require_space(self.directory, 100, lambda _: SimpleNamespace(free=segmentation.GIB + 200))
        with patch.dict(os.environ, MIN_FREE_SPACE_GB='5'):
            with self.assertRaises(segmentation.LowSpace):
                segmentation.require_space(self.directory, 0, lambda _: SimpleNamespace(free=4 * segmentation.GIB))

    def test_source_frame_identity_is_checked_before_deleting_raw(self):
        process = self.start(YOUTUBE_SEGMENT_SECONDS='4')
        self.wait(process)
        self.publish()
        session = next((self.work / 'sessions').glob('recording.*'))
        receipt = next(session.glob('seg*.published.json'))
        value = json.loads(receipt.read_text())
        value['video'][0][0]['sha256'] = '0' * 64
        receipt.write_text(json.dumps(value))
        segmentation.audit_sessions(self.work)
        self.assertTrue((session / 'capture.bin').exists())
        self.assertEqual('audit-failed', json.loads((session / 'session.json').read_text())['status'])

    def test_audit_waits_for_space_and_session_lock_without_losing_original(self):
        process = self.start(YOUTUBE_SEGMENT_SECONDS='4')
        self.wait(process)
        self.publish()
        session = next((self.work / 'sessions').glob('recording.*'))
        with patch.object(segmentation, 'require_space', side_effect=segmentation.LowSpace('injected')):
            segmentation.audit_sessions(self.work)
        self.assertEqual('completed', json.loads((session / 'session.json').read_text())['status'])
        with segmentation.lock(self.work / f'session-{session.name}.lock') as acquired:
            self.assertTrue(acquired)
            segmentation.audit_sessions(self.work)
            self.assertTrue((session / 'capture.bin').exists())
        segmentation.audit_sessions(self.work)
        self.assertFalse(session.exists())

    def test_low_space_during_rotation_stops_safely_and_alerts(self):
        self.env['DISCORD_WEBHOOK_URL'] = ''
        self.shell('new_recording_job disk Author 123')
        parent = next((self.work / 'active').glob('recording.*'))
        hold = self.directory / 'never-finish'
        environment = dict(self.env, HOLD=str(hold))
        with patch.dict(os.environ, environment):
            capture = segmentation.Capture(parent, [str(self.bin / 'streamlink'), '--stdout'], 2)
            real_check = segmentation.require_space

            def check(*args, **kwargs):
                if capture.current > 1:
                    raise segmentation.LowSpace('Injected low disk during rotation')
                return real_check(*args, **kwargs)

            with patch.object(segmentation, 'require_space', side_effect=check):
                self.assertEqual(1, capture.run())
        self.assertFalse((self.work / 'active.job').exists())
        self.assertGreater((capture.session / 'capture.bin').stat().st_size, 0)
        self.assertEqual('interrupted', json.loads((capture.session / 'session.json').read_text())['status'])
        alerts = list((self.work / 'notifications').glob('event.*.json'))
        self.assertTrue(any('Low Disk Space' in path.read_text() for path in alerts))


if __name__ == '__main__':
    unittest.main(verbosity=2)
