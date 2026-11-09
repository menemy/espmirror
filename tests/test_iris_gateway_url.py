# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror
import espmirror_iris as iris


class IrisGatewayURLTests(unittest.TestCase):
    def test_cli_and_environment_errors_precede_qt_and_connection_setup(self):
        for environment in (False, True):
            for bad in ('file:///tmp/gateway', 'http://host/path', 'http://host:bad',
                        'http://host:65536', 'http://host:0', 'http://host:',
                        'http://bad host', 'http://user:secret@host', 'http://[bad'):
                with self.subTest(environment=environment, url=bad):
                    args = ['espmirror', 'iris:' + 'a' * 32]
                    if not environment:
                        args += ['--iris-gateway', bad]
                    errors = io.StringIO()
                    with patch.object(sys, 'argv', args), \
                            patch.dict(os.environ, ESPMIRROR_IRIS_GATEWAY=bad if environment else 'http://localhost'), \
                            patch.object(mirror.importlib.util, 'find_spec') as find_qt, \
                            patch.object(mirror, 'run_window') as window, \
                            patch.object(mirror, 'make_link') as connect, contextlib.redirect_stderr(errors):
                        with self.assertRaises(SystemExit) as stopped:
                            mirror.main()
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertIn('--iris-gateway:', errors.getvalue())
                    self.assertNotIn('Traceback', errors.getvalue())
                    self.assertNotIn('secret', errors.getvalue())
                    find_qt.assert_not_called()
                    window.assert_not_called()
                    connect.assert_not_called()

    def test_valid_roots_include_ipv6_and_are_normalized(self):
        for url in ('http://localhost:8443', 'https://example.com', 'http://[::1]:8443'):
            self.assertEqual(iris.gateway_url(url + '/'), url)
