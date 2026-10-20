"""Worker health and quota boundaries, including daylight saving transitions."""
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


health = module('healthcheck')
operations = module('operations')


class HealthTests(unittest.TestCase):
    def runner(self, **states):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '\n'.join(
                f'{name} {states.get(name, "RUNNING")} pid 1' for name in args[2:]), '')
        return run

    def test_disabled_workers_are_excluded(self):
        run = Mock(side_effect=self.runner())
        self.assertTrue(health.check({}, run))
        args = run.call_args.args[0]
        self.assertNotIn('streamlink_upload', args)
        self.assertNotIn('streamlink_encode', args)
        self.assertIn('streamlink_notify', args)

    def test_every_enabled_worker_must_be_running(self):
        env = {'UPLOAD': 'true', 'ENCODE': 'true'}
        for worker in ('download', 'normalize', 'publish', 'status', 'notify', 'audit', 'upload', 'encode'):
            for state in ('FATAL', 'STOPPED', 'EXITED', 'STARTING', 'BACKOFF'):
                with self.subTest(worker=worker, state=state):
                    self.assertFalse(health.check(env, self.runner(**{'streamlink_' + worker: state})))
        self.assertTrue(health.check(env, self.runner()))

    def test_missing_process_supervisor_failure_and_timeout(self):
        for run in (Mock(return_value=subprocess.CompletedProcess([], 0, '', '')),
                    Mock(return_value=subprocess.CompletedProcess([], 3, 'connection refused', '')),
                    Mock(side_effect=OSError()), Mock(side_effect=subprocess.TimeoutExpired([], 8))):
            self.assertFalse(health.check({}, run))


class QuotaTests(unittest.TestCase):
    def test_reset_uses_pacific_calendar_midnight(self):
        cases = [('2026-01-15T12:00:00+00:00', '2026-01-16T08:00:00+00:00'),
                 ('2026-07-15T12:00:00+00:00', '2026-07-16T07:00:00+00:00'),
                 ('2026-03-08T08:00:00+00:00', '2026-03-09T07:00:00+00:00'),
                 ('2026-11-01T07:00:00+00:00', '2026-11-02T08:00:00+00:00'),
                 ('2026-07-15T06:59:59+00:00', '2026-07-15T07:00:00+00:00')]
        for start, end in cases:
            with self.subTest(start=start):
                now = datetime.datetime.fromisoformat(start).timestamp()
                expected = datetime.datetime.fromisoformat(end).timestamp()
                self.assertEqual(expected, operations.next_quota_reset(now))

    def test_shared_client_has_same_scope_across_config_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = (pathlib.Path(directory) / name for name in ('a.json', 'b.json'))
            a.write_text(json.dumps({'installed': {'client_id': 'shared'}}))
            b.write_text(json.dumps({'web': {'client_id': 'shared'}}))
            self.assertEqual(operations.quota_key(a), operations.quota_key(b))
            b.write_text(json.dumps({'web': {'client_id': 'other'}}))
            self.assertNotEqual(operations.quota_key(a), operations.quota_key(b))


class QuarantineTests(unittest.TestCase):
    def test_expiry_uses_quarantine_entry_age_and_stays_within_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            work = root / 'channel'
            quarantine = work / 'quarantine'
            quarantine.mkdir(parents=True)
            now = 2000000000
            for name in ('old', 'recent', 'legacy'):
                job = quarantine / ('recording.' + name)
                job.mkdir()
                (job / 'capture.bin').write_bytes(b'preserved media')
                os.utime(job / 'capture.bin', (now - 30 * 86400,) * 2)
                if name != 'legacy':
                    (job / 'quarantined-at').write_text(str(now))
                    entered = now if name == 'recent' else now - 8 * 86400
                    os.utime(job / 'quarantined-at', (entered,) * 2)
                os.utime(job, (now - 30 * 86400,) * 2)
            outside = root / 'another-channel'
            outside.mkdir()
            (outside / 'capture.bin').write_bytes(b'other channel')
            (quarantine / 'recording.symlink').symlink_to(outside, target_is_directory=True)
            active = work / 'active' / 'recording.old'
            active.mkdir(parents=True)
            operations.prune_quarantine(work, 0, now)
            self.assertTrue((quarantine / 'recording.old').exists())
            with operations.lock(work / 'quarantine.lock') as acquired:
                self.assertTrue(acquired)
                operations.prune_quarantine(work, 7, now)
                self.assertTrue((quarantine / 'recording.old').exists())
            operations.prune_quarantine(work, 7, now)
            self.assertFalse((quarantine / 'recording.old').exists())
            self.assertFalse((quarantine / 'recording.legacy').exists())
            self.assertTrue((quarantine / 'recording.recent' / 'capture.bin').exists())
            self.assertTrue((outside / 'capture.bin').exists())
            self.assertTrue(active.exists())

    def test_interrupted_expiry_cleanup_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            retired = work / 'quarantine' / '.expired.recording.old'
            retired.mkdir(parents=True)
            (retired / 'capture.bin').write_bytes(b'expired media')
            operations.prune_quarantine(work, 7)
            self.assertFalse(retired.exists())


class StorageRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.channel = self.root / 'channel'
        self.channel.mkdir()
        self.now = 2000000000

    def file(self, name, age=0):
        path = self.channel / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'media or metadata')
        os.utime(path, (self.now - age * 86400,) * 2)
        return path

    def test_expired_media_removes_literal_stem_companions_and_empty_directories(self):
        for extension in ('mp4', 'ts', 'mkv'):
            stem = f'Season/episode [a]*?.{extension}'
            media = self.file(stem, 10)
            for suffix in ('nfo', 'json', 'jpg', 'poster.jpg', 'en.srt', 'custom-metadata'):
                self.file(str(pathlib.Path(stem).with_suffix('')) + '.' + suffix)
            self.assertTrue(media.exists())
        unrelated = self.file('Other/season.json')
        operations.prune_recordings(self.channel, 7, self.now)
        self.assertFalse((self.channel / 'Season').exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.channel.exists())

    def test_keeps_fresh_alternate_media_more_specific_stems_and_other_channels(self):
        old = self.file('Season/show.mp4', 10)
        fresh = self.file('Season/show.ts', 1)
        companion = self.file('Season/show.nfo', 10)
        expired_base = self.file('Season/prefix.mp4', 10)
        retained_part = self.file('Season/prefix.part1.mp4', 1)
        retained_poster = self.file('Season/prefix.part1.poster.jpg', 10)
        expired_poster = self.file('Season/prefix.jpg')
        other = self.root / 'another-channel'
        other.mkdir()
        (other / 'show.mp4').write_bytes(b'other channel')
        # Unmanaged media formats remain intact along with their sidecars.
        self.file('Season/custom.mp4', 10)
        custom = self.file('Season/custom.webm', 10)
        custom_mpg = self.file('Season/custom.mpg', 1)
        custom_audio = self.file('Season/custom.flac', 1)
        custom_poster = self.file('Season/custom.jpg')
        operations.prune_recordings(self.channel, 7, self.now)
        for path in (fresh, companion, retained_part, retained_poster, custom, custom_mpg,
                     custom_audio, custom_poster, other / 'show.mp4'):
            self.assertTrue(path.exists(), str(path))
        for path in (old, expired_base, expired_poster):
            self.assertFalse(path.exists(), str(path))

    def test_disabled_policy_age_boundary_locks_and_symlinks(self):
        expired = self.file('Season/old.mp4', 10)
        rounded = self.file('Season/not-yet.mp4', 7.5)
        hidden = self.file('.publish.test/media.mp4', 10)
        outside = self.root / 'outside'
        outside.mkdir()
        external = outside / 'old.jpg'
        external.write_bytes(b'external data')
        link = self.channel / 'Season/old.jpg'
        link.symlink_to(external)
        (self.channel / 'linked-folder').symlink_to(outside, target_is_directory=True)
        operations.prune_recordings(self.channel, 0, self.now)
        self.assertTrue(expired.exists())
        with operations.lock(self.channel / '.retention.lock') as acquired:
            self.assertTrue(acquired)
            operations.prune_recordings(self.channel, 7, self.now)
            self.assertTrue(expired.exists())
        operations.prune_recordings(self.channel, 7, self.now)
        self.assertFalse(expired.exists())
        for path in (rounded, hidden, link, external):
            self.assertTrue(path.exists(), str(path))
        # A channel directory symlink is never traversed, either.
        operations.prune_recordings(self.channel / 'linked-folder', 7, self.now)
        self.assertTrue(external.exists())

    def test_media_unlink_failure_preserves_its_companions(self):
        media = self.file('Season/blocked.mp4', 10)
        companion = self.file('Season/blocked.json')
        unlink = pathlib.Path.unlink

        def fail_media(path, *args, **kwargs):
            if path == media:
                raise PermissionError('injected deletion failure')
            return unlink(path, *args, **kwargs)

        with patch.object(pathlib.Path, 'unlink', fail_media):
            operations.prune_recordings(self.channel, 7, self.now)
        self.assertTrue(media.exists())
        self.assertTrue(companion.exists())

    def test_shell_pruner_uses_the_current_channel_storage(self):
        media = self.file('Season/published.mp4')
        companion = self.file('Season/published.json')
        old = datetime.datetime.now(datetime.timezone.utc).timestamp() - 10 * 86400
        os.utime(media, (old, old))
        result = subprocess.run(['bash', '-c', 'source "$1/pipeline.sh"; pipeline_init; prune_old_recordings',
                                 'test', str(ROOT)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, MODE='twitch', CHANNEL='channel', STORAGE_DIR=str(self.root),
                                         SCRATCH_ROOT=str(self.root / 'scratch'), RETENTION_DAYS='7'))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(media.exists())
        self.assertFalse(companion.exists())
        self.assertFalse((self.channel / 'Season').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
