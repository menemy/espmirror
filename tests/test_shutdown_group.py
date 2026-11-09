# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror as mirror
from test_shutdown import fake_server


class GroupShutdownTests(unittest.TestCase):
    def test_close_does_not_repeat_reader_cleanup(self):
        with fake_server() as server:
            link = mirror.Link('127.0.0.1', server.port)
            ready = threading.Event()
            link.on_board = lambda info: ready.set()
            with patch.object(link, 'disconnect', wraps=link.disconnect) as cleanup:
                link.start()
                try:
                    self.assertTrue(ready.wait(3))
                    link.close()
                    link.close()
                    self.assertEqual(cleanup.call_count, 1)
                finally:
                    link.close()

    def test_group_join_has_one_deadline(self):
        release = threading.Event()
        links = [mirror.Link('test') for _ in range(4)]
        try:
            for link in links:
                link.thread = threading.Thread(target=release.wait, daemon=True)
                link.thread.start()
            started = time.monotonic()
            self.assertFalse(mirror.join_links(links, timeout=.1))
            self.assertLess(time.monotonic() - started, .3, 'deadline was repeated for each link')
        finally:
            release.set()
            for link in links:
                link.thread.join(2)

    def test_serial_cancel_does_not_wait_for_writer_lock(self):
        link = mirror.Link('/dev/test')
        finished = threading.Event()
        errors = []

        def cancel():
            try:
                link.cancel()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with link.tx_lock:
            worker = threading.Thread(target=cancel)
            worker.start()
            completed = finished.wait(.2)
        worker.join(2)
        self.assertTrue(completed, 'cancelling one serial link delayed cancellation of all other links')
        self.assertFalse(errors, errors)

    def test_stats_start_failure_still_joins_started_worker(self):
        with fake_server() as server:
            link = mirror.Link('127.0.0.1', server.port)
            start = link.start

            def fail_after_start():
                start()
                raise RuntimeError('failure after worker start')

            try:
                with patch.object(sys, 'argv', ['mirror', 'test', '--stats', '--seconds', '.001']), \
                        patch.object(mirror, 'make_link', return_value=link), \
                        patch.object(link, 'start', side_effect=fail_after_start):
                    with self.assertRaisesRegex(RuntimeError, 'failure after worker start'):
                        mirror.main()
                self.assertFalse(link.thread.is_alive(), 'stats startup failure abandoned its reader')
            finally:
                link.close()

    def test_headless_waits_for_buffered_stdout_workers(self):
        script = r'''
import io, sys, threading
from unittest.mock import patch
import espmirror as mirror

mode = sys.argv.pop(1)
joining, writing, release, returned = (threading.Event() for _ in range(4))
main_thread = threading.current_thread()

class Raw(io.RawIOBase):
    def __init__(self):
        super().__init__()
        self.data = bytearray()
    def writable(self):
        return True
    def write(self, data):
        if threading.current_thread() is not main_thread:
            writing.set()
            assert release.wait(3), 'stdout writer was never released'
        self.data.extend(data)
        return len(data)

class Reader(threading.Thread):
    def join(self, timeout=None):
        joining.set()
        return super().join(timeout)

class LoggingLink(mirror.Link):
    def start(self):
        self.fb = object()
        self.thread = Reader(target=self.run, daemon=True)
        self.thread.start()
        return self
    def run(self):
        assert joining.wait(3), 'headless path never joined its reader'
        if mode == 'reader':
            self.logs.feed(b'shutdown console tail\n')
        else:
            self.writer_thread = threading.Thread(target=lambda: self.logs.feed(b'shutdown console tail\n'), daemon=True)
            self.writer_thread.start()
            assert writing.wait(3), 'writer never acquired the buffered stdout lock'

def unblock():
    assert writing.wait(3)
    returned.wait(.15)
    release.set()

raw = Raw()
output = io.TextIOWrapper(io.BufferedWriter(raw, buffer_size=8), encoding='utf-8')
original = sys.stdout
link = LoggingLink('test')
unblocker = threading.Thread(target=unblock)
unblocker.start()
try:
    sys.stdout = output
    with patch.object(mirror, 'make_link', return_value=link), patch.object(mirror, 'save_png', return_value=0):
        code = mirror.main()
    survivors = [t for t in (link.thread, link.writer_thread) if t is not None and t.is_alive()]
    returned.set()
finally:
    release.set()
    unblocker.join(3)
    for worker in (link.thread, link.writer_thread):
        if worker:
            worker.join(3)
    output.flush()
    sys.stdout = original
assert code == 0
assert writing.is_set(), 'test never held the buffered stdout lock'
assert b'shutdown console tail' in raw.data
assert not survivors, 'headless main returned with a worker holding buffered stdout'
'''
        for mode in ('reader', 'writer'):
            for flags in (['--png', 'unused.png'], ['--stats', '--seconds', '.001'],
                          ['--stats', '--seconds', '.001', '--png', 'unused.png']):
                with self.subTest(worker=mode, flags=flags):
                    result = subprocess.run([sys.executable, '-c', script, mode, 'test', *flags],
                                            cwd=ROOT / 'tools', capture_output=True, text=True, timeout=10,
                                            env={**os.environ, 'PYTHONFAULTHANDLER': '1'})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn('_enter_buffered_busy', result.stderr)

    @unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 is needed for the GUI shutdown check')
    def test_window_cancels_every_link_before_waiting(self):
        script = r'''
import sys, tempfile, threading, time
from pathlib import Path
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
import espmirror as mirror

app = QApplication([])
links = []
release, rescued = threading.Event(), threading.Event()
deadline = sys.argv[1] == 'deadline'
pending_after_close = []
errors = []
class GroupLink(mirror.Link):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = False
        self.cancels = self.cleanups = 0
        links.append(self)
    def cancel(self):
        self.cancels += 1
        self.cancelled = True
        if not deadline and all(link.stopped.is_set() and link.cancelled for link in links):
            release.set()
    def run(self):
        self.stopped.wait(3)
        release.wait(3)
        self.cleanups += 1

mirror.Link = GroupLink
def rescue():
    rescued.set()
    release.set()

def close_window():
    watchdog = threading.Timer(1.5, rescue)
    watchdog.start()
    try:
        win = next(w for w in app.topLevelWidgets() if hasattr(w, 'render_devices'))
        started = time.monotonic()
        win.close()
        win.close()
        pending_after_close.append(any(link.thread.is_alive() for link in links))
        assert time.monotonic() - started < 1.4, 'window exceeded the shared link deadline'
    except BaseException as error:
        errors.append(repr(error))
        app.quit()
    finally:
        release.set()
        watchdog.cancel()

QTimer.singleShot(100, close_window)
with tempfile.TemporaryDirectory() as folder:
    settings = QSettings(str(Path(folder) / 'viewer.ini'), QSettings.Format.IniFormat)
    mirror.run_window([('test-a', 7331), ('test-b', 7331)], 1, False, True, None, settings=settings)
assert not rescued.is_set(), 'first link waited while later links were still running'
assert not errors, errors
assert all(link.cancels == 1 for link in links), 'window repeated cancellation after close'
assert all(link.cleanups == 1 and not link.thread.is_alive() for link in links)
if deadline:
    assert pending_after_close == [True], 'test did not exercise pending cleanup after the GUI deadline'
'''
        for mode in ('group', 'deadline'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', script, mode], cwd=ROOT / 'tools', capture_output=True,
                                        text=True, timeout=10,
                                        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONFAULTHANDLER': '1'})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
