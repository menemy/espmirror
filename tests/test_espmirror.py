# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0


"""Firmware frame coding decoded by the production viewer, without Qt or IDF."""

import importlib.util
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:
    import numpy as np
except ImportError:
    if __name__ == '__main__':
        print('SKIP: numpy is needed for mirror tests')
        sys.exit(0)
    raise unittest.SkipTest("numpy is needed for mirror fixtures and decoder tests")

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mirror = load('espmirror')


@unittest.skipUnless(os.name == 'posix', 'C harnesses require a POSIX sanitizer toolchain')
class MirrorCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.exe = Path(cls.tmp.name) / 'mirror-core'
        command = shlex.split(os.environ.get('CC', 'cc')) + [
            '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
            '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
            '-I' + str(ROOT / 'src'),
            str(ROOT / 'src/espmirror_core.c'),
            str(ROOT / 'tests/espmirror_harness.c'), '-o', str(cls.exe)]
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, 'TMPDIR': cls.tmp.name})
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @staticmethod
    def update(x, y, pixels):
        h, w = pixels.shape
        return b'U' + struct.pack('<4H', x, y, w, h) + pixels.astype('<u2').tobytes()

    def code(self, source, updates=None, cap=8192, flags=0):
        h, w = source.shape
        commands = self.update(0, 0, source) if updates is None else updates
        data = struct.pack('<HHI', w, h, cap) + bytes([ord('C'), flags]) + commands + b'SD\x02B\x00'
        result = subprocess.run([self.exe], input=data, capture_output=True, timeout=20,
                                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0:abort_on_error=1',
                                     'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1'})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        fb = np.zeros_like(source)
        frames, codings, off = [], set(), 0
        while off < len(result.stdout):
            n, = struct.unpack_from('<I', result.stdout, off)
            msg = result.stdout[off + 4:off + 4 + n]
            self.assertEqual(len(msg), n)
            self.assertLessEqual(n, cap)
            state = mirror.apply_frame(fb, msg)
            self.assertIn(state, (0, 2))
            end = 8
            count, = struct.unpack_from('<H', msg, 2)
            for _ in range(count):
                codings.add(msg[end + 8])
                payload, = struct.unpack_from('<I', msg, end + 10)
                end += 14 + payload
            self.assertEqual(end, n)
            frames.append(msg)
            off += 4 + n
        self.assertEqual(frames[-1], struct.pack('<BBHHH', ord('F'), 0, 0, w, h))
        np.testing.assert_array_equal(fb, source)
        return frames[:-1], codings

    def test_import_needs_numpy_only(self):
        subprocess.run([sys.executable, '-c',
                        'import sys; sys.path.insert(0, sys.argv[1]); import espmirror; '
                        'assert not any(n.startswith(("PySide6", "zeroconf")) for n in sys.modules)',
                        str(ROOT / 'tools')], check=True)

    def test_solid_and_horizontal_bars(self):
        for source in (np.full((128, 200), 0xf81f, dtype=np.uint16),
                       np.repeat(np.arange(128, dtype=np.uint16)[:, None], 200, axis=1)):
            frames, codings = self.code(source)
            self.assertEqual(len(frames), 1)
            self.assertEqual(codings, {1})

    def test_vertical_bars(self):
        source = np.tile(np.arange(200, dtype=np.uint16), (128, 1))
        frames, codings = self.code(source)
        self.assertEqual(len(frames), 1)
        self.assertEqual(codings, {2})

    def test_noise_larger_than_message(self):
        rng = np.random.default_rng(37)
        for w, h in ((466, 466), (172, 640), (412, 412), (135, 240), (320, 480), (800, 480)):
            for cap in (8192, 32768, 65536):
                with self.subTest(w=w, h=h, cap=cap):
                    source = rng.integers(0, 65536, (h, w), dtype=np.uint16)
                    frames, codings = self.code(source, cap=cap)
                    self.assertEqual(len(frames) > 1, source.nbytes + 22 > cap)
                    self.assertEqual(codings, {0})

    def test_random_screens(self):
        rng = np.random.default_rng(9)
        for _ in range(30):
            w, h = rng.integers(1, 300, 2)
            source = rng.integers(0, 65536, (h, w), dtype=np.uint16)
            self.code(source)

    def test_dirty_overflow_overlap_and_clipping(self):
        rng = np.random.default_rng(83)
        source = np.zeros((250, 250), dtype=np.uint16)
        commands = bytearray()
        for y in range(0, 250, 50):
            for x in range(0, 250, 50):
                pixels = rng.integers(0, 65536, (12, 13), dtype=np.uint16)
                source[y:y + 12, x:x + 13] = pixels
                commands += self.update(x, y, pixels)
        pixels = np.full((100, 110), 0x07e0, dtype=np.uint16)
        source[7:107, 9:119] = pixels
        commands += self.update(9, 7, pixels)
        self.code(source, commands)
        self.code(source, commands + b'A')

    def test_tail_with_continuous_updates(self):
        rng = np.random.default_rng(42)
        source = rng.integers(0, 65536, (466, 466), dtype=np.uint16)
        commands = self.update(0, 0, source)
        for _ in range(70):
            commands += b'B\x00'
            pixels = rng.integers(0, 65536, (10, 466), dtype=np.uint16)
            source[:10] = pixels
            commands += self.update(0, 0, pixels)
        frames, _ = self.code(source, commands)
        # The initial bottom must arrive despite new dirt at the top every frame.
        bottom_frames = []
        for i, msg in enumerate(frames):
            off = 8
            for _ in range(struct.unpack_from('<H', msg, 2)[0]):
                _x, y, _w, h, _enc, _pad, n = struct.unpack_from('<HHHHBBI', msg, off)
                if y + h == 466:
                    bottom_frames.append(i)
                off += 14 + n
        self.assertLess(bottom_frames[0], 60)

    def test_single_pixel_and_one_row_capacity(self):
        self.code(np.array([[0xffff]], dtype=np.uint16), cap=64)
        self.code(np.arange(300, dtype=np.uint16).reshape(3, 100), cap=222)


    def test_optional_codecs_and_delta_chain(self):
        rng = np.random.default_rng(105)
        source = rng.integers(0, 65536, (71, 97), dtype=np.uint16)
        commands = self.update(0, 0, source) + b'D\x00'
        for _ in range(12):
            source ^= np.uint16(0x8421)
            commands += self.update(0, 0, source) + b'D\x00'
        frames, codings = self.code(source, commands, flags=3, cap=32768)
        self.assertEqual(codings, {0, 3})
        decoded = np.zeros_like(source)
        for i, msg in enumerate(frames):
            mirror.apply_frame(decoded, msg)
            expected = source if i % 2 == 0 else source ^ np.uint16(0x8421)
            np.testing.assert_array_equal(decoded, expected)

    def test_all_codings_in_one_message(self):
        rng = np.random.default_rng(47)
        source = rng.integers(0, 65536, (32, 170), dtype=np.uint16)
        commands = self.update(0, 0, source) + b'D\x00'
        choices = [rng.integers(0, 65536, (24, 24), dtype=np.uint16),
                   np.repeat(np.arange(24, dtype=np.uint16)[:, None], 24, axis=1),
                   np.tile(np.arange(24, dtype=np.uint16), (24, 1)),
                   source[3:27, 105:129] ^ np.uint16(0xffff),
                   rng.choice(np.array([0xf800, 0x07e0, 0x001f], np.uint16), (24, 24))]
        for i, pixels in enumerate(choices):
            x = 3 + i * 34
            source[3:27, x:x + 24] = pixels
            commands += self.update(x, 3, pixels)
        frames, _ = self.code(source, commands, cap=32768, flags=3)
        msg, off, encs = frames[-1], 8, []
        for _ in range(struct.unpack_from('<H', msg, 2)[0]):
            encs.append(msg[off + 8])
            off += 14 + struct.unpack_from('<I', msg, off + 10)[0]
        self.assertEqual(encs, list(range(5)))

    def test_extended_random_rectangles_and_tails(self):
        rng = np.random.default_rng(512)
        for flags in (1, 2, 3):
            source = np.zeros((81, 101), np.uint16)
            commands = bytearray()
            for i in range(45):
                x, y = int(rng.integers(0, 90)), int(rng.integers(0, 70))
                w, h = int(rng.integers(1, 102 - x)), int(rng.integers(1, 82 - y))
                pixels = rng.integers(0, 65536, (h, w), dtype=np.uint16)
                if i % 3 == 0:
                    pixels &= 0x0821
                source[y:y + h, x:x + w] = pixels
                commands += self.update(x, y, pixels)
                commands += b'B\x00'
            self.code(source, commands, flags=flags, cap=256)

    def test_qoi_palette_and_adversarial_pixels(self):
        rng = np.random.default_rng(106)
        for shape in ((1, 1), (1, 200), (200, 1), (256, 256)):
            source = rng.choice(np.array([0, 1, 0x07e0, 0xf800, 0xffff], np.uint16), shape)
            _, encs = self.code(source, flags=2)
            if min(shape) > 1:
                self.assertIn(4, encs)
        self.code(np.arange(65536, dtype=np.uint16).reshape(256, 256), flags=3)
        # Run counters cross 65535; QOI counters cross 62, including initial black.
        for colour in (0, 0xffff):
            self.code(np.full((256, 256), colour, np.uint16), flags=3, cap=200000)
        source = np.zeros((3, 103), np.uint16)
        source[:, 62::63] = 0xffff
        self.code(source, flags=2)

    def test_last_candidate_small_message_remainders(self):
        rng = np.random.default_rng(740)
        source = np.zeros((24, 24), np.uint16)
        commands = bytearray()
        for x in (0, 9, 18):
            pixels = rng.choice(np.array([0, 0xf800, 0x07e0, 0x001f], np.uint16), (24, 6))
            source[:, x:x + 6] = pixels
            commands += self.update(x, 0, pixels)
        for flags in (0, 1, 2, 3):
            for capacity in range(70, 129):
                with self.subTest(flags=flags, capacity=capacity):
                    self.code(source, commands, flags=flags, cap=capacity)

    def test_delta_reset_for_new_viewer(self):
        source = np.arange(300, dtype=np.uint16).reshape(3, 100)
        commands = self.update(0, 0, source) + b'D\x00R' + self.update(0, 0, source)
        frames, _ = self.code(source, commands, flags=1)
        fresh = np.zeros_like(source)
        mirror.apply_frame(fresh, frames[-1])
        np.testing.assert_array_equal(fresh, source)


