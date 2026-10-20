#!/usr/bin/env python3
"""Require every enabled application worker to be running."""
import os
import subprocess
import sys


def check(environ=os.environ, run=subprocess.run):
    names = ['download', 'normalize', 'publish', 'status', 'notify', 'audit']
    names += [name for name in ('upload', 'encode') if environ.get(name.upper(), 'false') == 'true']
    expected = {'streamlink_' + name for name in names}
    try:
        result = run(['supervisorctl', 'status', *sorted(expected)],
                     capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    states = {fields[0]: fields[1] for line in result.stdout.splitlines()
              if len(fields := line.split()) >= 2}
    return result.returncode == 0 and all(states.get(name) == 'RUNNING' for name in expected)


if __name__ == '__main__':
    sys.exit(0 if check() else 1)
