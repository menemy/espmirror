#!/usr/bin/env python3
"""The board's live screen as the device on your desktop: no title bar, no window background, the real body
with its keys around the glass, the mouse as the touch.
Plain TCP to the board (the espmirror component, port 7331) and a native window (AppKit through pyobjc).

  tools/mirror_window.py HOST [--scale K] [--on-top] [--sound]
  tools/mirror_window.py HOST --stats [--seconds N] [--png out.png]     no window: frame statistics, a PNG

The wire is in docs/protocol.md: 4-byte little-endian lengths, "MIRR1\n" first, 'F' frames of run-length coded
rectangles, 'B' the board, 'A' audio, 'L' the LED, 'T' when another viewer takes over; keys, listen and talk back.
MirrorClient speaks it; Screen keeps the pixels (RGB565, numpy) and applies frames. The board's name in 'B' picks
its look from DEVICES: the body, the keys and where they are, the LED; a board without a profile gets a plain
frame around its screen. New devices are a line in DEVICES (docs/devices.md).

The glass is the touch; the handle at the bottom left drags the window, the one at the bottom right resizes it,
keeping the device's proportions; View > Actual size is the device's real size on the display the window is on.
Above the device: talk, sound, record, screenshot and close. Recordings (mp4, through ffmpeg) and screenshots go
to the Desktop as mirror-<time>.*, the path is printed. One line a second of statistics goes to stdout.

  View menu            Screenshot (Cmd S), Record (Cmd R), Sound (hear what the board plays, Cmd L), Mic (hear
                       its microphone, Cmd Shift L), Talk (your microphone to the board's speaker, Cmd K),
                       Bigger / Smaller / Actual size (Cmd =, Cmd -, Cmd 0), Float on Top (Cmd T)
  1, 2, 3              the device's keys (hold for a long press); the drawn keys too

Needs Python 3.11+ with numpy, pyobjc and sounddevice (tools/requirements.txt); macOS only for now.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

PORT = 7331
HELLO = b"MIRR1\n"
RETRY_S = 1.5                       # the board admits one new viewer a second

# What a board looks like, by a fragment of the name it sends in 'B', in millimetres from Waveshare's outline
# drawings. screen_mm: the active area's width (its pixels come with the message). body: a rectangle (w, h, the
# active area's top left at x, y, corner radius r) or a disc (diameter d), in a finish. glass: the dark border of
# the cover glass around the active area and its corner radius. keys: (label, key code the firmware takes: 1 KEY3,
# 2 BOOT, 3 PWR, where, position, length): on the "top" edge at x from the left, on the "right" edge at y from
# the top, or on the rim at an "angle" clockwise from the top. `hold` keys send the press and the release as they
# happen (6, key, 1/0) instead of a tap with its length (3, key).
DEVICES = [
    # White plastic box, the three round buttons on the top edge: KEY, PWR, BOOT.
    # The body's margin around the screen is drawn at 2.8 mm, under the drawing's 3.5: the owner's eye.
    ("2.16", dict(screen_mm=38.99, body=dict(shape="rect", w=44.6, h=44.6, x=2.8, y=2.8, r=5.5, finish="white"),
                  glass=dict(border=2.15, r=4.5),
                  keys=[("key3", 1, "top", 12.3, 5.3), ("pwr", 3, "top", 22.3, 5.3), ("boot", 2, "top", 32.3, 5.3)], hold=False)),
    # White plastic box, taller than wide, the panel in its upper part; two keys low on the right edge. BOOT held
    # is push-to-talk; PWR tapped pages, held a second rotates: both sent as they happen.
    # Its green LED shows low on the left, at about seven o'clock on the body. The 2.16's charge LED is inside.
    ("1.54G", dict(screen_mm=27.8, body=dict(shape="rect", w=39.8, h=53.0, x=6.0, y=10.9, r=4.5, finish="white"),
                   glass=dict(border=0.8, r=0.6), led=(5.5, 46.5, 1.6, (0x30, 0xd0, 0x60)),
                   keys=[("boot", 2, "right", 31.1, 5.5), ("pwr", 3, "right", 41.3, 5.5)], hold=True)),
    # Aluminium disc, two keys on the rim at about 2 and 4 o'clock: the top one is KEY3 (its double click is the
    # PWR press there), the bottom one BOOT.
    ("1.75C", dict(screen_mm=43.76, body=dict(shape="round", d=55.0, finish="silver"), glass=dict(border=2.6, r=0),
                   keys=[("key3", 1, "angle", 50, 7.0), ("boot", 2, "angle", 130, 5.0)], hold=False)),
    # AIPI Lite (XOrigin): a red slab 55.5 x 47.5 x 7.8 mm with a stud hole in each corner, the 1.44" screen up
    # in the middle, both buttons on the bottom edge (power left, the talk key right), the LED and the mic there
    # too (from the maker's manual; the owner's unit may be another of its five colours).
    ("AIPI", dict(screen_mm=25.9, body=dict(shape="rect", w=55.5, h=47.5, x=14.8, y=5.0, r=5.0, finish="red",
                                           studs=((6.0, 6.0), (49.5, 6.0), (6.0, 41.5), (49.5, 41.5), 2.4)),
                  glass=dict(border=1.0, r=1.0),
                  keys=[("pwr", 3, "bottom", 19.0, 4.0), ("boot", 2, "bottom", 38.5, 4.0)], hold=True)),
]
GENERIC = dict(screen_mm=None, body=dict(shape="rect", w=None, h=None, x=3.0, y=3.0, r=3.0, finish="white"),
               glass=dict(border=1.5, r=2.0), keys=[], hold=False)
FINISHES = {
    "white": dict(fill=(0xf2, 0xf2, 0xee), fill2=(0xe4, 0xe4, 0xdf), edge=(0xc4, 0xc4, 0xbe), shine=.0,
                  key=((0xee, 0xee, 0xea), (0xc2, 0xc2, 0xbc)), key_down=((0xd4, 0xd4, 0xcf), (0xa8, 0xa8, 0xa2))),
    "silver": dict(fill=(0xdd, 0xdd, 0xe0), fill2=(0xa4, 0xa4, 0xaa), edge=(0x7a, 0x7a, 0x80), shine=.0,
                   key=((0xe2, 0xe2, 0xe5), (0x9e, 0x9e, 0xa4)), key_down=((0xc0, 0xc0, 0xc4), (0x80, 0x80, 0x86))),
    "red": dict(fill=(0xe0, 0x30, 0x2e), fill2=(0xc4, 0x22, 0x22), edge=(0x8e, 0x16, 0x16), shine=.0,
                key=((0x30, 0x30, 0x32), (0x18, 0x18, 0x1a)), key_down=((0x22, 0x22, 0x24), (0x10, 0x10, 0x12))),
}

def device_profile(name):
    for fragment, profile in DEVICES:
        if fragment in (name or ""):
            return profile
    return GENERIC


class Screen:
    """The pixels as the board sends them: RGB565 in a numpy array, h x w."""

    def __init__(self):
        import numpy as np
        self.np = np
        self.w = self.h = 0
        self.px = np.zeros((0, 0), dtype=np.uint16)
        self.state = "bright"
        self.changes = 0             # bumped when pixels changed: cheaper than hashing them

    def _size(self, w, h):
        if (w, h) != (self.w, self.h):
            self.w, self.h = w, h
            self.px = self.np.zeros((h, w), dtype=self.np.uint16)

    def apply(self, frame):
        """One 'F' message; returns the list of changed rectangles (x, y, w, h)."""
        np = self.np
        if not frame or frame[0:1] != b"F":
            return []
        self.state = ("bright", "dim", "dark")[frame[1]] if frame[1] < 3 else self.state
        n, w, h = struct.unpack_from("<HHH", frame, 2)
        self._size(w, h)
        o, rects = 8, []
        for _ in range(n):
            x, y, rw, rh, enc, _, ln = struct.unpack_from("<HHHHBBI", frame, o)
            o += 14
            data = np.frombuffer(frame, dtype="<u2", count=ln // 2, offset=o)
            o += ln
            if enc == 0:
                block = data.reshape(rh, rw)
            else:
                runs, cols = data[0::2].astype(np.int64), data[1::2]
                flat = np.repeat(cols, runs)
                block = flat[: rw * rh].reshape(rh, rw) if enc == 1 else flat[: rw * rh].reshape(rw, rh).T
            self.px[y:y + rh, x:x + rw] = block
            rects.append((x, y, rw, rh))
        if rects:
            self.changes += 1
        return rects

    def digest(self):
        return hashlib.blake2b(self.px.tobytes(), digest_size=8).hexdigest()

    def rgbx(self):
        """The screen as 8-bit RGB with a spare byte, h x w x 4, for CoreGraphics."""
        np = self.np
        p = self.px.astype(np.uint32)
        r, g, b = (p >> 11) & 31, (p >> 5) & 63, p & 31
        out = np.empty((self.h, self.w, 4), dtype=np.uint8)
        out[..., 0] = (r * 527 + 23) >> 6
        out[..., 1] = (g * 259 + 33) >> 6
        out[..., 2] = (b * 527 + 23) >> 6
        out[..., 3] = 255
        return out

    def png(self, path):
        """Writes the screen as a PNG (no Pillow needed)."""
        png_write(path, self.rgbx(), 6)


def png_write(path, rgbx, level):
    """A PNG from h x w x 4 RGBX bytes, at a zlib level."""
    h, w = rgbx.shape[:2]
    rgb = rgbx[..., :3]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))


class MirrorClient:
    """One connection to the board's mirror port. recv() returns whole messages; send*() the page's messages."""

    def __init__(self, host, port=PORT, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall(HELLO)
        self.lock = threading.Lock()
        self.boards = None
        self.taken = False

    def _recv_all(self, n):
        buf = bytearray()
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                raise ConnectionError("the board closed the connection")
            buf += part
        return bytes(buf)

    def recv(self):
        """The next message from the board, or None once it is over ('T', or the socket closed)."""
        try:
            n = struct.unpack("<I", self._recv_all(4))[0]
            msg = self._recv_all(n)
        except (ConnectionError, socket.timeout, OSError):
            return None
        if msg[:1] == b"B":
            self.boards = json.loads(msg[1:])
        elif msg[:1] == b"T":
            self.taken = True
            return None
        return msg

    def send(self, payload):
        with self.lock:
            try:
                self.sock.sendall(struct.pack("<I", len(payload)) + payload)
            except OSError:
                pass

    def pointer(self, x, y, down):
        self.send(struct.pack("<BHH", 1 if down else 0, x, y))

    def wheel(self, x, y, dx, dy):
        self.send(struct.pack("<BHHhh", 4, x, y, dx, dy))

    def key(self, k):
        self.send(bytes([3, k]))

    def key_state(self, k, down):
        self.send(bytes([6, k, 1 if down else 0]))

    def listen(self, source):
        """0 stops, 1 the board's mics, 2 what the board plays."""
        self.send(bytes([7, source]))

    def talk(self, on):
        self.send(bytes([8, 1 if on else 0]))

    def speak(self, pcm):
        self.send(b"S\x01" + pcm)

    def keepalive(self):
        self.send(bytes([2]))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def frames(client, screen):
    """Applies every frame to the screen; yields (message, changed rectangles). Ends when the connection does."""
    while True:
        msg = client.recv()
        if msg is None:
            return
        yield msg, screen.apply(msg) if msg[:1] == b"F" else []


