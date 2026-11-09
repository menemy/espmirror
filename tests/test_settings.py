# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror as mirror
from espmirror_settings import DEFAULTS, read_preferences, validate_hosts


class StartupSettingsTests(unittest.TestCase):
    def test_unavailable_log_does_not_interrupt_console_input(self):
        for error in (FileNotFoundError('folder removed'), PermissionError('read only'), OSError('disk full')):
            output, errors = io.StringIO(), io.StringIO()
            log = mirror.ConsoleLog('test', '/tmp/unavailable-console.log')
            with self.subTest(error=error), patch('builtins.open', side_effect=error) as open_log, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                log.feed(b'first line\nsecond line\n')
                log.flush()
            self.assertIsNone(log.file)
            self.assertIn('first line', output.getvalue())
            self.assertIn('second line', output.getvalue())
            self.assertEqual(errors.getvalue().count('console log disabled'), 1)
            open_log.assert_called_once()

    def test_capture_folder_preserves_default_path_hook(self):
        with patch.object(mirror, 'desktop_path', return_value='/tmp/default.png') as desktop:
            self.assertEqual(mirror.capture_path('png', '-window', 'stamp'), '/tmp/default.png')
            desktop.assert_called_once_with('png', '-window', 'stamp')
            self.assertEqual(mirror.capture_path('mp4', stamp='stamp', folder='/tmp/custom'),
                             os.path.join('/tmp/custom', 'mirror-stamp.mp4'))

    def test_saved_targets_and_explicit_cli_overrides(self):
        preferences = dict(DEFAULTS, usb=False, wifi=True, hosts='192.0.2.8:7444\n192.0.2.8:7444',
                           sound=True, on_top=True, scale=2.0, log='/tmp/viewer.log')
        cases = [([], [('192.0.2.7', 7331), ('192.0.2.8', 7444)], True, 2.0, True, True, '/tmp/viewer.log'),
                 (['--no-wifi', '--no-sound', '--no-on-top', '--scale', '1.5', '--log', ''],
                  [('192.0.2.8', 7444)], False, 1.5, False, False, ''),
                 (['192.0.2.9'], [('192.0.2.9', 7331)], False, 2.0, True, True, '/tmp/viewer.log')]
        for argv, targets, discovery, scale, top, sound, log in cases:
            with self.subTest(argv=argv), patch.object(sys, 'argv', ['mirror'] + argv), \
                    patch('espmirror_settings.read_preferences', return_value=preferences), \
                    patch.object(mirror.importlib.util, 'find_spec', return_value=True), \
                    patch.object(mirror, 'usb_boards') as usb, \
                    patch.object(mirror, 'discover', return_value=[('remote', '192.0.2.7', 7331, '')]) as net, \
                    patch.object(mirror, 'run_window', return_value=0) as window:
                self.assertEqual(mirror.main(), 0)
                self.assertEqual(window.call_args.args[:3], (targets, scale, top))
                self.assertEqual(window.call_args.kwargs, dict(
                    log_file=log, sound=sound,
                    iris_gateway=os.environ.get('ESPMIRROR_IRIS_GATEWAY', 'http://127.0.0.1:8443')))
                self.assertEqual(net.called, discovery)
                usb.assert_not_called()

    def test_headless_does_not_load_preferences(self):
        with patch.object(sys, 'argv', ['mirror', '--list']), \
                patch('espmirror_settings.read_preferences') as prefs, \
                patch.object(mirror, 'usb_boards', return_value=[]), \
                patch.object(mirror, 'discover') as net, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mirror.main(), 0)
            prefs.assert_not_called()
            net.assert_not_called()

    def test_saved_iris_target_and_discovery_are_deduplicated(self):
        device = '0123456789abcdef0123456789abcdef'
        preferences = dict(DEFAULTS, usb=False, hosts='iris:' + device)
        with patch.object(sys, 'argv', ['mirror', '--iris', '--iris-gateway', 'http://localhost:9999']), \
                patch('espmirror_settings.read_preferences', return_value=preferences), \
                patch.object(mirror.importlib.util, 'find_spec', return_value=True), \
                patch('espmirror_iris.discover', return_value=[dict(device_id=device)]), \
                patch.object(mirror, 'run_window', return_value=0) as window:
            self.assertEqual(mirror.main(), 0)
            self.assertEqual(window.call_args.args[0], [('iris:' + device, mirror.PORT)])
            self.assertEqual(window.call_args.kwargs['iris_gateway'], 'http://localhost:9999')

    def test_invalid_scale_and_saved_targets(self):
        for scale in ('nan', 'inf', '0', '-1'):
            with self.subTest(scale=scale), patch.object(sys, 'argv', ['mirror', '--list', '--scale', scale]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                mirror.main()
        for host in ('host:0', 'host:65536', ':7331', 'host:nope', 'iris:123', 'iris:' + 'g' * 32):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_hosts(host)
        self.assertEqual(validate_hosts(' COM7\n::1\nlocalhost:7331 '), 'COM7\n::1\nlocalhost:7331')


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 is needed for preferences tests')
class QtSettingsTests(unittest.TestCase):
    def test_persistence_and_invalid_scale(self):
        from PySide6.QtCore import QSettings
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'settings.ini')
            settings = QSettings(path, QSettings.Format.IniFormat)
            self.assertEqual(read_preferences(settings), DEFAULTS)
            settings.setValue('sound', True)
            settings.setValue('scale', 2.5)
            settings.sync()
            restored = read_preferences(QSettings(path, QSettings.Format.IniFormat))
            self.assertTrue(restored['sound'])
            self.assertEqual(restored['scale'], 2.5)
            for invalid in ('garbage', float('nan'), float('inf'), -2, 100):
                settings.setValue('scale', invalid)
                self.assertEqual(read_preferences(settings)['scale'], 1.0)

    def test_dialog_save_cancel_and_window_integration(self):
        script = r'''
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from PySide6.QtCore import QPoint, QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialogButtonBox,
                              QDoubleSpinBox, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QTabWidget)
import espmirror as mirror
from espmirror_settings import edit_preferences, read_preferences
import fake_board

app = QApplication([])
errors, completed = [], []
folder = tempfile.TemporaryDirectory()
settings = QSettings(str(Path(folder.name) / 'settings.ini'), QSettings.Format.IniFormat)

def dialog_action(save, scale=200, remember=True):
    try:
        dialog = app.activeModalWidget()
        assert dialog.objectName() == 'viewer_settings'
        dialog.findChild(QDoubleSpinBox, 'scale').setValue(scale)
        for name in ('on_top', 'sound'):
            dialog.findChild(QCheckBox, name).setChecked(True)
        dialog.findChild(QCheckBox, 'show_touches').setChecked(False)
        dialog.findChild(QCheckBox, 'remember_position').setChecked(remember)
        for name in ('screenshots', 'recordings'):
            field = dialog.findChild(QLineEdit, name)
            field.setText(folder.name)
            assert any(label.buddy() == field for label in dialog.findChildren(QLabel))
        editor = dialog.findChild(QPlainTextEdit, 'hosts')
        editor.setPlainText('127.0.0.1:7331')
        dialog.findChild(QTabWidget).setCurrentIndex(2)
        editor.setFocus()
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        assert not editor.hasFocus()
        assert '\t' not in editor.toPlainText()
        buttons = dialog.findChild(QDialogButtonBox)
        buttons.button(QDialogButtonBox.StandardButton.Save if save else
                       QDialogButtonBox.StandardButton.Cancel).click()
    except BaseException as error:
        errors.append(error)
        if app.activeModalWidget():
            app.activeModalWidget().reject()

QTimer.singleShot(50, lambda: dialog_action(False))
assert not edit_preferences(settings=settings)
assert not settings.allKeys(), settings.allKeys()
QTimer.singleShot(50, lambda: dialog_action(True))
assert edit_preferences(settings=settings)
assert read_preferences(settings)['scale'] == 2
settings.setValue('position', QPoint(25, 30))
QTimer.singleShot(50, lambda: dialog_action(True, remember=False))
assert edit_preferences(settings=settings)
assert not settings.contains('position')
settings.setValue('remember_position', True)
settings.setValue('position', QPoint(999999, 999999))

failed_store = QSettings(str(Path(folder.name) / 'failed.ini'), QSettings.Format.IniFormat)
failed_store.setValue('scale', 1.25)
failed_store.setValue('position', QPoint(7, 8))
failed_store.sync()
failing = MagicMock(wraps=failed_store)
failing.status.return_value = QSettings.Status.AccessError

def save_failure(*args):
    dialog = app.activeModalWidget()
    try:
        assert set(failed_store.allKeys()) == {'scale', 'position'}
        assert read_preferences(failed_store)['scale'] == 1.25
        assert failed_store.value('position') == QPoint(7, 8)
        assert not dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).isEnabled()
    except BaseException as error:
        errors.append(error)
    finally:
        dialog.reject()

QTimer.singleShot(50, lambda: dialog_action(True, remember=False))
with patch.object(QMessageBox, 'warning', side_effect=save_failure) as warning:
    assert not edit_preferences(settings=failing)
    warning.assert_called_once()
failed_store.sync()
assert set(QSettings(failed_store.fileName(), QSettings.Format.IniFormat).allKeys()) == {'scale', 'position'}

def check():
    win = next(w for w in app.topLevelWidgets() if hasattr(w, 'render_devices'))
    try:
        assert win.screen().availableGeometry().contains(win.pos())
        area = win.screen().availableGeometry()
        settings.setValue('position', QPoint(area.left() - 1, area.top() + 40))
        win.restore_position()
        assert win.pos() == QPoint(area.left(), area.top() + 40)
        assert not win.touches_act.isChecked()
        info = fake_board.PRESETS['waveshare-s3-175c']
        link = win.devs[0].link
        link.fb = fake_board.Pattern(info).draw(3.5)
        win.on_board(0, info)
        win.on_frame(0)
        win.screenshot()
        assert len(list(Path(folder.name).glob('*.png'))) == 2
        assert not win.top_act.isChecked()
        QTimer.singleShot(50, lambda: dialog_action(True))
        win.open_settings()
        assert not win.top_act.isChecked(), 'unrelated Save must preserve CLI override'
        assert settings.value('on_top', type=bool), 'unrelated Save must preserve default'
        win.top_act.trigger()
        assert settings.value('on_top', type=bool)
        QTimer.singleShot(50, lambda: dialog_action(True, 250))
        win.open_settings()
        assert abs(win.zoom - 2.5) < .01
        assert win.top_act.isChecked()
        assert win.preferences['recordings'] == folder.name
        win.preferences['recordings'] = str(Path(folder.name) / 'deleted')
        with patch.object(mirror.shutil, 'which', return_value='/fake/ffmpeg'), \
                patch.object(mirror.subprocess, 'Popen') as launch:
            win.record()
            assert win.rec is None
            launch.assert_not_called()
        win.preferences['recordings'] = folder.name
        process = MagicMock()
        process.poll.return_value = process.wait.return_value = 1
        with patch.object(mirror.shutil, 'which', return_value='/fake/ffmpeg'), \
                patch.object(mirror.subprocess, 'Popen', return_value=process):
            win.record()
            writer = win.rec['thread']
            win.rec_frame()
            assert win.rec is None
            assert not win.rec_timer.isActive()
            writer.join(2)
            assert not writer.is_alive()
        win.move(25, 30)
        completed.append(True)
    except BaseException as error:
        errors.append(error)
    finally:
        win.close()
        app.quit()

QTimer.singleShot(100, check)
QTimer.singleShot(10000, app.quit)
with patch.object(mirror.Link, 'start', lambda self: self):
    mirror.run_window([('test', 7331)], 1, False, False, None, settings=settings)
assert settings.value('position') == QPoint(25, 30)
if errors:
    raise errors[0]
assert completed
folder.cleanup()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT / 'tools',
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
