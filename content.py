#!/usr/bin/env python3
"""Thumbnail selection, live chapter sampling and chapter-aware publication."""
import contextlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

from notifications import atomic_json


def timestamp(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def thumbnail_offset(value, duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Cannot select thumbnail without a positive duration')
    if re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)%', value):
        percent = float(value[:-1])
        if not 0 < percent < 100:
            raise ValueError('Thumbnail percentage must be between 0 and 100')
        offset = duration * percent / 100
    elif re.fullmatch(r'\d+:\d{2}:\d{2}(?:\.\d+)?', value):
        hours, minutes, seconds = map(float, value.split(':'))
        if minutes >= 60 or seconds >= 60:
            raise ValueError('Invalid thumbnail timestamp')
        offset = hours * 3600 + minutes * 60 + seconds
    elif re.fullmatch(r'\d+(?:\.\d+)?', value):
        offset = float(value)
    else:
        raise ValueError('Thumbnail timestamp must be seconds, HH:MM:SS, or a percentage')
    # Explicit timestamps beyond a short recording fall back into the content.
    return min(offset if offset < duration else duration * 0.2, max(0, duration - 0.1))


def make_thumbnail(source, destination, value='20%', frames=30):
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration', '-of', 'default=nw=1:nk=1', str(source)], text=True))
    offset = thumbnail_offset(value, duration)
    destination = Path(destination)
    descriptor, temporary = tempfile.mkstemp(suffix='.jpg', dir=destination.parent)
    os.close(descriptor)
    try:
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-ss', str(offset), '-i', str(source),
            '-vf', f"scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,thumbnail={frames}",
            '-frames:v', '1', '-q:v', '3', temporary], check=True, stderr=subprocess.PIPE)
        if Path(temporary).stat().st_size == 0:
            raise OSError('Thumbnail selection produced no frame')
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def field(job, name, default=''):
    try:
        return (Path(job) / name).read_bytes().rstrip(b'\0\n').decode('utf-8')
    except FileNotFoundError:
        return default


def chapter_label(title, category):
    title, category = (' '.join(str(value).split()) for value in (title, category))
    return f'{category} - {title}' if title and category and title != category else category or title or 'Stream'


def chapter_text(chapters):
    return ''.join(f"{timestamp(item['offset'])} - {item['label']}\n" for item in chapters)


def atomic_text(path, text):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.content.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ChapterPoller:
    def __init__(self, job, environ=os.environ, opener=urllib.request.urlopen, now=time.time):
        self.job, self.env, self.opener, self.now = Path(job), environ, opener, now
        self.stopping = threading.Event()
        self.thread = None
        self.token, self.expires = '', 0
        self.chapters = []
        self.last = None
        self.started = float(field(job, 'started-at', str(now())))

    def sample(self, title, category):
        if self.stopping.is_set():
            return
        current = (title, category)
        if current == self.last:
            return
        offset = max(0, int(self.now() - self.started)) if self.chapters else 0
        item = {'offset': offset, 'label': chapter_label(title, category), 'title': title, 'category': category}
        if self.chapters and offset <= self.chapters[-1]['offset']:
            self.chapters[-1] = item
        else:
            self.chapters.append(item)
        atomic_json(self.job / 'chapters.json', self.chapters)
        atomic_text(self.job / 'chapters.txt', chapter_text(self.chapters))
        self.last = current

    def get_json(self, request):
        with self.opener(request, timeout=10) as response:
            return json.loads(response.read(1024 * 1024))

    def poll_once(self):
        mode, channel = self.env['MODE'], self.env['CHANNEL']
        if self.now() >= self.expires:
            endpoint = 'https://id.twitch.tv/oauth2/token' if mode == 'twitch' else 'https://id.kick.com/oauth/token'
            token = self.get_json(urllib.request.Request(endpoint, data=urllib.parse.urlencode({
                'client_id': self.env['CLIENT_ID'], 'client_secret': self.env['CLIENT_SECRET'],
                'grant_type': 'client_credentials'}).encode()))
            self.token = token['access_token']
            self.expires = self.now() + max(1, int(token['expires_in']) - 60)
        headers = {'Authorization': 'Bearer ' + self.token}
        if mode == 'twitch':
            endpoint = 'https://api.twitch.tv/helix/streams?user_login=' + urllib.parse.quote(channel, safe='')
            headers['Client-ID'] = self.env['CLIENT_ID']
        else:
            endpoint = 'https://api.kick.com/public/v1/channels?slug=' + urllib.parse.quote(channel, safe='')
        try:
            data = self.get_json(urllib.request.Request(endpoint, headers=headers))['data']
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.expires = 0
            raise
        if data:
            stream = data[0]
            if mode == 'twitch':
                self.sample(stream['title'], stream.get('game_name', ''))
            elif stream.get('stream', {}).get('is_live'):
                category = stream.get('category') or {}
                self.sample(stream['stream_title'], category.get('name', ''))

    def start(self):
        if self.env.get('STREAM_CHAPTERS', 'false') != 'true' or (self.job / 'vod-id').exists():
            return
        self.sample(field(self.job, 'title'), field(self.job, 'category'))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.wait(int(self.env.get('CHAPTER_POLL_INTERVAL', '60'))):
            try:
                self.poll_once()
            except Exception:
                # HTTP exceptions can contain credentials. Preserve the last good chapter.
                print('[Chapters] Metadata lookup failed; retrying next interval.', file=sys.stderr)

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=12)


