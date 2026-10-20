"""Exercise the persistent notification worker with mocked HTTP only."""
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('notifications', ROOT / 'notifications.py')
discord = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discord)


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body, self.status, self.headers = body or {'id': 'message-1'}, status, headers or {}

    def read(self, _):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class DiscordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = pathlib.Path(self.temporary.name) / 'work'
        self.work.mkdir()
        self.now = 1000
        self.env = {'SCRATCH_ROOT': self.temporary.name,
                    'DISCORD_WEBHOOK_URL': 'https://example.invalid/public/token',
                    'DISCORD_MONITOR_WEBHOOK_URL': 'https://example.invalid/monitor/token'}
        self.open = Mock(return_value=Response())
        self.worker = discord.Worker(self.work, self.env, self.open, lambda: self.now)
        self.status = {'channel': 'test', 'mode': 'twitch', 'status': 'monitoring',
                       'updated_at': '2026-10-19T00:00:00Z', 'active_capture': None,
                       'queues': {'active': 0}, 'disk_space': {
                           'scratch_free_gb': 10, 'storage_free_gb': 20, 'min_free_space_gb': 5}}
        discord.atomic_json(self.work / 'status.json', self.status)

    def event(self):
        return discord.enqueue(self.work, 'public', {'content': '@everyone "quoted"\\\n caf\u00e9'})

    def error(self, code, body=None, headers=None):
        return urllib.error.HTTPError('https://secret.invalid/token', code, 'failure',
                                      headers or {}, io.BytesIO(json.dumps(body or {}).encode()))

    def test_enqueue_is_local_and_payload_prevents_mentions(self):
        path = self.event()
        self.open.assert_not_called()
        payload = discord.read_json(path, {})['payload']
        self.assertIn('@everyone', payload['content'])
        self.assertEqual({'parse': []}, payload['allowed_mentions'])
        self.worker.deliver()
        self.assertFalse(path.exists())
        sent = json.loads(self.open.call_args.args[0].data)
        self.assertEqual(payload, sent)

    def test_retry_survives_restart_and_does_not_block_other_events(self):
        failed = self.event()
        self.open.side_effect = [self.error(500), Response()]
        self.worker.deliver()
        self.assertTrue(failed.exists())
        later = self.event()
        self.worker.deliver()
        self.assertFalse(later.exists())
        self.assertEqual(2, self.open.call_count)
        self.now += 31
        self.open.side_effect = None
        discord.Worker(self.work, self.env, self.open, lambda: self.now).deliver()
        self.assertFalse(failed.exists())

    def test_waiting_events_do_not_starve_later_events(self):
        for _ in range(25):
            path = self.event()
            event = discord.read_json(path, {})
            event['retry_at'] = self.now + 1000
            discord.atomic_json(path, event)
        ready = self.event()
        self.worker.deliver()
        self.assertFalse(ready.exists())
        self.assertEqual(1, self.open.call_count)

    def test_rate_limit_pauses_events_and_dashboard_across_workers(self):
        self.env['DISCORD_MONITOR_WEBHOOK_URL'] = self.env['DISCORD_WEBHOOK_URL']
        self.open.side_effect = self.error(429, {'retry_after': 120.5}, {'Retry-After': '130'})
        path = self.event()
        self.worker.cycle()
        self.assertEqual(1, self.open.call_count)
        self.assertGreaterEqual(discord.read_json(path, {})['retry_at'], 1130)
        other = discord.Worker(self.work, self.env, self.open, lambda: self.now)
        other.cycle()
        self.assertEqual(1, self.open.call_count)
        self.now = 1131
        self.open.side_effect = None
        other.cycle()
        self.assertFalse(path.exists())
        self.assertEqual(3, self.open.call_count)

    def test_successful_response_rate_headers_are_honored(self):
        self.open.return_value = Response(headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset-After': '10'})
        self.worker.request(self.env['DISCORD_WEBHOOK_URL'], {'content': 'hi'})
        self.worker.request(self.env['DISCORD_WEBHOOK_URL'], {'content': 'hi'})
        self.assertEqual(1, self.open.call_count)

    def test_public_webhook_has_no_persistent_dashboard(self):
        del self.env['DISCORD_MONITOR_WEBHOOK_URL']
        self.worker.persistent_status()
        self.open.assert_not_called()
        self.assertEqual(self.env['DISCORD_WEBHOOK_URL'], self.worker.target('monitor'))
        self.env['DISCORD_NOTIFY_ON_ERROR'] = 'false'
        self.assertEqual('', self.worker.target('monitor'))

    def test_persistent_lifecycle_deleted_message_and_webhook_rotation(self):
        self.env['DISCORD_MONITOR_WEBHOOK_URL'] += '?thread_id=123&wait=false'
        self.env['DISCORD_NOTIFY_ON_ERROR'] = 'false'
        self.worker.persistent_status()
        self.assertEqual('POST', self.open.call_args.args[0].method)
        self.assertIn('thread_id=123&wait=true', self.open.call_args.args[0].full_url)
        self.worker.persistent_status()
        self.assertEqual(1, self.open.call_count)
        self.now += 30
        self.open.side_effect = [self.error(404), Response({'id': 'replacement'})]
        self.worker.persistent_status()
        patch = self.open.call_args_list[-2].args[0]
        self.assertEqual('PATCH', patch.method)
        self.assertTrue(patch.full_url.endswith('/messages/message-1?thread_id=123'))
        self.assertEqual('replacement', (self.work / 'status_message.id').read_text().strip())
        self.env['DISCORD_MONITOR_WEBHOOK_URL'] = 'https://example.invalid/new/token'
        self.open.side_effect = None
        self.worker.persistent_status()
        self.assertEqual('POST', self.open.call_args.args[0].method)

    def test_legacy_message_id_is_reused_and_errors_do_not_erase_it(self):
        (self.work / 'status_message.id').write_text('legacy\n')
        self.open.side_effect = self.error(503)
        self.worker.persistent_status()
        self.assertEqual('PATCH', self.open.call_args.args[0].method)
        self.assertEqual('legacy', discord.read_json(self.work / 'discord-status.json', {})['id'])

    def test_limits_include_combined_embed_budget_and_unicode(self):
        payload = {'content': '\U0001f600' * 2000, 'embeds': [{
            'title': 't' * 400, 'description': 'd' * 5000, 'author': {'name': 'a' * 300},
            'footer': {'text': 'f' * 2500},
            'fields': [{'name': 'n' * 300, 'value': 'v' * 1500} for _ in range(30)]}] * 2}
        sent = discord.sanitize(payload)
        self.assertLessEqual(len(sent['content'].encode('utf-16-le')) // 2, 2000)
        total = 0
        for embed in sent['embeds']:
            self.assertLessEqual(len(embed.get('title', '')), 256)
            self.assertLessEqual(len(embed.get('description', '')), 4096)
            self.assertLessEqual(len(embed.get('fields', [])), 25)
            total += sum(len(embed.get(key, '')) for key in ('title', 'description'))
            total += len(embed.get('author', {}).get('name', '')) + len(embed.get('footer', {}).get('text', ''))
            for field in embed.get('fields', []):
                self.assertTrue(field['name'] and field['value'])
                self.assertLessEqual(len(field['name']), 256)
                self.assertLessEqual(len(field['value']), 1024)
                total += len(field['name']) + len(field['value'])
        self.assertLessEqual(total, 6000)

    def test_worker_lock_prevents_duplicate_delivery(self):
        self.event()
        with discord.lock(self.work / 'notifications.lock') as acquired:
            self.assertTrue(acquired)
            self.worker.cycle()
        self.open.assert_not_called()

    def test_invalid_events_are_retained_without_blocking_valid_events(self):
        path = self.event()
        event = discord.read_json(path, {})
        event['retry_at'] = 'damaged'
        discord.atomic_json(path, event)
        valid = self.event()
        self.worker.deliver()
        self.assertTrue(path.with_suffix('.invalid').exists())
        self.assertFalse(valid.exists())

    def test_partial_budget_never_emits_empty_unicode_fields(self):
        payload = discord.sanitize({'embeds': [{'description': 'a' * 4096,
            'footer': {'text': 'b' * 1902}, 'fields': [{'name': '\U0001f600', 'value': '\U0001f600'}]}]})
        field = payload['embeds'][0]['fields'][0]
        self.assertEqual('\u200b', field['name'])
        self.assertEqual('\u200b', field['value'])

    def test_webhook_attribution_and_segmented_status_duration(self):
        # Default attribution from CHANNEL
        self.env['CHANNEL'] = 'gamer'
        self.env['DISCORD_AVATAR_URL'] = 'https://img.test/pic.png'
        self.worker.request(self.env['DISCORD_WEBHOOK_URL'], {'content': 'hello'})
        sent = json.loads(self.open.call_args.args[0].data)
        self.assertEqual('Streamlink (gamer)', sent.get('username'))
        self.assertEqual('https://img.test/pic.png', sent.get('avatar_url'))

        # Explicit DISCORD_USERNAME override
        self.env['DISCORD_USERNAME'] = 'Custom Recorder'
        self.worker.request(self.env['DISCORD_WEBHOOK_URL'], {'content': 'hello2'})
        sent2 = json.loads(self.open.call_args.args[0].data)
        self.assertEqual('Custom Recorder', sent2.get('username'))

        # PATCH messages never include username or avatar_url
        self.worker.request(self.env['DISCORD_WEBHOOK_URL'], {'content': 'patch'}, message_id='msg-1')
        sent3 = json.loads(self.open.call_args.args[0].data)
        self.assertNotIn('username', sent3)
        self.assertNotIn('avatar_url', sent3)

        # Segmented status payload displays segment index and total duration
        status = {
            'channel': 'gamer',
            'mode': 'twitch',
            'status': 'recording',
            'updated_at': '2026-10-20T00:00:00Z',
            'active_capture': {
                'title': 'Speedrun',
                'duration_human': '01:00:00',
                'size_human': '1 GB',
                'segment_index': 3,
                'total_duration_human': '03:15:00',
            },
            'queues': {'active': 1},
            'disk_space': {'scratch_free_gb': 10, 'storage_free_gb': 20, 'min_free_space_gb': 5}
        }
        payload = discord.status_payload(status)
        active_field = next(f for f in payload['embeds'][0]['fields'] if f['name'] == 'Active Recording')
        self.assertIn('**Duration**: 01:00:00 (Part 3) | Total: 03:15:00', active_field['value'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
