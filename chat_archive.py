#!/usr/bin/env python3
"""Read-only chat collectors and atomic publication of durable chat sidecars."""
import contextlib
import datetime
import json
import os
from pathlib import Path
import random
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.request

from notifications import atomic_json


def irc_message(line):
    tags = {}
    if line.startswith('@'):
        raw, line = line.split(' ', 1)
        escapes = {'s': ' ', ':': ';', 'r': '\r', 'n': '\n', '\\': '\\'}
        for item in raw[1:].split(';'):
            name, _, value = item.partition('=')
            tags[name] = re.sub(r'\\(.)', lambda m: escapes.get(m[1], m[1]), value)
    match = re.match(r':([^! ]+)[^ ]* PRIVMSG #[^ ]+ :(.*)', line)
    if match:
        return {'author': tags.get('display-name') or match[1], 'message': match[2],
                'id': tags.get('id'), 'tags': tags}
    return None


def kick_message(frame):
    frame = json.loads(frame)
    data = frame.get('data', {})
    if isinstance(data, str):
        data = json.loads(data)
    if frame.get('event') == 'App\\Events\\ChatMessageEvent':
        return {'author': data.get('sender', {}).get('username', ''),
                'message': data.get('content', ''), 'id': data.get('id'), 'tags': data}
    return None


