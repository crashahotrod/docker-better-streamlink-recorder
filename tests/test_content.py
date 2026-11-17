"""Chapter timelines and representative thumbnails, including real MP4 metadata."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import content


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name) / 'work' / 'publish' / 'recording.test'
        (self.job / 'parts').mkdir(parents=True)
        (self.job / 'started-at').write_text('1000')
        (self.job / 'normalized.json').write_text(json.dumps({'parts': [
            {'id': '0001', 'validation': {'duration': 100}},
            {'id': '0002', 'validation': {'duration': 100}}]}))
        self.chapters = [{'offset': 0, 'label': 'Just Chatting'},
                         {'offset': 80, 'label': 'Elden Ring'},
                         {'offset': 150, 'label': 'Q&A'}]
        (self.job / 'chapters.json').write_text(json.dumps(self.chapters))

    def test_offset_percent_explicit_time_and_short_recordings(self):
        self.assertEqual(1200, content.thumbnail_offset('20%', 6000))
        self.assertEqual(1800, content.thumbnail_offset('00:30:00', 6000))
        self.assertEqual(12, content.thumbnail_offset('00:30:00', 60))
        self.assertEqual(10.5, content.thumbnail_offset('10.5', 60))
        for value in ('0%', '100%', '-5', '00:61:00', 'nan', 'bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                content.thumbnail_offset(value, 100)

    def test_title_category_changes_deduplicate_and_preserve_zero_anchor(self):
        now = [1000]
        poller = content.ChapterPoller(self.job, {}, now=lambda: now[0])
        poller.sample('Hello', 'Just Chatting')
        now[0] = 7120
        poller.sample('Hello', 'Just Chatting')
        poller.sample('Boss fight', 'Elden Ring')
        self.assertEqual([0, 6120], [item['offset'] for item in poller.chapters])
        self.assertIn('01:42:00 - Elden Ring - Boss fight', (self.job / 'chapters.txt').read_text())
        poller.stop()
        poller.sample('Late response', 'Other')
        self.assertEqual(2, len(poller.chapters))

    def test_poller_authenticates_then_reuses_token(self):
        responses = [{'access_token': 'secret', 'expires_in': 3600},
                     {'data': [{'title': 'Hello', 'game_name': 'Chatting'}]},
                     {'data': [{'title': 'Hello', 'game_name': 'Game'}]}]
        opener = Mock()
        opener.return_value.__enter__ = Mock(return_value=opener.return_value)
        opener.return_value.__exit__ = Mock(return_value=False)
        opener.return_value.read.side_effect = [json.dumps(value).encode() for value in responses]
        poller = content.ChapterPoller(self.job, {'MODE': 'twitch', 'CHANNEL': 'test',
            'CLIENT_ID': 'client', 'CLIENT_SECRET': 'secret'}, opener=opener, now=lambda: 1000)
        poller.poll_once()
        poller.poll_once()
        self.assertEqual(3, opener.call_count)
        self.assertIn('helix/streams?user_login=test', opener.call_args.args[0].full_url)
        self.assertEqual('Bearer secret', opener.call_args.args[0].get_header('Authorization'))
        self.assertEqual('Game - Hello', poller.chapters[-1]['label'])

    def test_chapters_rebase_for_parts_and_segment_sessions(self):
        self.assertEqual([{'offset': 0, 'label': 'Elden Ring'}, {'offset': 50, 'label': 'Q&A'}],
                         content.chapters_for_part(self.job, '0002'))
        session = self.job.parent.parent / 'sessions' / 'recording.session1'
        session.mkdir(parents=True)
        (self.job / 'segment-parent').write_text('session1')
        (self.job / 'chat-start').write_text('100')
        (session / 'chapters.json').write_text(json.dumps(self.chapters))
        self.assertEqual([{'offset': 0, 'label': 'Elden Ring'}, {'offset': 50, 'label': 'Q&A'}],
                         content.chapters_for_part(self.job, '0001'))
        text = content.description(self.job, '0001', 'x' * 6000)
        self.assertEqual(5000, len(text))
        self.assertTrue(text.endswith('00:00:50 - Q&A'))

    def test_sidecars_retry_without_overwriting_different_content(self):
        source = self.job / 'parts' / '0001.thumbnail.jpg'
        source.write_bytes(b'thumbnail')
        final = Path(self.temp.name) / 'archive.mp4'
        paths = content.publish_part(self.job, '0001', final)
        self.assertEqual(2, len(paths))
        content.publish_part(self.job, '0001', final)
        self.assertIn('00:01:20 - Elden Ring', final.with_suffix('.chapters.txt').read_text())
        final.with_suffix('.thumbnail.jpg').write_bytes(b'existing other thumbnail')
        with self.assertRaises(OSError):
            content.publish_part(self.job, '0001', final)
        self.assertTrue(source.exists())

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_thumbnail_avoids_intro_and_embedded_chapters_preserve_video(self):
        media = self.job / 'parts' / '0001.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=red:s=160x90:r=5:d=1', '-f', 'lavfi', '-i', 'color=blue:s=160x90:r=5:d=9',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
            '-c:v', 'libx264', '-threads', '1', str(media)], check=True)
        thumbnail = media.with_suffix('.jpg')
        content.make_thumbnail(media, thumbnail)
        rgb = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(thumbnail),
            '-vf', 'scale=1:1', '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
        self.assertGreater(rgb[2], rgb[0] + 100)
        (self.job / 'chapters.json').write_text(json.dumps([
            {'offset': 0, 'label': 'Chatting'}, {'offset': 4, 'label': 'Game #1; title=fun'}]))
        def frames():
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(media),
                '-map', '0:v:0', '-f', 'framemd5', '-'])
        before = frames()
        content.embed_chapters(self.job, '0001', media)
        self.assertEqual(before, frames())
        chapters = json.loads(subprocess.check_output(['ffprobe', '-v', 'error',
            '-show_chapters', '-of', 'json', str(media)]))['chapters']
        self.assertEqual(['Chatting', 'Game #1; title=fun'], [item['tags']['title'] for item in chapters])
        self.assertEqual('4.000000', chapters[1]['start_time'])


if __name__ == '__main__':
    unittest.main()
