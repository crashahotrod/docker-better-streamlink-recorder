"""Cloud publication keeps originals and only records successful remote commits."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_sync


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / 'storage'
        self.media = self.storage / 'channel' / 'Season 202601' / 'episode.mp4'
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(b'original archive')
        self.receipt = self.root / 'cloud.json'
        self.env = {'STORAGE_DIR': str(self.storage), 'RCLONE_REMOTE': 'b2:bucket/archive',
                    'RCLONE_BWLIMIT': '8M', 'RCLONE_RETRIES': '1'}

    def test_staging_commit_receipt_and_idempotent_retry(self):
        run = Mock()
        cloud_sync.sync_files([self.media], self.receipt, self.env, run)
        copy, move = [call.args[0] for call in run.call_args_list]
        self.assertEqual('copyto', copy[1])
        self.assertEqual('moveto', move[1])
        self.assertEqual(copy[3], move[2])
        self.assertIn('/.pending/', copy[3])
        self.assertEqual('b2:bucket/archive/channel/Season 202601/episode.mp4', move[3])
        self.assertEqual(['--bwlimit', '8M'], move[-2:])
        self.assertIn('--immutable', move)
        self.assertTrue(self.media.exists())
        self.assertIn('channel/Season 202601/episode.mp4', json.loads(self.receipt.read_text())['files'])
        cloud_sync.sync_files([self.media], self.receipt, self.env, run)
        self.assertEqual(2, run.call_count)
        self.env['RCLONE_REMOTE'] = 'b2:other/archive'
        cloud_sync.sync_files([self.media], self.receipt, self.env, run)
        self.assertEqual(4, run.call_count)

    def test_failed_remote_commit_retains_original_and_retries(self):
        run = Mock(side_effect=[None, subprocess.CalledProcessError(1, 'rclone')])
        with self.assertRaises(OSError):
            cloud_sync.sync_files([self.media], self.receipt, self.env, run)
        self.assertTrue(self.media.exists())
        self.assertFalse(self.receipt.exists())
        run.side_effect = None
        cloud_sync.sync_files([self.media], self.receipt, self.env, run)
        self.assertTrue(self.receipt.exists())
        self.assertTrue(self.media.exists())

    def test_rejects_invalid_settings_and_files_outside_storage(self):
        for variable, value in [('RCLONE_REMOTE', '-wrong'), ('RCLONE_BWLIMIT', 'unlimited'),
                                ('RCLONE_RETRIES', '0'), ('RCLONE_RETRIES', '11')]:
            with self.subTest(variable=variable), self.assertRaises(ValueError):
                cloud_sync.validate_config({**self.env, variable: value})
        outside = self.root / 'outside.mp4'
        outside.write_bytes(b'other data')
        run = Mock()
        with self.assertRaises(ValueError):
            cloud_sync.sync_files([outside], self.receipt, self.env, run)
        run.assert_not_called()

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_real_rclone_stages_commits_and_handles_lost_receipt(self):
        remote = self.root / 'remote'
        self.env['RCLONE_REMOTE'] = str(remote)
        cloud_sync.sync_files([self.media], self.receipt, self.env)
        destination = remote / self.media.relative_to(self.storage)
        self.assertEqual(self.media.read_bytes(), destination.read_bytes())
        self.receipt.unlink()
        cloud_sync.sync_files([self.media], self.receipt, self.env)
        self.assertTrue(self.receipt.exists())
        self.assertTrue(self.media.exists())


if __name__ == '__main__':
    unittest.main()
