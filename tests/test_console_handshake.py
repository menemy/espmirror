# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import itertools
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import espmirror as mirror


class Serial:
    def __init__(self, stale, reply):
        self.pending = bytearray(stale)
        self.reply = reply
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, size):
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data

    def write(self, data):
        if data == b'\n>mirror\n':
            self.pending += self.reply
        return len(data)

    def close(self):
        self.closed = True


class ConsoleHandshakeTests(unittest.TestCase):
    def handshake(self, stale, reply):
        serial = Serial(stale, reply)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'console.log'
            log = mirror.ConsoleLog('test', path)
            with patch.object(mirror, 'open_usb_serial', return_value=serial), \
                    patch.object(mirror.time, 'sleep'), contextlib.redirect_stdout(output):
                self.assertIs(mirror.open_serial('test', on_text=log.feed), serial)
                log.flush()
            self.assertEqual(output.getvalue(), path.read_text(encoding='utf-8') if path.exists() else '')
        self.assertFalse(serial.closed)
        return serial, output.getvalue()

    def test_stale_frames_and_late_frames_do_not_reach_console(self):
        old = mirror.cable_frame(b'F\0\xff\nPAYLOAD ONLY\n' + bytes(range(128)))
        first_board = mirror.cable_frame(b'B{"w":1,"h":1}')
        for split in (0, 1, 5, 17, len(old)):
            with self.subTest(split=split):
                serial, output = self.handshake(old[:split], old[split:] + b'console ready\n' + mirror.MARK + first_board)
                self.assertEqual(bytes(serial.pending), first_board, 'handshake consumed the first board frame')
                self.assertEqual(output.count('console ready'), 1)
                self.assertNotIn('PAYLOAD ONLY', output)
                self.assertNotIn('\0', output)
                self.assertNotIn('\ufffd', output)

    def test_marker_in_old_frame_is_not_a_new_handshake(self):
        old = mirror.cable_frame(b'F' + mirror.MARK + b'PAYLOAD ONLY\n')
        first_board = mirror.cable_frame(b'B{}')
        serial, output = self.handshake(b'', old + b'console ready\n' + mirror.MARK + first_board)
        self.assertEqual(bytes(serial.pending), first_board)
        self.assertIn('console ready', output)
        self.assertNotIn('PAYLOAD ONLY', output)

    def test_mid_frame_tail_and_corrupt_frame_do_not_print_binary(self):
        damaged = bytearray(mirror.cable_frame(b'F\0\xff\n\x01\0\x10\n'))
        damaged[-1] ^= 1
        for stale in (b'\x01\0\xff\x10\n', bytes(damaged)):
            with self.subTest(stale=stale):
                _, output = self.handshake(stale, b'\nconsole recovered\n' + mirror.MARK)
                self.assertIn('console recovered', output)
                self.assertNotIn('\0', output)
                self.assertNotIn('\ufffd', output)
                self.assertNotIn('\x01', output)

    def test_truncated_old_frame_retains_board_bytes_read_during_resync(self):
        import struct
        board = b'B{"w":1,"h":1}'
        serial = Serial(mirror.SYNC + struct.pack('<I', 60000) + b'F\0', mirror.MARK + mirror.cable_frame(board))
        log = mirror.ConsoleLog('test')
        demux = mirror.CableDemux(log.feed, lambda: None)
        output = io.StringIO()
        with patch.object(mirror, 'open_usb_serial', return_value=serial), \
                patch.object(mirror.time, 'sleep'), \
                patch.object(mirror.time, 'monotonic', side_effect=itertools.count(0, .01)), \
                contextlib.redirect_stdout(output):
            mirror.open_serial('test', on_text=log.feed, demux=demux)
            messages = demux.feed(serial.read(serial.in_waiting), now=4)
            log.flush()
        self.assertEqual(messages, [board])
        self.assertNotIn('\0', output.getvalue())
        self.assertIsNone(demux.marker)

    def test_console_rejects_binary_lines_and_keeps_utf8_and_ansi(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'console.log'
            log = mirror.ConsoleLog('test', path)
            with contextlib.redirect_stdout(output):
                for byte in b'bad\0line\ninvalid\xff\n' + '\x1b[32mготово\x1b[0m\tOK\r\n'.encode():
                    log.feed(bytes([byte]))
                log.feed(b'partial\x01')
                log.flush()
            self.assertEqual(output.getvalue(), path.read_text(encoding='utf-8'))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn('\x1b[32mготово\x1b[0m\tOK', output.getvalue())

    def test_long_console_records_keep_utf8_boundaries_and_discard_binary_tail(self):
        for data, expected in ((('a' * 4095 + 'é\n').encode(), 'a' * 4095 + 'é'),
                               (b'\0' + b'a' * 5000 + b'\nafter\n', 'after')):
            with self.subTest(binary=data.startswith(b'\0')):
                output = io.StringIO()
                log = mirror.ConsoleLog('test')
                with contextlib.redirect_stdout(output):
                    log.feed(data)
                    log.flush()
                text = ''.join(line.split('] ', 1)[1] for line in output.getvalue().splitlines())
                self.assertEqual(text, expected)


if __name__ == '__main__':
    unittest.main()