class DecoderTests(unittest.TestCase):
    @staticmethod
    def frame(enc, payload, w=3, h=2):
        return (struct.pack('<BBHHH', ord('F'), 0, 1, w, h)
                + struct.pack('<4HBBI', 0, 0, w, h, enc, 0, len(payload)) + payload)

    def test_qoi_spec_operations(self):
        # RGB, RUN, INDEX, DIFF, LUMA, RGBA; arithmetic wraps modulo 256.
        data = bytes([0xfe, 8, 16, 24, 0xc0, (8*3 + 16*5 + 24*7 + 255*11) % 64,
                      0x7f, 0x80 | 35, 0x97, 0xff, 255, 0, 255, 255])
        rgb = np.array([(8, 16, 24)] * 3 + [(9, 17, 25), (13, 20, 27), (255, 0, 255)], np.uint16)
        expected = (rgb[:, 0] >> 3) << 11 | (rgb[:, 1] >> 2) << 5 | rgb[:, 2] >> 3
        np.testing.assert_array_equal(mirror.decode_qoi(data, 6), expected)
        np.testing.assert_array_equal(mirror.decode_qoi(bytes([0x40]), 1), [0xffff])

    def test_malformed_frames(self):
        good = self.frame(0, bytes(12))
        bad = [good[:n] for n in range(len(good))]
        bad += [good + b'x', b'X' + good[1:], self.frame(5, bytes(12)),
                self.frame(0, bytes(10)), self.frame(1, b'\x00'),
                self.frame(2, struct.pack('<HH', 0, 0)),
                self.frame(3, struct.pack('<HH', 65535, 0)),
                self.frame(3, struct.pack('<HH', 5, 0)),
                self.frame(4, b''), self.frame(4, b'\xfe\x00'),
                self.frame(4, b'\xff\x00\x00\x00'), self.frame(4, b'\x80'),
                self.frame(4, b'\xc6'), self.frame(4, b'\xc4'),
                self.frame(4, b'\xc5\xc0'), self.frame(4, b'\x00\xc4'),
                self.frame(4, b'\xff\x00\x00\x00\x00\xc4')]
        for offset, value in ((8, 3), (12, 0), (14, 0), (17, 1)):
            corrupt = bytearray(good)
            corrupt[offset] = value
            bad.append(corrupt)
        for msg in bad:
            with self.subTest(msg=msg), self.assertRaises(ValueError):
                mirror.apply_frame(np.zeros((2, 3), np.uint16), msg)

    def test_delta_overlap_and_absolute_reference(self):
        fb = np.zeros((2, 3), np.uint16)
        absolute = self.frame(0, np.arange(6, dtype='<u2').tobytes())
        delta = self.frame(3, struct.pack('<HH', 6, 0x8421))
        mixed = bytearray(absolute + delta[8:] + delta[8:])
        struct.pack_into('<H', mixed, 2, 3)
        mirror.apply_frame(fb, mixed)
        np.testing.assert_array_equal(fb, np.arange(6).reshape(2, 3))


