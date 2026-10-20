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
        process = self.start(REMUX='true', STREAMLINK_EXTRA_ARGS='--hls-live-restart --hls-live-edge 99 --hls-playlist-reload-attempts 20')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not list((self.work / 'normalize').glob('recording.*')):
            if process.poll() is not None:
                self.fail(self.log.read_text())
            time.sleep(0.1)
        self.assertTrue(list((self.work / 'normalize').glob('recording.*')), self.log.read_text())
        self.publish()
        self.assertTrue(list(pathlib.Path(self.env['STORAGE_DIR']).rglob('*part-0001.mp4')))
        self.assertIsNone(process.poll())
        temporary = playlist.with_suffix('.tmp')
        temporary.write_text(full)
        temporary.replace(playlist)
        self.wait(process)
        self.publish()
        segmentation.audit_sessions(self.work)
        self.assertFalse(list((self.work / 'sessions').glob('recording.*')))

    def test_failed_source_retains_unconfirmed_tail_and_raw_capture(self):
        process = self.start(FAIL_SOURCE='true')
        self.assertNotEqual(0, process.wait(timeout=30))
        self.assertTrue(list((self.work / 'sessions').glob('recording.*/capture.bin')))
        self.assertFalse((self.work / 'active.job').exists())
        self.assertTrue(list((self.work / 'active').glob('recording.*.seg*')))
        before = list((self.work / 'active').glob('recording.*'))
        self.shell('AUTO_RECOVER_INTERRUPTED=true recover_completed_recordings')
        self.assertEqual(before, list((self.work / 'active').glob('recording.*')))

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
