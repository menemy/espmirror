# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Offscreen viewer with real sockets or pyserial on a POSIX PTY."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror as mirror
import fake_board


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--transport', choices=('tcp', 'serial'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    info = fake_board.PRESETS['waveshare-s3-175c']
    serial = args.transport == 'serial'
    server = (fake_board.FakeCable(info, corrupt_every=0) if serial
              else fake_board.FakeBoard(info, port=0))
    target = (server.port, mirror.PORT) if serial else ('127.0.0.1', server.port)
    stop = threading.Event()
    errors, complete, links = [], [], []
    state = {'phase': 'frame', 'until': 0, 'record': None}
    deadline = time.monotonic() + 15

    def serve():
        try:
            while not stop.is_set():
                server.step(.005)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()

    def output_path(ext, tag='', stamp=None):
        return str(args.output / ('mirror%s.%s' % (tag, ext)))

    def finish(win):
        timer.stop()
        win.close()
        app.quit()

    def check():
        win = next((w for w in app.topLevelWidgets() if hasattr(w, 'render_devices')), None)
        if win is None:
            return
        try:
            d = win.devs[0]
            if not links:
                links.append(d.link)
            assert time.monotonic() < deadline, 'viewer smoke timeout in ' + state['phase']
            if errors:
                raise errors[0]
            if state['phase'] == 'frame':
                with d.link.lock:
                    ready = d.link.live and d.link.seen is not None and d.link.seen.all()
                if not ready or d.image is None:
                    return
                if serial and not (args.output / 'console.log').exists():
                    return
                win.on_frame(0)
                win.screenshot()
                for name in ('mirror.png', 'mirror-window.png'):
                    assert not QImage(str(args.output / name)).isNull(), name
                shot = QImage(str(args.output / 'mirror.png'))
                assert (shot.width(), shot.height()) == (info['w'], info['h'])
                win.press(d, info['buttons'][0]['role'], True)
                state['phase'] = 'key'
            elif state['phase'] == 'key':
                if info['buttons'][0]['role'] not in server.pattern.held:
                    return
                win.press(d, info['buttons'][0]['role'], False)
                state['phase'] = 'release'
            elif state['phase'] == 'release':
                if server.pattern.held:
                    return
                if serial:
                    log = (args.output / 'console.log').read_text(encoding='utf-8')
                    if 'frame sent' not in log:
                        return
                if args.record:
                    win.record()
                    assert win.rec is not None, 'recording did not start'
                    state.update(phase='record', until=time.monotonic() + .6, record=win.rec)
                else:
                    complete.append(True)
                    finish(win)
            elif (state['phase'] == 'record' and time.monotonic() >= state['until']
                  and state['record']['n'] >= 2):
                win.record()
                state['phase'] = 'encode'
            elif state['phase'] == 'encode' and not state['record']['thread'].is_alive():
                complete.append(True)
                finish(win)
        except BaseException as error:
            errors.append(error)
            finish(win)

    timer = QTimer(interval=20, timeout=check)
    timer.start()
    with ExitStack() as stack:
        try:
            stack.enter_context(patch.object(mirror, 'desktop_path', output_path))
            if serial:
                import serial as pyserial
                # PTYs have no modem-control lines; data, termios and locking use pyserial.
                stack.enter_context(patch.object(pyserial.Serial, '_update_dtr_state', lambda self: None))
                stack.enter_context(patch.object(pyserial.Serial, '_update_rts_state', lambda self: None))
            mirror.run_window([target], 1, False, False, str(ROOT / 'tools/devices'),
                              log_file=str(args.output / 'console.log'))
        finally:
            timer.stop()
            for win in app.topLevelWidgets():
                if hasattr(win, 'render_devices'):
                    for d in win.devs:
                        if d.link not in links:
                            links.append(d.link)
                    if win.rec:
                        state['record'] = win.rec
                        win.record()
                    win.close()
            for link in links:
                link.close()
            stop.set()
            thread.join(3)
            server.close()
            for link in links:
                link.thread.join(7)
            if state['record']:
                state['record']['thread'].join(7)
    if errors:
        raise errors[0]
    assert complete, 'viewer check did not finish'
    assert not thread.is_alive()
    assert all(not link.thread.is_alive() for link in links)
    assert not state['record'] or not state['record']['thread'].is_alive()
    if args.record:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                 '-show_entries', 'stream=codec_name,width,height,nb_frames',
                                 '-of', 'json', output_path('mp4')],
                                capture_output=True, text=True, timeout=10, check=True)
        stream = json.loads(result.stdout)['streams'][0]
        assert stream['codec_name'] == 'h264', stream
        assert int(stream['nb_frames']) >= 2, stream
        assert stream['width'] % 2 == stream['height'] % 2 == 0, stream
        print(json.dumps(stream))
    print(args.transport + ' viewer smoke passed')


if __name__ == '__main__':
    main()
