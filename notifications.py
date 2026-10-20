#!/usr/bin/env python3
"""Persistent Discord outbox and status-message worker; never run HTTP in producers."""
import contextlib
import email.utils
import hashlib
import json
import math
import os
import pathlib
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def atomic_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.pending.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=True)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def read_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def lock(path):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        acquired = False
        try:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        except ImportError:
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except (ImportError, OSError):
                pass
        if not acquired:
            yield False
        else:
            try:
                yield True
            finally:
                try:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    try:
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except (ImportError, OSError):
                        pass


def clip(value, limit):
    # Count UTF-16 units conservatively, including astral Unicode characters.
    return str(value).encode('utf-16-le')[:max(0, limit) * 2].decode('utf-16-le', 'ignore')


def sanitize(payload):
    payload = json.loads(json.dumps(payload))
    payload['allowed_mentions'] = {'parse': []}
    if 'content' in payload:
        payload['content'] = clip(payload['content'], 2000)
    if 'username' in payload and payload['username']:
        payload['username'] = clip(payload['username'], 80)
    if 'avatar_url' in payload and payload['avatar_url']:
        payload['avatar_url'] = str(payload['avatar_url'])[:2048]
    budget = 6000

    def text(value, maximum):
        nonlocal budget
        result = clip(value, min(maximum, budget))
        budget -= len(result.encode('utf-16-le')) // 2
        return result

    embeds = []
    for embed in payload.get('embeds', [])[:10]:
        for key, maximum in (('title', 256), ('description', 4096)):
            if key in embed:
                value = text(embed[key], maximum)
                if value:
                    embed[key] = value
                else:
                    del embed[key]
        for key, field, maximum in (('author', 'name', 256), ('footer', 'text', 2048)):
            if key in embed:
                value = text(embed[key].get(field, ''), maximum)
                if value:
                    embed[key][field] = value
                else:
                    del embed[key]
        fields = []
        for field in embed.get('fields', [])[:25]:
            if budget < 2:
                break
            name = text(field.get('name') or '\u200b', min(256, budget - 1))
            if not name:
                name = text('\u200b', 1)
            value = text(field.get('value') or '\u200b', 1024)
            if not value:
                value = text('\u200b', 1)
            fields.append({**field, 'name': name, 'value': value})
        if 'fields' in embed:
            embed['fields'] = fields
        if any(embed.get(key) for key in ('title', 'description', 'fields', 'author', 'footer')):
            embeds.append(embed)
    if 'embeds' in payload:
        payload['embeds'] = embeds
    return payload


def webhook_url(webhook, message_id=None):
    parts = urllib.parse.urlsplit(webhook)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query.pop('wait', None)
    path = parts.path.rstrip('/')
    if message_id:
        path += '/messages/' + urllib.parse.quote(str(message_id), safe='')
    else:
        query['wait'] = 'true'
    return urllib.parse.urlunsplit(parts._replace(path=path, query=urllib.parse.urlencode(query)))


def webhook_key(webhook):
    parts = urllib.parse.urlsplit(webhook)
    return hashlib.sha256((parts.netloc + parts.path.rstrip('/')).encode()).hexdigest()


def enqueue(work, target, payload):
    if target not in ('public', 'monitor'):
        raise ValueError('Invalid notification target')
    path = pathlib.Path(work) / 'notifications' / f'event.{time.time_ns()}.{uuid.uuid4().hex}.json'
    atomic_json(path, {'target': target, 'payload': sanitize(payload), 'attempts': 0, 'retry_at': 0})
    return path


