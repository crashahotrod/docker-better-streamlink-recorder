#!/usr/bin/env python3
"""Small operational helpers shared by the shell workers."""
import datetime
import hashlib
import json
import pathlib
import sys
from zoneinfo import ZoneInfo


def next_quota_reset(now):
    pacific = ZoneInfo('America/Los_Angeles')
    local = datetime.datetime.fromtimestamp(now, pacific)
    tomorrow = local.date() + datetime.timedelta(days=1)
    return int(datetime.datetime.combine(tomorrow, datetime.time(), pacific).timestamp())


def quota_key(secrets):
    path = pathlib.Path(secrets)
    try:
        credentials = json.loads(path.read_text())
        identity = (credentials.get('installed') or credentials.get('web') or {})['client_id']
        if not isinstance(identity, str) or not identity:
            raise ValueError('Missing client ID')
    except (OSError, ValueError, KeyError, TypeError):
        # Offline tests and incomplete configurations still share a stable scope.
        identity = str(path.resolve())
    return hashlib.sha256(identity.encode()).hexdigest()


if __name__ == '__main__':
    if sys.argv[1] == 'quota-reset':
        print(next_quota_reset(int(sys.argv[2])))
    elif sys.argv[1] == 'quota-key':
        print(quota_key(sys.argv[2]))
    else:
        raise SystemExit('Unknown operation')
