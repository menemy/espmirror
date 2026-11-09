# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('PySide6') and importlib.util.find_spec('numpy'),
                     'PySide6 and numpy are needed for the offscreen viewer test')
class ViewerTests(unittest.TestCase):
    def test_profiles_states_keys_and_capture(self):
        script = r'''
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import espmirror as mirror
import fake_board

app = QApplication([])
errors = []
completed = []

def check():
    win = next(w for w in app.topLevelWidgets() if hasattr(w, 'render_devices'))
    try:
        scale = float(os.environ['QT_SCALE_FACTOR'])
        assert win.devicePixelRatioF() == scale
        names = ['waveshare-s3-216', 'waveshare-s3-154g', 'waveshare-s3-349',
                 'waveshare-p4-35', 'waveshare-s3-175c', 'aipi-lite']
        for i, name in enumerate(names):
            info = fake_board.PRESETS[name]
            d = win.devs[i]
            d.link.live = True
            d.link.fb = fake_board.Pattern(info).draw(3.5)
            win.on_board(i, info)
            win.on_frame(i)
            role = info['buttons'][0]['role']
            win.press(d, role, True)
            win.press(d, role, False)
            first = d.link.out.get_nowait()
            if d.look['profile'].get('hold', True):
                assert first == bytes([6, role, 1])
                assert d.link.out.get_nowait() == bytes([6, role, 0])
            else:
                assert first == bytes([3, role])
        with tempfile.TemporaryDirectory() as folder:
            renders = []
            for state in range(3):
                for d in win.devs:
                    d.link.state = state
                    d.link.led = state + 1
                shot = win.render_devices()
                assert not shot.isNull()
                assert shot.devicePixelRatio() == scale
                assert shot.save(str(Path(folder) / ('state%d.png' % state)))
                renders.append(bytes(shot.constBits()))
            assert renders[0] != renders[1] != renders[2]
            win.grab()
        completed.append(True)
    except BaseException as error:
        errors.append(error)
    finally:
        win.close()
        app.quit()

QTimer.singleShot(100, check)
QTimer.singleShot(10000, app.quit)
with patch.object(mirror.Link, 'start', lambda self: self):
    mirror.run_window([('test%d' % i, 7331) for i in range(6)], 1, False, True,
                      str(Path(mirror.__file__).parent / 'devices'))
if errors:
    raise errors[0]
assert completed, "viewer callback did not run"
'''
        for scale in ('1', '1.5', '2'):
            with self.subTest(scale=scale):
                result = subprocess.run([sys.executable, '-c', script], cwd=ROOT / 'tools',
                                        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QT_SCALE_FACTOR': scale},
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