def status_payload(status):
    state = status['status']
    display, color = {
        'recording': ('🔴 Recording', 15158332), 'monitoring': ('🟢 Monitoring', 3447003),
        'offline': ('⚪ Offline', 9807270), 'error': ('🔴 Recorder error or stalled capture', 15158332),
    }.get(state, ('Unknown', 9807270))
    fields = [{'name': 'Channel Status', 'value': display, 'inline': True},
              {'name': 'Platform', 'value': status['mode'].upper(), 'inline': True}]
    active = status.get('active_capture')
    if active:
        duration_desc = active['duration_human']
        if active.get('segment_index'):
            duration_desc += f" (Part {active['segment_index']})"
            if active.get('total_duration_human'):
                duration_desc += f" | Total: {active['total_duration_human']}"
        fields.append({'name': 'Active Recording', 'value':
                       f"**Title**: {active['title']}\n**Duration**: {duration_desc}\n"
                       f"**Current Size**: {active['size_human']}", 'inline': False})
    queues = status['queues']
    fields.append({'name': 'Pipeline Queues', 'value': ' | '.join(
        f'{key.title()}: {value}' for key, value in queues.items()), 'inline': False})
    disk = status['disk_space']
    fields.append({'name': 'Disk Space', 'value':
                   f"Scratch: {disk['scratch_free_gb']} GB | Storage: {disk['storage_free_gb']} GB "
                   f"(Min: {disk['min_free_space_gb']} GB)", 'inline': False})
    return sanitize({'embeds': [{'title': '📊 Streamlink Recorder Status: ' + status['channel'],
                                'color': color, 'fields': fields,
                                'footer': {'text': 'Last updated: ' + status['updated_at']},
                                'timestamp': status['updated_at']}]})


