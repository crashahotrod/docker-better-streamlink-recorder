"""Validate actual Supervisor configuration without launching processes."""
import os
import pathlib
try:
    import pwd
    from supervisor.options import ServerOptions
except ImportError:
    pwd = None
    ServerOptions = None
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipIf(ServerOptions is None or pwd is None, 'Supervisor configuration tests require supervisor and pwd')
class SupervisorTests(unittest.TestCase):
    def test_worker_flags_and_process_group_shutdown(self):
        # Repeated enabled-to-disabled transitions catch stale defaults.
        with patch.dict(os.environ, USER_NAME=pwd.getpwuid(os.getuid()).pw_name):
            for upload in ('true', 'false', 'true', 'false'):
                for encode in ('true', 'false'):
                    with self.subTest(upload=upload, encode=encode):
                        os.environ.update(UPLOAD=upload, ENCODE=encode)
                        options = ServerOptions()
                        options.realize(args=['-c', str(ROOT / 'supervisord.conf')])
                        programs = {p.name: p for g in options.process_group_configs for p in g.process_configs}
                        expected = {'download': True, 'normalize': True, 'publish': True,
                                    'upload': upload == 'true', 'encode': encode == 'true',
                                    'status': True}
                        self.assertEqual({f'streamlink_{name}' for name in expected}, set(programs))
                        for name, enabled in expected.items():
                            process = programs[f'streamlink_{name}']
                            self.assertEqual(enabled, process.autostart)
                            self.assertTrue(process.stopasgroup)
                            self.assertTrue(process.killasgroup)
                            self.assertGreaterEqual(process.stopwaitsecs, 30)


if __name__ == '__main__':
    unittest.main(verbosity=2)