def chapters_for_part(job, identity):
    job = Path(job)
    parent = field(job, 'segment-parent')
    source = job
    base = 0.0
    if parent:
        if not re.fullmatch('[A-Za-z0-9]+', parent):
            raise ValueError('Invalid chapter session identity')
        source = job.parent.parent / 'sessions' / ('recording.' + parent)
        base = float(field(job, 'chat-start', '0'))
    path = source / 'chapters.json'
    if not path.exists():
        return []
    chapters = json.loads(path.read_text())
    manifest = json.loads((job / 'normalized.json').read_text())
    end = base
    found = False
    for part in manifest['parts']:
        duration = float(part.get('validation', {}).get('duration', 0))
        if part['id'] == identity:
            end = base + duration
            found = True
            break
        base += duration
    if not found or end <= base:
        return []
    prior = [item for item in chapters if item['offset'] <= base]
    selected = [{'offset': 0, 'label': prior[-1]['label']}] if prior else []
    selected += [{'offset': item['offset'] - base, 'label': item['label']}
                 for item in chapters if base < item['offset'] < end]
    return selected


def description(job, identity, text):
    chapters = chapter_text(chapters_for_part(job, identity)).rstrip()
    if not chapters:
        return text[:5000]
    suffix = '\n\n' + chapters
    return text[:max(0, 5000 - len(suffix))] + suffix[:5000]


def embed_chapters(job, identity, path):
    chapters = chapters_for_part(job, identity)
    if not chapters or Path(path).suffix != '.mp4':
        return
    path = Path(path)
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration', '-of', 'default=nw=1:nk=1', str(path)], text=True))
    metadata = path.with_name('chapters.ffmetadata')
    candidate = path.with_name('chapters.mp4')
    text = ';FFMETADATA1\n'
    for index, item in enumerate(chapters):
        end = chapters[index + 1]['offset'] if index + 1 < len(chapters) else duration
        label = re.sub(r'([\\=;#])', r'\\\1', item['label']).replace('\n', ' ')
        text += f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(item['offset'] * 1000)}\nEND={int(end * 1000)}\ntitle={label}\n"
    try:
        metadata.write_text(text, encoding='utf-8')
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'warning', '-i', str(path), '-i', str(metadata),
            '-map', '0', '-map_metadata', '0', '-map_chapters', '1', '-c', 'copy', '-movflags', '+faststart',
            str(candidate)], check=True, stderr=subprocess.PIPE)
        os.replace(candidate, path)
    finally:
        metadata.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def publish_file(source, destination):
    import filecmp
    source, destination = Path(source), Path(destination)
    if destination.exists():
        if not filecmp.cmp(source, destination, shallow=False):
            raise OSError('Existing content sidecar differs; scratch retained')
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.sidecar.', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output, source.open('rb') as input_file:
            import shutil
            shutil.copyfileobj(input_file, output)
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_part(job, identity, final):
    final, job = Path(final), Path(job)
    files = []
    thumbnail = job / 'parts' / (identity + '.thumbnail.jpg')
    if thumbnail.exists():
        destination = final.with_suffix('.thumbnail.jpg')
        publish_file(thumbnail, destination)
        files.append(destination)
    chapters = chapters_for_part(job, identity)
    if chapters:
        source = job / 'parts' / (identity + '.chapters.txt')
        atomic_text(source, chapter_text(chapters))
        destination = final.with_suffix('.chapters.txt')
        publish_file(source, destination)
        files.append(destination)
    return files


if __name__ == '__main__':
    operation, *args = sys.argv[1:]
    if operation == 'thumbnail':
        make_thumbnail(*args, value=os.environ.get('YOUTUBE_THUMBNAIL_TIMESTAMP', '20%'))
    elif operation == 'description':
        print(description(*args), end='')
    elif operation == 'embed':
        embed_chapters(*args)