class Worker:
    def __init__(self, work, environ=os.environ, opener=urllib.request.urlopen, now=time.time):
        self.work = pathlib.Path(work)
        self.environ, self.opener, self.now = environ, opener, now
        self.rate_dir = pathlib.Path(environ.get('SCRATCH_ROOT', '/etc/streamlink/scratch')) / 'discord-state'

    def target(self, name):
        primary = self.environ.get('DISCORD_WEBHOOK_URL', '')
        if name == 'public':
            return primary
        if self.environ.get('DISCORD_NOTIFY_ON_ERROR', 'true') == 'false':
            return ''
        return self.environ.get('DISCORD_MONITOR_WEBHOOK_URL') or primary

    def request(self, webhook, payload, message_id=None):
        """Return status, body, earliest next attempt. Never sleep on rate limits."""
        key = webhook_key(webhook)
        rate_file = self.rate_dir / (key + '.json')
        with lock(self.rate_dir / (key + '.lock')) as acquired:
            if not acquired:
                return 0, {}, self.now() + 5
            retry_at = read_json(rate_file, {}).get('retry_at', 0)
            if retry_at > self.now():
                return 0, {}, retry_at
            out_payload = dict(payload)
            if not message_id:
                channel = self.environ.get('CHANNEL')
                username = self.environ.get('DISCORD_USERNAME') or (f"Streamlink ({channel})" if channel else None)
                avatar_url = self.environ.get('DISCORD_AVATAR_URL')
                if username and 'username' not in out_payload:
                    out_payload['username'] = username
                if avatar_url and 'avatar_url' not in out_payload:
                    out_payload['avatar_url'] = avatar_url
            request = urllib.request.Request(webhook_url(webhook, message_id),
                data=json.dumps(sanitize(out_payload)).encode(),
                headers={'Content-Type': 'application/json', 'User-Agent': 'StreamlinkRecorder/1.0'},
                method='PATCH' if message_id else 'POST')
            try:
                with self.opener(request, timeout=30) as response:
                    code, headers, raw = response.status, response.headers, response.read(65536)
            except urllib.error.HTTPError as error:
                code, headers, raw = error.code, error.headers, error.read(65536)
            except (OSError, urllib.error.URLError):
                return 0, {}, self.now() + 30
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                body = {}
            retry_at = 0
            if code == 429:
                delays = [60.0]
                for value in (body.get('retry_after'), headers.get('Retry-After')):
                    try:
                        delay = float(value)
                    except (TypeError, ValueError):
                        try:
                            delay = email.utils.parsedate_to_datetime(value).timestamp() - self.now()
                        except (TypeError, ValueError, OverflowError):
                            continue
                    if math.isfinite(delay) and delay >= 0:
                        delays.append(delay)
                # Use server instructions when present; otherwise retry after one minute.
                retry_at = self.now() + max(delays[1:] or delays)
            elif headers.get('X-RateLimit-Remaining') == '0':
                try:
                    delay = float(headers.get('X-RateLimit-Reset-After', '0'))
                    if math.isfinite(delay) and delay > 0:
                        retry_at = self.now() + delay
                except ValueError:
                    pass
            if retry_at:
                atomic_json(rate_file, {'retry_at': retry_at})
            return code, body, retry_at

    def deliver(self):
        considered = 0
        for path in sorted((self.work / 'notifications').glob('event.*.json')):
            event = read_json(path, None)
            if (not isinstance(event, dict) or event.get('target') not in ('public', 'monitor')
                    or not isinstance(event.get('payload'), dict)
                    or type(event.get('attempts', 0)) is not int or not 0 <= event.get('attempts', 0) <= 6
                    or type(event.get('retry_at', 0)) not in (int, float)
                    or not math.isfinite(event.get('retry_at', 0)) or event.get('retry_at', 0) < 0):
                path.rename(path.with_suffix('.invalid'))
                print('[Discord] Invalid queued event retained for inspection.', file=sys.stderr)
                continue
            if event.get('retry_at', 0) > self.now():
                continue
            considered += 1
            if considered > 20:
                break
            webhook = self.target(event['target'])
            if not webhook:
                path.unlink()
                continue
            code, _, retry_at = self.request(webhook, event['payload'])
            if 200 <= code < 300:
                path.unlink()
            else:
                attempts = min(event.get('attempts', 0) + 1, 6)
                event.update(attempts=attempts, retry_at=max(retry_at, self.now() + min(30 * 2 ** (attempts - 1), 900)))
                atomic_json(path, event)
                print(f'[Discord] Event delivery failed (HTTP {code}); queued for retry.', file=sys.stderr)

    def persistent_status(self):
        # A public webhook is deliberately never used for the dashboard.
        webhook = self.environ.get('DISCORD_MONITOR_WEBHOOK_URL', '')
        if not webhook:
            return
        status = read_json(self.work / 'status.json', None)
        if not status:
            return
        state_file = self.work / 'discord-status.json'
        state = read_json(state_file, {})
        if not state_file.exists():
            try:
                state['id'] = (self.work / 'status_message.id').read_text().strip()
            except OSError:
                pass
        identity = hashlib.sha256(webhook.encode()).hexdigest()
        if state.get('webhook') and state['webhook'] != identity:
            state = {'webhook': identity}
        state['webhook'] = identity
        now = self.now()
        if state.get('retry_at', 0) > now:
            return
        active = status.get('active_capture') or {}
        fingerprint = json.dumps([status['status'], active.get('job_id'), active.get('title'),
                                  status['queues'], status['disk_space']], sort_keys=True)
        elapsed = now - state.get('sent_at', 0)
        if state.get('id') and elapsed < 30 and (fingerprint == state.get('fingerprint') or elapsed < 5):
            return
        payload = status_payload(status)
        code, body, retry_at = self.request(webhook, payload, state.get('id'))
        if code == 404 and state.get('id'):
            state.pop('id')
            # Save the deletion before attempting recreation, including across a crash.
            atomic_json(state_file, state)
            code, body, retry_at = self.request(webhook, payload)
        if 200 <= code < 300 and (state.get('id') or body.get('id')):
            state.update(id=state.get('id') or str(body['id']), sent_at=now,
                         retry_at=0, fingerprint=fingerprint)
        else:
            state['retry_at'] = max(retry_at, now + 30)
            print(f'[Discord] Status delivery failed (HTTP {code}); retry deferred.', file=sys.stderr)
        atomic_json(state_file, state)
        if state.get('id'):
            (self.work / 'status_message.id').write_text(state['id'] + '\n', encoding='utf-8')

    def cycle(self):
        with lock(self.work / 'notifications.lock') as acquired:
            if acquired:
                self.deliver()
                self.persistent_status()


def main():
    work = pathlib.Path(os.environ.get('SCRATCH_ROOT', '/etc/streamlink/scratch')) / os.environ['MODE'] / os.environ['CHANNEL'] / 'queue-v2'
    if len(sys.argv) > 1 and sys.argv[1] == 'enqueue':
        enqueue(work, sys.argv[2], json.load(sys.stdin))
        return
    worker = Worker(work)
    while True:
        try:
            worker.cycle()
        except (OSError, ValueError, KeyError, TypeError):
            print('[Discord] Worker cycle failed; pending events retained.', file=sys.stderr)
        if os.environ.get('NOTIFY_ONCE') == 'true':
            return
        time.sleep(1)


if __name__ == '__main__':
    main()
