"""Read-only HTTP behavior against a real local server."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notifications import atomic_json
import status_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.server = status_server.make_server(self.work, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_dashboard_and_atomic_status_refresh(self):
        with urllib.request.urlopen(self.url, timeout=5) as response:
            self.assertIn(b'Active capture', response.read())
            self.assertEqual('no-store', response.headers['Cache-Control'])
        for state in ('recording', 'offline'):
            atomic_json(self.work / 'status.json', {'status': state})
            with urllib.request.urlopen(self.url + '/status.json', timeout=5) as response:
                self.assertEqual({'status': state}, json.load(response))
        request = urllib.request.Request(self.url + '/status.json', method='HEAD')
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(b'', response.read())

    def test_unavailable_status_and_read_only_paths(self):
        for route, method, expected in (('/status.json', 'GET', 503), ('/healthz', 'GET', 503),
                                        ('/metrics', 'GET', 503),
                                        ('/../notifications.py', 'GET', 404), ('/.env', 'GET', 404),
                                        ('/status.json', 'POST', 405)):
            request = urllib.request.Request(self.url + route, method=method)
            with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(expected, error.exception.code)
        (self.work / 'status.json').write_text('incomplete json')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url + '/status.json', timeout=5)
        self.assertEqual(503, error.exception.code)

    def test_prometheus_metrics_expose_capture_queues_and_exact_disk_bytes(self):
        atomic_json(self.work / 'status.json', {'channel': 'test"\\\nchannel', 'mode': 'twitch',
            'status': 'recording', 'active_capture': {'total_duration_seconds': 120,
            'duration_seconds': 10, 'size_bytes': 123456}, 'queues': {'normalize': 2, 'quarantine': 1},
            'disk_space': {'scratch_available_bytes': 123456789, 'storage_available_bytes': 987654321}})
        with urllib.request.urlopen(self.url + '/metrics', timeout=5) as response:
            body = response.read().decode()
            self.assertIn('text/plain', response.headers['Content-Type'])
        labels = 'channel="test\\"\\\\\\nchannel",mode="twitch"'
        self.assertIn(f'streamlink_channel_status{{{labels}}} 2.0\n', body)
        self.assertIn(f'streamlink_active_duration_seconds{{{labels}}} 120.0\n', body)
        self.assertIn(f'streamlink_active_bytes_written{{{labels}}} 123456.0\n', body)
        self.assertIn(f'streamlink_scratch_available_bytes{{{labels}}} 123456789.0\n', body)
        self.assertIn(f'streamlink_storage_available_bytes{{{labels}}} 987654321.0\n', body)
        self.assertIn(f'streamlink_queue_depth{{{labels},stage="normalize"}} 2.0\n', body)
        self.assertIn(f'streamlink_queue_depth{{{labels},stage="quarantine"}} 1.0\n', body)
        self.assertEqual(6, body.count('# TYPE '))


if __name__ == '__main__':
    unittest.main()
