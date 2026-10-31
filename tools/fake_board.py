#!/usr/bin/env python3
# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0


"""Fake espmirror board: python fake_board.py --board waveshare-s3-175c

Connect espmirror.py to 127.0.0.1:7331. Only numpy is required.
Use --host 0.0.0.0 for viewers on other machines (pass this machine's LAN IP).
Presets exercise the built-in device profiles and generic layouts.
"""

import argparse
import copy
import json
import os
import selectors
import socket
import struct
import time
import zlib

import numpy as np

PORT = 7331
HELLO = b'MIRROR1\n'
ROLES = ('TALK', 'AUX', 'POWER', 'SPK', 'UP', 'DOWN', 'LEFT', 'RIGHT', 'ENTER', 'ESC')


def board(name, w, h, round_, diag, touch, buttons):
    return dict(name=name, w=w, h=h, round=round_, diag=diag, touch=touch,
                version=2, audio=True, keyboard=False, buttons=[dict(role=r, name=n, x=x, y=y) for r, n, x, y in buttons])


PRESETS = {
    'waveshare-s3-216': board('Waveshare 2.16', 480, 480, False, 2.16, True,
                             [(1, 'key', -1, -1), (2, 'boot', -1, -1), (3, 'power', -1, -1)]),
    'waveshare-s3-154g': board('Waveshare 1.54G', 200, 200, False, 1.54, False,
                              [(2, 'boot', -1, -1), (3, 'power', -1, -1)]),
    'waveshare-s3-349': board('Waveshare 3.49', 172, 640, False, 3.49, True,
                             [(2, 'boot', -1, -1), (3, 'power', -1, -1)]),
    'waveshare-p4-35': board('Waveshare 3.5', 480, 320, False, 3.5, True,
                            [(2, 'boot', -1, -1), (3, 'power', -1, -1)]),
    'aipi-lite': board('XOrigin AIPI Lite', 128, 128, False, 1.44, False,
                       [(2, 'boot', -1, -1), (3, 'power', -1, -1)]),
    'waveshare-s3-175c': board('Waveshare ESP32-S3-Touch-AMOLED-1.75C', 466, 466, True, 1.75, True,
                             [(1, 'top', 386, 104), (2, 'bottom', 386, 362)]),
    'sensecap-watcher': board('Seeed SenseCAP Watcher', 412, 412, True, 1.45, True,
                             [(1, 'wheel', 306, 63), (2, 'scroll', -1, -1)]),
    'm5stack-sticks3': board('M5Stack StickS3', 135, 240, False, 1.14, False,
                           [(1, 'front', 67, 236), (2, 'side', 133, 181)]),
    'guition-jc3248w535': board('Guition JC3248W535', 320, 480, False, 3.5, True,
                              [(1, 'boot', 304, 472), (2, 'boot', -1, -1)]),
}

GLYPHS = {
    '0': '111101101101111', '1': '010110010010111', '2': '111001111100111',
    '3': '111001111001111', '4': '101101111001001', '5': '111100111001111',
    '6': '111100111101111', '7': '111001010010010', '8': '111101111101111',
    '9': '111101111001111', 'A': '010101111101101', 'B': '110101110101110',
    'C': '111100100100111', 'D': '110101101101110', 'E': '111100110100111',
    'F': '111100110100100', 'G': '111100101101111', 'H': '101101111101101',
    'I': '111010010010111', 'J': '001001001101111', 'K': '101101110101101',
    'L': '100100100100111', 'M': '101111111101101', 'N': '101111111111101',
    'O': '111101101101111', 'P': '111101111100100', 'Q': '111101101111001',
    'R': '110101110101101', 'S': '111100111001111', 'T': '111010010010010',
    'U': '101101101101111', 'V': '101101101101010', 'W': '101101111111101',
    'X': '101101010101101', 'Y': '101101010010010', 'Z': '111001010100111',
    '-': '000000111000000', ':': '000010000010000', ' ': '000000000000000',
}


