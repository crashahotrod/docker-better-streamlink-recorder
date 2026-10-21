#!/usr/bin/env python3
"""Small read-only HTTP interface to the existing atomic status exporter."""
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import urllib.parse
import math


def metrics(status):
    def escaped(value):
        return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')

    def number(value):
        try:
            value = float(value)
            return str(value) if math.isfinite(value) and value >= 0 else '0'
        except (TypeError, ValueError):
            return '0'

    labels = f'channel="{escaped(status.get("channel", ""))}",mode="{escaped(status.get("mode", ""))}"'
    active, disk = status.get('active_capture') or {}, status.get('disk_space') or {}
    samples = [
        ('streamlink_channel_status', 'Channel state: offline=0, monitoring=1, recording=2, error=3.',
         {'offline': 0, 'monitoring': 1, 'recording': 2, 'error': 3}.get(status.get('status'), 3)),
        ('streamlink_active_duration_seconds', 'Elapsed active capture duration in seconds.',
         active.get('total_duration_seconds') or active.get('duration_seconds', 0)),
        ('streamlink_active_bytes_written', 'Bytes written to the active capture.', active.get('size_bytes', 0)),
        ('streamlink_scratch_available_bytes', 'Available scratch filesystem bytes.',
         disk.get('scratch_available_bytes', disk.get('scratch_free_gb', 0) * 1024 ** 3)),
        ('streamlink_storage_available_bytes', 'Available archive filesystem bytes.',
         disk.get('storage_available_bytes', disk.get('storage_free_gb', 0) * 1024 ** 3)),
    ]
    lines = []
    for name, help_text, value in samples:
        lines += [f'# HELP {name} {help_text}', f'# TYPE {name} gauge', f'{name}{{{labels}}} {number(value)}']
    lines += ['# HELP streamlink_queue_depth Number of jobs in each queue.', '# TYPE streamlink_queue_depth gauge']
    for stage in ('active', 'normalize', 'upload', 'encode', 'publish', 'quarantine'):
        lines.append(f'streamlink_queue_depth{{{labels},stage="{stage}"}} {number(status.get("queues", {}).get(stage, 0))}')
    return ('\n'.join(lines) + '\n').encode('utf-8')


class StatusHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, work, **kwargs):
        self.work = Path(work)
        super().__init__(*args, **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass

    def respond(self, code, body, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path).path
        if route in ('/', '/index.html'):
            self.respond(200, Path(__file__).with_name('dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        elif route in ('/status.json', '/healthz', '/metrics'):
            try:
                with (self.work / 'status.json').open('rb') as source:
                    body = source.read(1024 * 1024 + 1)
                status = json.loads(body)
                if len(body) > 1024 * 1024 or not isinstance(status, dict):
                    raise ValueError('Invalid status')
                if route == '/metrics':
                    body = metrics(status)
            except (OSError, ValueError):
                self.respond(503, b'{"error":"Status is not available yet"}\n', 'application/json')
                return
            self.respond(200, body, 'text/plain; version=0.0.4; charset=utf-8' if route == '/metrics' else 'application/json')
        else:
            self.respond(404, b'Not found\n', 'text/plain; charset=utf-8')

    do_HEAD = do_GET

    def do_POST(self):
        self.close_connection = True
        self.respond(405, b'Read-only endpoint\n', 'text/plain; charset=utf-8')

    do_PUT = do_DELETE = do_PATCH = do_POST


def make_server(work, host='0.0.0.0', port=8080):
    return ThreadingHTTPServer((host, port), functools.partial(StatusHandler, work=work))


if __name__ == '__main__':
    work = Path(os.environ.get('SCRATCH_ROOT', '/etc/streamlink/scratch')) / os.environ['MODE'] / os.environ['CHANNEL'] / 'queue-v2'
    with make_server(work, os.environ.get('HTTP_STATUS_HOST', '0.0.0.0'),
                     int(os.environ.get('HTTP_STATUS_PORT', '8080'))) as server:
        print(f'[Status] Read-only dashboard listening on port {server.server_port}', flush=True)
        server.serve_forever()
