"""Real queue publication combines MP4 chapters, sidecars, and retryable cloud sync."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import segment_capture


@unittest.skipUnless(os.name == 'posix' and all(shutil.which(tool) for tool in
    ('ffmpeg', 'ffprobe', 'exiftool', 'rclone')), 'Linux media/cloud publication tools')
class PublicationTests(unittest.TestCase):
    def test_session_audit_retains_original_until_broadcast_cloud_copy_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / 'work'
            session = work / 'sessions' / 'recording.test'
            session.mkdir(parents=True)
            for name, value in {'capture.bin': 'original', 'season': '202601', 'stem': 'broadcast',
                'chapters.txt': '00:00:00 - Chatting\n', 'broadcast-finished': 'true',
                'seg1.published.json': '{}', 'session.json': json.dumps({'status': 'completed',
                    'segments': [1], 'source_periods': []})}.items():
                (session / name).write_text(value)
            env = {'STORAGE_DIR': str(Path(directory) / 'storage'), 'CHANNEL': 'test',
                'RCLONE_REMOTE': str(Path(directory) / 'remote'), 'RCLONE_BIN': '/bin/false'}
            with patch.dict(os.environ, env), patch.object(segment_capture, 'verify_session_source') as verify:
                segment_capture.audit_session(work, session)
                self.assertEqual('original', (session / 'capture.bin').read_text())
                verify.assert_not_called()
                self.assertEqual('completed', json.loads((session / 'session.json').read_text())['status'])
                os.environ['RCLONE_BIN'] = 'rclone'
                segment_capture.audit_session(work, session)
                verify.assert_called_once()
                self.assertFalse(session.exists())

    def test_cloud_failure_retains_job_then_retry_commits_chapters_and_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                'testsrc2=s=160x90:r=10:d=4', '-f', 'lavfi', '-i', 'sine=d=4', '-c:v', 'libx264',
                '-threads', '1', '-c:a', 'aac', '-shortest', str(source)], check=True)
            fail = root / 'fail-rclone'
            fail.write_text('#!/bin/sh\nexit 1\n')
            fail.chmod(0o755)
            script = '''
set -euo pipefail
source "$1/pipeline.sh"
pipeline_init
new_recording_job 'A broadcast' test 123
cp "$FIXTURE" "$RECORDING_JOB/capture.bin"
printf '[{"offset":0,"label":"Chatting"},{"offset":2,"label":"Game"}]' > "$RECORDING_JOB/chapters.json"
printf '{"offset_seconds":1,"message":"context"}\n' > "$RECORDING_JOB/chat.jsonl"
complete_recording "$RECORDING_JOB"
job="$WORK_DIR/normalize/${RECORDING_JOB##*/}"
ensure_normalized_job "$job"
advance_job "$job" publish
job="$WORK_DIR/publish/${RECORDING_JOB##*/}"
export RCLONE_BIN="$FAILBIN"
if publish_recording "$job"; then echo 'Failed cloud copy retired the job' >&2; exit 1; fi
[[ -s "$job/capture.bin" && -f "$job/parts/0001.published.sha256" && ! -f "$job/parts/0001.cloud.json" ]]
[[ ! -f "$HOOK_LOG" ]]
export RCLONE_BIN=rclone
publish_recording "$job"
[[ ! -d "$job" ]]
[[ $(wc -l < "$HOOK_LOG") == 1 ]]
'''
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT)], text=True,
                capture_output=True, timeout=45, env={**os.environ, 'MODE': 'twitch', 'CHANNEL': 'test',
                'SCRATCH_ROOT': str(root / 'scratch'), 'STORAGE_DIR': str(root / 'storage'),
                'RCLONE_REMOTE': str(root / 'remote'), 'RCLONE_RETRIES': '1', 'FIXTURE': str(source),
                'FAILBIN': str(fail), 'UPLOAD': 'false', 'ENCODE': 'false', 'REMUX': 'true',
                'HOOK_LOG': str(root / 'hooks'), 'POST_PUBLISH_COMMAND': 'printf "%s\\n" "$PUBLISHED_FILE" >> "$HOOK_LOG"'})
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            final, = (root / 'storage').rglob('*.mp4')
            remote, = (root / 'remote').rglob('*.mp4')
            self.assertEqual(final.read_bytes(), remote.read_bytes())
            self.assertEqual(final.with_suffix('.chapters.txt').read_bytes(),
                             remote.with_suffix('.chapters.txt').read_bytes())
            self.assertEqual('context', json.loads(remote.with_suffix('.chat.json').read_text())[0]['message'])
            chapters = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_chapters',
                '-of', 'json', str(final)]))['chapters']
            self.assertEqual(['Chatting', 'Game'], [item['tags']['title'] for item in chapters])


if __name__ == '__main__':
    unittest.main()
