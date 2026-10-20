"""Worker health and quota boundaries, including daylight saving transitions."""
import datetime
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
