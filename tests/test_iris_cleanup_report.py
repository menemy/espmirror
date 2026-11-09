# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror
import espmirror_iris as iris


class IrisCleanupReportTests(unittest.TestCase):
    def test_shutdown_cleanup_failures_reach_stderr_after_status_is_disabled(self):
        link = mirror.IrisLink('iris:' + 'a' * 32)
        client = Mock()

        def fail_open():
            link.alive = False
            link.stopped.set()
            raise ConnectionError('closed during startup')

        client.open.side_effect = fail_open
        client.cleanup.side_effect = ConnectionError('cannot stop mirror')
        output = io.StringIO()
        with patch.object(iris, 'IrisClient', return_value=client), contextlib.redirect_stderr(output):
            link.run()
        self.assertEqual(client.cleanup.call_count, 2)
        self.assertIn('Iris disconnect: cannot stop mirror', output.getvalue())
        self.assertIn('Iris cleanup failed: cannot stop mirror', output.getvalue())
