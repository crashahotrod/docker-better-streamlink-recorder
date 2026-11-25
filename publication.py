#!/usr/bin/env python3
"""Publish recording companions and complete optional cloud receipts."""
import os
from pathlib import Path
import sys

import chat_archive
import content
from cloud_sync import sync_files


def publish_part(job, identity, final):
    job, final = Path(job), Path(final)
    chat_archive.publish(job, final)
    files = [final, *content.publish_part(job, identity, final)]
    chat = final.with_suffix('.chat.json')
    if chat.exists():
        files.append(chat)
    sync_files(files, job / 'parts' / (identity + '.cloud.json'))


def publish_session(session, storage, channel):
    session = Path(session)
    chat_archive.publish_session(session, storage, channel)
    if not (session / 'chat.jsonl').exists() and not (session / 'chapters.txt').exists():
        return
    stem = content.field(session, 'stem')
    season = content.field(session, 'season')
    import re
    if not stem or '/' in stem or not re.fullmatch(r'\d{6}', season) or not re.fullmatch(r'[A-Za-z0-9_-]+', channel):
        raise ValueError('Invalid broadcast publication identity')
    base = Path(storage) / channel / ('Season ' + season) / stem
    base.parent.mkdir(parents=True, exist_ok=True)
    files = []
    if (session / 'chapters.txt').exists():
        chapters = Path(str(base) + '.chapters.txt')
        content.publish_file(session / 'chapters.txt', chapters)
        files.append(chapters)
    chat = Path(str(base) + '.chat.json')
    if chat.exists():
        files.append(chat)
    sync_files(files, session / 'broadcast.cloud.json')


if __name__ == '__main__':
    try:
        operation, *args = sys.argv[1:]
        if operation == 'part':
            publish_part(*args)
        elif operation == 'session':
            publish_session(*args, channel=os.environ['CHANNEL'])
    except Exception:
        print('[Publication] Sidecar or cloud publication failed; scratch retained for retry.', file=sys.stderr)
        raise SystemExit(1) from None
