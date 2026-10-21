#!/usr/bin/env python3
"""Small read-only HTTP interface to the existing atomic status exporter."""
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import urllib.parse


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
        elif route in ('/status.json', '/healthz'):
            try:
                with (self.work / 'status.json').open('rb') as source:
                    body = source.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024 or not isinstance(json.loads(body), dict):
                    raise ValueError('Invalid status')
            except (OSError, ValueError):
                self.respond(503, b'{"error":"Status is not available yet"}\n', 'application/json')
                return
            self.respond(200, body, 'application/json')
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
