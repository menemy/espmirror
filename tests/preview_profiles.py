# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Draw every fake_board preset as the viewer would, offscreen: python tests/preview_profiles.py OUT_DIR [PRESET ...]

Writes OUT_DIR/<preset>.png and OUT_DIR/index.md, which names the profile each preset's name picks."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import espmirror as mirror  # noqa: E402
import fake_board  # noqa: E402


def shoot(preset, out):
    from unittest.mock import patch
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    done = []

    def draw():
        win = next(w for w in app.topLevelWidgets() if hasattr(w, 'render_devices'))
        try:
            info = fake_board.PRESETS[preset]
            d = win.devs[0]
            d.link.live = True
            d.link.fb = fake_board.Pattern(info).draw(3.5)
            win.on_board(0, info)
            win.on_frame(0)
            win.apply_zoom(2, say=False)
            app.processEvents()
            done.append(win.render_devices(ground=QColor('white')).save(str(out)))
        finally:
            win.close()
            app.quit()

    QTimer.singleShot(100, draw)
    QTimer.singleShot(15000, app.quit)
    with patch.object(mirror.Link, 'start', lambda self: self):
        with tempfile.TemporaryDirectory() as prefs:
            settings = QSettings(str(Path(prefs) / 'viewer.ini'), QSettings.Format.IniFormat)
            mirror.run_window([('preview', 7331)], 1, False, True, str(TOOLS / 'devices'), settings=settings)
    return 0 if done and done[0] else 1


def picked(name):
    profile = mirror.device_profile(name)
    return next(('`%s`' % fragment for fragment, p in mirror.DEVICES if p is profile), 'none (a plain frame)')


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--one':
        return shoot(sys.argv[2], Path(sys.argv[3]))
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    rows, failed = ['| Preset | Name | Profile |', '|---|---|---|'], []
    for preset in sys.argv[2:] or fake_board.PRESETS:
        name = fake_board.PRESETS[preset]['name']
        result = subprocess.run([sys.executable, __file__, '--one', preset, str(out / (preset + '.png'))],
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}, capture_output=True, text=True,
                                timeout=60)
        if result.returncode:
            failed.append(preset)
            print(result.stdout + result.stderr, file=sys.stderr)
        rows.append('| %s | %s | %s |' % (preset, name, picked(name)))
    (out / 'index.md').write_text('\n'.join(rows) + '\n')
    print('\n'.join(rows))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
