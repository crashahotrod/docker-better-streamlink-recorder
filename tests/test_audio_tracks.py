"""Real dual-audio normalization, track selection, and publication checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Requires FFmpeg')
class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'dual.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=160x90:rate=10:duration=1', '-f', 'lavfi', '-i',
            'sine=frequency=400:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=1600:duration=1',
            '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-c:a', 'aac',
            '-metadata:s:a:0', 'title=Live', '-metadata:s:a:1', 'title=VOD', str(self.source)],
            check=True, capture_output=True)

    def job(self, policy):
        job = self.root / ('job-' + policy)
        job.mkdir()
        shutil.copyfile(self.source, job / 'capture.bin')
        (job / 'audio-track').write_text(policy)
        (job / 'remux-required').write_text('true')
        return job

    def test_all_and_selected_tracks_keep_expected_audio(self):
        original = media.Tools(self.root / 'probe').inspect(self.source, self.root)
        for policy in ('all', '1', '2'):
            with self.subTest(policy=policy):
                job = self.job(policy)
                before = media.digest(job / 'capture.bin')
                media.normalize(job)
                manifest = json.loads((job / 'normalized.json').read_text())
                media.validate_manifest(manifest)
                report = manifest['parts'][0]['validation']
                self.assertEqual(2 if policy == 'all' else 1, report['stream_types'].count('audio'))
                expected = media.select_audio_report(original, policy)
                self.assertEqual(expected['packet_hashes'], report['packet_hashes'])
                self.assertEqual(before, media.digest(job / 'capture.bin'))

    def test_missing_track_keeps_original_without_manifest(self):
        job = self.job('2')
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(self.source),
                        '-map', '0:v', '-map', '0:a:0', '-c', 'copy', '-f', 'mp4', str(job / 'capture.bin')],
                       check=True, capture_output=True)
        before = media.digest(job / 'capture.bin')
        with self.assertRaises(media.UnsafeMedia):
            media.normalize(job)
        self.assertEqual(before, media.digest(job / 'capture.bin'))
        self.assertFalse((job / 'normalized.json').exists())

    @unittest.skipUnless(os.name == 'posix' and shutil.which('exiftool'), 'Requires Linux publication tools')
    def test_selection_survives_real_encode_and_chat_publication(self):
        root = Path(__file__).resolve().parents[1]
        script = '''
set -euo pipefail
source "$1/encode.sh"
pipeline_init
new_recording_job dual test 123
cp -- "$FIXTURE" "$RECORDING_JOB/capture.bin"
printf '{"offset_seconds":0,"message":"chat context"}\n' > "$RECORDING_JOB/chat.jsonl"
complete_recording "$RECORDING_JOB"
job="$WORK_DIR/normalize/${RECORDING_JOB##*/}"
ensure_normalized_job "$job"
advance_job "$job" encode
encode_job "$WORK_DIR/encode/${RECORDING_JOB##*/}"
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(root)],
            env={**os.environ, 'SCRATCH_ROOT': str(self.root / 'scratch'), 'STORAGE_DIR': str(self.root / 'storage'),
                 'MODE': 'twitch', 'CHANNEL': 'audio', 'TWITCH_AUDIO_TRACK': '2', 'REMUX': 'true',
                 'ENCODE': 'true', 'UPLOAD': 'false', 'FIXTURE': str(self.source)},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        final, = list((self.root / 'storage').rglob('*.mp4'))
        probe = media.Tools(self.root / 'final-probe').inspect(final, self.root)
        self.assertEqual(1, probe['stream_types'].count('audio'))
        self.assertEqual('chat context', json.loads(final.with_suffix('.chat.json').read_text())[0]['message'])


if __name__ == '__main__':
    unittest.main()
