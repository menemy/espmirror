# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import contextlib
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror as mirror
import fake_board


@contextlib.contextmanager
def fake_server(server=None):
    server = server or fake_board.FakeBoard(fake_board.PRESETS['waveshare-s3-175c'], port=0, fps=30)
    stop, errors = threading.Event(), []

    def serve():
        try:
            while not stop.is_set():
                server.step(.005)
        except BaseException as error:
            errors.append(error)
        finally:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield server
    finally:
        stop.set()
        thread.join(3)
        if thread.is_alive() or errors:
            raise AssertionError('fake server did not close cleanly: %r' % errors)


class LinkShutdownTests(unittest.TestCase):
    def test_close_waits_for_inflight_frame_callback(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        with fake_server() as server:
            link = mirror.Link('127.0.0.1', server.port)

            def frame():
                entered.set()
                release.wait(3)

            link.on_frame = frame
            link.start()
            closer = None
            try:
                self.assertTrue(entered.wait(3))
                workers = [t for t in threading.enumerate()
                           if getattr(getattr(t, '_target', None), '__self__', None) is link]
                closer = threading.Thread(target=lambda: (link.close(), closed.set()))
                closer.start()
                self.assertFalse(closed.wait(.05), 'close returned while frame callback was still running')
                release.set()
                self.assertTrue(closed.wait(2))
                self.assertFalse(link.thread.is_alive())
                self.assertTrue(all(not t.is_alive() for t in workers))
            finally:
                release.set()
                link.close()
                link.thread.join(3)
                if closer:
                    closer.join(3)

    def test_close_wakes_blocked_tcp_receive(self):
        receiving, closed = threading.Event(), threading.Event()
        reader, peer = socket.socketpair()

        class Transport:
            def recv(self, count):
                receiving.set()
                return reader.recv(count)

            sendall = reader.sendall
            shutdown = reader.shutdown
            close = reader.close

        link = mirror.Link('test')
        closer = None
        try:
            with patch.object(link, 'connect', return_value=Transport()):
                link.start()
                self.assertTrue(receiving.wait(2))
                workers = [t for t in threading.enumerate()
                           if getattr(getattr(t, '_target', None), '__self__', None) is link]
                closer = threading.Thread(target=lambda: (link.close(), closed.set()))
                closer.start()
                self.assertTrue(closed.wait(1), 'close did not wake the blocked socket read')
                self.assertFalse(link.thread.is_alive())
                self.assertTrue(all(not t.is_alive() for t in workers), 'a Link worker survived close')
        finally:
            peer.close()
            if closer:
                closer.join(3)
            link.close()
            link.thread.join(3)
            reader.close()

    def test_close_waits_for_pending_connection_and_closes_it(self):
        connecting, release, closed = threading.Event(), threading.Event(), threading.Event()
        reader, peer = socket.socketpair()
        link = mirror.Link('test')
        closer = None

        def connect():
            connecting.set()
            release.wait(3)
            return reader

        try:
            with patch.object(link, 'connect', side_effect=connect):
                link.start()
                self.assertTrue(connecting.wait(2))
                closer = threading.Thread(target=lambda: (link.close(), closed.set()))
                closer.start()
                self.assertFalse(closed.wait(.05), 'close returned while connection attempt was still running')
                release.set()
                self.assertTrue(closed.wait(2))
                self.assertFalse(link.thread.is_alive())
                self.assertEqual(reader.fileno(), -1)
        finally:
            release.set()
            if closer:
                closer.join(3)
            link.close()
            link.thread.join(3)
            reader.close()
            peer.close()

    def test_close_from_reader_callback_is_safe(self):
        with fake_server() as server:
            link = mirror.Link('127.0.0.1', server.port)
            errors = []

            def board(_info):
                try:
                    link.close()
                except BaseException as error:
                    errors.append(error)

            link.on_board = board
            link.start()
            try:
                link.thread.join(3)
                self.assertFalse(link.thread.is_alive())
                self.assertFalse(errors, errors)
                self.assertFalse(link.live)
            finally:
                link.close()
                link.thread.join(3)

    @unittest.skipUnless(os.name == 'posix', 'serial handshake check uses a POSIX PTY')
    def test_close_during_serial_handshake_ends_board_session(self):
        import mirror_serial_fake
        ready, release, ended = threading.Event(), threading.Event(), threading.Event()
        server = fake_board.FakeCable(fake_board.PRESETS['m5stack-sticks3'], fps=30, corrupt_every=0)
        original_input = server.input

        def receive(data, now):
            original_input(data, now)
            if server.breaks:
                ended.set()

        server.input = receive
        with fake_server(server), patch.dict(sys.modules, serial=mirror_serial_fake):
            link = mirror.Link(server.port)
            original_connect = link.connect

            def connect():
                sock = original_connect()
                ready.set()
                release.wait(3)
                return sock

            closer = None
            with patch.object(link, 'connect', side_effect=connect):
                link.start()
                try:
                    self.assertTrue(ready.wait(3), 'serial handshake did not complete')
                    self.assertTrue(server.active)
                    closer = threading.Thread(target=link.close)
                    closer.start()
                    self.assertTrue(link.stopped.wait(1))
                    release.set()
                    closer.join(2)
                    self.assertFalse(closer.is_alive())
                    self.assertTrue(ended.wait(1), 'close skipped BREAK after the serial handshake')
                    self.assertFalse(server.active)
                    self.assertFalse(link.thread.is_alive())
                finally:
                    release.set()
                    link.close()
                    if closer:
                        closer.join(3)

    def test_serial_cancel_read_does_not_overlap_transport_close(self):
        cancelling, release, closed = threading.Event(), threading.Event(), threading.Event()
        errors = []

        class Transport:
            def cancel_read(self):
                cancelling.set()
                release.wait(3)
                if closed.is_set():
                    raise TypeError('cancel_read used an already closed serial handle')

            def write(self, data):
                return len(data)

            def close(self):
                closed.set()

        link = mirror.Link('/dev/test')
        link.sock = Transport()

        def close():
            try:
                link.close()
            except BaseException as error:
                errors.append(error)

        closer = threading.Thread(target=close)
        disconnector = None
        closer.start()
        try:
            self.assertTrue(cancelling.wait(1))
            disconnector = threading.Thread(target=link.disconnect)
            disconnector.start()
            self.assertFalse(closed.wait(.05), 'transport closed during cancel_read')
        finally:
            release.set()
            closer.join(3)
            if disconnector:
                disconnector.join(3)
        self.assertFalse(closer.is_alive())
        self.assertFalse(disconnector.is_alive())
        self.assertTrue(closed.is_set())
        self.assertFalse(errors, errors)

    def test_repeated_stats_png_process_exit(self):
        with fake_server() as server, tempfile.TemporaryDirectory() as folder:
            target = '127.0.0.1:%d' % server.port
            for index in range(8):
                path = Path(folder) / ('capture-%d.png' % index)
                result = subprocess.run([sys.executable, str(ROOT / 'tools/espmirror.py'), target,
                                         '--stats', '--seconds', '.03', '--png', str(path)],
                                        capture_output=True, text=True, timeout=12,
                                        env={**os.environ, 'PYTHONFAULTHANDLER': '1'})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(path.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
                self.assertNotIn('Exception in thread', result.stderr)

    def test_png_error_still_closes_link(self):
        with patch.object(sys, 'argv', ['mirror', '127.0.0.1', '--png', 'capture.png']), \
                patch.object(mirror, 'Link') as link_type, \
                patch.object(mirror, 'save_png', side_effect=OSError('disk full')):
            link = link_type.return_value
            link.start.return_value = link
            with self.assertRaisesRegex(OSError, 'disk full'):
                mirror.main()
            link.close.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 is needed for the GUI shutdown check')
    def test_application_quit_stops_links_before_return(self):
        script = r'''
import sys, tempfile
from pathlib import Path
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
import espmirror as mirror

app = QApplication([])
links = []
class TrackedLink(mirror.Link):
    def start(self):
        links.append(self)
        return super().start()

mirror.Link = TrackedLink
QTimer.singleShot(150, app.quit)
with tempfile.TemporaryDirectory() as folder:
    settings = QSettings(str(Path(folder) / 'viewer.ini'), QSettings.Format.IniFormat)
    mirror.run_window([('127.0.0.1', int(sys.argv[1]))], 1, False, False, None, settings=settings)
    alive = [link for link in links if link.thread.is_alive()]
    for link in links:
        link.close()
        link.thread.join(3)
    assert links, 'viewer never started a connection'
    assert not alive, 'QApplication returned while Link threads were still alive'
'''
        with fake_server() as server:
            result = subprocess.run([sys.executable, '-c', script, str(server.port)], cwd=ROOT / 'tools',
                                    capture_output=True, text=True, timeout=15,
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONFAULTHANDLER': '1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 is needed for the GUI shutdown check')
    def test_startup_failure_closes_started_links_and_preserves_position(self):
        script = r'''
import sys, tempfile
from pathlib import Path
from PySide6.QtCore import QPoint, QSettings
from PySide6.QtWidgets import QApplication
import espmirror as mirror

app = QApplication([])
links = []
class TrackedLink(mirror.Link):
    def start(self):
        links.append(self)
        if len(links) == 2:
            raise RuntimeError('startup failure')
        return super().start()

mirror.Link = TrackedLink
with tempfile.TemporaryDirectory() as folder:
    settings = QSettings(str(Path(folder) / 'viewer.ini'), QSettings.Format.IniFormat)
    settings.setValue('position', QPoint(99, 88))
    try:
        mirror.run_window([('127.0.0.1', int(sys.argv[1]))] * 2, 1, False, True, None, settings=settings)
    except RuntimeError as error:
        assert str(error) == 'startup failure', str(error)
    else:
        raise AssertionError('startup unexpectedly succeeded')
    alive = links[0].thread.is_alive()
    for link in links:
        link.close()
    assert not alive, 'startup failure left the first connection running'
    assert settings.value('position') == QPoint(99, 88), 'unshown window overwrote the saved position'
'''
        with fake_server() as server:
            result = subprocess.run([sys.executable, '-c', script, str(server.port)], cwd=ROOT / 'tools',
                                    capture_output=True, text=True, timeout=15,
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONFAULTHANDLER': '1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