def wait_stable(client, screen, quiet=0.5, timeout=10):
    """Returns True once no pixel has changed for `quiet` seconds (heartbeats keep coming while nothing changes)."""
    end, last = time.monotonic() + timeout, time.monotonic()
    client.sock.settimeout(max(0.1, quiet))
    try:
        for _, rects in frames(client, screen):
            now = time.monotonic()
            if rects:
                last = now
            elif now - last >= quiet:
                return True
            if now > end:
                return False
    except socket.timeout:
        return time.monotonic() - last >= quiet
    return False


def stats(host, seconds, png):
    """Frame statistics from the TCP port for a few seconds, then a PNG once the screen has been still."""
    c, s = MirrorClient(host), Screen()
    t0, n, nbytes, nrects = time.monotonic(), 0, 0, 0
    c.sock.settimeout(2)
    while time.monotonic() - t0 < seconds:
        try:
            msg = c.recv()
        except socket.timeout:
            continue
        if msg is None:
            print("connection over" + (" (taken)" if c.taken else ""))
            break
        nbytes += len(msg)
        if msg[:1] == b"F":
            nrects += len(s.apply(msg))
            n += 1
        elif msg[:1] == b"B":
            print("boards:", ", ".join(o["name"] for o in [c.boards["board"]] + c.boards["forms"]))
    dt = max(1e-3, time.monotonic() - t0)
    print(f"{s.w}x{s.h} screen {s.state} · {n} frames ({n / dt:.1f}/s) · {nrects} rects · {nbytes / dt / 1024:.1f} KB/s · hash {s.digest()}")
    if png:
        if wait_stable(c, s, 0.5, 5):
            s.png(png)
            print("saved", png)
        else:
            print("the screen kept changing, no PNG")
    c.close()


class Player:
    """Plays the board's audio on the Mac's default output. The board sends it in bursts (a few 40 ms chunks per
    pass of its loop), so the callback waits for a quarter second to gather before it starts, and gathers again
    after running dry, instead of playing the gaps; a short sound (a click, a cue) that never fills that much
    plays once nothing more has come for a tenth of a second. A buffer over two seconds long drops its oldest
    part."""

    PREFILL = 8000               # bytes: a quarter second at 16 kHz
    SETTLE = 0.1                 # seconds without new audio after which what there is plays
    CAP = 64000                  # two seconds

    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.stream = None
        self.peak = 0
        self.primed = False
        self.fed_at = 0.0

    def feed(self, pcm):
        with self.lock:
            self.buf += pcm
            self.fed_at = time.monotonic()
            if len(self.buf) > self.CAP:
                del self.buf[:len(self.buf) - self.CAP]
        if pcm:
            import numpy as np
            a = np.frombuffer(pcm, dtype="<i2")
            self.peak = int(np.abs(a.astype(np.int32)).max()) if a.size else 0

    def _cb(self, out, frames, t, status):
        n = frames * 2
        with self.lock:
            if not self.primed and self.buf and (len(self.buf) >= self.PREFILL
                                                 or time.monotonic() - self.fed_at > self.SETTLE):
                self.primed = True
            chunk = bytes(self.buf[:n]) if self.primed else b""
            del self.buf[:len(chunk)]
            if self.primed and not chunk:
                self.primed = False                       # ran dry: gather again before going on
        out[:len(chunk)] = chunk
        if len(chunk) < n:
            out[len(chunk):] = b"\0" * (n - len(chunk))

    def start(self):
        if self.stream:
            return
        self.primed = False
        self.stream = self.sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16", blocksize=320,
                                              latency="high", callback=self._cb)
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        with self.lock:
            self.buf.clear()
        self.peak = 0


class Capture:
    """The Mac's default input, 16 kHz mono, 20 ms at a time to a callback (the board's speaker)."""

    def __init__(self, sink):
        import sounddevice as sd
        self.sd, self.sink, self.stream = sd, sink, None

    def _cb(self, data, frames, t, status):
        self.sink(bytes(data))

    def start(self):
        if self.stream:
            return
        self.stream = self.sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=self._cb)
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None


def desktop_path(ext):
    return os.path.expanduser(time.strftime(f"~/Desktop/mirror-%Y%m%d-%H%M%S.{ext}"))


# ---------------------------------------------------------------------------------------------------------------
# The window. Everything below needs pyobjc; the protocol above does not.