class ChatArchive:
    def __init__(self, job, environ=os.environ):
        self.job, self.env = Path(job), environ
        self.stopping = threading.Event()
        self.thread = None
        self.connection = None
        self.status = 'disabled'
        self.started = time.time()

    def start(self):
        if self.env.get('ARCHIVE_CHAT', 'false') != 'true':
            return
        self.job.mkdir(parents=True, exist_ok=True)
        # Match recording start, including a collector reconnect after startup delay.
        with contextlib.suppress(OSError, ValueError):
            self.started = float((self.job / 'started-at').read_text())
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def save_status(self, status):
        self.status = status
        atomic_json(self.job / 'chat.status.json', {'status': status, 'updated_at': time.time()})

    def write(self, output, message):
        now = time.time()
        record = {**message, 'platform': self.env['MODE'], 'channel': self.env['CHANNEL'],
                  'timestamp': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
                  'offset_seconds': max(0, now - self.started)}
        output.write(json.dumps(record, ensure_ascii=True) + '\n')
        output.flush()

    def twitch(self, output):
        channel = self.env['CHANNEL'].lower()
        connection = ssl.create_default_context().wrap_socket(
            socket.create_connection(('irc.chat.twitch.tv', 6697), timeout=10),
            server_hostname='irc.chat.twitch.tv')
        self.connection = connection
        connection.settimeout(1)
        token = self.env.get('TWITCH_CHAT_TOKEN', '').removeprefix('oauth:')
        nickname = self.env.get('TWITCH_CHAT_USERNAME') if token else f'justinfan{random.randrange(10000, 99999)}'
        if token and not nickname:
            raise ValueError('TWITCH_CHAT_USERNAME is required with TWITCH_CHAT_TOKEN')
        auth = f'PASS oauth:{token}\r\n' if token else 'PASS SCHMOOPIIE\r\n'
        connection.sendall((auth + f'NICK {nickname}\r\nCAP REQ :twitch.tv/tags twitch.tv/commands\r\n'
                            f'JOIN #{channel}\r\n').encode())
        pending = b''
        self.save_status('connected')
        while not self.stopping.is_set():
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise OSError('IRC connection closed')
            pending += chunk
            if len(pending) > 1024 * 1024:
                raise ValueError('IRC frame too large')
            while b'\r\n' in pending:
                raw, pending = pending.split(b'\r\n', 1)
                line = raw.decode('utf-8', 'replace')
                if line.startswith('PING '):
                    connection.sendall(('PONG ' + line[5:] + '\r\n').encode())
                elif 'Login authentication failed' in line or 'Improperly formatted auth' in line:
                    raise ValueError('Chat authentication rejected')
                elif line.startswith('RECONNECT') or ' RECONNECT' in line:
                    raise OSError('IRC reconnect requested')
                elif message := irc_message(line):
                    self.write(output, message)

    def kick(self, output):
        import websocket
        room = self.env.get('KICK_CHATROOM_ID')
        if not room:
            request = urllib.request.Request(
                f"https://kick.com/api/v2/channels/{self.env['CHANNEL']}/chatroom",
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                room = str(json.load(response)['id'])
        if not re.fullmatch(r'[1-9][0-9]*', room):
            raise ValueError('Invalid Kick chatroom ID')
        url = 'wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0&flash=false'
        connection = websocket.create_connection(url, timeout=10, origin='https://kick.com')
        self.connection = connection
        connection.settimeout(1)
        connection.send(json.dumps({'event': 'pusher:subscribe', 'data': {'auth': '', 'channel': f'chatrooms.{room}.v2'}}))
        self.save_status('connected')
        last_ping = time.monotonic()
        while not self.stopping.is_set():
            if time.monotonic() - last_ping > 30:
                connection.send(json.dumps({'event': 'pusher:ping', 'data': {}}))
                last_ping = time.monotonic()
            try:
                frame = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not frame:
                raise OSError('Kick chat connection closed')
            if json.loads(frame).get('event') == 'pusher:ping':
                connection.send(json.dumps({'event': 'pusher:pong', 'data': {}}))
            elif message := kick_message(frame):
                self.write(output, message)

    def run(self):
        try:
            with (self.job / 'chat.jsonl').open('a', encoding='utf-8') as output:
                while not self.stopping.is_set():
                    try:
                        self.save_status('connecting')
                        (self.twitch if self.env['MODE'] == 'twitch' else self.kick)(output)
                    except Exception:
                        # Service/library exceptions can contain credentials; log no raw exception.
                        if not self.stopping.is_set():
                            self.save_status('reconnecting')
                            hint = ' Set KICK_CHATROOM_ID if chatroom lookup is blocked.' if self.env['MODE'] == 'kick' else ''
                            print('[Chat] Connection failed; retained messages saved; retrying in 5s.' + hint, file=sys.stderr)
                    finally:
                        if self.connection:
                            with contextlib.suppress(Exception):
                                self.connection.close()
                            self.connection = None
                    self.stopping.wait(5)
            self.save_status('stopped')
        except OSError:
            print('[Chat] Unable to write archive; video recording continues.', file=sys.stderr)

    def stop(self):
        if self.thread is None:
            return
        self.stopping.set()
        if self.connection:
            with contextlib.suppress(Exception):
                self.connection.close()
        self.thread.join(timeout=15)


def publish(job, final):
    job, final = Path(job), Path(final)
    source = job / 'chat.jsonl'
    start, end = 0, float('inf')
    if (job / 'segment-parent').exists():
        parent = (job / 'segment-parent').read_text().strip()
        if not re.fullmatch('[A-Za-z0-9]+', parent):
            raise ValueError('Invalid chat session identity')
        source = job.parent.parent / 'sessions' / ('recording.' + parent) / 'chat.jsonl'
        if (job / 'chat-start').exists():
            start = float((job / 'chat-start').read_text())
            end = float((job / 'chat-end').read_text())
    if not source.exists():
        return
    target = final.with_suffix('.chat.json')
    descriptor, temporary = tempfile.mkstemp(prefix='.chat.', dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output, source.open(encoding='utf-8') as messages:
            output.write('[')
            separator = ''
            for line in messages:
                # A killed collector can leave an incomplete last line; preserve the JSONL source.
                if not line.endswith('\n'):
                    break
                record = json.loads(line)
                if start <= record['offset_seconds'] < end:
                    output.write(separator + json.dumps(record, ensure_ascii=True))
                    separator = ','
            output.write(']\n')
        if target.exists():
            import filecmp
            if not filecmp.cmp(target, temporary, shallow=False):
                raise OSError('Existing chat sidecar differs; source retained')
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def publish_session(session, storage, channel):
    session = Path(session)
    if not (session / 'chat.jsonl').exists():
        return
    stem = (session / 'stem').read_bytes().rstrip(b'\0').decode('utf-8')
    season = (session / 'season').read_text().strip()
    if not stem or '/' in stem or not re.fullmatch(r'[0-9]{6}', season) or not re.fullmatch(r'[A-Za-z0-9_-]+', channel):
        raise ValueError('Invalid chat publication identity')
    directory = Path(storage) / channel / ('Season ' + season)
    directory.mkdir(parents=True, exist_ok=True)
    publish(session, directory / (stem + '.mp4'))


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != 'publish':
        if len(sys.argv) == 4 and sys.argv[1] == 'session':
            publish_session(sys.argv[2], sys.argv[3], os.environ['CHANNEL'])
        else:
            raise SystemExit('Usage: chat_archive.py publish JOB FINAL_MEDIA | session SESSION STORAGE')
    else:
        publish(sys.argv[2], sys.argv[3])
