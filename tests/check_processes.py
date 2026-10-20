"""Check real supervised workers; offline curl fixtures prevent network access."""
import os
import pathlib

expected = {'download': True, 'publish': True, 'normalize': True, 'status': True, 'notify': True, 'audit': True,
            'upload': os.environ['UPLOAD'] == 'true',
            'encode': os.environ['ENCODE'] == 'true'}
arguments = []
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    try:
        arguments.append(path.read_bytes().split(b'\0'))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
for worker, enabled in expected.items():
    command = ('/etc/streamlink/tools/notifications.py' if worker == 'notify'
               else f'/etc/streamlink/tools/{worker}.sh').encode()
    running = any(command in args for args in arguments)
    assert running == enabled, f'{worker}: expected {enabled}, running {running}'
