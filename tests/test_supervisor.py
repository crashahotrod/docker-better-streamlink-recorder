"""Parse the actual Supervisor configuration without launching processes."""
import os
import pathlib
import pwd

from supervisor.options import ServerOptions

root = pathlib.Path(__file__).resolve().parent.parent
os.environ['USER_NAME'] = pwd.getpwuid(os.getuid()).pw_name
for upload in ('true', 'false', 'true', 'false'):
    for encode in ('true', 'false'):
        os.environ.update(UPLOAD=upload, ENCODE=encode)
        options = ServerOptions()
        options.realize(args=['-c', str(root / 'supervisord.conf')])
        programs = {
            process.name: process.autostart
            for group in options.process_group_configs
            for process in group.process_configs
        }
        assert programs['streamlink_upload'] == (upload == 'true')
        assert programs['streamlink_encode'] == (encode == 'true')
        assert programs['streamlink_download'] and programs['streamlink_publish']
print('PASS: Supervisor parses flags correctly, including enabled-to-disabled changes')
