"""Exercise PID 1 signal forwarding and delayed child flush with real processes."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Linux container lifecycle')
class LifecycleTests(unittest.TestCase):
    def application(self, command, **kwargs):
        # exec models gosu's process replacement without requiring root in tests.
        return subprocess.Popen(['bash', '-c',
            'source "$1/entrypoint.sh"; shift; USER_NAME=fixture; '
            'gosu() { shift; exec "$@"; }; run_application "$@"', 'test', str(ROOT), *command], **kwargs)

    def test_termination_waits_for_flush_and_returns_child_status(self):
        with tempfile.TemporaryDirectory() as directory:
            ready, flushed = Path(directory) / 'ready', Path(directory) / 'flushed'
            code = '''
import pathlib, signal, sys, time
def stop(*args):
    time.sleep(0.3)
    pathlib.Path(sys.argv[2]).write_text('closed cleanly')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
pathlib.Path(sys.argv[1]).touch()
while True:
    time.sleep(0.01)
'''
            child = self.application([sys.executable, '-c', code, str(ready), str(flushed)])
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                started = time.monotonic()
                child.terminate()
                self.assertEqual(0, child.wait(timeout=5))
                self.assertGreaterEqual(time.monotonic() - started, 0.25)
                self.assertEqual('closed cleanly', flushed.read_text())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_stdin_and_nonzero_exit_status_are_preserved(self):
        process = self.application([sys.executable, '-c', 'import sys; print(sys.stdin.read()); sys.exit(7)'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        output, _ = process.communicate('interactive command', timeout=5)
        self.assertEqual(7, process.returncode)
        self.assertEqual('interactive command\n', output)

    def test_segment_writers_receive_sigint_before_shared_flush_wait(self):
        import signal
        sys.path.insert(0, str(ROOT))
        import segment_capture
        events = []
        first, second = Mock(pid=101), Mock(pid=102)
        for process in (first, second):
            process.poll.return_value = None
            process.wait.side_effect = lambda timeout: events.append(('wait', timeout))
        with patch.object(segment_capture.os, 'killpg', side_effect=lambda pid, sig: events.append((pid, sig))), \
                patch.object(segment_capture.time, 'monotonic', side_effect=[0, 10, 20]):
            segment_capture.stop_processes([first, second])
        self.assertEqual([(101, signal.SIGINT), (102, signal.SIGINT), ('wait', 80), ('wait', 70)], events)


if __name__ == '__main__':
    unittest.main()