fake = load('fake_board')


class FakeBoardTests(unittest.TestCase):
    def test_presets_and_pattern_roundtrip(self):
        for info in fake.PRESETS.values():
            with self.subTest(board=info['name']):
                pattern = fake.Pattern(info)
                source = pattern.draw(3.5)
                self.assertFalse(np.array_equal(source, pattern.draw(4.5)))
                decoded = np.zeros_like(source)
                for msg in fake.frames(source):
                    self.assertLessEqual(len(msg), 32768)
                    mirror.apply_frame(decoded, msg)
                np.testing.assert_array_equal(decoded, source)
                pattern.input(bytes([6, 1, 1]), 3.5)
                self.assertEqual(pattern.held, {1})
                self.assertFalse(np.array_equal(source, pattern.draw(3.5)))
                pattern.input(bytes([6, 1, 0]), 3.5)
                pattern.input(struct.pack('<BHH', 1, 10, 20), 3.5)
                if info['touch']:
                    self.assertEqual(pattern.pointer, (10, 20))
                    self.assertEqual(int(pattern.draw(3.5)[20, 10]), 0xffff)
                    pattern.input(struct.pack('<BHH', 0, 10, 20), 3.5)
                    self.assertIsNone(pattern.pointer)
                    self.assertEqual(int(pattern.draw(3.5)[20, 10]), 0xf81f)
                else:
                    self.assertIsNone(pattern.pointer)
                pattern.release()
                self.assertFalse(pattern.held)
                self.assertFalse(pattern.trail)

    def start_server(self):
        server = fake.FakeBoard(fake.PRESETS['waveshare-s3-175c'], port=0, fps=20)
        stop = threading.Event()
        failures = []

        def run():
            try:
                while not stop.is_set():
                    server.step(.005)
            except Exception as e:
                failures.append(e)
            finally:
                server.close()

        thread = threading.Thread(target=run)
        thread.start()

        def cleanup():
            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures, failures)

        self.addCleanup(cleanup)
        return server

    def connect(self, server, hello=True):
        sock = socket.create_connection(('127.0.0.1', server.port), timeout=3)
        self.addCleanup(sock.close)
        if hello:
            sock.sendall(fake.HELLO)
        return sock

    def recv_exact(self, sock, n):
        result = bytearray()
        while len(result) < n:
            data = sock.recv(n - len(result))
            self.assertTrue(data, 'unexpected EOF')
            result += data
        return bytes(result)

    def message(self, sock):
        n, = struct.unpack('<I', self.recv_exact(sock, 4))
        self.assertLessEqual(n, 1 << 20)
        return self.recv_exact(sock, n)

    def send(self, sock, msg):
        sock.sendall(struct.pack('<I', len(msg)) + msg)

    def eventually(self, predicate):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.005)
        self.fail('server did not process input before deadline')

    def test_handshake_frames_input_takeover_disconnect(self):
        import json
        server = self.start_server()
        first = self.connect(server, hello=False)
        first.sendall(fake.HELLO[:3])
        time.sleep(.02)
        first.sendall(fake.HELLO[3:])
        msg = self.message(first)
        self.assertEqual(msg[:1], b'B')
        self.assertEqual(json.loads(msg[1:]), server.info)
        fb = np.zeros((server.info['h'], server.info['w']), dtype=np.uint16)
        bottom = 0
        while bottom < fb.shape[0]:
            msg = self.message(first)
            self.assertEqual(msg[:1], b'F')
            mirror.apply_frame(fb, msg)
            _x, y, _w, h = struct.unpack_from('<4H', msg, 8)
            bottom = y + h
        self.assertTrue(np.any(fb[-1]))
        self.send(first, bytes([6, 2, 1]))
        self.send(first, struct.pack('<BHH', 1, 23, 45))
        self.send(first, b'\x02')
        self.eventually(lambda: server.pattern.held == {2} and server.pattern.pointer == (23, 45))
        self.send(first, bytes([6, 2, 0]))
        self.send(first, struct.pack('<BHH', 0, 23, 45))
        self.eventually(lambda: not server.pattern.held and server.pattern.pointer is None)
        self.send(first, bytes([6, 3, 1]))
        self.eventually(lambda: server.pattern.held == {3})
        second = self.connect(server)
        self.assertEqual(self.message(second)[:1], b'B')
        self.eventually(lambda: not server.pattern.held)
        while self.message(first)[:1] != b'T':
            pass
        self.assertEqual(first.recv(1), b'')
        self.send(second, bytes([6, 1, 1]))
        self.eventually(lambda: server.pattern.held == {1})
        second.close()
        self.eventually(lambda: server.active is None and not server.pattern.held)

    def test_tcp_audio_led_tap_and_refresh(self):
        server = self.start_server()
        sock = self.connect(server)
        self.assertEqual(self.message(sock)[:1], b'B')
        self.send(sock, b'\x07\x02')
        self.send(sock, b'\x08\x01')
        self.send(sock, b'S\x01' + bytes(640))
        self.send(sock, b'\x03\x02')
        self.eventually(lambda: server.pattern.talk_samples == 320 and 2 in server.pattern.taps)
        kinds = set()
        for _ in range(200):
            msg = self.message(sock)
            kinds.add(msg[:1])
            if msg[:1] == b'A':
                self.assertEqual(msg[1], 2)
                self.assertLessEqual(len(msg), 1282)
                self.assertTrue(any(msg[2:]))
            if {b'L', b'A'} <= kinds:
                break
        self.assertTrue({b'L', b'A'} <= kinds)
        self.send(sock, b'R')
        while self.message(sock)[:1] != b'B':
            pass
        self.send(sock, b'\x07\x00')
        self.send(sock, b'\x08\x00')
        self.eventually(lambda: not server.pattern.listen and not server.pattern.talk)

    def test_bad_candidate_preserves_viewer_and_oversize_input_disconnects(self):
        server = self.start_server()
        first = self.connect(server)
        self.assertEqual(self.message(first)[:1], b'B')
        candidate = self.connect(server, hello=False)
        candidate.sendall(b'NOTMUSE')
        self.assertEqual(candidate.recv(1), b'')
        self.send(first, bytes([6, 4, 1]))
        self.eventually(lambda: server.pattern.held == {4})
        first.sendall(struct.pack('<I', 1025))
        self.eventually(lambda: server.active is None and not server.pattern.held)


