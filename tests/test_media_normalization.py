"""Media normalization, completeness, and source-retention regressions.

Requires FFmpeg/ffprobe; Streamlink enables the offline HLS integration test.
TEST_MEDIA_INTRO_SECONDS and TEST_MEDIA_MAIN_SECONDS control fixture durations.
"""
import functools
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('media', ROOT / 'media.py')
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


class MediaNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='media-normalization-')
        cls.directory = pathlib.Path(cls.temporary.name)
        cls.fixtures = cls.directory / 'fixtures'
        subprocess.run(['python3', str(ROOT / 'tests/generate_media_fixtures.py'),
                        '--intro-seconds', os.getenv('TEST_MEDIA_INTRO_SECONDS', '2'),
                        '--main-seconds', os.getenv('TEST_MEDIA_MAIN_SECONDS', '1'), '--output', str(cls.fixtures)], check=True)
        cls.intro = int(os.getenv('TEST_MEDIA_INTRO_SECONDS', '2')) * 10
        cls.main = int(os.getenv('TEST_MEDIA_MAIN_SECONDS', '1')) * 10

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.job = self.directory / self.id().split('.')[-1]
        self.job.mkdir()

    def tearDown(self):
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        if failed and (self.job / 'routes.log').exists():
            print((self.job / 'routes.log').read_text(errors='replace')[-4000:])
        if os.getenv('TEST_ARTIFACT_DIR') and failed:
            target = pathlib.Path(os.environ['TEST_ARTIFACT_DIR']) / self.job.name
            shutil.copytree(self.job, target, dirs_exist_ok=True)
            shutil.copytree(self.fixtures, target / 'fixtures', dirs_exist_ok=True)

    def generate(self, name, audio=True, vfr=False, fragmented=False, audio_offset=0,
                 audio_rate=44100, channels=1):
        path = self.job / name
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                   'testsrc2=size=160x90:rate=10:duration=2']
        if audio:
            command += ['-itsoffset', str(audio_offset), '-f', 'lavfi', '-i',
                        'sine=frequency=800:duration=2', '-c:a', 'aac', '-ar', str(audio_rate), '-ac', str(channels)]
        if vfr:
            command += ['-vf', r"select=not(eq(mod(n\,3)\,1))", '-fps_mode', 'vfr']
        command += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-bf', '2', '-g', '10']
        if fragmented:
            command += ['-movflags', 'empty_moov+frag_keyframe+default_base_moof']
        subprocess.run(command + [str(path)], check=True)
        return path

    def capture(self, fixture):
        shutil.copyfile(self.fixtures / fixture, self.job / 'capture.bin')
        self.original = media.digest(self.job / 'capture.bin')

    def normalize(self):
        media.normalize(self.job)
        self.assertEqual(self.original, media.digest(self.job / 'capture.bin'))
        return json.loads((self.job / 'normalized.json').read_text())

    def cli(self, *arguments):
        result = subprocess.run(['python3', str(ROOT / 'media.py'), arguments[0], str(self.job),
                                 *map(str, arguments[1:])], capture_output=True, text=True, timeout=60)
        (self.job / 'cli.stdout').write_text(result.stdout)
        (self.job / 'cli.stderr').write_text(result.stderr)
        return result

    def assert_quarantined(self, result):
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual('quarantine', json.loads((self.job / 'validation.json').read_text())['status'])
        self.assertTrue((self.job / 'capture.bin').exists())

    def test_healthy(self):
        self.capture('control-single-init.mp4')
        manifest = self.normalize()
        self.assertEqual([self.intro + self.main], [p['validation']['frames'][0] for p in manifest['parts']])

    def test_compatible_headers_merge_losslessly(self):
        self.capture('duplicate-same-codec.mp4')
        manifest = self.normalize()
        self.assertEqual(2, len(manifest['periods']))
        self.assertEqual([self.intro + self.main], [p['validation']['frames'][0] for p in manifest['parts']])

    def test_changed_codec_preserves_both_parts(self):
        self.capture('duplicate-changed-codec.mp4')
        manifest = self.normalize()
        self.assertEqual([self.intro, self.main], [p['validation']['frames'][0] for p in manifest['parts']])
        for period, part in zip(manifest['periods'], manifest['parts']):
            self.assertEqual(period['validation']['frame_hashes'], part['validation']['frame_hashes'])
            self.assertLess(part['validation']['start_times'][0], 0.2)

    def test_complete_concatenated_files_preserve_both_parts(self):
        self.capture('concatenated-normal-mp4.mp4')
        self.assertEqual([self.intro, self.main], [p['validation']['frames'][0] for p in self.normalize()['parts']])

    def test_wrong_initialization_is_retained(self):
        self.capture('mismatched-first-init.mp4')
        with self.assertRaises(media.UnsafeMedia):
            self.normalize()
        self.assertEqual(self.original, media.digest(self.job / 'capture.bin'))
        self.assertFalse((self.job / 'normalized.json').exists())

    def test_truncated_box_is_retained(self):
        self.capture('duplicate-changed-codec.mp4')
        with (self.job / 'capture.bin').open('r+b') as output:
            output.truncate((self.job / 'capture.bin').stat().st_size - 20)
        with self.assertRaises(media.UnsafeMedia):
            media.normalize(self.job)
        self.assertTrue((self.job / 'capture.bin').exists())

    def test_open_ended_box_is_uncertain(self):
        self.capture('control-single-init.mp4')
        with (self.job / 'capture.bin').open('r+b') as source:
            source.write(b'\0\0\0\0')
        with self.assertRaises(media.UnsafeMedia):
            media.normalize(self.job)
        self.assertTrue((self.job / 'capture.bin').exists())

    def test_extended_box_size_and_invalid_size(self):
        path = self.job / 'box'
        path.write_bytes(struct.pack('>I4sQ', 1, b'free', 20) + b'abcd')
        with path.open('rb') as source:
            self.assertEqual([(b'free', 0, 16, 20)], list(media.boxes(source, 0, 20)))
        with path.open('rb') as source, self.assertRaises(media.UnsafeMedia):
            list(media.boxes(source, 0, 19))

    def test_sample_count_mismatch(self):
        self.capture('normal-intro.mp4')
        data = bytearray((self.job / 'capture.bin').read_bytes())
        position = data.index(b'stsz') + 12
        struct.pack_into('>I', data, position, self.intro + 1)
        (self.job / 'capture.bin').write_bytes(data)
        with self.assertRaises(media.UnsafeMedia):
            media.normalize(self.job)

    def test_legacy_extension_is_not_trusted(self):
        self.capture('duplicate-changed-codec.mp4')
        (self.job / 'capture.bin').rename(self.job / 'media.ts')
        media.normalize(self.job)
        manifest = json.loads((self.job / 'normalized.json').read_text())
        self.assertEqual('mp4', manifest['capture']['format'])
        self.assertEqual(2, len(manifest['parts']))
        self.assertEqual(self.original, media.digest(self.job / 'media.ts'))

    def test_zero_exit_with_decoder_errors_is_rejected(self):
        with self.assertRaises(media.UnsafeMedia):
            media.Tools(self.job / 'diagnostics').run('false-success',
                ['bash', '-c', 'echo "decode_slice_header error" >&2; exit 0'])

    def test_encoding_cannot_silently_drop_frames(self):
        self.capture('control-single-init.mp4')
        self.normalize()
        truncated = self.job / 'short.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.job / 'parts/0001.mp4'),
                        '-t', '0.5', '-c', 'copy', str(truncated)], check=True)
        result = subprocess.run(['python3', str(ROOT / 'media.py'), 'verify', str(self.job),
                                 '0001', str(truncated), '--encoded'], stderr=subprocess.PIPE)
        self.assertEqual(2, result.returncode)
        self.assertEqual(self.original, media.digest(self.job / 'capture.bin'))
        self.assertFalse((self.job / 'parts/0001.encoded.json').exists())

    def test_audio_b_frames_and_variable_rate(self):
        source = self.generate('source.mp4', vfr=True)
        shutil.copyfile(source, self.job / 'capture.bin')
        self.original = media.digest(source)
        manifest = self.normalize()
        part = manifest['parts'][0]['validation']
        self.assertEqual(['video', 'audio'], part['stream_types'])
        self.assertEqual(13, part['frames'][0])
        self.assertEqual(manifest['periods'][0]['validation']['timing_hashes'][0], part['timing_hashes'][0])

    def test_audio_and_track_layout_change(self):
        with_audio = self.generate('audio.mp4')
        silent = self.generate('silent.mp4', audio=False)
        (self.job / 'capture.bin').write_bytes(with_audio.read_bytes() + silent.read_bytes())
        self.original = media.digest(self.job / 'capture.bin')
        manifest = self.normalize()
        self.assertEqual([['video', 'audio'], ['video']], [p['validation']['stream_types'] for p in manifest['parts']])

    def test_timestamp_reset_with_b_frames(self):
        source = self.generate('period.mp4', audio=False, fragmented=True)
        (self.job / 'capture.bin').write_bytes(source.read_bytes() * 2)
        self.original = media.digest(self.job / 'capture.bin')
        self.assertEqual(40, self.normalize()['parts'][0]['validation']['frames'][0])

    def test_compatible_audio_periods(self):
        source = self.generate('period.mp4', fragmented=True)
        (self.job / 'capture.bin').write_bytes(source.read_bytes() * 2)
        self.original = media.digest(self.job / 'capture.bin')
        manifest = self.normalize()
        self.assertEqual(40, sum(p['validation']['frames'][0] for p in manifest['parts']))

    def test_operational_io_failure_is_retryable(self):
        with self.assertRaises(OSError):
            media.Tools(self.job / 'diagnostics').run('disk-full',
                ['bash', '-c', 'echo "No space left on device" >&2; exit 1'])

    def test_cli_normalization_replaces_only_incomplete_candidates(self):
        self.capture('control-single-init.mp4')
        (self.job / 'parts').mkdir()
        (self.job / 'parts/0001.mp4').write_bytes(b'partial output')
        result = self.cli('normalize')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.original, media.digest(self.job / 'capture.bin'))
        self.assertEqual(0, self.cli('check').returncode)
        manifest = (self.job / 'normalized.json').read_bytes()
        self.assert_quarantined(self.cli('normalize'))
        self.assertEqual(manifest, (self.job / 'normalized.json').read_bytes())

    def test_capture_checksum_change_is_quarantined(self):
        self.capture('control-single-init.mp4')
        self.normalize()
        with (self.job / 'capture.bin').open('ab') as output:
            output.write(b'changed')
        self.assert_quarantined(self.cli('check'))

    def test_part_checksum_change_is_quarantined(self):
        self.capture('control-single-init.mp4')
        self.normalize()
        with (self.job / 'parts/0001.mp4').open('ab') as output:
            output.write(b'changed')
        self.assert_quarantined(self.cli('check'))
        self.assertEqual(self.original, media.digest(self.job / 'capture.bin'))

    def test_missing_part_is_retryable_without_deleting_capture(self):
        self.capture('control-single-init.mp4')
        self.normalize()
        (self.job / 'parts/0001.mp4').unlink()
        self.assertEqual(1, self.cli('check').returncode)
        self.assertEqual('retry', json.loads((self.job / 'validation.json').read_text())['status'])
        self.assertEqual(self.original, media.digest(self.job / 'capture.bin'))

    def test_unsafe_manifest_paths_and_versions_are_rejected(self):
        self.capture('control-single-init.mp4')
        original = self.normalize()
        mutations = [lambda m: m.update(version=0), lambda m: m.update(parts=[]),
                     lambda m: m['capture'].update(file='../outside'),
                     lambda m: m['parts'][0].update(file='../outside')]
        for mutate in mutations:
            manifest = json.loads(json.dumps(original))
            mutate(manifest)
            with self.subTest(manifest=manifest):
                (self.job / 'normalized.json').write_text(json.dumps(manifest))
                self.assert_quarantined(self.cli('check'))

    def test_encoder_error_log_cannot_certify_success(self):
        self.capture('control-single-init.mp4')
        self.normalize()
        (self.job / 'diagnostics/encode-0001.stderr').write_text('decode_slice_header error\n')
        self.assert_quarantined(self.cli('verify', '0001', self.job / 'parts/0001.mp4', '--encoded'))
        self.assertFalse((self.job / 'parts/0001.encoded.json').exists())

    def test_publication_rejects_changed_packet_content(self):
        self.capture('control-single-init.mp4')
        self.normalize()
        changed = self.job / 'changed.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.job / 'parts/0001.mp4'),
                        '-c:v', 'libx264', '-crf', '35', str(changed)], check=True)
        self.assert_quarantined(self.cli('verify', '0001', changed, '--publication'))

    def test_publication_rejects_changed_timing_with_identical_packets(self):
        self.capture('control-single-init.mp4')
        manifest = self.normalize()
        shifted = self.job / 'shifted.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-itsoffset', '1', '-i',
                        str(self.job / 'parts/0001.mp4'), '-c', 'copy', '-copyts', str(shifted)], check=True)
        actual = media.Tools(self.job / 'shift-check').inspect(shifted, self.job, deep=False)
        self.assertEqual(manifest['parts'][0]['validation']['packet_hashes'], actual['packet_hashes'])
        self.assert_quarantined(self.cli('verify', '0001', shifted, '--publication'))

    def test_empty_unknown_and_audio_only_captures_are_retained(self):
        for payload in (b'', b'not a recognized container'):
            with self.subTest(payload=payload):
                (self.job / 'capture.bin').write_bytes(payload)
                self.assert_quarantined(self.cli('normalize'))
                self.assertEqual(payload, (self.job / 'capture.bin').read_bytes())
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=duration=1', '-c:a', 'aac', '-f', 'mp4', '-y', str(self.job / 'capture.bin')], check=True)
        self.assert_quarantined(self.cli('normalize'))

    def test_fragment_track_associations_must_be_unambiguous(self):
        original = (self.fixtures / 'intro.mp4').read_bytes()
        for unknown_track, absolute_offset in ((True, False), (False, True)):
            data = bytearray(original)
            position = data.index(b'tfhd')
            if unknown_track:
                struct.pack_into('>I', data, position + 8, 9999)
            if absolute_offset:
                flags = struct.unpack_from('>I', data, position + 4)[0]
                struct.pack_into('>I', data, position + 4, flags | 1)
            with self.subTest(unknown_track=unknown_track, absolute_offset=absolute_offset):
                (self.job / 'capture.bin').write_bytes(data)
                self.assert_quarantined(self.cli('normalize'))
                self.assertEqual(data, (self.job / 'capture.bin').read_bytes())

    def test_duplicate_moov_without_file_boundary_is_retained(self):
        path = self.fixtures / 'duplicate-same-codec.mp4'
        data = path.read_bytes()
        with path.open('rb') as source:
            atoms = list(media.boxes(source, 0, len(data)))
        seen_ftyp = False
        output = bytearray()
        for kind, begin, _, end in atoms:
            if kind == b'ftyp':
                if seen_ftyp:
                    continue
                seen_ftyp = True
            output.extend(data[begin:end])
        (self.job / 'capture.bin').write_bytes(output)
        self.assert_quarantined(self.cli('normalize'))
        self.assertEqual(output, (self.job / 'capture.bin').read_bytes())

    def test_audio_offset_is_preserved(self):
        source = self.generate('offset.mp4', audio_offset=0.3)
        shutil.copyfile(source, self.job / 'capture.bin')
        self.original = media.digest(source)
        manifest = self.normalize()
        expected = manifest['periods'][0]['validation']['start_times']
        actual = manifest['parts'][0]['validation']['start_times']
        self.assertGreater(expected[1] - expected[0], 0.2)
        self.assertAlmostEqual(expected[1] - expected[0], actual[1] - actual[0], delta=0.025)

    def test_audio_configuration_changes_produce_separate_parts(self):
        first = self.generate('mono.mp4')
        second = self.generate('stereo.mp4', audio_rate=48000, channels=2)
        (self.job / 'capture.bin').write_bytes(first.read_bytes() + second.read_bytes())
        self.original = media.digest(self.job / 'capture.bin')
        manifest = self.normalize()
        self.assertEqual(2, len(manifest['parts']))
        audio = [part['validation']['signature'][1] for part in manifest['parts']]
        self.assertEqual([1, 2], [stream['channels'] for stream in audio])
        self.assertEqual(['44100', '48000'], [stream['sample_rate'] for stream in audio])

    @unittest.skipUnless(shutil.which('streamlink'), 'Streamlink HLS capture runs in the built image')
    def test_real_streamlink_hls_capture(self):
        web = self.job / 'hls'
        web.mkdir()
        for name in ('intro', 'main-new-codec'):
            with (self.fixtures / f'{name}.mp4').open('rb') as source:
                data = source.read()
                atoms = list(media.boxes(source, 0, len(data)))
            (web / f'{name}-init.mp4').write_bytes(b''.join(data[start:end] for kind, start, _, end in atoms if kind in (b'ftyp', b'moov')))
            (web / f'{name}.m4s').write_bytes(b''.join(data[start:end] for kind, start, _, end in atoms if kind in (b'moof', b'mdat')))
        seconds = self.intro // 10
        (web / 'test.m3u8').write_text('#EXTM3U\n#EXT-X-VERSION:7\n'
            f'#EXT-X-TARGETDURATION:{max(seconds, self.main // 10)}\n#EXT-X-MEDIA-SEQUENCE:0\n'
            f'#EXT-X-MAP:URI="intro-init.mp4"\n#EXTINF:{seconds},\nintro.m4s\n'
            '#EXT-X-DISCONTINUITY\n#EXT-X-MAP:URI="main-new-codec-init.mp4"\n'
            f'#EXTINF:{self.main // 10},\nmain-new-codec.m4s\n#EXT-X-ENDLIST\n')

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(web)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(['streamlink', '--hls-playlist-reload-attempts', '1', '--output', str(self.job / 'capture.bin'),
                            f'hls://http://127.0.0.1:{server.server_port}/test.m3u8', 'best'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            script = r'''
set -euo pipefail
source "$1/download.sh"
source "$1/upload.sh"
source "$1/encode.sh"
streamlink() {
    local args=("$@")
    args[-2]="hls://$LOCAL_HLS_URL"
    command streamlink "${args[@]}"
}
invoke_uploader() { printf 'part\n' >> "$SCRATCH_ROOT/uploads"; }
for MODE in twitch kick; do
    pipeline_init
    for REMUX in false true; do
        for UPLOAD in false true; do
            for ENCODE in false true; do
                record_stream 'HLS boundary' media-test 1
                id="${RECORDING_JOB##*/}"
                normalize_job "$WORK_DIR/normalize/$id"
                if [[ "$UPLOAD" == true ]]; then upload_job "$WORK_DIR/upload/$id"; fi
                if [[ "$ENCODE" == true ]]; then
                    encode_job "$WORK_DIR/encode/$id"
                else
                    publish_recording "$WORK_DIR/publish/$id"
                fi
            done
        done
    done
done
'''
            environment = dict(os.environ, CHANNEL='media-test', SCRATCH_ROOT=str(self.job / 'scratch'),
                               STORAGE_DIR=str(self.job / 'storage'), UPLOAD_BOT_NAME='offline',
                               LOCAL_HLS_URL=f'http://127.0.0.1:{server.server_port}/test.m3u8',
                               DISCORD_WEBHOOK_URL='', TWITCH_USER_TOKEN='')
            with (self.job / 'routes.log').open('wb') as log:
                subprocess.run(['bash', '-c', script, 'regression', str(ROOT)], env=environment,
                               stdout=log, stderr=log, check=True, timeout=180)
            outputs = list((self.job / 'storage').rglob('*.mp4'))
            self.assertEqual(32, len(outputs))
            self.assertEqual(16, len((self.job / 'scratch/uploads').read_text().splitlines()))
            for path in outputs:
                report = media.Tools(self.job / 'output-checks').inspect(path, self.job)
                self.assertEqual(self.intro if 'part-0001' in path.name else self.main, report['frames'][0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.original = media.digest(self.job / 'capture.bin')
        self.assertEqual([self.intro, self.main], [p['validation']['frames'][0] for p in self.normalize()['parts']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