def run_window(host, scale, on_top, selftest=False, sound=False):
    import AppKit
    import Quartz
    import objc
    from PyObjCTools import AppHelper

    BUTTON = 34                      # record, screenshot, close
    GRIP = 16
    MIN_GLASS = 120                  # points: the screen is never drawn narrower, a tiny device is no use
                                     # (under the real size on any display here: the Dell gives 125)

    def rgb(r, g, b, a=1.0):
        return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)

    def nsrect(r):
        """AppKit wants ((x, y), (w, h)); the layout keeps flat (x, y, w, h) tuples."""
        return ((r[0], r[1]), (r[2], r[3])) if len(r) == 4 else r

    def rounded(rect, radius):
        return AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(nsrect(rect), radius, radius)

    def oval(rect):
        return AppKit.NSBezierPath.bezierPathWithOvalInRect_(nsrect(rect))

    def fill(rect):
        AppKit.NSRectFillUsingOperation(nsrect(rect), AppKit.NSCompositingOperationSourceOver)

    def device_layout(prof, cw, ch, r_px, ppi, s, ox, oy):
        """The device at s points per millimetre with its body's top left at (ox, oy): the body, the glass, the
        active area (its corners from the board's r), and the keys as shapes (centre, rotation in degrees where 0
        is a vertical bar on the right edge, thickness w, length h, and the outward direction). Flipped
        coordinates, the view's and the icon's."""
        import math
        body, glass = prof["body"], prof["glass"]
        screen_mm = prof["screen_mm"] or (cw / ppi * 25.4 if ppi else 40.0)
        mm = screen_mm / cw                                   # millimetres per screen pixel
        aw, ah = cw * mm * s, ch * mm * s
        if body["shape"] == "round":
            d = body["d"] * s
            brect, body_r = (ox, oy, d, d), d / 2
            ax, ay = ox + (d - aw) / 2, oy + (d - ah) / 2
        else:
            bw = (body["w"] or cw * mm + 2 * body["x"]) * s
            bh = (body["h"] or ch * mm + 2 * body["y"]) * s
            brect, body_r = (ox, oy, bw, bh), body["r"] * s
            ax, ay = ox + body["x"] * s, oy + body["y"] * s
        g = glass["border"] * s
        grect = (ax - g, ay - g, aw + 2 * g, ah + 2 * g)
        round_ = r_px * 2 >= min(cw, ch)
        glass_r = (aw + 2 * g) / 2 if round_ else glass["r"] * s
        active_r = aw / 2 if round_ else r_px * mm * s
        keys = []
        cx, cy = brect[0] + brect[2] / 2, brect[1] + brect[3] / 2
        for label, code, where, pos, length in prof["keys"]:
            L, T = length * s * .75, length * s * .2            # along the edge, and out of it: low, as on the case
            if where == "angle":
                a = math.radians(pos)
                rr = body_r + T / 2 - 1
                keys.append(dict(label=label, code=code, cx=cx + rr * math.sin(a), cy=cy - rr * math.cos(a),
                                 rot=pos - 90, w=T, h=L, dx=math.sin(a), dy=-math.cos(a)))
            elif where == "top":
                keys.append(dict(label=label, code=code, cx=brect[0] + pos * s, cy=brect[1] - T / 2 + 1, rot=90,
                                 w=T, h=L, dx=0, dy=-1))
            elif where == "bottom":
                keys.append(dict(label=label, code=code, cx=brect[0] + pos * s,
                                 cy=brect[1] + brect[3] + T / 2 - 1, rot=90, w=T, h=L, dx=0, dy=1))
            else:
                keys.append(dict(label=label, code=code, cx=brect[0] + brect[2] + T / 2 - 1, cy=brect[1] + pos * s,
                                 rot=0, w=T, h=L, dx=1, dy=0))
        # led: (x, y of its centre from the body's top left corner, diameter, colour), all in millimetres; the
        # board reports its state with 'L' (0 off, 1 slow blink, 2 fast blink, 3 on).
        led = None
        if prof.get("led"):
            lx, ly, dia, colour = prof["led"]
            d = dia * s
            led = dict(rect=(brect[0] + lx * s - d / 2, brect[1] + ly * s - d / 2, d, d), colour=colour)
        studs = []
        if body.get("studs"):
            *at, dia = body["studs"]
            studs = [(brect[0] + x * s, brect[1] + y * s, dia * s) for x, y in at]
        return dict(body=brect, body_r=body_r, glass=grect, glass_r=glass_r, active=(ax, ay, aw, ah),
                    active_r=active_r, keys=keys, s=s, led=led, studs=studs,
                    right=max([k["w"] - 1 for k in keys if k["dx"] > 0] + [0]),
                    top=max([k["w"] - 1 for k in keys if k["dy"] < 0] + [0]),
                    bottom=max([k["w"] - 1 for k in keys if k["dy"] > 0] + [0]))

    def key_path(shape, pushed):
        """The key as a path; pushed in, it is half as thick and sits closer to the body."""
        w = shape["w"] * (.5 if pushed else 1)
        t = AppKit.NSAffineTransform.transform()
        t.translateXBy_yBy_(shape["cx"] - (shape["w"] / 4 * shape["dx"] if pushed else 0),
                            shape["cy"] - (shape["w"] / 4 * shape["dy"] if pushed else 0))
        t.rotateByDegrees_(shape["rot"])
        path = rounded((-w / 2, -shape["h"] / 2, w, shape["h"]), min(2.5, w / 2))
        path.transformUsingAffineTransform_(t)
        return path

    def key_hit(shape, p):
        import math
        r = math.radians(shape["rot"])
        dx, dy = p.x - shape["cx"], p.y - shape["cy"]
        lx = math.cos(r) * dx + math.sin(r) * dy
        ly = -math.sin(r) * dx + math.cos(r) * dy
        return abs(lx) <= shape["w"] / 2 + 5 and abs(ly) <= shape["h"] / 2

    def paint_led(L, mode, now):
        """A small lens on the body: dark when off, the colour with a glow when on; the slow and fast blinks are
        timed here (the 1.54G blinks itself and reports each change)."""
        if not L["led"]:
            return
        lit = mode == 3 or (mode == 1 and now % 1.0 < .5) or (mode == 2 and now % .25 < .125)
        r = L["led"]["rect"]
        rgb(0x9a, 0x9a, 0x96).set()
        oval((r[0] - .5, r[1] - .5, r[2] + 1, r[3] + 1)).fill()
        (rgb(*L["led"]["colour"]) if lit else rgb(0x3a, 0x3a, 0x3c)).set()
        oval(r).fill()
        if lit:
            glow = AppKit.NSShadow.alloc().init()
            glow.setShadowColor_(rgb(*L["led"]["colour"], .8))
            glow.setShadowOffset_((0, 0))
            glow.setShadowBlurRadius_(r[2])
            AppKit.NSGraphicsContext.saveGraphicsState()
            glow.set()
            rgb(*L["led"]["colour"]).set()
            oval(r).fill()
            AppKit.NSGraphicsContext.restoreGraphicsState()

    def paint_device(L, prof, glass_img, pressed, veil, label_text, led_mode=0):
        """Draws the device from a layout: body with its finish and a drop shadow, keys, the glass, the screen."""
        style = FINISHES[prof["body"]["finish"]]
        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowColor_(rgb(0, 0, 0, .45))
        shadow.setShadowOffset_((0, -L["s"] * 1.2))
        shadow.setShadowBlurRadius_(L["s"] * 3)
        AppKit.NSGraphicsContext.saveGraphicsState()
        shadow.set()
        rgb(*style["fill"]).set()
        rounded(L["body"], L["body_r"]).fill()
        AppKit.NSGraphicsContext.restoreGraphicsState()
        # the keys sit behind the body's edge, so first
        for shape in L["keys"]:
            pushed = pressed == shape["label"]
            c1, c2 = style["key_down"] if pushed else style["key"]
            grad = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb(*c1), rgb(*c2))
            grad.drawInBezierPath_angle_(key_path(shape, pushed), shape["rot"] + 90)
        grad = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb(*style["fill"]), rgb(*style["fill2"]))
        grad.drawInBezierPath_angle_(rounded(L["body"], L["body_r"]), -60)
        edge = rounded(L["body"], L["body_r"])
        edge.setLineWidth_(1)
        rgb(*style["edge"]).set()
        edge.stroke()
        for sx, sy, sd in L["studs"]:                           # the corner holes: a dark ring, a lighter floor
            rgb(*style["edge"]).set()
            oval((sx - sd / 2, sy - sd / 2, sd, sd)).fill()
            rgb(*style["fill2"]).set()
            oval((sx - sd / 2 + 1, sy - sd / 2 + 1, sd - 2, sd - 2)).fill()
        paint_led(L, led_mode, time.monotonic())
        # the cover glass, then the screen inside it
        rgb(0x08, 0x08, 0x0a).set()
        rounded(L["glass"], L["glass_r"]).fill()
        AppKit.NSGraphicsContext.saveGraphicsState()
        rounded(L["active"], L["active_r"]).addClip()
        rgb(0, 0, 0).set()
        fill(L["active"])
        if glass_img is not None:
            glass_img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                nsrect(L["active"]), AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0, True, None)
        if veil:
            rgb(0, 0, 0, veil).set()
            fill(L["active"])
        if label_text:
            attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(12),
                     AppKit.NSForegroundColorAttributeName: rgb(0xc9, 0xc9, 0xc9)}
            t = AppKit.NSAttributedString.alloc().initWithString_attributes_(label_text, attrs)
            sz = t.size()
            a = L["active"]
            t.drawAtPoint_((a[0] + (a[2] - sz.width) / 2, a[1] + (a[3] - sz.height) / 2))
        AppKit.NSGraphicsContext.restoreGraphicsState()
        # the glass's reflection
        hl = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb(255, 255, 255, .07), rgb(255, 255, 255, 0))
        hl.drawInBezierPath_angle_(rounded(L["glass"], L["glass_r"]), -70)

    class Model:
        """Shared between the network thread and the view: the screen, the connection and what it said."""

        def __init__(self):
            self.lock = threading.Lock()
            self.screen = Screen()
            self.client = None
            self.board = None        # {name, w, h, ppi, r}: this board, from the 'B' message
            self.connected = False
            self.taken = False
            self.frames = self.bytes = self.audio_bytes = 0
            self.rec = None          # {dir, n, last, list}
            self.listen = 0          # the source asked for (1 mics, 2 speaker); re-asked on every connection
            self.player = None
            self.talk = False        # the Mac's mic to the board's speaker
            self.capture = None
            self.led = 0             # the board's LED: 0 off, 1 slow blink, 2 fast blink, 3 on

    model = Model()

    def net_loop(view):
        """Connects, applies frames, asks the view to redraw; reconnects after a pause unless taken over."""
        while True:
            if model.taken:
                time.sleep(0.2)
                continue
            try:
                client = MirrorClient(host, timeout=20)     # on battery the radio sleeps between beacons
            except OSError as e:
                print(f"{time.strftime('%H:%M:%S')} {host}:{PORT}: {e}; retrying", flush=True)
                time.sleep(RETRY_S)
                continue
            client.sock.settimeout(6)
            with model.lock:
                model.client, model.connected = client, True
                if model.listen:
                    client.listen(model.listen)
                if model.talk:
                    client.talk(True)
            AppHelper.callAfter(view.redraw)
            last_size = None
            while True:
                try:
                    msg = client.recv()
                except socket.timeout:
                    continue
                if msg is None:
                    break
                snap = player = None
                with model.lock:
                    model.bytes += len(msg)
                    if msg[:1] == b"B":
                        model.board = client.boards["board"]
                    elif msg[:1] == b"F":
                        rects = model.screen.apply(msg)
                        model.frames += 1
                        size = (model.screen.w, model.screen.h)
                        snap = model.screen.rgbx() if model.rec and rects else None
                    elif msg[:1] == b"A" and len(msg) > 2:
                        model.audio_bytes += len(msg) - 2
                        player = model.player if model.listen == msg[1] else None
                    elif msg[:1] == b"L" and len(msg) == 2:
                        model.led = msg[1]
                        AppHelper.callAfter(view.led_changed)
                if player:
                    player.feed(msg[2:])
                if snap is not None:
                    record_frame(snap)        # the PNG is compressed outside the lock
                if msg[:1] == b"F":
                    if size != last_size:
                        last_size = size
                        AppHelper.callAfter(view.screen_resized)
                    AppHelper.callAfter(view.redraw)
            with model.lock:
                model.client, model.connected, model.taken = None, False, client.taken
                player = model.player
            if player:
                player.feed(b"")
            client.close()
            AppHelper.callAfter(view.redraw)
            if not client.taken:
                time.sleep(RETRY_S)

    def record_frame(rgbx):
        """One frame of the recording (its pixels already copied), with how long the one before was on screen."""
        with model.lock:
            r = model.rec
            if not r:
                return
            now = time.monotonic()
            if r["n"]:
                r["list"].write(f"duration {now - r['last']:.4f}\n")
            name = f"f{r['n']:05d}.png"
            r["list"].write(f"file '{name}'\n")
            r["n"] += 1
            r["last"] = now
            path = os.path.join(r["dir"], name)
        png_write(path, rgbx, 1)             # level 1: ffmpeg re-encodes anyway

    def record_stop():
        with model.lock:
            r = model.rec
            snap = model.screen.rgbx() if r and not r["n"] and model.screen.w else None
        if snap is not None:
            record_frame(snap)                      # a still screen: the one frame covers the whole take
        with model.lock:
            r, model.rec = model.rec, None
        if not r:
            return
        if not r["n"]:
            r["list"].close()
            shutil.rmtree(r["dir"], ignore_errors=True)
            view.notice("nothing recorded: no frame came")
            return
        # the concat demuxer honours the last duration only when the last frame is listed once more
        r["list"].write(f"duration {max(0.04, time.monotonic() - r['last']):.4f}\nfile 'f{r['n'] - 1:05d}.png'\n")
        r["list"].close()
        out = desktop_path("mp4")

        def encode():
            if not shutil.which("ffmpeg"):
                print(f"no ffmpeg: the frames are in {r['dir']}", flush=True)
                return
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", r["list"].name,
                   "-fps_mode", "vfr", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", out]
            if subprocess.run(cmd).returncode == 0:
                shutil.rmtree(r["dir"], ignore_errors=True)
                print(f"saved {out} ({r['n']} frames)", flush=True)
                AppHelper.callAfter(view.notice, "saved " + os.path.basename(out))
            else:
                print(f"ffmpeg failed: the frames are in {r['dir']}", flush=True)
                AppHelper.callAfter(view.notice, "ffmpeg failed, frames kept in " + r["dir"])

        threading.Thread(target=encode, daemon=True).start()

    def record_start():
        d = tempfile.mkdtemp(prefix="mirror-rec-")
        with model.lock:
            model.rec = {"dir": d, "n": 0, "last": time.monotonic(), "list": open(os.path.join(d, "frames.txt"), "w")}
            snap = model.screen.rgbx() if model.screen.w else None
        if snap is not None:
            record_frame(snap)

    def icon_image(glass_img, b):
        """The Dock icon: the device, its screen inside, 512 px. b is the board shown (w, h, r, profile)."""
        size, margin = 512, 44

        def paint():
            prof = b.get("profile", GENERIC)
            screen_mm = prof["screen_mm"] or (b["w"] / b["ppi"] * 25.4 if b.get("ppi") else 40.0)
            probe = device_layout(prof, b["w"], b["h"], b.get("r", 0), b.get("ppi", 0), 1.0, 0, 0)
            s = (size - 2 * margin) / max(probe["body"][2] + probe["right"], probe["body"][3] + probe["top"])
            L = device_layout(prof, b["w"], b["h"], b.get("r", 0), b.get("ppi", 0), s,
                              (size - L_w(probe, s)) / 2, (size - probe["body"][3] * s) / 2 + probe["top"] * s / 2)
            paint_device(L, prof, glass_img, None, 0, None)

        def L_w(probe, s):
            return probe["body"][2] * s + probe["right"] * s

        def draw(rect):
            AppKit.NSGraphicsContext.currentContext().setImageInterpolation_(AppKit.NSImageInterpolationHigh)
            paint()
            return True

        return AppKit.NSImage.imageWithSize_flipped_drawingHandler_((size, size), True, draw)

    class Window(AppKit.NSWindow):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return True

    class DeviceView(AppKit.NSView):
        """Draws the device and takes the mouse. Coordinates are flipped: the origin is the top left."""

        def isFlipped(self):
            return True

        def acceptsFirstResponder(self):
            return True

        @objc.python_method
        def setup(self):
            self.K = scale
            self.down = False
            self.pressed = None
            self.wheel_acc = [0.0, 0.0, 0, 0]
            self.wheel_timer = False
            self.key_t0 = {}
            self.grip_drag = None
            self.rects = {}
            self.image = None
            self.image_data = None
            self.image_hash = None
            self.flash_until = 0         # a screenshot: the glass flashes, the button lights, a toast names the file
            self.toast = None            # (text, until)
            return self

        # -- geometry ------------------------------------------------------------------------------------------
        @objc.python_method
        def board(self):
            with model.lock:
                b = model.board
                w, h = model.screen.w or (b or {}).get("w", 480), model.screen.h or (b or {}).get("h", 480)
            return {"w": w, "h": h, "r": b["r"] if b and (b["w"], b["h"]) == (w, h) else 72 if (w, h) == (480, 480) else 0,
                    "ppi": (b or {}).get("ppi", 0), "name": (b or {}).get("name", ""),
                    "profile": device_profile((b or {}).get("name"))}

        @objc.python_method
        def geometry(self, k, b=None):
            """The device at k window points per screen pixel, and the window around it: a margin for the drop
            shadow, more above for the buttons, room on the right and the top for keys that stick out."""
            b = b or self.board()
            prof = b["profile"]
            screen_mm = prof["screen_mm"] or (b["w"] / b["ppi"] * 25.4 if b["ppi"] else 40.0)
            s = k * b["w"] / screen_mm
            m = max(24, 16 * k)
            L = device_layout(prof, b["w"], b["h"], b["r"], b["ppi"], s, 0, 0)
            mt = max(m, BUTTON + 14) + L["top"]              # the keys on a top edge clear the buttons
            W = L["body"][2] + 2 * m + L["right"]
            tools = 5 * (BUTTON + 6) + 8                     # the five buttons at the top never shrink
            pad = max(0, tools - W) / 2                      # a small device sits centred under them
            return dict(b=b, cw=b["w"], ch=b["h"], s=s, m=m + pad, mt=mt, L=L, W=W + 2 * pad,
                        H=L["body"][3] + m + mt + L["bottom"])

        def resetCursorRects(self):
            """The cursor says what is under it: a hand over the screen and the keys, an open hand on the move
            handle, the diagonal resize arrows on the grip, the arrow on the buttons."""
            r = self.rects or (self.layout() if self.board() else None)
            if not r:
                return
            hand = AppKit.NSCursor.pointingHandCursor()
            self.addCursorRect_cursor_(nsrect(r["glass"]), hand)
            for shape in r["keys"]:
                self.addCursorRect_cursor_(key_path(shape, False).bounds(), hand)
            self.addCursorRect_cursor_(nsrect(r["move"]), AppKit.NSCursor.openHandCursor())
            resize = AppKit.NSCursor.crosshairCursor()
            if hasattr(AppKit.NSCursor, "frameResizeCursorFromPosition_inDirections_"):   # macOS 15 and later
                resize = AppKit.NSCursor.frameResizeCursorFromPosition_inDirections_(
                    AppKit.NSCursorFrameResizePositionBottomRight, AppKit.NSCursorFrameResizeDirectionsAll)
            self.addCursorRect_cursor_(nsrect(r["grip"]), resize)

        @objc.python_method
        def k_min(self, b=None):
            b = b or self.board()
            return MIN_GLASS / b["w"] if b else .1

        @objc.python_method
        def size_window(self, k):
            """The window around the device at k points per pixel, its top left corner in place."""
            k = max(self.k_min(), k)
            self.K = k
            g = self.geometry(k)
            win = self.window()
            gm = self.geometry(self.k_min())
            win.setMinSize_((gm["W"], gm["H"]))
            f = win.frame()
            win.setFrame_display_(((f.origin.x, f.origin.y + f.size.height - g["H"]), (g["W"], g["H"])), True)
            win.setContentAspectRatio_((g["W"], g["H"]))
            win.invalidateCursorRectsForView_(self)

        @objc.python_method
        def fit_k(self):
            """k from the window's size (an edge drag); the clamped parts settle in a pass or two."""
            w, h = self.bounds().size
            k, b = self.K, self.board()
            for _ in range(3):
                g = self.geometry(k, b)
                bw, bh = g["L"]["body"][2] / k, g["L"]["body"][3] / k       # the body per unit of k
                kw = (w - (g["W"] - g["L"]["body"][2])) / bw
                if g["m"] > max(24, 16 * k) + .5:                         # width held by the buttons: height rules
                    kw = float("inf")
                k = max(self.k_min(b), min(kw, (h - (g["H"] - g["L"]["body"][3])) / bh))
            self.K = k
            return self.geometry(k, b)

        @objc.python_method
        def layout(self):
            """The rectangles of the parts at the current size: the device, the buttons, the grip."""
            g = self.fit_k()
            b = g["b"]
            L = device_layout(b["profile"], b["w"], b["h"], b["r"], b["ppi"], g["s"], g["m"], g["mt"])
            W, H = self.bounds().size
            buttons = {"close": (W - 4 - BUTTON, 4, BUTTON, BUTTON), "shot": (W - 44 - BUTTON, 4, BUTTON, BUTTON),
                       "rec": (W - 84 - BUTTON, 4, BUTTON, BUTTON), "listen": (W - 124 - BUTTON, 4, BUTTON, BUTTON),
                       "talk": (W - 164 - BUTTON, 4, BUTTON, BUTTON)}
            grip = (W - GRIP - 5, H - GRIP - 5, GRIP, GRIP)
            move = (5, H - GRIP - 5, GRIP, GRIP)
            self.rects = dict(g=g, L=L, body=L["body"], glass=L["active"], radius=L["active_r"], keys=L["keys"],
                              buttons=buttons, grip=grip, move=move, key_names=[s["label"] for s in L["keys"]],
                              key_codes={s["label"]: s["code"] for s in L["keys"]})
            return self.rects

        # -- drawing -------------------------------------------------------------------------------------------
        def drawRect_(self, dirty):
            r = self.layout()
            AppKit.NSGraphicsContext.currentContext().setImageInterpolation_(AppKit.NSImageInterpolationHigh)
            img = self.screen_image()
            with model.lock:
                state, connected, taken = model.screen.state, model.connected, model.taken
            veil = .4 if state == "dim" else .65 if state == "dark" else 0
            if not connected:
                veil = max(veil, .5)
            text = ("another window took over, click to take it back" if taken else "connecting...") if not connected else None
            now = time.monotonic()
            with model.lock:
                led_mode = model.led
            paint_device(r["L"], r["g"]["b"]["profile"], img, self.pressed[0] if self.pressed else None, veil, text, led_mode)
            if now < self.flash_until:
                AppKit.NSGraphicsContext.saveGraphicsState()
                rounded(r["glass"], r["radius"]).addClip()
                rgb(255, 255, 255, .7 * (self.flash_until - now) / .35).set()
                fill(r["glass"])
                AppKit.NSGraphicsContext.restoreGraphicsState()
            # the buttons
            with model.lock:
                recording, listening, peak = model.rec is not None, model.listen, (model.player.peak if model.player else 0)
                talking = model.talk
            for name, br in r["buttons"].items():
                lit = (name == "rec" and recording or name == "shot" and now < self.flash_until + .4
                       or name == "listen" and listening or name == "talk" and talking)
                if name == "listen" and listening == 1:
                    self.label("mic", (br[0], br[1] + br[3] + 2, br[2], 10), 8)
                (rgb(0xeb, 0x57, 0x57) if name == "rec" and lit else rgb(0, 0x9b, 0x6d) if lit else rgb(0x3a, 0x3a, 0x40, .75)).set()
                oval(br).fill()
                cx, cy = br[0] + BUTTON / 2, br[1] + BUTTON / 2
                if name == "close":
                    self.label("✕", br, 17)
                elif name == "listen":
                    rgb(0xdd, 0xdd, 0xdd).set()
                    sp = AppKit.NSBezierPath.bezierPath()               # a speaker: a box and a cone
                    sp.moveToPoint_((cx - 8, cy - 3)); sp.lineToPoint_((cx - 4, cy - 3)); sp.lineToPoint_((cx + 1, cy - 7))
                    sp.lineToPoint_((cx + 1, cy + 7)); sp.lineToPoint_((cx - 4, cy + 3)); sp.lineToPoint_((cx - 8, cy + 3))
                    sp.closePath(); sp.fill()
                    arc = AppKit.NSBezierPath.bezierPath()
                    arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_((cx + 1, cy), 6, -40, 40)
                    arc.setLineWidth_(1.5); arc.stroke()
                    if listening and peak:
                        rgb(0, 0xcd, 0x90).set()
                        lvl = min(1.0, peak / 32768) ** .5
                        fill((br[0], br[1] + br[3] + 3, br[2] * lvl, 3))
                elif name == "talk":
                    rgb(0xdd, 0xdd, 0xdd).set()                  # a microphone: a capsule on a stand
                    oval((cx - 3.5, cy - 9, 7, 11)).fill()
                    arc = AppKit.NSBezierPath.bezierPath()
                    arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_((cx, cy - 2), 6.5, 0, 180)
                    arc.setLineWidth_(1.5); arc.stroke()
                    fill((cx - .75, cy + 4.5, 1.5, 4)); fill((cx - 4, cy + 8, 8, 1.5))
                elif name == "rec":
                    (rgb(255, 255, 255) if recording else rgb(0xeb, 0x57, 0x57)).set()
                    if recording:
                        fill((cx - 6, cy - 6, 12, 12))
                    else:
                        oval((cx - 7, cy - 7, 14, 14)).fill()
                else:
                    rgb(0xdd, 0xdd, 0xdd).set()
                    cam = rounded((cx - 9, cy - 4, 18, 12), 2)
                    cam.setLineWidth_(1.5)
                    cam.stroke()
                    lens = oval((cx - 3.5, cy - 1.5, 7, 7))
                    lens.setLineWidth_(1.5)
                    lens.stroke()
                    fill((cx - 4, cy - 7, 8, 3))
            # the toast, under the buttons
            if self.toast and now < self.toast[1]:
                attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
                         AppKit.NSForegroundColorAttributeName: rgb(0xdd, 0xdd, 0xdd)}
                ts = AppKit.NSAttributedString.alloc().initWithString_attributes_(self.toast[0], attrs)
                tw, th = ts.size()
                W = self.bounds().size.width
                box = (W - 8 - tw - 16, 4 + BUTTON + 6, tw + 16, th + 8)
                rgb(0x2a, 0x2a, 0x2e, .92).set()
                rounded(box, 6).fill()
                ts.drawAtPoint_((box[0] + 8, box[1] + 4))
            elif self.toast:
                self.toast = None
            # the two handles at the bottom: move on the left, resize on the right; always there, the only way to
            # drag the window (the body and the background do nothing, too easy to miss)
            gr = r["grip"]
            rgb(0x9a, 0x9a, 0xa0, .7).set()
            for i in (0, 5, 10):
                line = AppKit.NSBezierPath.bezierPath()
                line.moveToPoint_((gr[0] + GRIP, gr[1] + i))
                line.lineToPoint_((gr[0] + i, gr[1] + GRIP))
                line.setLineWidth_(1.2)
                line.stroke()
            mv = r["move"]
            cx, cy, h = mv[0] + GRIP / 2, mv[1] + GRIP / 2, GRIP / 2
            arrows = AppKit.NSBezierPath.bezierPath()
            arrows.setLineWidth_(1.2)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                tip = (cx + dx * h, cy + dy * h)
                arrows.moveToPoint_((cx, cy))
                arrows.lineToPoint_(tip)
                for side in (1, -1):                                # the head: two short strokes back from the tip
                    arrows.moveToPoint_(tip)
                    arrows.lineToPoint_((tip[0] - dx * 3.5 + side * dy * 3, tip[1] - dy * 3.5 + side * dx * 3))
            arrows.stroke()

        @objc.python_method
        def label(self, text, rect, size=12):
            attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(size),
                     AppKit.NSForegroundColorAttributeName: rgb(0xc9, 0xc9, 0xc9)}
            s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            sz = s.size()
            s.drawAtPoint_((rect[0] + (rect[2] - sz.width) / 2, rect[1] + (rect[3] - sz.height) / 2))

        @objc.python_method
        def screen_image(self):
            """The screen as an NSImage, rebuilt only when the pixels changed."""
            with model.lock:
                s = model.screen
                if not s.w:
                    return None
                h = (s.w, s.h, s.changes)
                if h == self.image_hash:
                    return self.image
                data = s.rgbx().tobytes()
                w, hh = s.w, s.h
            provider = Quartz.CGDataProviderCreateWithData(None, data, len(data), None)
            cg = Quartz.CGImageCreate(w, hh, 8, 32, w * 4, Quartz.CGColorSpaceCreateDeviceRGB(),
                                      Quartz.kCGImageAlphaNoneSkipLast, provider, None, True, Quartz.kCGRenderingIntentDefault)
            self.image = AppKit.NSImage.alloc().initWithCGImage_size_(cg, (w, hh))
            self.image_data = data              # the provider reads it as long as the image lives
            self.image_hash = h
            return self.image

        # -- from the network thread, on the main thread -------------------------------------------------------
        @objc.python_method
        def redraw(self):
            """A new frame: only the glass is repainted (the body with its blurred shadow is costly)."""
            if self.rects:
                g = self.rects["glass"]
                self.setNeedsDisplayInRect_(((g[0], g[1]), (g[2], g[3])))
            else:
                self.setNeedsDisplay_(True)

        @objc.python_method
        def led_changed(self):
            if self.rects and self.rects["L"]["led"]:
                r = self.rects["L"]["led"]["rect"]
                self.setNeedsDisplayInRect_(((r[0] - r[2], r[1] - r[2]), (r[2] * 3, r[3] * 3)))
            with model.lock:
                blinking = model.led in (1, 2)
            if blinking and not getattr(self, "led_timer", False):
                self.led_timer = True
                AppHelper.callLater(0.125, self.led_tick)

        @objc.python_method
        def led_tick(self):
            with model.lock:
                blinking = model.led in (1, 2)
            self.led_timer = blinking
            if self.rects and self.rects["L"]["led"]:
                r = self.rects["L"]["led"]["rect"]
                self.setNeedsDisplayInRect_(((r[0] - r[2], r[1] - r[2]), (r[2] * 3, r[3] * 3)))
            if blinking:
                AppHelper.callLater(0.125, self.led_tick)

        @objc.python_method
        def animate(self):
            """Redraws while a flash or a toast is on, then stops."""
            self.setNeedsDisplay_(True)
            if time.monotonic() < max(self.flash_until + .4, self.toast[1] if self.toast else 0):
                AppHelper.callLater(0.05, self.animate)

        @objc.python_method
        def notice(self, text, flash=False):
            now = time.monotonic()
            if flash:
                self.flash_until = now + .35
            self.toast = (text, now + 2.5)
            self.animate()

        @objc.python_method
        def screen_resized(self):
            """A board arrived, or its screen changed shape: the window at the kept share of the real size."""
            ctl.zoom_to(ctl.zoom, say=False)
            self.setNeedsDisplay_(True)

        # -- mouse ---------------------------------------------------------------------------------------------
        @objc.python_method
        def where(self, event):
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
            r = self.rects or self.layout()
            for name, br in r["buttons"].items():
                if br[0] <= p.x <= br[0] + br[2] and br[1] <= p.y <= br[1] + br[3]:
                    return name, p
            for name in ("grip", "move"):
                gr = r[name]
                if gr[0] - 3 <= p.x <= gr[0] + gr[2] + 3 and gr[1] - 3 <= p.y <= gr[1] + gr[3] + 3:
                    return name, p
            for shape in r["keys"]:
                if key_hit(shape, p):
                    return shape["label"], p
            if self.on_glass(p, r):
                return "glass", p
            return "body", p

        @objc.python_method
        def on_glass(self, p, r):
            gx, gy, gw, gh = r["glass"]
            if not (gx <= p.x <= gx + gw and gy <= p.y <= gy + gh):
                return False
            rad = r["radius"]
            cx = min(max(p.x, gx + rad), gx + gw - rad)
            cy = min(max(p.y, gy + rad), gy + gh - rad)
            return (p.x - cx) ** 2 + (p.y - cy) ** 2 <= rad * rad

        @objc.python_method
        def screen_xy(self, p):
            gx, gy, gw, gh = self.rects["glass"]
            g = self.rects["g"]
            x = int(round((p.x - gx) * g["cw"] / gw))
            y = int(round((p.y - gy) * g["ch"] / gh))
            return max(0, min(g["cw"] - 1, x)), max(0, min(g["ch"] - 1, y))

        def mouseDown_(self, event):
            what, p = self.where(event)
            with model.lock:
                client, taken = model.client, model.taken
            if what == "glass":
                if taken:
                    with model.lock:
                        model.taken = False      # the network thread reconnects
                    return
                if client:
                    self.down = True
                    x, y = self.screen_xy(p)
                    client.pointer(x, y, True)
            elif what == "grip":
                self.grip_drag = (event.locationInWindow().x, self.K, self.bounds().size.width)
            elif what == "move":
                self.window().performWindowDragWithEvent_(event)
            elif what in ("close", "shot", "rec", "listen", "talk") or what in self.rects["key_codes"]:
                self.pressed = (what, time.monotonic())
                if what in self.rects["key_codes"] and self.rects["g"]["b"]["profile"]["hold"] and client:
                    client.key_state(self.rects["key_codes"][what], True)
                self.setNeedsDisplay_(True)

        def mouseDragged_(self, event):
            if self.down:
                with model.lock:
                    client = model.client
                if client:
                    x, y = self.screen_xy(self.convertPoint_fromView_(event.locationInWindow(), None))
                    client.pointer(x, y, True)
            elif self.grip_drag:
                x0, k0, w0 = self.grip_drag
                self.size_window(k0 * (w0 + event.locationInWindow().x - x0) / w0)
                self.setNeedsDisplay_(True)

        def mouseUp_(self, event):
            if self.down:
                self.down = False
                with model.lock:
                    client = model.client
                if client:
                    x, y = self.screen_xy(self.convertPoint_fromView_(event.locationInWindow(), None))
                    client.pointer(x, y, False)
            if self.grip_drag:
                ctl.resized()
            self.grip_drag = None
            pressed, self.pressed = self.pressed, None
            if not pressed:
                return
            self.setNeedsDisplay_(True)
            what, t0 = pressed
            held = time.monotonic() - t0
            if what == "close":
                AppKit.NSApp.terminate_(None)
            elif what == "shot":
                ctl.shot_(None)
            elif what == "rec":
                ctl.rec_(None)
            elif what == "listen":
                ctl.sound_(None)
            elif what == "talk":
                ctl.talk_(None)
            elif self.rects["g"]["b"]["profile"]["hold"]:
                with model.lock:
                    client = model.client
                if client:
                    client.key_state(self.rects["key_codes"][what], False)
            else:
                ctl.send_key(self.rects["key_codes"][what], held)

        def scrollWheel_(self, event):
            with model.lock:
                client = model.client
            if not client or not self.rects:
                return
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
            if not self.on_glass(p, self.rects):
                return
            mult = 1 if event.hasPreciseScrollingDeltas() else 40
            self.wheel_acc[0] -= event.scrollingDeltaX() * mult / self.K
            self.wheel_acc[1] -= event.scrollingDeltaY() * mult / self.K
            self.wheel_acc[2], self.wheel_acc[3] = self.screen_xy(p)
            if not self.wheel_timer:
                self.wheel_timer = True
                AppHelper.callLater(0.04, self.wheel_flush)

        @objc.python_method
        def wheel_flush(self):
            self.wheel_timer = False
            dx, dy = int(self.wheel_acc[0]), int(self.wheel_acc[1])
            self.wheel_acc[0] -= dx
            self.wheel_acc[1] -= dy
            dx, dy = max(-32767, min(32767, dx)), max(-32767, min(32767, dy))
            with model.lock:
                client = model.client
            if client and (dx or dy):
                client.wheel(self.wheel_acc[2], self.wheel_acc[3], dx, dy)

        # -- keyboard ------------------------------------------------------------------------------------------
        def keyDown_(self, event):
            if event.modifierFlags() & AppKit.NSEventModifierFlagCommand:
                return objc.super(DeviceView, self).keyDown_(event)
            ch = event.charactersIgnoringModifiers()
            codes = self.rects.get("key_codes", {}).values() if self.rects else ()
            if ch in ("1", "2", "3") and int(ch) in codes and not event.isARepeat():
                self.key_t0[ch] = time.monotonic()
                if self.rects["g"]["b"]["profile"]["hold"]:
                    with model.lock:
                        client = model.client
                    if client:
                        client.key_state(int(ch), True)

        def keyUp_(self, event):
            ch = event.charactersIgnoringModifiers()
            t0 = self.key_t0.pop(ch, None)
            if t0 is None:
                return
            if self.rects["g"]["b"]["profile"]["hold"]:
                with model.lock:
                    client = model.client
                if client:
                    client.key_state(int(ch), False)
            else:
                ctl.send_key(int(ch), time.monotonic() - t0)

    class Controller(AppKit.NSObject):
        """Menu actions, the statistics line, and what both the view and the menus do."""

        @objc.python_method
        def send_key(self, k, held):
            with model.lock:
                client = model.client
            if client:
                client.key(4 if k == 3 and held > 1.5 else 5 if k == 1 and held > .7 else 6 if k == 2 and held > .7 else k)

        def shot_(self, sender):
            path = desktop_path("png")
            with model.lock:
                ok = bool(model.screen.w)
                if ok:
                    model.screen.png(path)
            if ok:
                print("saved", path, flush=True)
                view.notice("saved " + os.path.basename(path), flash=True)
            else:
                view.notice("no frame yet")

        def rec_(self, sender):
            with model.lock:
                on = model.rec is not None
            if on:
                record_stop()
                view.notice("recording stopped, encoding...")
            else:
                record_start()
                view.notice("recording")
            view.setNeedsDisplay_(True)

        @objc.python_method
        def listen_to(self, source):
            """A source of the board's audio on the Mac's default output, or none: asks the board, plays what comes."""
            with model.lock:
                source = 0 if model.listen == source else source
                model.listen = source
                client = model.client
                if source and not model.player:
                    try:
                        model.player = Player()
                    except Exception as e:                     # no sounddevice or no output device
                        model.listen = 0
                        view.notice(f"no audio output: {e}")
                        return
                player = model.player
            if source:
                player.start()
            elif player:
                player.stop()
            if client:
                client.listen(source)
            self.audio_items["sound:"].setState_(AppKit.NSControlStateValueOn if source == 2 else AppKit.NSControlStateValueOff)
            self.audio_items["listen:"].setState_(AppKit.NSControlStateValueOn if source == 1 else AppKit.NSControlStateValueOff)
            view.notice({2: "hearing what the board plays", 1: "hearing the board's mics"}.get(source, "stopped listening"))
            view.setNeedsDisplay_(True)

        def sound_(self, sender):
            self.listen_to(2)

        def listen_(self, sender):
            self.listen_to(1)

        def talk_(self, sender):
            """The Mac's default input to the board's speaker, 20 ms at a time."""
            with model.lock:
                on = not model.talk
                model.talk = on
                client = model.client
                if on and not model.capture:
                    try:
                        model.capture = Capture(self.speak)
                    except Exception as e:
                        model.talk = False
                        view.notice(f"no audio input: {e}")
                        return
                capture = model.capture
            if client:
                client.talk(on)
            if on:
                capture.start()
            else:
                capture.stop()
            self.audio_items["talk:"].setState_(AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff)
            view.notice("the Mac's mic plays on the board" if on else "stopped talking")

        @objc.python_method
        def speak(self, pcm):
            with model.lock:
                client = model.client if model.talk else None
            if client:
                client.speak(pcm)

        def bigger_(self, sender):
            self.zoom_to(self.zoom * 1.25)

        def smaller_(self, sender):
            self.zoom_to(self.zoom * .8)

        def screenChanged_(self, note):
            """The window moved to another display, where a point is another length: the same share of the
            real size there."""
            self.zoom_to(self.zoom, say=False)

        @objc.python_method
        def resized(self):
            """After a drag of the grip: the share of the real size this came to."""
            self.zoom = view.K / self.k_actual()

        @objc.python_method
        def k_actual(self):
            """k at which the device is its real size on the display the window is on: the screen's millimetres
            through that display's points per millimetre (its frame in points over its size as it reports it)."""
            b = view.board()
            if not b:
                return scale
            screen = win.screen() or AppKit.NSScreen.mainScreen()
            mm = Quartz.CGDisplayScreenSize(screen.deviceDescription()["NSScreenNumber"])
            prof = b["profile"]
            screen_mm = prof["screen_mm"] or (b["w"] / b["ppi"] * 25.4 if b["ppi"] else 40.0)
            if mm.width <= 0:
                return scale
            k = screen.frame().size.width / mm.width * screen_mm / b["w"]
            if os.environ.get("MIRROR_DEBUG"):
                print(f"real size on {screen.localizedName()}: {screen.frame().size.width:.0f} pt over "
                      f"{mm.width:.0f} mm, k {k:.3f}", flush=True)
            return k

        @objc.python_method
        def zoom_to(self, zoom, say=True):
            """The size as a share of the real size, kept across displays: the same share there."""
            self.zoom = zoom
            view.size_window(zoom * self.k_actual())
            self.zoom = view.K / self.k_actual()                 # after the floor
            if say:
                view.notice("actual size" if abs(self.zoom - 1) < .02 else f"{self.zoom * 100:.0f} % of actual")

        def actual_(self, sender):
            self.zoom_to(1.0)

        def top_(self, sender):
            on = win.level() != AppKit.NSFloatingWindowLevel
            win.setLevel_(AppKit.NSFloatingWindowLevel if on else AppKit.NSNormalWindowLevel)
            sender.setState_(AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff)

        def tick_(self, timer):
            with model.lock:
                f, b, a = model.frames, model.bytes, model.audio_bytes
                model.frames = model.bytes = model.audio_bytes = 0
                state, connected, client = model.screen.state, model.connected, model.client
            self.ticks = getattr(self, "ticks", 0) + 1
            if client and self.ticks % 5 == 0:
                client.keepalive()
            self.refresh_icon()
            if a:
                view.setNeedsDisplay_(True)                       # the level bar
            audio = f" · {'mic' if model.listen == 1 else 'sound'} {a / 1024:.0f} KB/s" if a else ""
            print(f"{time.strftime('%H:%M:%S')} {'live' if connected else 'offline'} · {f} updates/s · {b / 1024:.0f} KB/s{audio} · screen {state}", flush=True)

        @objc.python_method
        def refresh_icon(self):
            """The Dock icon follows the screen: set once at start, then again whenever the pixels changed."""
            img = view.screen_image()
            if getattr(self, "icon_set", False) and view.image_hash == self.icon_hash:
                return
            self.icon_set, self.icon_hash = True, view.image_hash
            AppKit.NSApp.setApplicationIconImage_(icon_image(img, view.board()))


    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    ctl = Controller.alloc().init()
    ctl.zoom = scale
    ctl.audio_items = {}

    # menus: the app's and View
    bar = AppKit.NSMenu.alloc().init()
    app_item = bar.addItemWithTitle_action_keyEquivalent_("Mirror", None, "")
    app_menu = AppKit.NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit Mirror", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    view_item = bar.addItemWithTitle_action_keyEquivalent_("View", None, "")
    view_menu = AppKit.NSMenu.alloc().initWithTitle_("View")
    for title, sel, key in (("Screenshot", "shot:", "s"), ("Record / Stop", "rec:", "r"), (None, None, None),
                            ("Sound: hear what the board plays", "sound:", "l"), ("Mic: hear the board's mics", "listen:", "L"),
                            ("Talk: the Mac's mic to the board's speaker", "talk:", "k"), (None, None, None),
                            ("Bigger", "bigger:", "="), ("Smaller", "smaller:", "-"), ("Actual size", "actual:", "0"),
                            (None, None, None), ("Float on Top", "top:", "t")):
        if title is None:
            view_menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        item = view_menu.addItemWithTitle_action_keyEquivalent_(title, sel, key)
        item.setTarget_(ctl)
        if sel == "top:":
            item.setState_(AppKit.NSControlStateValueOn if on_top else AppKit.NSControlStateValueOff)
        if sel in ("sound:", "listen:", "talk:"):
            ctl.audio_items[sel] = item
    view_item.setSubmenu_(view_menu)
    app.setMainMenu_(bar)

    # the window and the view
    view = DeviceView.alloc().initWithFrame_(((0, 0), (560, 580))).setup()
    g = view.geometry(scale)
    win = Window.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (g["W"], g["H"])), AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskResizable,
        AppKit.NSBackingStoreBuffered, False)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.setHasShadow_(False)
    win.setMovableByWindowBackground_(False)
    win.setContentView_(view)
    win.setMinSize_((120, 120))
    win.setContentAspectRatio_((g["W"], g["H"]))
    win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
                               AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
    if on_top:
        win.setLevel_(AppKit.NSFloatingWindowLevel)
    win.center()
    win.makeKeyAndOrderFront_(None)
    win.makeFirstResponder_(view)
    app.activate()
    ctl.refresh_icon()

    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, ctl, "tick:", None, True)
    AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        ctl, "screenChanged:", AppKit.NSWindowDidChangeScreenNotification, win)
    threading.Thread(target=net_loop, args=(view,), daemon=True).start()
    if sound:
        ctl.listen_to(2)
    if selftest:
        """A screenshot, a short recording and every zoom step, then quit: exercises the drawing paths an
        exception would kill silently (pyobjc turns it into an uncaught ObjC exception)."""
        AppHelper.callLater(5, ctl.shot_, None)
        AppHelper.callLater(6, ctl.rec_, None)
        AppHelper.callLater(7, ctl.bigger_, None)
        AppHelper.callLater(8, ctl.smaller_, None)
        AppHelper.callLater(9, ctl.rec_, None)
        AppHelper.callLater(12, lambda: (print("selftest done", flush=True), AppKit.NSApp.terminate_(None)))
    AppHelper.runEventLoop()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("host", help="the board's IP address or host name")
    ap.add_argument("--scale", type=float, default=1.0, help="the size as a share of the real size (default 1)")
    ap.add_argument("--on-top", action="store_true", help="start floating above other windows (View menu toggles it)")
    ap.add_argument("--sound", action="store_true", help="start with View > Sound on: the board's clicks and cues play here")
    ap.add_argument("--stats", action="store_true", help="no window: frame statistics from the TCP port")
    ap.add_argument("--seconds", type=float, default=5, help="with --stats: how long to listen (default 5)")
    ap.add_argument("--png", help="with --stats: write the screen here once it has been still for half a second")
    ap.add_argument("--selftest", action="store_true", help="screenshot, a short recording and the zoom steps, then quit")
    a = ap.parse_args()
    if a.stats:
        stats(a.host, a.seconds, a.png)
        return
    try:
        import AppKit  # noqa: F401
    except ImportError:
        sys.exit("pyobjc is not installed: .venv/bin/pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz")
    run_window(a.host, a.scale, a.on_top, a.selftest, a.sound)


if __name__ == "__main__":
    main()