class CableFramingTests(unittest.TestCase):
    def test_crc_chunks_logs_and_embedded_sync(self):
        logs, errors, got = bytearray(), [], []
        demux = mirror.CableDemux(logs.extend, lambda: errors.append(True))
        messages = [b'B{}', b'F' + mirror.cable_frame(b'E') + bytes(range(256)), b'E']
        wire = b'boot\r\n' + b''.join(mirror.cable_frame(m) + b'log line\n' for m in messages)
        for byte in wire:
            got.extend(demux.feed(bytes([byte])))
        self.assertEqual(got, messages)
        self.assertEqual(logs, b'boot\r\n' + b'log line\n' * 3)
        self.assertFalse(errors)
        self.assertEqual(mirror.cable_frame(b'R').hex(), 'a55a010000005275ee277e')

    def test_corrupt_frame_and_bad_length_recover(self):
        logs, errors = bytearray(), []
        demux = mirror.CableDemux(logs.extend, lambda: errors.append(True))
        broken = mirror.cable_frame(b'F' + b'\x80' * 40)
        broken = broken[:20] + b'\nROM interrupted\n' + broken[20:]
        got = []
        for start in range(0, len(broken), 3):
            got.extend(demux.feed(broken[start:start + 3]))
        got.extend(demux.feed(mirror.cable_frame(b'E')))
        self.assertEqual(got, [b'E'])
        self.assertIn(b'ROM interrupted\n', logs)
        self.assertEqual(len(errors), 1)
        self.assertEqual(demux.feed(mirror.SYNC + b'\xff' * 4 + mirror.cable_frame(b'T')), [b'T'])
        self.assertEqual(len(errors), 2)

    def test_incomplete_header_and_frame_expire(self):
        for prefix in (mirror.SYNC, mirror.SYNC + struct.pack('<I', 60000) + b'F'):
            errors = []
            demux = mirror.CableDemux(lambda data: None, lambda: errors.append(True))
            self.assertEqual(demux.feed(prefix, now=10), [])
            self.assertEqual(demux.feed(mirror.cable_frame(b'E'), now=14), [b'E'])
            self.assertEqual(len(errors), 1)
            self.assertLess(len(demux.buffer), 2)

    @unittest.skipUnless(os.name == 'posix', 'C harness uses pthreads and sanitizers')
    def test_firmware_serial_send_and_log_lock(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        send = source[source.index('#define SERIAL_CHUNK'):source.index('static bool viewer_send', source.index('#define SERIAL_CHUNK'))]
        log = source[source.index('static char s_log_line'):source.index('static bool serial_enqueue')]
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'serial_impl.inc').write_text(send + log)
            exe = folder / 'serial-harness'
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                '-fsanitize=address,undefined', '-I' + str(folder),
                str(ROOT / 'tests/espmirror_serial_harness.c'), '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'TMPDIR': tempfile.gettempdir()})
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            logs, errors = bytearray(), []
            demux = mirror.CableDemux(logs.extend, lambda: errors.append(True))
            messages = demux.feed(result.stdout)
            self.assertEqual(messages, [bytes(i % 256 for i in range(3000))] * 100)
            self.assertFalse(errors)
            self.assertEqual(len(logs.splitlines()), 100)
            for i, line in enumerate(logs.splitlines()):
                self.assertEqual(line.decode(), 'log %d %0600d' % (i, i))

    @unittest.skipUnless(os.name == 'posix', 'C harness requires a POSIX sanitizer toolchain')
    def test_firmware_session_pump(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        impl = source[source.index('static void serial_end(void)\n'):source.index('static char s_log_line')]
        start = source.index('static bool serial_enqueue')
        impl += source[start:source.index('static void viewer_drop', start)]
        declarations = source[source.index('typedef struct { uint16_t len; uint8_t b[IN_MAX]; } serial_msg_t;'):source.index('static QueueHandle_t s_serial_in;')]
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'session_impl.inc').write_text(impl)
            (folder / 'session_types.inc').write_text(declarations)
            exe = folder / 'session-harness'
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-I' + str(folder), str(ROOT / 'tests/espmirror_session_harness.c'), '-o', str(exe)]
            for audio in (0, 1):
                with self.subTest(audio=audio):
                    result = subprocess.run(command + [f'-DCONFIG_ESPMIRROR_AUDIO={audio}'], capture_output=True, text=True, env={**os.environ, 'TMPDIR': tempfile.gettempdir()})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = subprocess.run([exe], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr.decode())

    @unittest.skipUnless(os.name == 'posix', 'C harness requires a POSIX sanitizer toolchain')
    def test_sound_routing(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        state = source[source.index('#define MIC_RING'):source.index('#endif', source.index('#define MIC_RING'))]
        impl = source[source.index('void espmirror_audio_output'):
                      source.index('/* Mirror task: the viewer listens to a source, or stops. */')]
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'route_impl.inc').write_text(state + impl)
            exe = folder / 'route-harness'
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-I' + str(folder), str(ROOT / 'tests/espmirror_route_harness.c'), '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True,
                                    env={**os.environ, 'TMPDIR': tempfile.gettempdir()})
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_windows_port_names(self):
        for port in ('COM1', 'com12', r'\\.\COM32'):
            self.assertTrue(mirror.is_port(port), port)
        self.assertFalse(mirror.is_port('example:7331'))

    def test_log_lines_stdout_and_file(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'console.log'
            output = io.StringIO()
            logger = mirror.ConsoleLog('COM12', path)
            with contextlib.redirect_stdout(output):
                logger.feed(b'hel')
                logger.feed(b'lo\r\nnext')
                logger.flush()
            self.assertEqual(output.getvalue(), path.read_text())
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertRegex(lines[0], r'^\[\d{4}-\d\d-\d\d .* COM12\] hello$')
            self.assertTrue(lines[1].endswith('COM12] next'))


@unittest.skipUnless(os.name == 'posix', 'PTY tests require POSIX')
class CableLinkTests(unittest.TestCase):
    def setUp(self):
        import contextlib
        import io
        import mirror_serial_fake
        from unittest.mock import patch
        self.patch = patch.dict(sys.modules, serial=mirror_serial_fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def start_fake(self, **kw):
        server = fake.FakeCable(fake.PRESETS['m5stack-sticks3'], fps=20, **kw)
        stop, failures = threading.Event(), []

        def run():
            try:
                while not stop.is_set():
                    server.step(.002)
            except Exception as e:
                failures.append(e)

        thread = threading.Thread(target=run)
        thread.start()

        def cleanup():
            stop.set()
            thread.join(2)
            server.close()
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures, failures)

        self.addCleanup(cleanup)
        return server

    def eventually(self, condition, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if condition():
                return
            time.sleep(.01)
        self.fail('cable condition timed out: ' + self.output.getvalue()[-500:])

    def start_link(self, server, **kw):
        link = mirror.Link(server.port, **kw).start()

        def cleanup():
            link.close()
            link.thread.join(3)
            self.assertFalse(link.thread.is_alive())

        self.addCleanup(cleanup)
        return link

    def test_link_logs_refresh_and_screen_recovery(self):
        server = self.start_fake(corrupt_every=5)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cable.log'
            link = self.start_link(server, log_file=path)
            self.eventually(lambda: server.broken > 0 and server.refreshes > 0
                            and link.seen is not None and link.seen.all())
            server.corrupt_every = 0
            self.eventually(lambda: 'ROM injected frame' in self.output.getvalue())
            self.assertGreater(link.changes, 0)
            self.assertIn('console: waiting for >mirror', self.output.getvalue())
            self.assertIn('frame sent', self.output.getvalue())
            self.assertIn(server.port + '] ', self.output.getvalue())
            self.assertEqual(self.output.getvalue(), path.read_text())
            link.close()
            link.thread.join(3)

    def test_cable_audio_and_controls(self):
        server = self.start_fake(corrupt_every=0)
        link = self.start_link(server)
        self.eventually(lambda: link.live and link.audio_capable)
        link.send(b'\x07\x01')
        self.eventually(lambda: not link.audio.empty())
        source, pcm = link.audio.get_nowait()
        self.assertEqual(source, 1)
        self.assertTrue(any(pcm))
        link.send(b'\x08\x01')
        link.send(b'S\x01' + bytes(640))
        link.tap(2)
        self.eventually(lambda: server.pattern.talk_samples == 320 and 2 in server.pattern.taps)
        link.close()
        link.thread.join(3)
        self.eventually(lambda: not server.pattern.listen and not server.pattern.talk)

    def test_typing_into_the_board_field(self):
        server = self.start_fake(corrupt_every=0)
        link = self.start_link(server)
        self.eventually(lambda: link.live and link.text_open)
        self.assertTrue(server.pattern.typer)
        self.assertTrue(mirror.route_text_key(link, None, 'h\u00e9llo'))
        self.assertTrue(mirror.route_text_key(link, 'Backspace'))
        self.eventually(lambda: server.pattern.field == 'h\u00e9ll')
        link.close()
        link.thread.join(3)
        self.assertFalse(link.text_open)

    def test_end_reconnect_and_no_input_becomes_console_commands(self):
        server = self.start_fake(corrupt_every=0)
        link = self.start_link(server)
        self.eventually(lambda: link.live)
        link.pointer(122, 40, True)
        server.end()
        self.eventually(lambda: not server.active)
        for _ in range(20):
            link.pointer(122, 40, False)
        self.eventually(lambda: server.sessions >= 2 and link.live, timeout=6)
        link.close()
        link.thread.join(3)
        self.eventually(lambda: not server.active and not server.drain_until)
        self.assertFalse(server.console_bytes, server.console_bytes)
        self.assertGreater(server.breaks, 0)

    def test_board_without_mirror_never_gets_binary_input(self):
        server = self.start_fake(enabled=False)
        with self.assertRaisesRegex(ConnectionError, 'No mirror reply'):
            mirror.open_serial(server.port, wait=.6)
        self.assertEqual(server.sessions, 0)
        self.assertFalse(server.console_bytes)

    def test_quiet_end_and_break_drain(self):
        server = self.start_fake(corrupt_every=0)
        ser = mirror.open_serial(server.port)
        self.addCleanup(ser.close)
        messages = []
        demux = mirror.CableDemux(lambda data: None, lambda: None)
        start = time.monotonic()
        while b'E' not in messages and time.monotonic() - start < 8:
            messages += demux.feed(ser.read(max(1, min(ser.in_waiting, 8192))))
        self.assertIn(b'E', messages)
        self.assertGreater(time.monotonic() - start, 5.5)
        time.sleep(.3)
        ser.write(b'\n>mirror\n')
        self.eventually(lambda: server.sessions == 2)
        packet = struct.pack('<I', 5) + struct.pack('<BHH', 1, 122, 40)
        ser.write(mirror.BREAK + packet)
        self.eventually(lambda: server.breaks > 0)
        ser.write(packet)
        self.eventually(lambda: not server.drain_until)
        self.assertIn(packet, server.drained)
        self.assertFalse(server.console_bytes, server.console_bytes)


class MirrorDefaultsTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from espmirror_settings import DEFAULTS
        prefs = patch('espmirror_settings.read_preferences', return_value=DEFAULTS.copy())
        prefs.start()
        self.addCleanup(prefs.stop)

    def test_viewer_default_wifi_opt_in_and_explicit_address(self):
        from unittest.mock import patch
        usb = [('/dev/test-muse', 'aa')]
        net = [('same', '192.0.2.1', 7331, 'aa'), ('other', '192.0.2.2', 7442, 'bb')]
        cases = [([], [('/dev/test-muse', 7331)], False, True),
                 (['--wifi'], [('/dev/test-muse', 7331), ('192.0.2.2', 7442)], True, True),
                 (['192.0.2.9:7444'], [('192.0.2.9', 7444)], False, False)]
        for args, expected, wifi, probing in cases:
            with self.subTest(args=args), patch.object(sys, 'argv', ['mirror.py'] + args), \
                    patch.object(mirror, 'usb_boards', return_value=usb) as u, \
                    patch.object(mirror, 'discover', return_value=net) as n, \
                    patch.object(mirror.importlib.util, 'find_spec', return_value=True), \
                    patch.object(mirror, 'run_window', return_value=0) as window:
                self.assertEqual(mirror.main(), 0)
                self.assertEqual(window.call_args.args[0], expected)
                self.assertEqual(u.called, probing)
                self.assertEqual(n.called, wifi)

    def test_list_obeys_wifi_opt_in(self):
        import contextlib
        import io
        from unittest.mock import patch
        for wifi in (False, True):
            output = io.StringIO()
            with patch.object(sys, 'argv', ['mirror.py', '--list'] + (['--wifi'] if wifi else [])), \
                    patch.object(mirror, 'usb_boards', return_value=[('/dev/test', 'aa')]), \
                    patch.object(mirror, 'discover', return_value=[('remote', '192.0.2.1', 7331, 'bb')]) as net, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(mirror.main(), 0)
            self.assertEqual(net.called, wifi)
            self.assertEqual('remote' in output.getvalue(), wifi)
            self.assertIn('/dev/test', output.getvalue())



class SerialTests(unittest.TestCase):
    class Serial:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.is_open = False
            self.events = []
            self._dtr = self._rts = True
            self.port = kwargs['port']
            self.timeout = kwargs['timeout']
            self.reads = bytearray(b'boot log\n')
            self.error = None
            self.platform = 'linux'
            self.driver_states = []

        @property
        def dtr(self):
            return self._dtr

        @dtr.setter
        def dtr(self, value):
            self._dtr = value
            self.events.append(('dtr', value, self.is_open))
            if self.is_open:
                self.driver_states.append((value, self._rts))

        @property
        def rts(self):
            return self._rts

        @rts.setter
        def rts(self, value):
            self._rts = value
            self.events.append(('rts', value, self.is_open))
            if self.is_open:
                self.driver_states.append((self._dtr, value))

        def open(self):
            self.events.append(('open', self.dtr, self.rts))
            if self.error:
                raise self.error
            self.is_open = True
            # POSIX DTR first, with an inherited asserted RTS; Windows DCB together.
            if self.platform != 'win32':
                self.driver_states.append((self.dtr, True))
            self.driver_states.append((self.dtr, self.rts))

        def close(self):
            self.events.append(('close',))
            self.is_open = False

        def write(self, data):
            self.events.append(('write', data))
            if data == b'\n>mirror\n':
                self.reads += mirror.MARK
            return len(data)

        @property
        def in_waiting(self):
            return len(self.reads)

        def read(self, size):
            result = bytes(self.reads[:size])
            del self.reads[:size]
            return result

        def reset_input_buffer(self):
            self.events.append(('reset_input_buffer',))

    def install(self, platform='linux', error=None):
        from types import SimpleNamespace
        from unittest.mock import patch
        instances = []

        def create(**options):
            ser = self.Serial(**options)
            ser.platform, ser.error = platform, error
            instances.append(ser)
            return ser

        self.enterContext(patch.dict(sys.modules, {'serial': SimpleNamespace(Serial=create)}))
        self.enterContext(patch.object(mirror.sys, 'platform', platform))
        self.enterContext(patch.object(mirror.time, 'sleep'))
        return instances

    def test_open_line_order_and_existing_handshake(self):
        for platform, port in (('linux', '/dev/ttyACM0'), ('darwin', '/dev/cu.usbmodem101'), ('win32', 'COM12')):
            with self.subTest(platform=platform):
                instances = self.install(platform)
                ser = mirror.open_serial(port)
                self.assertIs(ser, instances[0])
                self.assertIsNone(ser.options['port'])
                self.assertTrue(ser.options['exclusive'])
                for name in ('rtscts', 'dsrdtr', 'xonxoff'):
                    self.assertFalse(ser.options[name])
                self.assertEqual(ser.events[:3], [('dtr', True, False), ('rts', False, False), ('open', True, False)])
                self.assertNotIn((False, True), ser.driver_states)
                self.assertEqual(ser.driver_states[-1], (False, False))
                self.assertEqual([e for e in ser.events if e[0] == 'write'],
                                 [('write', mirror.BREAK), ('write', b'\n>mirror\n')])
                self.assertEqual(ser.timeout, .1)
                ser.close()

    def test_actionable_open_errors(self):
        import errno
        for platform, error, expected in (
                ('linux', PermissionError(errno.EACCES, 'denied'), 'dialout'),
                ('linux', PermissionError(errno.EPERM, 'denied'), 'uucp'),
                ('linux', OSError(errno.EBUSY, 'busy'), 'idf.py monitor'),
                ('linux', OSError(errno.EAGAIN, 'Could not exclusively lock port'), 'another viewer'),
                ('linux', FileNotFoundError(errno.ENOENT, 'gone'), 'Reconnect'),
                ('win32', OSError("could not open port 'COM12': PermissionError(13, 'Access is denied.')"), 'idf.py monitor')):
            with self.subTest(platform=platform, error=error):
                instances = self.install(platform, error)
                with self.assertRaisesRegex(ConnectionError, expected):
                    mirror.open_usb_serial('COM12' if platform == 'win32' else '/dev/ttyACM0')
                self.assertEqual(instances[0].events[-1], ('close',))

    def test_missing_marker_closes_port(self):
        from unittest.mock import patch
        instances = self.install()
        with patch.object(mirror.time, 'monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(ConnectionError, 'CONFIG_ESPMIRROR_USB=y'):
                mirror.open_serial('/dev/ttyACM0', wait=5)
        self.assertFalse(instances[0].is_open)

    def test_missing_pyserial(self):
        from unittest.mock import patch
        with patch.dict(sys.modules, {'serial': None}):
            with self.assertRaisesRegex(ConnectionError, 'pip install pyserial'):
                mirror.open_usb_serial('COM12')

    def test_mac_normalization(self):
        for serial in ('aabbccddeeff', 'AA:BB:CC:DD:EE:FF', 'AA_BB_CC_DD_EE_FF', 'AA-BB-CC-DD-EE-FF'):
            self.assertEqual(mirror.usb_serial_mac(serial), 'aa:bb:cc:dd:ee:ff')
        for serial in (None, '', '6&ABCDEF&0&0000', 'AABBCCDDEEFF&0', 'USB\\VID_303A&PID_1001', 'AA:BB_CC:DD:EE:FF'):
            self.assertEqual(mirror.usb_serial_mac(serial), '')

    def test_list_ports_on_all_platforms(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        data = [SimpleNamespace(device=d, serial_number=s, vid=vid, pid=pid)
                for d,s,vid,pid in [('COM12', 'AABBCCDDEEFF', 0x303a, 0x1001),
                    ('/dev/ttyACM0', None, 0x303a, 0x1001),
                    ('/dev/cu.usbmodem1', 'aa:bb:cc:dd:ee:01', 0x303a, 0x1001),
                    ('COM13', None, 0x1234, 0x1001)]]
        data += [SimpleNamespace(device='/dev/cu.usbmodem2', serial_number='1020BA050328', vid=0x303a, pid=0x4001,
                                 product='espmirror', interface=None),
                 SimpleNamespace(device='/dev/cu.iris', serial_number='1020BA050330', vid=0x303a, pid=0x4002,
                                 product='ESP-Iris espmirror', interface='ESP-Iris data'),
                 SimpleNamespace(device='/dev/cu.usbmodem3', serial_number='1020BA050329', vid=0x303a, pid=0x4001,
                                 product='Espressif Device', interface=None),
                 # pyserial on Windows fills neither product nor interface.
                 SimpleNamespace(device='COM14', serial_number='1020BA05032A', vid=0x303a, pid=0x4001,
                                 product=None, interface=None),
                 SimpleNamespace(device='COM15', serial_number='1020BA05032B', vid=0x303a, pid=0x0002,
                                 product=None, interface=None)]
        listing = SimpleNamespace(comports=lambda: data)
        with patch.dict(sys.modules, {'serial.tools': SimpleNamespace(list_ports=listing)}), \
                patch.object(mirror, 'open_serial') as probe:
            self.assertEqual(mirror.usb_boards(), [('COM12', 'aa:bb:cc:dd:ee:ff'),
                ('/dev/ttyACM0', ''), ('/dev/cu.usbmodem1', 'aa:bb:cc:dd:ee:01'),
                ('/dev/cu.usbmodem2', '10:20:ba:05:03:28'), ('COM14', '10:20:ba:05:03:2a')])
            probe.assert_not_called()

    def test_hupcl_cleared_before_handshake_and_close(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        instances = self.install()
        attrs = [11, 22, 0x471, 44, 55, 66, []]
        termios = SimpleNamespace(HUPCL=0x400, TCSANOW=0, tcgetattr=Mock(return_value=attrs), tcsetattr=Mock())
        with patch.object(mirror.os, 'name', 'posix'), patch.dict(sys.modules, {'termios': termios}), \
                patch.object(self.Serial, 'fileno', lambda self: 17, create=True):
            with patch.object(mirror.time, 'monotonic', side_effect=[0, 6]):
                with self.assertRaises(ConnectionError):
                    mirror.open_serial('/dev/ttyACM0')
        termios.tcgetattr.assert_called_once_with(17)
        termios.tcsetattr.assert_called_once_with(17, 0, [11, 22, 0x71, 44, 55, 66, []])
        self.assertFalse(instances[0].is_open)

    def test_hupcl_unavailable_or_unsupported(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        for termios in (None, SimpleNamespace(tcgetattr=Mock(side_effect=OSError('unsupported')))):
            with patch.object(mirror.os, 'name', 'posix'), patch.dict(sys.modules, {'termios': termios}):
                mirror.keep_serial_lines_on_close(SimpleNamespace(fileno=lambda: 17))

    def test_hupcl_not_used_on_windows(self):
        from unittest.mock import Mock, patch
        ser = Mock()
        with patch.object(mirror.os, 'name', 'nt'):
            mirror.keep_serial_lines_on_close(ser)
        ser.fileno.assert_not_called()



class TextInputTests(unittest.TestCase):
    def test_field_routing_digits_edits_paste(self):
        link = mirror.Link('localhost')
        self.assertFalse(mirror.route_text_key(link, None, '1'))
        self.assertTrue(link.out.empty())
        link.text_open = True
        self.assertTrue(mirror.route_text_key(link, None, '1234'))
        self.assertEqual(link.out.get_nowait(), b'I1234')
        for key, code in mirror.TEXT_EDITS.items():
            self.assertTrue(mirror.route_text_key(link, key))
            self.assertEqual(link.out.get_nowait(), bytes((ord('K'), code)))
        text = 'token-\u00e9\U0001f642' * 50
        self.assertTrue(mirror.route_text_key(link, None, paste=text, modified=True))
        chunks = []
        while not link.out.empty():
            msg = link.out.get_nowait()
            self.assertLessEqual(len(msg), 64)
            chunks.append(msg[1:].decode('utf-8'))
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(mirror.route_text_key(link, 'Escape'))
        self.assertTrue(link.out.empty())
        self.assertFalse(mirror.route_text_key(link, None, 's', modified=True))
        self.assertFalse(mirror.route_text_key(link, None, '\x07'))
        link.disconnect()
        self.assertFalse(mirror.route_text_key(link, None, paste=text, modified=True))

    @unittest.skipUnless(os.name == 'posix', 'C harness requires a POSIX sanitizer toolchain')
    def test_firmware_text_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'text-harness'
            result = subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-I' + str(ROOT / 'src'), str(ROOT / 'src/espmirror_core.c'),
                str(ROOT / 'tests/espmirror_text_harness.c'), '-o', str(exe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == '__main__':
    unittest.main()