def text(fb, x, y, value, colour=0xffff, scale=1):
    for char in value.upper():
        glyph = np.array([v == '1' for v in GLYPHS.get(char, GLYPHS[' '])]).reshape(5, 3)
        glyph = np.repeat(np.repeat(glyph, scale, axis=0), scale, axis=1)
        if x >= 0 and y >= 0 and y + 5 * scale <= fb.shape[0] and x + 3 * scale <= fb.shape[1]:
            fb[y:y + 5 * scale, x:x + 3 * scale][glyph] = colour
        x += 4 * scale


def runs(flat):
    starts = np.r_[0, np.flatnonzero(flat[1:] != flat[:-1]) + 1]
    counts = np.diff(np.r_[starts, flat.size])
    return np.column_stack((counts, flat[starts])).astype('<u2').tobytes()


def frames(fb, limit=32768, state=0):
    h, w = fb.shape
    band = (limit - 22) // (w * 2)
    if band < 1:
        raise ValueError('message must fit one row')
    # Keep a run's count within the wire's u16, even with a larger test limit.
    band = min(band, 65535 // w)
    for y in range(0, h, band):
        pixels = fb[y:y + band]
        choices = (pixels.astype('<u2').tobytes(), runs(pixels.ravel()), runs(pixels.T.ravel()))
        enc = min(range(3), key=lambda i: len(choices[i]))
        payload = choices[enc]
        yield (struct.pack('<BBHHH', ord('F'), state, 1, w, h)
               + struct.pack('<HHHHBBI', 0, y, w, len(pixels), enc, 0, len(payload)) + payload)


class Pattern:
    def __init__(self, info):
        self.info = info
        self.held = set()
        self.pointer = None
        self.trail = []
        self.listen = 0
        self.talk = False
        self.talk_samples = 0
        self.audio_at = 0
        self.led_sent = None
        self.taps = {}
        self.yy, self.xx = np.indices((info['h'], info['w']))

    def release(self):
        self.held.clear()
        self.pointer = None
        self.trail.clear()
        self.listen = 0
        self.talk = False
        self.led_sent = None
        self.taps.clear()

    def input(self, msg, now):
        if len(msg) == 2 and msg[0] == 7 and msg[1] <= 2:
            self.listen = msg[1]
        elif len(msg) == 2 and msg[0] == 8:
            self.talk = bool(msg[1])
        elif 2 < len(msg) <= 642 and msg[:2] == b'S\x01' and len(msg) % 2 == 0 and self.talk:
            self.talk_samples += (len(msg) - 2) // 2
        elif len(msg) == 2 and msg[0] == 3 and 1 <= msg[1] <= 10:
            self.taps[msg[1]] = now + .15
        if len(msg) == 3 and msg[0] == 6 and 1 <= msg[1] <= 10:
            (self.held.add if msg[2] else self.held.discard)(msg[1])
        elif len(msg) == 5 and msg[0] <= 1 and self.info['touch']:
            down, x, y = struct.unpack('<BHH', msg)
            if x < self.info['w'] and y < self.info['h']:
                self.pointer = (x, y) if down else None
                self.trail.append((x, y, now))
                self.trail = self.trail[-128:]

    def extras(self, now, period):
        mode = int(now) % 4
        if mode != self.led_sent:
            self.led_sent = mode
            yield bytes([ord('L'), mode])
        if self.listen:
            samples = max(1, round(16000 * period))
            for at in range(0, samples, 640):
                n = min(640, samples - at)
                t = np.arange(self.audio_at, self.audio_at + n)
                pcm = (np.sin(t * (440 if self.listen == 1 else 660) * 2 * np.pi / 16000) * 4000).astype('<i2')
                self.audio_at += n
                yield b'A' + bytes([self.listen]) + pcm.tobytes()

    def draw(self, now):
        self.taps = {k: end for k, end in self.taps.items() if now < end}
        w, h = self.info['w'], self.info['h']
        palette = np.array([0xf800, 0x07e0, 0x001f, 0xffe0, 0x07ff, 0xf81f], dtype=np.uint16)
        fb = palette[(self.xx * 6 // w + int(now)) % 6].copy()
        fb[((self.xx // 16 + self.yy // 16) % 2) == 0] &= 0x7bef
        cx, cy = int((np.sin(now) + 1) * (w - 1) / 2), int((np.cos(now * .7) + 1) * (h - 1) / 2)
        fb[(self.xx - cx) ** 2 + (self.yy - cy) ** 2 < (min(w, h) // 9) ** 2] = 0xffff
        scale = max(1, min(w // 110, h // 100))
        panel_w, panel_h = 104 * scale, 54 * scale
        x0, y0 = (w - panel_w) // 2, (h - panel_h) // 2
        fb[y0:y0 + panel_h, x0:x0 + panel_w] = 0x1082
        text(fb, x0 + scale, y0 + 2 * scale, 'ESPMIRROR', scale=scale)
        for role, label in enumerate(ROLES, 1):
            row, col = (role - 1) % 5, (role - 1) // 5
            text(fb, x0 + (1 + col * 52) * scale, y0 + (11 + row * 8) * scale,
                 '%d %s' % (role, label), 0xffe0 if role in self.held or role in self.taps else 0x7bef, scale)
        self.trail = [p for p in self.trail if now - p[2] < 1.5]
        for x, y, _ in self.trail:
            fb[max(0, y - 2):min(h, y + 3), max(0, x - 2):min(w, x + 3)] = 0xf81f
        if self.pointer:
            x, y = self.pointer
            fb[max(0, y - 8):min(h, y + 9), x] = 0xffff
            fb[y, max(0, x - 8):min(w, x + 9)] = 0xffff
            text(fb, x0, y0 + panel_h + 3, '%d %d' % (x, y), scale=scale)
        return fb


class Peer:
    def __init__(self, sock):
        self.sock = sock
        self.received = bytearray()
        self.out = bytearray()
        self.deadline = time.monotonic() + 2
        self.partial_until = None
        self.sending_since = None
        self.admitted = False
        self.closing = False


class FakeBoard:
    def __init__(self, info, host='127.0.0.1', port=PORT, fps=10):
        self.info = copy.deepcopy(info)
        self.pattern = Pattern(self.info)
        self.period = 1 / fps
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, port))
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, None)
        self.port = self.listener.getsockname()[1]
        self.peers = set()
        self.active = None
        self.next_frame = 0

    def close_peer(self, peer):
        if peer not in self.peers:
            return
        self.peers.remove(peer)
        self.selector.unregister(peer.sock)
        peer.sock.close()
        if self.active is peer:
            self.active = None
            self.pattern.release()

    def queue(self, peer, msg):
        if not peer.out:
            peer.sending_since = time.monotonic()
        peer.out += struct.pack('<I', len(msg)) + msg
        self.selector.modify(peer.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, peer)

    def read(self, peer):
        data = peer.sock.recv(4096)
        if not data:
            self.close_peer(peer)
            return
        peer.received += data
        if peer.closing:
            peer.received.clear()
            return
        if not peer.admitted:
            if len(peer.received) < len(HELLO):
                return
            if peer.received[:len(HELLO)] != HELLO:
                self.close_peer(peer)
                return
            del peer.received[:len(HELLO)]
            if self.active:
                self.active.closing = True
                self.queue(self.active, b'T')
            self.pattern.release()
            peer.admitted = True
            self.active = peer
            self.next_frame = 0
            self.queue(peer, b'B' + json.dumps(self.info).encode())
        now = time.monotonic()
        if peer.received and peer.partial_until is None:
            peer.partial_until = now + 2
        while len(peer.received) >= 4:
            n, = struct.unpack_from('<I', peer.received)
            if n > 1024:
                self.close_peer(peer)
                return
            if len(peer.received) < 4 + n:
                break
            message = bytes(peer.received[4:4 + n])
            if message == b'R':
                self.queue(peer, b'B' + json.dumps(self.info).encode())
                self.next_frame = 0
            else:
                self.pattern.input(message, now)
            del peer.received[:4 + n]
            peer.partial_until = now + 2 if peer.received else None

    def step(self, timeout=.02):
        for key, mask in self.selector.select(timeout):
            peer = key.data
            if peer is None:
                sock, _ = self.listener.accept()
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                for candidate in list(self.peers):
                    if not candidate.admitted:
                        self.close_peer(candidate)
                peer = Peer(sock)
                self.peers.add(peer)
                self.selector.register(sock, selectors.EVENT_READ, peer)
                continue
            try:
                if mask & selectors.EVENT_READ:
                    self.read(peer)
                if peer in self.peers and mask & selectors.EVENT_WRITE:
                    n = peer.sock.send(peer.out)
                    del peer.out[:n]
                    if not peer.out:
                        peer.sending_since = None
                        if peer.closing:
                            self.close_peer(peer)
                        else:
                            self.selector.modify(peer.sock, selectors.EVENT_READ, peer)
            except BlockingIOError:
                pass
            except OSError:
                self.close_peer(peer)
        now = time.monotonic()
        for peer in list(self.peers):
            if ((not peer.admitted and now > peer.deadline)
                    or (peer.partial_until is not None and now > peer.partial_until)
                    or (peer.sending_since is not None and now - peer.sending_since > 3)):
                self.close_peer(peer)
        if self.active and now >= self.next_frame and not self.active.out:
            self.next_frame = now + self.period
            for msg in frames(self.pattern.draw(now)):
                self.queue(self.active, msg)
            for msg in self.pattern.extras(now, self.period):
                self.queue(self.active, msg)

    def close(self):
        for peer in list(self.peers):
            self.close_peer(peer)
        self.selector.unregister(self.listener)
        self.listener.close()
        self.selector.close()


class FakeCable:
    """POSIX PTY with console/session/drain states and occasional ROM interference."""
    def __init__(self, info, fps=10, corrupt_every=25, quiet=6, enabled=True):
        import pty
        import tty
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.port = os.ttyname(self.slave)
        self.info = copy.deepcopy(info)
        self.pattern = Pattern(self.info)
        self.period, self.quiet = 1 / fps, quiet
        self.corrupt_every = corrupt_every
        self.enabled = enabled
        self.out = bytearray(b'boot: fake espmirror\nconsole ready\n')
        self.received = bytearray()
        self.console_bytes = bytearray()
        self.drained = bytearray()
        self.active = False
        self.drain_until = 0
        self.drain_limit = 0
        self.heard = time.monotonic()
        self.partial_until = None
        self.next_frame = 0
        self.sessions = self.refreshes = self.broken = self.frame_count = self.breaks = 0
        self.waiting_refresh = False
        self.end_requested = False
        self.next_console_log = 0

    def queue(self, msg):
        body = struct.pack('<I', len(msg)) + msg
        self.out += b'\xa5\x5a' + body + struct.pack('<I', zlib.crc32(body))

    def end(self):
        self.end_requested = True

    def finish(self, now):
        if self.active:
            self.queue(b'E')
        self.active = False
        self.pattern.release()
        self.drained += self.received
        self.received.clear()
        self.drain_until, self.drain_limit = now + .15, now + 2
        self.partial_until = None

    def input(self, data, now):
        if self.drain_until:
            self.drained += data
            self.drain_until = min(now + .15, self.drain_limit)
            return
        self.received += data
        if not self.active:
            while self.received:
                if self.received[0] == ord('>'):
                    if b'\n' not in self.received:
                        return
                    line, _, rest = self.received.partition(b'\n')
                    self.received = bytearray(rest)
                    if line.rstrip(b'\r') == b'>mirror':
                        if not self.enabled:
                            self.out += b'mirror: error: unknown command or bad value\n'
                            continue
                        self.out += b'\0MIRROR1\n'
                        self.queue(b'B' + json.dumps(self.info).encode())
                        self.active = True
                        self.sessions += 1
                        self.heard = now
                        self.next_frame = 0
                        self.waiting_refresh = False
                        break
                    self.console_bytes += line + b'\n'
                else:
                    c = self.received.pop(0)
                    if c not in (10, 13, 255):
                        self.console_bytes.append(c)
        if not self.active:
            return
        if self.received and self.partial_until is None:
            self.partial_until = now + 2
        while len(self.received) >= 4:
            n, = struct.unpack_from('<I', self.received)
            if n > 1024:
                if self.received[:4] == b'\xff' * 4:
                    self.breaks += 1
                self.finish(now)
                return
            if len(self.received) < 4 + n:
                break
            msg = bytes(self.received[4:4 + n])
            del self.received[:4 + n]
            self.heard = now
            self.partial_until = now + 2 if self.received else None
            if msg == b'R':
                self.queue(b'B' + json.dumps(self.info).encode())
                self.refreshes += 1
                self.waiting_refresh = False
                self.next_frame = 0
            else:
                self.pattern.input(msg, now)

    def step(self, timeout=.01):
        import select
        now = time.monotonic()
        if self.drain_until and now >= self.drain_until:
            self.drain_until = 0
            self.out += b'console resumed\n'
        if not self.active and not self.drain_until and now >= self.next_console_log:
            self.next_console_log = now + .25
            self.out += b'console: waiting for >mirror\n'
        readable, writable, _ = select.select([self.master], [self.master] if self.out else [], [], timeout)
        if readable:
            try:
                self.input(os.read(self.master, 8192), time.monotonic())
            except BlockingIOError:
                pass
        if writable:
            try:
                n = os.write(self.master, self.out[:4096])
                del self.out[:n]
            except BlockingIOError:
                pass
        now = time.monotonic()
        if self.end_requested:
            self.end_requested = False
            self.finish(now)
        if self.active and (now - self.heard > self.quiet
                            or self.partial_until is not None and now > self.partial_until):
            self.finish(now)
        if self.active and not self.out and now >= self.next_frame:
            self.next_frame = now + self.period
            if self.waiting_refresh:
                self.queue(struct.pack('<BBHHH', ord('F'), 0, 0, self.info['w'], self.info['h']))
                return
            self.frame_count += 1
            for i, msg in enumerate(frames(self.pattern.draw(now))):
                if i == 0 and self.corrupt_every and self.frame_count % self.corrupt_every == 0:
                    body = struct.pack('<I', len(msg)) + msg
                    wire = b'\xa5\x5a' + body + struct.pack('<I', zlib.crc32(body))
                    cut = len(wire) // 2
                    self.out += wire[:cut] + b'\nROM injected frame\n' + wire[cut:]
                    self.waiting_refresh = True
                    self.broken += 1
                else:
                    self.queue(msg)
            self.out += b'frame sent\n'
            for msg in self.pattern.extras(now, self.period):
                self.queue(msg)


    def close(self):
        os.close(self.master)
        os.close(self.slave)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--board', choices=PRESETS, default='waveshare-s3-175c')
    ap.add_argument('--serial', action='store_true', help='serve a POSIX PTY cable instead of TCP')
    ap.add_argument('--corrupt-every', type=int, default=25, help='cable: insert ROM text every N screens; 0 disables')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--fps', type=float, default=10)
    ap.add_argument('--list', action='store_true', help='list preset names and dimensions')
    args = ap.parse_args()
    if args.list:
        for name, info in PRESETS.items():
            print('%-22s %dx%d  %s' % (name, info['w'], info['h'], info['name']))
        return
    if not 0 < args.fps <= 60:
        ap.error('--fps must be between 0 and 60')
    if args.serial:
        server = FakeCable(PRESETS[args.board], args.fps, args.corrupt_every)
        print('Fake cable: python espmirror.py %s' % server.port, flush=True)
    else:
        server = FakeBoard(PRESETS[args.board], args.host, args.port, args.fps)
        print('%s listening on %s:%d' % (server.info['name'], args.host, server.port), flush=True)
    try:
        while True:
            server.step()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == '__main__':
    main()
