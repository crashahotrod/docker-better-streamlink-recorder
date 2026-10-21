"""Chat parsing, reconnect behavior, capture lifecycle and sidecar publication."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import types
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_archive
import capture


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name) / 'recording.test'
        self.job.mkdir()

    def test_twitch_tags_and_kick_message(self):
        record = chat_archive.irc_message('@display-name=Test\\sName;id=123 :test!test@host PRIVMSG #room :hello café')
        self.assertEqual('Test Name', record['author'])
        self.assertEqual('hello café', record['message'])
        self.assertIsNone(chat_archive.irc_message(':server NOTICE test :welcome'))
        frame = {'event': 'App\\Events\\ChatMessageEvent', 'data': json.dumps({
                 'id': '123', 'content': 'hello', 'sender': {'username': 'test'}})}
        self.assertEqual('hello', chat_archive.kick_message(json.dumps(frame))['message'])

    def test_twitch_connection_handles_ping_and_flushes_messages(self):
        archive = chat_archive.ChatArchive(self.job, {'ARCHIVE_CHAT': 'true', 'MODE': 'twitch', 'CHANNEL': 'test'})
        connection = Mock()
        connection.recv.side_effect = [b'PING :server\r\n@id=1 :user!x@y PRIVMSG #test :hello\r\n', b'']
        with patch.object(chat_archive.socket, 'create_connection'), \
                patch.object(chat_archive.ssl, 'create_default_context') as tls, \
                (self.job / 'chat.jsonl').open('w') as output:
            tls.return_value.wrap_socket.return_value = connection
            with self.assertRaises(OSError):
                archive.twitch(output)
        self.assertIn(b'PONG :server\r\n', [call.args[0] for call in connection.sendall.call_args_list])
        record = json.loads((self.job / 'chat.jsonl').read_text())
        self.assertEqual('hello', record['message'])
        self.assertIn('offset_seconds', record)

    def test_reconnect_preserves_messages_and_stops(self):
        archive = chat_archive.ChatArchive(self.job, {'ARCHIVE_CHAT': 'true', 'MODE': 'twitch', 'CHANNEL': 'test'})
        attempts = []

        def collect(output):
            attempts.append(True)
            archive.write(output, {'message': str(len(attempts)), 'author': 'test'})
            if len(attempts) == 1:
                raise OSError('disconnect')
            archive.stopping.set()

        archive.twitch = collect
        with patch.object(archive.stopping, 'wait', return_value=False):
            archive.run()
        self.assertEqual(2, len((self.job / 'chat.jsonl').read_text().splitlines()))
        self.assertEqual('stopped', json.loads((self.job / 'chat.status.json').read_text())['status'])

    def test_kick_subscription_and_ping(self):
        archive = chat_archive.ChatArchive(self.job, {'ARCHIVE_CHAT': 'true', 'MODE': 'kick', 'CHANNEL': 'test', 'KICK_CHATROOM_ID': '47'})
        connection = Mock()
        message = json.dumps({'event': 'App\\Events\\ChatMessageEvent', 'data': {
            'content': 'Kick context', 'id': 'one', 'sender': {'username': 'test'}}})
        connection.recv.side_effect = ['{"event":"pusher:ping"}', message, '']
        websocket = types.SimpleNamespace(create_connection=Mock(return_value=connection),
                                          WebSocketTimeoutException=TimeoutError)
        with patch.dict(sys.modules, websocket=websocket), (self.job / 'chat.jsonl').open('w') as output:
            with self.assertRaises(OSError):
                archive.kick(output)
        requests = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual('chatrooms.47.v2', requests[0]['data']['channel'])
        self.assertEqual('pusher:pong', requests[1]['event'])
        self.assertEqual('Kick context', json.loads((self.job / 'chat.jsonl').read_text())['message'])

    def test_atomic_sidecar_idempotency_and_collision(self):
        source = self.job / 'chat.jsonl'
        source.write_text('{"offset_seconds":0,"message":"hello"}\n{"incomplete":')
        final = Path(self.temp.name) / 'video.mp4'
        for _ in range(2):
            chat_archive.publish(self.job, final)
        sidecar = final.with_suffix('.chat.json')
        self.assertEqual('hello', json.loads(sidecar.read_text())[0]['message'])
        self.assertTrue(source.exists())
        sidecar.write_text('unrelated')
        with self.assertRaises(OSError):
            chat_archive.publish(self.job, final)

    def test_segment_ranges_and_full_broadcast(self):
        work = Path(self.temp.name) / 'queue'
        child = work / 'encode/recording.test.seg1'
        session = work / 'sessions/recording.test'
        child.mkdir(parents=True)
        session.mkdir(parents=True)
        (session / 'chat.jsonl').write_text(''.join(json.dumps({'offset_seconds': n, 'message': str(n)}) + '\n' for n in (1, 6)))
        (session / 'stem').write_bytes(b'full-broadcast\0')
        (session / 'season').write_text('202610')
        (child / 'segment-parent').write_text('test')
        (child / 'chat-start').write_text('0')
        (child / 'chat-end').write_text('5')
        final = Path(self.temp.name) / 'part.mp4'
        chat_archive.publish(child, final)
        self.assertEqual(1, len(json.loads(final.with_suffix('.chat.json').read_text())))
        chat_archive.publish_session(session, Path(self.temp.name) / 'storage', 'test')
        full = Path(self.temp.name) / 'storage/test/Season 202610/full-broadcast.chat.json'
        self.assertEqual(2, len(json.loads(full.read_text())))

    @unittest.skipUnless(sys.platform != 'win32', 'Linux capture process')
    def test_collector_stops_on_capture_failure(self):
        collector = Mock()
        self.assertEqual(7, capture.capture(self.job, [sys.executable, '-c', 'raise SystemExit(7)'], 0, chat=collector))
        collector.start.assert_called_once()
        collector.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
