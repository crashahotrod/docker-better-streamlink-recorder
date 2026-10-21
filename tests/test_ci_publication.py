"""Publication joins tested registry digests without rebuilding or trusting stale receipts."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publish_images', ROOT / '.github/scripts/publish_images.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class ImagePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.metadata = {'tags': ['owner/recorder:latest', 'owner/recorder:master',
                                  'ghcr.io/owner/recorder:latest', 'ghcr.io/owner/recorder:master']}
        self.env = {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40}

    def receipts(self):
        for index, arch in enumerate(publication.PLATFORMS, 1):
            images = {repository: repository + '@sha256:' + str(index) * 64
                      for repository in publication.repositories(self.metadata)}
            (self.directory / (arch + '.json')).write_text(json.dumps({'arch': arch, 'run_id': '123',
                'run_attempt': '1', 'commit': 'a' * 40, 'images': images}))

    def test_stage_pushes_loaded_test_image_and_records_each_registry_digest(self):
        configuration = {'Os': 'linux', 'Architecture': 'arm', 'Variant': 'v7',
                         'RepoDigests': ['docker.io/owner/recorder@sha256:' + '1' * 64,
                                         'ghcr.io/owner/recorder@sha256:' + '2' * 64]}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([configuration])))
        publication.stage(self.metadata, 'armv7', self.directory, environ=self.env, run=run)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(2, sum(command[1] == 'push' for command in commands))
        self.assertIn(['docker', 'tag', 'streamlink-recorder:test', 'owner/recorder:ci-123-1-armv7'], commands)
        receipt = json.loads((self.directory / 'armv7.json').read_text())
        self.assertTrue(receipt['images']['owner/recorder'].endswith('1' * 64))
        self.assertTrue(receipt['images']['ghcr.io/owner/recorder'].endswith('2' * 64))
        self.assertFalse(any(command[1] in ('build', 'buildx') for command in commands))

    def test_wrong_platform_or_failed_push_cannot_produce_receipt(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([{
            'Os': 'linux', 'Architecture': 'amd64'}])))
        with self.assertRaises(ValueError):
            publication.stage(self.metadata, 'armv7', self.directory, environ=self.env, run=run)
        self.assertEqual(1, run.call_count)
        run.reset_mock()
        run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps([{
            'Os': 'linux', 'Architecture': 'arm', 'Variant': 'v7'}])), None,
            subprocess.CalledProcessError(1, 'docker push')]
        with self.assertRaises(subprocess.CalledProcessError):
            publication.stage(self.metadata, 'armv7', self.directory, environ=self.env, run=run)
        self.assertFalse((self.directory / 'armv7.json').exists())

    def test_merge_creates_all_release_tags_from_three_verified_digests(self):
        self.receipts()
        run = Mock()
        publication.merge(self.metadata, self.directory, self.env, run)
        self.assertEqual(2, run.call_count)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(['docker', 'buildx', 'imagetools', 'create'], command[:4])
            self.assertEqual(2, command.count('--tag'))
            self.assertEqual(3, sum('@sha256:' in item for item in command))
            self.assertNotIn('build', command)

    def test_missing_or_invalid_receipts_fail_before_any_release_changes(self):
        for corrupt in ('missing', 'commit', 'run_id', 'digest', 'repository', 'future_attempt'):
            with self.subTest(corrupt=corrupt):
                self.receipts()
                path = self.directory / 'armv7.json'
                value = json.loads(path.read_text())
                if corrupt == 'missing':
                    path.unlink()
                else:
                    if corrupt in ('commit', 'run_id'):
                        value[corrupt] = 'different'
                    elif corrupt == 'digest':
                        value['images']['owner/recorder'] = 'owner/recorder:mutable'
                    elif corrupt == 'repository':
                        value['images'].pop('owner/recorder')
                    else:
                        value['run_attempt'] = '2'
                    path.write_text(json.dumps(value))
                run = Mock()
                with self.assertRaises((ValueError, OSError)):
                    publication.merge(self.metadata, self.directory, self.env, run)
                run.assert_not_called()

    def test_retry_can_reuse_successful_platforms_from_same_run_and_commit(self):
        self.receipts()
        run = Mock()
        publication.merge(self.metadata, self.directory, {**self.env, 'GITHUB_RUN_ATTEMPT': '2'}, run)
        self.assertEqual(2, run.call_count)

    def test_no_tags_or_invalid_metadata_cannot_start_publication(self):
        for tags in ([], ['repo:bad tag'], ['-option:latest'], ['repo@sha256:invalid']):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                publication.repositories({'tags': tags})

    def test_registry_ports_are_preserved_when_grouping_tags(self):
        tags = ['localhost:5000/test/image:latest', 'localhost:5000/test/image:v1']
        self.assertEqual({'localhost:5000/test/image': tags}, publication.repositories({'tags': tags}))


if __name__ == '__main__':
    unittest.main()
