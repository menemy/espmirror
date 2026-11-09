# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('PySide6') and importlib.util.find_spec('numpy'),
                     'PySide6 and numpy are needed for the offscreen viewer test')
class ViewerTransportTests(unittest.TestCase):
    def smoke(self, transport, record=False):
        with tempfile.TemporaryDirectory(prefix='mirror smoke ') as tmp:
            result = subprocess.run([sys.executable, str(ROOT / 'tests/viewer_transport_smoke.py'),
                                     '--transport', transport, '--output', str(Path(tmp) / 'captures'),
                                     *(['--record'] if record else [])],
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tcp_frames_keys_and_screenshot(self):
        self.smoke('tcp')

    @unittest.skipUnless(os.name == 'posix' and importlib.util.find_spec('serial'),
                         'serial smoke requires POSIX PTYs and pyserial')
    def test_serial_frames_keys_log_and_screenshot(self):
        self.smoke('serial')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                         'recording smoke requires ffmpeg and ffprobe on PATH')
    def test_tcp_recording(self):
        self.smoke('tcp', record=True)


if __name__ == '__main__':
    unittest.main()
