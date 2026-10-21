"""Exercise unsplit capture shutdown without filling a filesystem."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture


@unittest.skipUnless(os.name == 'posix', 'Capture runs in the Linux container')
class CaptureTests(unittest.TestCase):
    def test_empty_minimum_uses_disabled_default(self):
        result = subprocess.run([sys.executable, str(Path(capture.__file__)), '.', '--',
                                 sys.executable, '-c', 'pass'],
                                env={**os.environ, 'MIN_FREE_SPACE_GB': ''}, timeout=5)
        self.assertEqual(result.returncode, 0)

    def test_child_status_and_disabled_guard(self):
        for status in (0, 7):
            with self.subTest(status=status):
                usage = mock.Mock(side_effect=AssertionError('Disabled guard checked disk'))
                self.assertEqual(capture.capture('.', [sys.executable, '-c', f'raise SystemExit({status})'],
                                                 0, interval=0.01, usage=usage), status)

    def test_low_space_sends_sigint_and_retains_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'capture.bin'
            stopped = Path(directory) / 'stopped'
            code = '''
import pathlib, signal, sys, time
def stop(*args):
    pathlib.Path(sys.argv[2]).write_text('SIGINT')
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
pathlib.Path(sys.argv[1]).write_bytes(b'original capture')
while True:
    time.sleep(0.01)
'''
            calls = []

            def usage(_):
                calls.append(True)
                # Allow the writer to start, then simulate reaching exactly the reserve.
                low = output.exists() and len(calls) > 1
                return types.SimpleNamespace(free=(1 if low else 2) * 1024 ** 3)

            self.assertEqual(capture.capture(directory, [sys.executable, '-c', code, str(output), str(stopped)],
                                             1, interval=0.01, usage=usage), 75)
            self.assertGreater(len(calls), 1)
            self.assertEqual(stopped.read_text(), 'SIGINT')
            self.assertEqual(output.read_bytes(), b'original capture')

    def test_disk_probe_error_stops_child(self):
        with mock.patch.object(capture.subprocess, 'Popen') as spawn, \
                mock.patch.object(capture, 'stop') as stop:
            spawn.return_value.poll.return_value = None
            usage = mock.Mock(side_effect=OSError('disk disappeared'))
            self.assertEqual(capture.capture('.', ['streamlink'], 5, usage=usage), 1)
            stop.assert_called_once_with(spawn.return_value)

    def test_stuck_child_escalates(self):
        process = mock.Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired('capture', 10),
                                    subprocess.TimeoutExpired('capture', 10), 0, 0]
        with mock.patch.object(capture.os, 'killpg') as kill:
            capture.stop(process)
        self.assertEqual(kill.call_args_list, [mock.call(123, signal.SIGINT),
                                              mock.call(123, signal.SIGTERM),
                                              mock.call(123, signal.SIGKILL)])

    def test_supervisor_termination_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            ready, stopped = Path(directory) / 'ready', Path(directory) / 'stopped'
            code = '''
import pathlib, signal, sys, time
def stop(*args):
    pathlib.Path(sys.argv[2]).touch()
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
pathlib.Path(sys.argv[1]).touch()
while True:
    time.sleep(0.01)
'''
            process = subprocess.Popen([sys.executable, str(Path(capture.__file__)), directory, '--',
                                        sys.executable, '-c', code, str(ready), str(stopped)],
                                       env={**os.environ, 'MIN_FREE_SPACE_GB': '0'})
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
                self.assertTrue(stopped.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    unittest.main()
