"""Provider payload and durable delivery tests; all HTTP is mocked."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifications


class Response:
    status, headers = 200, {}

    def __init__(self, body=None):
        self.body = body or {'ok': True}

    def read(self, _limit):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / 'work'
        self.env = {'SCRATCH_ROOT': self.temp.name, 'CHANNEL': 'test', 'MODE': 'twitch',
                    'NOTIFICATION_URL': 'https://notify.invalid/topic', 'NOTIFICATION_TOKEN': 'secret',
                    'TELEGRAM_BOT_TOKEN': '123:secret', 'TELEGRAM_CHAT_ID': '-123'}
        self.opener = Mock(return_value=Response())
        self.payload = {'embeds': [{'title': 'café ' * 60, 'description': 'Recording finished',
                                   'fields': [{'name': 'Size', 'value': '2 GiB'}]}]}

    def test_each_provider_delivers_without_discord(self):
        for provider in ('webhook', 'ntfy', 'gotify', 'telegram'):
            with self.subTest(provider=provider):
                self.env['NOTIFICATION_PROVIDER'] = provider
                notifications.validate_config(self.env)
                event = notifications.enqueue(self.work, 'public', self.payload)
                worker = notifications.Worker(self.work, self.env, self.opener, lambda: 1000)
                worker.cycle()
                self.assertFalse(event.exists())
                request = self.opener.call_args.args[0]
                if provider == 'ntfy':
                    self.assertIn('Recording finished', request.data.decode())
                    self.assertNotIn('\n', request.get_header('Title'))
                    self.assertEqual('Bearer secret', request.get_header('Authorization'))
                else:
                    body = json.loads(request.data)
                    if provider == 'gotify':
                        self.assertEqual('secret', request.get_header('X-gotify-key'))
                        self.assertEqual(5, body['priority'])
                    elif provider == 'telegram':
                        self.assertEqual('-123', body['chat_id'])
                        self.assertIn('/bot123:secret/sendMessage', request.full_url)
                        self.assertNotIn('parse_mode', body)
                    else:
                        self.assertEqual('test', body['channel'])
                        self.assertIn('Size: 2 GiB', body['message'])
                self.assertFalse((self.work / 'discord-status.json').exists())

    def test_telegram_rate_limit_and_false_success_are_retained(self):
        self.env['NOTIFICATION_PROVIDER'] = 'telegram'
        self.opener.side_effect = urllib.error.HTTPError('https://invalid', 429, 'limited', {},
            io.BytesIO(b'{"ok":false,"parameters":{"retry_after":120}}'))
        event = notifications.enqueue(self.work, 'public', self.payload)
        worker = notifications.Worker(self.work, self.env, self.opener, lambda: 1000)
        worker.deliver()
        self.assertEqual(1120, notifications.read_json(event, {})['retry_at'])
        self.opener.side_effect = None
        self.opener.return_value = Response({'ok': False, 'error_code': 500})
        worker = notifications.Worker(self.work, self.env, self.opener, lambda: 1121)
        worker.deliver()
        self.assertTrue(event.exists())
        self.assertGreater(notifications.read_json(event, {})['retry_at'], 1121)

    def test_monitor_target_and_disable_flag(self):
        self.env.update(NOTIFICATION_PROVIDER='webhook', NOTIFICATION_MONITOR_URL='https://other.invalid/events')
        worker = notifications.Worker(self.work, self.env)
        self.assertEqual(self.env['NOTIFICATION_MONITOR_URL'], worker.target('monitor'))
        self.env['NOTIFICATION_ON_ERROR'] = 'false'
        self.assertEqual('', worker.target('monitor'))
        self.assertEqual(self.env['NOTIFICATION_URL'], worker.target('public'))

    def test_invalid_configuration(self):
        for change in ({'NOTIFICATION_PROVIDER': 'invalid'},
                       {'NOTIFICATION_PROVIDER': 'ntfy', 'NOTIFICATION_URL': 'file:///etc/passwd'},
                       {'NOTIFICATION_PROVIDER': 'gotify', 'NOTIFICATION_TOKEN': ''},
                       {'NOTIFICATION_PROVIDER': 'telegram', 'TELEGRAM_CHAT_ID': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                notifications.validate_config({**self.env, **change})

    @unittest.skipUnless(os.name == 'posix', 'Requires Bash')
    def test_shell_producers_queue_both_targets_without_discord(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['bash', '-c', 'source "$1/pipeline.sh"; pipeline_init; '
            'notify_discord live; notify_monitor_alert complete done', 'test', str(root)],
            env={**os.environ, **self.env, 'NOTIFICATION_PROVIDER': 'ntfy', 'DISCORD_WEBHOOK_URL': '',
                 'DISCORD_MONITOR_WEBHOOK_URL': ''}, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        outbox = Path(self.temp.name) / 'twitch/test/queue-v2/notifications'
        self.assertEqual({'public', 'monitor'}, {json.loads(p.read_text())['target'] for p in outbox.glob('event.*')})


if __name__ == '__main__':
    unittest.main()
