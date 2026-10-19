"""Prepare reviewed dependency updates from stable upstream releases."""
import json
import os
from pathlib import Path
import re
from urllib.parse import quote
from urllib.request import Request, urlopen


def fetch_json(url):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'recorder-dependency-check'}
    token = os.environ.get('GH_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def update(path, fetch=fetch_json):
    streamlink = fetch('https://api.github.com/repos/streamlink/streamlink/releases/latest')
    uploader = fetch('https://api.github.com/repos/porjo/youtubeuploader/releases/latest')
    for release in (streamlink, uploader):
        if release.get('draft') or release.get('prerelease') or not re.fullmatch(r'v?\d+\.\d+\.\d+', release.get('tag_name', '')):
            raise ValueError('Expected a stable, versioned release')
    commit = fetch('https://api.github.com/repos/streamlink/streamlink/commits/' +
                   quote(streamlink['tag_name'], safe=''))['sha']
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid upstream commit identity')
    original = path.read_text(encoding='utf-8')
    updated, count = re.subn(r'^ENV streamlinkCommit=[0-9a-f]{40}$',
                            f'ENV streamlinkCommit={commit}', original, flags=re.M)
    if count != 1:
        raise ValueError('Expected one Streamlink pin in Dockerfile')
    updated, count = re.subn(r'^ARG YTU_RELEASE=v?\d+\.\d+\.\d+$',
                            f'ARG YTU_RELEASE={uploader["tag_name"]}', updated, flags=re.M)
    if count != 1:
        raise ValueError('Expected one uploader pin in Dockerfile')
    if updated != original:
        path.write_text(updated, encoding='utf-8', newline='\n')
    return updated != original


if __name__ == '__main__':
    update(Path('Dockerfile'))
