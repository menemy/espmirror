# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror


class DesktopPathTests(unittest.TestCase):
    def qt_paths(self, folder):
        def location(kind):
            self.assertEqual(kind, paths.StandardLocation.DesktopLocation)
            return str(folder)
        paths = SimpleNamespace(StandardLocation=SimpleNamespace(DesktopLocation=0),
                                writableLocation=location)
        return patch.dict(sys.modules, {'PySide6.QtCore': SimpleNamespace(QStandardPaths=paths)})

    @unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 is needed for native path resolution')
    def test_native_qt_desktop_location(self):
        from PySide6.QtCore import QStandardPaths
        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        folder = Path(desktop) if desktop and Path(desktop).is_dir() else Path.home()
        path = Path(espmirror.desktop_path('png', stamp='native-path-test'))
        self.assertEqual(path, folder / 'mirror-native-path-test.png')

    def test_redirected_desktop_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'OneDrive' / 'Рабочий стол'
            folder.mkdir(parents=True)
            with self.qt_paths(folder):
                path = espmirror.desktop_path('png', '-window', '20261109-120000')
            self.assertEqual(Path(path), folder / 'mirror-20261109-120000-window.png')
            espmirror.png_write(path, espmirror.np.zeros((2, 2, 3), dtype='uint8'))
            self.assertTrue(Path(path).read_bytes().startswith(b'\x89PNG'))

    def test_missing_desktop_falls_back_to_home(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(espmirror.os.path, 'expanduser', return_value=tmp):
            for folder in ('', str(Path(tmp) / 'missing')):
                with self.subTest(folder=folder), self.qt_paths(folder):
                    self.assertEqual(espmirror.desktop_path('mp4', stamp='test'),
                                     os.path.join(tmp, 'mirror-test.mp4'))

    def test_without_qt_keeps_headless_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(espmirror.os.path, 'expanduser', return_value=tmp):
            folder = Path(tmp) / 'Desktop'
            folder.mkdir()
            with patch.dict(sys.modules, {'PySide6.QtCore': None}):
                self.assertEqual(Path(espmirror.desktop_path('png', stamp='test')),
                                 folder / 'mirror-test.png')


if __name__ == '__main__':
    unittest.main()
