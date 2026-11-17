"""Dependency automation must prepare stable changes without partial pin updates."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dependency_updates', ROOT / '.github/scripts/update_dependencies.py')
updates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updates)


class DependencyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'Dockerfile'
        self.original = 'FROM python:3.12-bookworm\nENV streamlinkCommit=' + 'a' * 40 + '\nARG YTU_RELEASE=v1.0.0\n'
        self.path.write_text(self.original)
        self.responses = [{'tag_name': '8.6.2'}, {'tag_name': 'v1.25.5'}, {'sha': 'b' * 40}]
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        return self.responses.pop(0)

    def test_stable_tag_resolves_to_immutable_commit(self):
        self.assertTrue(updates.update(self.path, self.fetch))
        self.assertTrue(self.urls[-1].endswith('/commits/8.6.2'))
        text = self.path.read_text()
        self.assertIn('ENV streamlinkCommit=' + 'b' * 40, text)
        self.assertIn('ARG YTU_RELEASE=v1.25.5', text)
        self.assertTrue(text.startswith('FROM python:3.12-bookworm\n'))

    def test_current_pins_leave_file_unchanged(self):
        self.responses = [{'tag_name': '8.6.2'}, {'tag_name': 'v1.0.0'}, {'sha': 'a' * 40}]
        self.assertFalse(updates.update(self.path, self.fetch))
        self.assertEqual(self.original, self.path.read_text())

    def test_invalid_or_prerelease_data_never_changes_pins(self):
        for bad in ({'tag_name': '8.6.2', 'prerelease': True}, {'tag_name': 'main'},
                    {'tag_name': '8.6.2', 'draft': True}, {'tag_name': 'v1.2.3; command'}):
            with self.subTest(response=bad):
                self.responses = [bad, {'tag_name': 'v1.25.5'}]
                with self.assertRaises(ValueError):
                    updates.update(self.path, self.fetch)
                self.assertEqual(self.original, self.path.read_text())

    def test_network_or_commit_failure_never_partially_updates(self):
        def unavailable(_):
            raise OSError('Upstream unavailable')
        with self.assertRaises(OSError):
            updates.update(self.path, unavailable)
        self.responses[-1] = {'sha': 'invalid'}
        with self.assertRaises(ValueError):
            updates.update(self.path, self.fetch)
        self.assertEqual(self.original, self.path.read_text())

    def test_missing_pin_is_rejected_before_writing(self):
        original = 'FROM python:3.12-bookworm\n'
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            updates.update(self.path, self.fetch)
        self.assertEqual(original, self.path.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
