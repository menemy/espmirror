#!/usr/bin/env python3
# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0


"""The live screen of espmirror boards, drawn as the boards themselves: tools/espmirror.py [HOST[:PORT] | PORT ...]

For boards built with CONFIG_ESPMIRROR_USB=y (docs/protocol.md).
A board is reached on its USB console (a serial port, /dev/cu.usbmodem101 or
COM5). Without arguments it opens only boards on USB Serial/JTAG ports.
--wifi also discovers boards announcing _musemirror._tcp; a board found on
both uses its cable. An explicit address connects over the network.
Network viewing requires CONFIG_ESPMIRROR_TCP=y on the board; it is off
by default. Several boards share one window, side by side or in a grid.

Each board is drawn at its real size from what it reports (the screen, round
or not, its diagonal): a board this file knows by its body, finish and photo
(--devices), any other as a plain body around its screen. Its buttons stand
out of the body's edge beside the screen edge where the board draws their
hints. The mouse on the glass is a finger (touch boards), and so are two
fingers on a trackpad over it (pages slide, lists scroll), shown as a fading
ring (View > Show touches); the drawn buttons, or the keys
1 through 4 are the board's advertised keys, held as long as the mouse
or key (or tapped on release for a tap-only profile); arrows, Enter and Esc drive boards with menu keys. The board
under the mouse gets the keys. The body and the arrows button at the top move
the window; any corner resizes it.

Above the device: talk and listen (QtMultimedia), record, screenshot and
close. Screenshots (each screen at its own pixels, and the devices as drawn,
without the window's buttons and handles) and recordings (the devices, mp4
through ffmpeg) go to the Desktop as mirror-<time>.*, unless changed in Settings.

  Ctrl+S (Cmd on a Mac)  screenshot
  Ctrl+R                 record, again to stop
  Ctrl+0 / + / -         actual size / bigger / smaller
  Ctrl+T                 float on top
  Ctrl+,                 settings (saved between launches)
  right click            the same, as a menu (on a Mac also the View menu)

  --list         print USB boards and exit; add --wifi for network boards
  --settings     edit preferences without connecting to boards
  --wifi         include mDNS discovery (network firmware opt-in required)
  --log FILE     append timestamped cable console logs to FILE
  --png FILE     save the first board's screen once all of it has arrived, and exit
  --scale K      start at K times the real size
  --devices DIR  device photos (default: devices/ next to this file)

pip install PySide6 zeroconf numpy pyserial
"""
import argparse
import errno
import importlib.util
import json
import math
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib

import numpy as np

PORT = 7331
HELLO = b"MIRROR1\n"
MARK = b"\0MIRROR1\n"     # the board's console turned into the mirror's link
BREAK = b"\xff" * 4        # not a message length: ends a cable session another viewer left open
DUMP = os.environ.get("MIRROR_DUMP")    # a file: the cable's last bytes each time it loses its framing
SERVICE = "_musemirror._tcp.local."
ROLES = {1: "talk", 2: "aux", 3: "power", 4: "speaker"}

# What a board looks like: (a fragment of the name the board reports, a profile).
# The first fragment found in the name, standing on its own ("1.75" is not in
# "1.75C"), picks the profile. Lengths in millimetres, from the makers' outline
# drawings, x to the right and y down from the body's top left corner.
#   screen_mm  the active area's width (the lit pixels; their count comes from the board)
#   body       shape="rect": w, h; x, y the active area's top left; r the corner radius
#              shape="round": d the diameter, the active area centred
#              finish: a name in FINISHES (the colours of the body and its buttons)
#   glass      border: the cover glass's dark border around the active area; r its corner radius
#   led        optional: x, y its centre, d its diameter, colour (r, g, b)
#   studs      optional: d the diameter of the corner holes, at their centres (x, y)
#   photo      optional: file, a front-on picture with the background cut out (PNG with alpha) in the
#              --devices directory, stretched to the body's w x h (d x d); screen the active area's
#              x, y, w, h in its pixels. The live screen goes there instead of at body x, y.
# A board whose w x h lies the other way from the profile's active area gets the profile turned a quarter
# counter-clockwise (its bottom edge on the right). The buttons are the board's own, from its hints.
DEVICES = [('2.16',
  {'screen_mm': 38.99,
   'body': {'shape': 'rect', 'w': 44.6, 'h': 44.6, 'x': 2.8, 'y': 2.8, 'r': 5.5, 'finish': 'white'},
   'glass': {'border': 2.15, 'r': 4.5},
   'photo': {'file': 'waveshare-2.16.png', 'screen': (54, 54, 801, 807)},
   'keys': [('key3', 1, 'top', 12.3, 5.3), ('pwr', 3, 'top', 22.3, 5.3), ('boot', 2, 'top', 32.3, 5.3)],
   'hold': True}),
 ('1.54G',
  {'screen_mm': 27.8,
   'body': {'shape': 'rect', 'w': 39.8, 'h': 53.0, 'x': 6.0, 'y': 10.9, 'r': 4.5, 'finish': 'white'},
   'glass': {'border': 0.8, 'r': 0.6},
   'led': {'x': 5.2, 'y': 45.0, 'd': 1.6, 'colour': (48, 208, 96)},
   'photo': {'file': 'waveshare-1.54g.png', 'screen': (125, 94, 526, 434)},
   'keys': [('boot', 2, 'right', 31.1, 5.5), ('pwr', 3, 'right', 41.3, 5.5)],
   'hold': True}),
 ('3.49',
  {'screen_mm': 84.0,
   'body': {'shape': 'rect', 'w': 98.5, 'h': 34.8, 'x': 7.25, 'y': 6.11, 'r': 4.0, 'finish': 'blue'},
   'glass': {'border': 3.2, 'r': 2.5},
   'photo': {'file': 'waveshare-3.49b.png', 'screen': (59, 44, 874, 603)},
   'keys': [('boot', 2, 'bottom', 40.0, 5.0), ('pwr', 3, 'bottom', 49.0, 5.0)],
   'hold': True}),
 ('3.5',
  {'screen_mm': 73.44,
   'body': {'shape': 'rect', 'w': 95.11, 'h': 63.67, 'x': 10.84, 'y': 7.34, 'r': 7.3, 'finish': 'black'},
   'glass': {'border': 2.5, 'r': 4.0},
   'photo': {'file': 'waveshare-p4-3.5.png', 'screen': (49, 34, 894, 624)},
   'keys': [('pwr', 3, 'bottom', 21.0, 4.0), ('boot', 2, 'bottom', 40.0, 4.0)],
   'hold': False}),
 ('1.75C',
  {'screen_mm': 43.76,
   'body': {'shape': 'round', 'd': 55.0, 'finish': 'silver'},
   'glass': {'border': 2.6, 'r': 0},
   'photo': {'file': 'waveshare-1.75c.png', 'screen': (86, 85, 693, 684)},
   'keys': [('key3', 1, 'angle', 50, 7.0), ('boot', 2, 'angle', 130, 5.0)],
   'hold': True}),
 ('AIPI',
  {'screen_mm': 25.9,
   'body': {'shape': 'rect', 'w': 55.5, 'h': 47.5, 'x': 14.8, 'y': 5.0, 'r': 5.0, 'finish': 'red'},
   'studs': {'d': 2.4, 'at': ((6.0, 6.0), (49.5, 6.0), (6.0, 41.5), (49.5, 41.5))},
   'glass': {'border': 1.0, 'r': 1.0},
   'photo': {'file': 'aipi-lite.png', 'screen': (216, 70, 458, 398)},
   'keys': [('pwr', 3, 'face', (17.6, 43.2), 6.2), ('boot', 2, 'face', (37.8, 43.2), 6.2)],
   'hold': True})]
GENERIC = dict(screen_mm=None, body=dict(shape="rect", w=None, h=None, x=3.0, y=3.0, r=3.0, finish="white"),
               glass=dict(border=1.5, r=2.0))
GENERIC_ROUND = dict(screen_mm=None, body=dict(shape="round", d=None, x=3.0, finish="white"),
                     glass=dict(border=1.5, r=0))
FINISHES = {
    "white": dict(fill=(0xf2, 0xf2, 0xee), fill2=(0xe4, 0xe4, 0xdf), edge=(0xc4, 0xc4, 0xbe), label=(0x8a, 0x8a, 0x84),
                  key=((0xee, 0xee, 0xea), (0xc2, 0xc2, 0xbc)), key_down=((0xd4, 0xd4, 0xcf), (0xa8, 0xa8, 0xa2))),
    "silver": dict(fill=(0xdd, 0xdd, 0xe0), fill2=(0xa4, 0xa4, 0xaa), edge=(0x7a, 0x7a, 0x80), label=(0x50, 0x50, 0x56),
                   key=((0xe2, 0xe2, 0xe5), (0x9e, 0x9e, 0xa4)), key_down=((0xc0, 0xc0, 0xc4), (0x80, 0x80, 0x86))),
    "red": dict(fill=(0xe0, 0x30, 0x2e), fill2=(0xc4, 0x22, 0x22), edge=(0x8e, 0x16, 0x16), label=(0xff, 0xdc, 0xd8),
                key=((0x30, 0x30, 0x32), (0x18, 0x18, 0x1a)), key_down=((0x22, 0x22, 0x24), (0x10, 0x10, 0x12))),
    "blue": dict(fill=(0x2c, 0x9c, 0xf0), fill2=(0x1f, 0x7f, 0xcc), edge=(0x16, 0x5a, 0x96), label=(0xe6, 0xf2, 0xff),
                 key=((0x2c, 0x9c, 0xf0), (0x1f, 0x7f, 0xcc)), key_down=((0x1f, 0x7f, 0xcc), (0x16, 0x5a, 0x96))),
    "black": dict(fill=(0x2e, 0x2e, 0x30), fill2=(0x1c, 0x1c, 0x1e), edge=(0x0c, 0x0c, 0x0e), label=(0xa0, 0xa0, 0xa6),
                  key=((0x3a, 0x3a, 0x3c), (0x20, 0x20, 0x22)), key_down=((0x28, 0x28, 0x2a), (0x14, 0x14, 0x16))),
}


def device_profile(name):
    """The profile whose fragment stands on its own in the name ("1.75" is not "1.75C"), else GENERIC."""
    for fragment, profile in DEVICES:
        if re.search(r"(?<![0-9A-Za-z.])" + re.escape(fragment) + r"(?![0-9A-Za-z])", name or ""):
            return profile
    return GENERIC


def turned(prof, w, h):
    """The profile as the board runs its screen: a quarter turn counter-clockwise (its bottom edge on the
    right) when the board's w x h and the profile's active area lie the other way."""
    body = prof["body"]
    if not prof.get("screen_mm") or body["shape"] != "rect" or not body.get("w"):
        return prof
    inner = (body["w"] - 2 * body["x"]) / max(1e-6, body["h"] - 2 * body["y"])
    if not ((w / h > 1.15 and inner < 1 / 1.15) or (w / h < 1 / 1.15 and inner > 1.15)):
        return prof
    W, along = body["w"], prof["screen_mm"]          # the active width runs along the board's h
    nb = dict(body, w=body["h"], h=W, x=body["y"], y=W - body["x"] - along)
    out = dict(prof, body=nb, screen_mm=along * w / h, turned=True)
    if prof.get("studs"):
        out["studs"] = dict(prof["studs"], at=[(y, W - x) for x, y in prof["studs"]["at"]])
    if prof.get("led"):
        out["led"] = dict(prof["led"], x=prof["led"]["y"], y=W - prof["led"]["x"])
    if prof.get("keys"):
        keys = []
        for label, role, edge, pos, length in prof["keys"]:
            if edge == "face":
                pos = (pos[1], W - pos[0])
            elif edge == "angle":
                pos = (pos - 90) % 360
            else:
                if edge in ("top", "bottom"):
                    pos = W - pos
                edge = {"top": "left", "bottom": "right", "right": "top", "left": "bottom"}[edge]
            keys.append((label, role, edge, pos, length))
        out["keys"] = keys
    return out


def board_look(info):
    """The board's 'B' as the window draws it: sizes, buttons and its profile, turned to its screen."""
    w, h = max(1, int(info.get("w") or 1)), max(1, int(info.get("h") or 1))
    diag = float(info.get("diag") or 0)
    prof = device_profile(info.get("name"))
    if prof is GENERIC and info.get("round"):
        prof = GENERIC_ROUND
    return dict(name=info.get("name") or "", w=w, h=h, round=bool(info.get("round")),
                ppi=(min(w, h) if info.get("round") else math.hypot(w, h)) / diag if diag > 0 else 0,
                touch=bool(info.get("touch")),
                buttons=[b for b in info.get("buttons") or [] if isinstance(b, dict) and 1 <= b.get("role", 0) <= 10],
                profile=turned(prof, w, h))


def is_port(target):
    return target.startswith("/dev/") or re.fullmatch(r"COM\d+", target.removeprefix("\\\\.\\"), re.I) is not None


SYNC = b"\xa5\x5a"
CABLE_MAX = 65536


def cable_frame(msg):
    body = struct.pack('<I', len(msg)) + msg
    return SYNC + body + struct.pack('<I', zlib.crc32(body))


class ConsoleLog:
    def __init__(self, port, file=None):
        self.port, self.file = port, file
        self.pending = bytearray()
        self.discarding = False

    def feed(self, data):
        self.pending += data
        while b'\n' in self.pending or len(self.pending) > 4096:
            end = self.pending.find(b'\n')
            end = end + 1 if 0 <= end < 4096 else min(4096, len(self.pending))
            raw = bytes(self.pending[:end])
            if self.discarding:
                del self.pending[:end]
                self.discarding = not raw.endswith(b'\n')
                continue
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError as error:
                if end == 4096 and error.reason == 'unexpected end of data':
                    end = error.start       # leave a split UTF-8 character for the next chunk
                    text = raw[:end].decode('utf-8')
                else:
                    del self.pending[:end]
                    self.discarding = not raw.endswith(b'\n')
                    continue
            del self.pending[:end]
            if any(not c.isprintable() and c not in '\t\r\n\x1b' for c in text):
                self.discarding = not raw.endswith(b'\n')
                continue                    # a reconnect can start in the middle of an old binary frame
            line = '[%s %s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), self.port,
                                   text.rstrip('\r\n'))
            with _LOG_LOCK:
                print(line, flush=True)
                if self.file:
                    try:
                        with open(self.file, 'a', encoding='utf-8') as out:
                            out.write(line + '\n')
                    except OSError as error:
                        print('console log disabled (%s): %s' % (self.file, error), file=sys.stderr, flush=True)
                        self.file = None

    def flush(self):
        if self.pending:
            self.feed(b'\n')


_LOG_LOCK = threading.Lock()


class CableDemux:
    """Incremental bounded cable framing; a partial frame expires after the board's send deadline."""
    def __init__(self, on_text, on_error):
        self.buffer = bytearray()
        self.on_text, self.on_error = on_text, on_error
        self.since = None
        self.recovering = False
        self.fragment = bytearray()
        self.line_start = False
        self.marker = None
        self.marked = False

    def text(self, data):
        if not self.recovering:
            self.on_text(data)
        else:
            # Only complete text lines can be recovered from a damaged binary payload.
            self.fragment += data
            while b'\n' in self.fragment:
                line, _, rest = self.fragment.partition(b'\n')
                self.fragment = bytearray(rest)
                if self.line_start and line and all(c in (9, 13) or 32 <= c < 127 for c in line):
                    self.on_text(bytes(line) + b'\n')
                self.line_start = True
            if len(self.fragment) > 4096:
                self.fragment.clear()
                self.line_start = False

    def feed(self, data, now=None):
        now = time.monotonic() if now is None else now
        self.buffer += data
        messages = []
        while self.buffer:
            start = self.buffer.find(SYNC)
            marker = self.buffer.find(self.marker) if self.marker else -1
            if marker >= 0 and (start < 0 or marker < start):
                self.text(bytes(self.buffer[:marker]))
                del self.buffer[:marker + len(self.marker)]
                self.marked = True
                break
            if start < 0:
                keep = 0
                for prefix in (SYNC, self.marker):
                    if prefix:
                        for size in range(1, len(prefix)):
                            if self.buffer.endswith(prefix[:size]):
                                keep = max(keep, size)
                n = len(self.buffer) - keep
                self.text(bytes(self.buffer[:n]))
                del self.buffer[:n]
                self.since = None
                break
            if start:
                self.text(bytes(self.buffer[:start]))
                del self.buffer[:start]
                self.since = None
            if self.since is None:
                self.since = now
            if len(self.buffer) < 6:
                if now - self.since <= 3:
                    break
                valid_length = False
            else:
                n, = struct.unpack_from('<I', self.buffer, 2)
                valid_length = 0 < n <= CABLE_MAX
            if valid_length:
                end = 6 + n
                if len(self.buffer) < end + 4 and now - self.since <= 3:
                    break
                if len(self.buffer) >= end + 4:
                    crc, = struct.unpack_from('<I', self.buffer, end)
                    if zlib.crc32(self.buffer[2:end]) == crc:
                        messages.append(bytes(self.buffer[6:end]))
                        del self.buffer[:end + 4]
                        self.since = None
                        self.recovering = False
                        self.fragment.clear()
                        self.line_start = False
                        continue
            if not self.recovering:
                self.on_error()
            self.recovering = True
            del self.buffer[:2]
            self.since = None
        return messages


def serial_open_error(port, error):
    message = "Cannot open %s: %s. Close idf.py monitor, serial terminals, or another viewer using this port." % (port, error)
    if sys.platform.startswith("linux") and (getattr(error, "errno", None) in (errno.EACCES, errno.EPERM)
                                              or "permission" in str(error).lower()):
        message += " Check the device's group with ls -l %s; join dialout (or uucp on your distribution), then log out and back in." % port
    elif getattr(error, "errno", None) in (errno.ENOENT, errno.ENODEV):
        message += " Reconnect the USB cable and select the board's current port."
    return ConnectionError(message)


def keep_serial_lines_on_close(ser):
    """Keep POSIX close from changing the USB control lines."""
    if os.name != "posix":
        return
    try:
        import termios
        fd = ser.fileno()
        settings = termios.tcgetattr(fd)
        settings[2] = settings[2] & ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, settings)
    except (ImportError, AttributeError, OSError):
        return


def open_usb_serial(port):
    """Open native USB Serial/JTAG without requesting a reset."""
    try:
        import serial
    except ImportError as e:
        raise ConnectionError("USB mirror needs pyserial: python3 -m pip install pyserial") from e
    ser = serial.Serial(port=None, baudrate=115200, timeout=0.5, write_timeout=2,
                        rtscts=False, dsrdtr=False, xonxoff=False, exclusive=True)
    try:
        # POSIX applies DTR before RTS: don't drop DTR while RTS may still be asserted.
        ser.dtr = True
        ser.rts = False
        ser.port = port
        ser.open()
        keep_serial_lines_on_close(ser)
        ser.rts = False
        ser.dtr = False
        ser.rts = False       # usbser.sys may defer DTR until the next RTS update.
        return ser
    except OSError as e:
        ser.close()
        raise serial_open_error(port, e) from e
    except BaseException:
        ser.close()
        raise


def usb_serial_mac(value):
    """Only a complete MAC; Windows composite-device instance IDs are not MACs."""
    value = (value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{12}", value):
        digits = value.lower()
    elif re.fullmatch(r"[0-9a-fA-F]{2}([:_-])[0-9a-fA-F]{2}(?:\1[0-9a-fA-F]{2}){4}", value):
        digits = re.sub(r"[:_-]", "", value).lower()
    else:
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def open_serial(port, wait=5.0, on_text=None, demux=None):
    """The board's console as the mirror's link: ">mirror", then everything after the marker is the protocol."""
    ser = open_usb_serial(port)
    ser.timeout = 0.1
    on_text = on_text or (lambda data: None)
    demux = demux or CableDemux(on_text, lambda: None)
    try:
        ser.write(BREAK)
        time.sleep(0.5)              # longer than the pause the board waits for before it reads commands again
        started = time.monotonic()
        demux.feed(ser.read(ser.in_waiting), now=started)
        ser.write(b"\n>mirror\n")
        demux.marker = MARK           # a marker inside a stale CRC-framed payload is not the new handshake
        end = started + wait
        while not demux.marked:
            now = time.monotonic()
            if now > end:
                demux.feed(b'', now=now)
                raise ConnectionError("No mirror reply on %s within %.1f s. Check that this is the board's USB Serial/JTAG port, "
                                      "flash firmware built with CONFIG_ESPMIRROR_USB=y or CONFIG_ESPMIRROR_USB_NETWORK=y, and close idf.py monitor or other viewers." % (port, wait))
            demux.feed(ser.read(1), now=now)
        demux.marker = None
        demux.marked = demux.recovering = False
        demux.since = None
        demux.fragment.clear()
        demux.line_start = False
        return ser
    except BaseException:
        ser.close()
        raise


def usb_boards():
    """USB descriptors only; never open a console during discovery."""
    try:
        from serial.tools import list_ports
    except ImportError:
        print("USB discovery needs pyserial: python3 -m pip install pyserial", file=sys.stderr)
        return []
    return [(p.device, usb_serial_mac(p.serial_number)) for p in list_ports.comports() if is_board_port(p)]


def is_board_port(port):
    """USB Serial/JTAG, or a TinyUSB serial port that names itself espmirror (CONFIG_ESPMIRROR_USB_NETWORK).
    Where the system gives no names (Windows), any TinyUSB serial port."""
    if port.vid != 0x303a:
        return False
    if port.pid == 0x1001:
        return True
    names = (getattr(port, "product", None), getattr(port, "interface", None))
    if any("esp-iris" in (name or "").lower() for name in names):
        return False
    if names == (None, None):
        # pyserial on Windows reports neither string, so every TinyUSB serial port is a candidate;
        # one that is not a mirror never answers the handshake.
        return 0x4000 <= (port.pid or 0) <= 0x40ff
    return any("espmirror" in (name or "").lower() for name in names)


def discover(seconds=2.5):
    """(name, address, port, MAC) of every board announcing the mirror."""
    from zeroconf import ServiceBrowser, Zeroconf

    found = {}

    class Listener:
        def add_service(self, zc, kind, name):
            info = zc.get_service_info(kind, name, 2000)
            if info and info.parsed_addresses():
                mac = (info.properties or {}).get(b"mac") or b""
                found[name] = (name.split("." + SERVICE)[0].split("._")[0], info.parsed_addresses()[0], info.port,
                               mac.decode(errors="replace").lower())

        def update_service(self, zc, kind, name):
            self.add_service(zc, kind, name)

        def remove_service(self, zc, kind, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE, Listener())
        time.sleep(seconds)
    finally:
        zc.close()
    return sorted(found.values())


def decode_qoi(data, count):
    """Headerless QOI operations; run expansion and RGB565 conversion use numpy."""
    index = [(0, 0, 0, 0)] * 64
    r, g, b, alpha = 0, 0, 0, 255
    values, counts = [], []
    off = total = 0
    while off < len(data):
        op = data[off]
        off += 1
        run = 1
        if op in (0xfe, 0xff):
            size = 3 if op == 0xfe else 4
            if off + size > len(data):
                raise ValueError("short QOI literal")
            r, g, b = data[off:off + 3]
            if size == 4:
                alpha = data[off + 3]
            off += size
        elif op < 0x40:
            r, g, b, alpha = index[op]
        elif op < 0x80:
            r = (r + ((op >> 4) & 3) - 2) & 255
            g = (g + ((op >> 2) & 3) - 2) & 255
            b = (b + (op & 3) - 2) & 255
        elif op < 0xc0:
            if off == len(data):
                raise ValueError("short QOI luma")
            dg = (op & 63) - 32
            r = (r + dg + (data[off] >> 4) - 8) & 255
            g = (g + dg) & 255
            b = (b + dg + (data[off] & 15) - 8) & 255
            off += 1
        else:
            run = (op & 63) + 1
        total += run
        if total > count or alpha != 255:
            raise ValueError("bad QOI pixels")
        index[(r * 3 + g * 5 + b * 7 + alpha * 11) & 63] = (r, g, b, alpha)
        values.append((r << 16) | (g << 8) | b)
        counts.append(run)
    if total != count:
        raise ValueError("QOI doesn't fill the rectangle")
    rgb = np.asarray(values, dtype=np.uint32)
    pixels = (((rgb >> 19) << 11) | (((rgb >> 10) & 63) << 5) | ((rgb >> 3) & 31)).astype(np.uint16)
    return np.repeat(pixels, counts)


def apply_frame(fb, msg, seen=None):
    """One 'F' message onto the screen copy; returns the screen state (0 bright, 2 dark).
    seen, a bool array the screen's size, gets every rectangle drawn marked.
    Raises ValueError on a message that doesn't add up, or a coding this viewer doesn't know."""
    if len(msg) < 8 or msg[:1] != b"F":
        raise ValueError("short frame")
    state, rects, w, h = msg[1], *struct.unpack_from("<HHH", msg, 2)
    if (h, w) != fb.shape:
        raise ValueError("frame size %dx%d, screen %dx%d" % (w, h, fb.shape[1], fb.shape[0]))
    off = 8
    for _ in range(rects):
        if off + 14 > len(msg):
            raise ValueError("short rectangle header")
        x, y, rw, rh, enc, _pad, n = struct.unpack_from("<HHHHBBI", msg, off)
        off += 14
        data = msg[off:off + n]
        off += n
        if _pad or len(data) != n or not rw or not rh or x + rw > w or y + rh > h:
            raise ValueError("bad rectangle")
        if enc == 0:
            if n != rw * rh * 2:
                raise ValueError("bad raw size")
            px = np.frombuffer(data, "<u2").reshape(rh, rw)
        elif enc in (1, 2, 3):
            if n % 4:
                raise ValueError("bad runs")
            pairs = np.frombuffer(data, "<u2").reshape(-1, 2)
            counts = pairs[:, 0].astype(np.int64)
            if not counts.all() or int(counts.sum()) != rw * rh:     # before np.repeat allocates by them
                raise ValueError("runs don't fill the rectangle")
            flat = np.repeat(pairs[:, 1], counts)
            px = flat.reshape(rw, rh).T if enc == 2 else flat.reshape(rh, rw)
            if enc == 3:
                px = np.bitwise_xor(px, fb[y:y + rh, x:x + rw])
        elif enc == 4:
            px = decode_qoi(data, rw * rh).reshape(rh, rw)
        else:
            raise ValueError("unknown coding %d" % enc)
        fb[y:y + rh, x:x + rw] = px
        if seen is not None:
            seen[y:y + rh, x:x + rw] = True
    if off != len(msg):
        raise ValueError("trailing frame bytes")
    return state


TEXT_EDITS = {"Backspace": 1, "Delete": 2, "Left": 3, "Right": 4, "Enter": 5}


def text_messages(text):
    """'I' messages of whole UTF-8 characters, 63 bytes of text at most each."""
    chunk = bytearray(b"I")
    for char in text:
        if char == "\0":
            continue
        encoded = char.encode("utf-8")
        if len(chunk) + len(encoded) > 64:
            yield bytes(chunk)
            chunk = bytearray(b"I")
        chunk.extend(encoded)
    if len(chunk) > 1:
        yield bytes(chunk)


def route_text_key(link, key, text="", *, paste=None, modified=False):
    """True when a field open on the board took the key; nothing typed goes to a log."""
    if not link.text_open:
        return False
    if paste is not None:
        for msg in text_messages(paste):
            link.send(msg)
        return True
    if modified:
        return False
    if key in TEXT_EDITS:
        link.send(bytes((ord("K"), TEXT_EDITS[key])))
        return True
    if key == "Escape":
        return True       # LVGL has no generic back action for a field; it stays with the field
    if text and all(c.isprintable() for c in text):
        for msg in text_messages(text):
            link.send(msg)
        return True
    return False


class Link:
    """One board's connection, on a thread of its own; it reconnects until closed, and after 'T' (another
    viewer took the screen) waits for take_back(). on_board(info), on_frame() and on_status(text) are
    called on that thread; status "" means live."""

    def __init__(self, host, port=PORT, log_file=None):
        self.host, self.port = host, port   # host: an address, or a serial port (the board's USB console)
        self.cable = is_port(host)
        self.raw = b""
        self.logs = ConsoleLog(host, log_file)
        self.demux = None
        self.incoming = []
        self.refresh_pending = False
        self.fb = None
        self.have_board = False
        self.seen = None            # pixels drawn since 'B': the board sends its whole screen first, in bands
        self.state = 0
        self.led = 0
        self.audio_capable = False
        self.audio = queue.Queue(maxsize=8)
        self.changes = 0            # frames that changed pixels
        self.live = False
        self.taken = False
        self.text_open = False      # a text field is open on the board: keys type into it
        self.lock = threading.Lock()
        self.out = queue.Queue()
        self.alive = True
        self.stopped = threading.Event()
        self.tx_lock = threading.Lock()
        self.sock = None
        self.thread = self.writer_thread = None
        self.on_board = self.on_frame = self.on_status = lambda *a: None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def send(self, payload):
        self.out.put(payload)

    def pointer(self, x, y, down):
        self.send(struct.pack("<BHH", 1 if down else 0, x, y))

    def tap(self, role):
        self.send(bytes([3, role]))

    def key(self, role, down):
        self.send(bytes([6, role, 1 if down else 0]))

    def take_back(self):
        self.taken = False

    def stop(self):
        self.alive = False
        self.live = False
        self.stopped.set()

    def cancel(self):
        """Wake transport IO without waiting for a worker or running its cleanup."""
        try:
            if self.cable:
                if not self.tx_lock.acquire(blocking=False):
                    return                  # serial reads/writes have timeouts; do not wait behind a writer
                try:                        # cancel_read uses handles that serial.close also releases
                    if self.sock is not None and hasattr(self.sock, 'cancel_read'):
                        self.sock.cancel_read()
                finally:
                    self.tx_lock.release()
            else:
                sock = self.sock
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def join(self, timeout=None):
        """Wait only; a reader can publish its writer after this call starts."""
        current = threading.current_thread()
        if current in (self.thread, self.writer_thread):
            return False
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        for name in ('thread', 'writer_thread'):
            worker = getattr(self, name)
            if worker is not None and worker.ident is not None:
                worker.join(None if deadline is None else max(0, deadline - time.monotonic()))
        return not any(worker is not None and worker.is_alive() for worker in (self.thread, self.writer_thread))

    def close(self):
        """Synchronous shutdown, including console output, unless called by a worker."""
        self.stop()
        try:
            self.cancel()
        finally:
            self.join()
        if self.thread is None:
            self.disconnect()

    def disconnect(self):
        self.audio_capable = False
        self.text_open = False
        self.led = 0
        while not self.audio.empty():
            try:
                self.audio.get_nowait()
            except queue.Empty:
                break
        with self.tx_lock:
            sock, self.sock = self.sock, None
            if sock:
                try:
                    if self.cable:
                        sock.write(BREAK)
                    else:
                        sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                finally:
                    try:
                        sock.close()
                    except OSError:
                        pass


    def status(self, text):
        if self.stopped.is_set():
            return
        self.live = not text
        self.on_status(text)

    def recv_exact(self, n):
        sock = self.sock
        if sock is None:
            raise ConnectionError("closed")
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return bytes(buf)

    def connect(self):
        self.have_board = False
        if self.cable:
            self.incoming = []
            self.refresh_pending = False
            self.demux = CableDemux(self.logs.feed, self.refresh)
            return open_serial(self.host, on_text=self.logs.feed, demux=self.demux)
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(6)       # the board sends a frame a second at least
        sock.sendall(HELLO)
        return sock

    def refresh(self):
        self.refresh_pending = True
        with self.lock:
            if self.seen is not None:
                self.seen.fill(False)
        self.send(b'R')

    def receive(self):
        if not self.cable:
            n, = struct.unpack('<I', self.recv_exact(4))
            if not 0 < n <= 1 << 20:
                raise ValueError("message of %d bytes" % n)
            return self.recv_exact(n)
        until = time.monotonic() + 6
        while self.alive:
            if self.incoming:
                return self.incoming.pop(0)
            sock = self.sock
            if sock is None:
                raise ConnectionError("closed")
            data = sock.read(max(1, min(sock.in_waiting, 8192)))
            if DUMP:
                self.raw = (self.raw + data)[-8192:]
            self.incoming.extend(self.demux.feed(data))
            if time.monotonic() > until:
                raise ConnectionError("no valid cable frame within 6 seconds")
        raise ConnectionError("closed")

    def writer(self, sock):
        last = time.time()
        while self.alive and self.sock is sock:
            try:
                payload = self.out.get(timeout=.1)
            except queue.Empty:
                payload = b"\x02" if time.time() - last > 5 else None   # keepalive
            if payload is None:
                continue
            try:
                data = struct.pack("<I", len(payload)) + payload
                with self.tx_lock:
                    if not self.alive or self.sock is not sock:
                        return
                    if self.cable:
                        if sock.write(data) != len(data):
                            raise ConnectionError("short serial write")
                    else:
                        sock.sendall(data)
                last = time.time()
            except OSError:
                return

    def extra(self, msg):
        if msg[:1] == b"L":
            if len(msg) != 2 or msg[1] > 3:
                raise ValueError("bad LED message")
            self.led = msg[1]
            self.on_frame()
        elif msg[:1] == b"A":
            if len(msg) < 4 or len(msg) > 1282 or len(msg) % 2 or msg[1] not in (1, 2):
                raise ValueError("bad audio message")
            try:
                self.audio.put_nowait((msg[1], msg[2:]))
            except queue.Full:
                try:
                    self.audio.get_nowait()
                except queue.Empty:
                    pass
                self.audio.put_nowait((msg[1], msg[2:]))

    def run(self):
        while self.alive:
            if self.taken:
                self.stopped.wait(0.2)
                continue
            writer = None
            try:
                self.status("connecting to %s" % self.host)
                if not self.alive:
                    break
                sock = self.connect()
                with self.tx_lock:
                    self.sock = sock
                    if not self.alive:
                        break
                while not self.out.empty():
                    self.out.get_nowait()
                writer = self.writer_thread = threading.Thread(target=self.writer, args=(sock,), daemon=True)
                writer.start()
                while self.alive:
                    msg = self.receive()
                    if not self.alive:
                        break
                    kind = msg[:1]
                    if kind == b"B":
                        info = json.loads(msg[1:])
                        with self.lock:
                            self.fb = np.zeros((info["h"], info["w"]), "<u2")
                            self.seen = np.zeros((info["h"], info["w"]), bool)
                        self.audio_capable = bool(info.get("audio", False))
                        self.text_open = False
                        self.send(b"O")               # this viewer types: the board says when a field opens
                        self.have_board = True
                        self.on_board(info)
                        self.status("")
                    elif kind == b"F":
                        if not self.have_board:
                            raise ConnectionError("frame before board geometry")
                        with self.lock:
                            was, self.state = self.state, apply_frame(self.fb, msg, self.seen)
                            rects = struct.unpack_from("<H", msg, 2)[0]
                            self.changes += 1 if rects else 0
                            if self.seen.all():
                                self.refresh_pending = False
                        if rects or self.state != was:
                            self.on_frame()
                    elif kind in (b"L", b"A"):
                        self.extra(msg)
                    elif kind == b"O" and len(msg) == 2 and msg[1] <= 1:
                        self.text_open = bool(msg[1])
                    elif kind == b"T":
                        self.taken = True
                        self.status("another viewer took the screen")
                        break
                    elif kind == b"E":                    # the board ended the cable session: ask again
                        self.status("cable session ended, reconnecting")
                        break
                    elif kind not in (b"F", b"B"):
                        raise ValueError("a %r message: the link lost its framing" % kind)
            except (OSError, ValueError, ConnectionError, struct.error, json.JSONDecodeError) as e:
                if DUMP and self.cable and self.sock and isinstance(e, ValueError):
                    self.sock.timeout = 1
                    with open(DUMP, "ab") as f:
                        f.write(b"\n==== %s %r\n" % (time.strftime("%H:%M:%S").encode(), str(e).encode()))
                        f.write(self.raw + b"\n==== after\n" + self.sock.read(4096))
                if self.alive:
                    self.status("%s: %s, retrying" % (self.host, e))
            finally:
                self.disconnect()
                if writer is not None:
                    writer.join()
                self.logs.flush()
            if self.alive and not self.taken:
                self.stopped.wait(2)


class IrisLink(Link):
    def __init__(self, host, port=PORT, log_file=None, gateway=None):
        super().__init__(host, port, log_file)
        from espmirror_iris import DEFAULT_GATEWAY, gateway_url
        self.gateway = gateway or os.environ.get("ESPMIRROR_IRIS_GATEWAY", DEFAULT_GATEWAY)
        gateway_url(self.gateway)
        parse_host(host)
        self.client = None
        self.bytes_received = 0
        self.out = queue.Queue(maxsize=64)

    def send(self, payload):
        if payload[:1] not in (b"\x00", b"\x01", b"\x03", b"\x06"):
            return
        try:
            self.out.put_nowait(payload)
        except queue.Full:
            self.status("Iris input queue full, reconnecting")
            self.disconnect()

    def disconnect(self):
        client = self.client
        if client:
            client.cancel()

    def cancel(self):
        self.disconnect()

    def writer(self, client):
        while self.alive and self.client is client and not client.closed:
            try:
                payload = self.out.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if self.client is not client or not self.alive:
                    return
                client.send(payload)
            except Exception as error:
                if self.alive:
                    self.status("Iris input: %s" % error)
                client.socket.close()
                return

    def run(self):
        from espmirror_iris import IrisClient
        pending = None
        while self.alive:
            if pending:
                try:
                    pending.close()
                    pending = None
                except Exception as error:
                    self.status("Iris cleanup: %s, retrying" % error)
                    self.stopped.wait(2)
                    continue
            client = IrisClient(self.gateway, self.host.removeprefix("iris:"))
            writer = None
            try:
                self.client = client
                if not self.alive:
                    break
                self.status("connecting to %s" % self.host)
                assembler = client.open()
                if not self.alive:
                    break
                while not self.out.empty():
                    self.out.get_nowait()
                with self.lock:
                    self.fb = np.zeros((assembler.height, assembler.width), "<u2")
                    self.seen = np.zeros(self.fb.shape, bool)
                self.have_board = True
                self.on_board(client.board)
                self.status("")
                prior_bytes = self.bytes_received
                writer = self.writer_thread = threading.Thread(target=self.writer, args=(client,), daemon=True)
                writer.start()
                while self.alive:
                    frame = client.receive()
                    self.bytes_received = prior_bytes + assembler.bytes_received
                    if frame is None:
                        continue
                    with self.lock:
                        self.fb = frame
                        self.seen.fill(True)
                        self.changes += 1
                    self.on_frame()
            except Exception as error:
                if self.alive:
                    self.status("%s: %s, retrying" % (self.host, error))
            finally:
                self.client = None
                if writer:
                    writer.join(4)
                try:
                    client.close()
                except Exception as error:
                    pending = client
                    if self.alive:
                        self.status("Iris disconnect: %s" % error)
            if self.alive:
                self.stopped.wait(2)
        if pending:
            try:
                pending.close()
            except Exception as error:
                self.status("Iris cleanup failed: %s; the Gateway may still have held input or a running mirror" % error)


def make_link(host, port=PORT, log_file=None, gateway=None):
    if host.startswith("iris:"):
        return IrisLink(host, port, log_file, gateway=gateway)
    return Link(host, port, log_file)


def stop_links(links):
    links = tuple(links)
    for link in links:
        link.stop()
    for link in links:
        link.cancel()


def join_links(links, timeout=None):
    deadline = None if timeout is None else time.monotonic() + max(0, timeout)
    done = True
    for link in links:
        if not link.join(None if deadline is None else max(0, deadline - time.monotonic())):
            done = False
    return done


def rgb565_to_rgb(fb):
    p = fb.astype(np.uint32)
    r, g, b = (p >> 11) & 31, (p >> 5) & 63, p & 31
    out = np.empty(fb.shape + (3,), np.uint8)
    out[..., 0] = (r * 527 + 23) >> 6
    out[..., 1] = (g * 259 + 33) >> 6
    out[..., 2] = (b * 527 + 23) >> 6
    return out


def png_write(path, rgb):
    """A PNG from h x w x 3 bytes, without Pillow or Qt."""
    h, w = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def desktop_path(ext, tag="", stamp=None):
    home = os.path.expanduser("~")
    try:
        from PySide6.QtCore import QStandardPaths
    except ImportError:
        folder = os.path.join(home, "Desktop")
    else:
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(folder if os.path.isdir(folder) else home, "mirror-%s%s.%s" % (stamp, tag, ext))


def capture_path(ext, tag="", stamp=None, folder=None):
    if not folder:
        return desktop_path(ext, tag, stamp)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(folder, "mirror-%s%s.%s" % (stamp, tag, ext))


def save_png(link, path, timeout=15):
    """The screen as a PNG once every pixel of it has arrived."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.05)
        with link.lock:
            rgb = rgb565_to_rgb(link.fb) if link.seen is not None and link.seen.all() else None
        if rgb is not None:
            png_write(path, rgb)
            print("saved", path)
            return 0
    print("no whole screen within %d s" % timeout, file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------------------------------------------
# The window. Everything below needs PySide6; the protocol above does not.

def run_window(targets, scale, on_top, wall, devices_dir, log_file=None, sound=False, settings=None,
               iris_gateway=None):
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import (QAction, QBrush, QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QIcon,
                               QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                               QPolygonF, QRadialGradient, QTransform)
    from espmirror_audio import AudioSession
    from espmirror_settings import edit_preferences, read_preferences, settings_store
    from PySide6.QtWidgets import QApplication, QMenu, QMenuBar, QToolTip, QWidget

    BUTTON = 34                      # talk, listen, record, screenshot, close
    CORNER = 14                      # points from a corner of the devices' area that resize the window
    GAP = 28                         # between boards side by side
    MIN_GLASS = 120                  # points: the screen is never drawn narrower, a tiny device is no use
    KEY_MM = 6.0                     # a button: .75 of this along the edge, .2 out of it
    TOUCH = (150, 120, 255)          # the accent: a held button's name; a ring where the mouse touches the glass
    TOUCH_S = 0.6                    # the ring fades in this many seconds
    WHEEL_SLOP = 16                  # board pixels a two-finger touch moves before it lands: past LVGL's scroll limit
    KEY_ROLES = {Qt.Key.Key_1: 1, Qt.Key.Key_2: 2, Qt.Key.Key_3: 3, Qt.Key.Key_4: 4, Qt.Key.Key_Up: 5,
                 Qt.Key.Key_Down: 6, Qt.Key.Key_Left: 7, Qt.Key.Key_Right: 8, Qt.Key.Key_Return: 9,
                 Qt.Key.Key_Enter: 9, Qt.Key.Key_Escape: 10}
    AA = QPainter.RenderHint.Antialiasing
    SMOOTH = QPainter.RenderHint.SmoothPixmapTransform
    CENTER = int(Qt.AlignmentFlag.AlignCenter)
    NOPEN = Qt.PenStyle.NoPen

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("espmirror")

    def rgb(r, g, b, a=1.0):
        c = QColor(r, g, b)
        c.setAlphaF(a)
        return c

    def font(px):
        f = QFont()
        f.setPixelSize(max(1, int(round(px))))
        return f

    def rounded(r, radius):
        path = QPainterPath()
        if radius > 0:
            path.addRoundedRect(QRectF(*r), radius, radius)
        else:
            path.addRect(QRectF(*r))
        return path

    def grad_fill(p, path, angle, c1, c2):
        """A linear gradient across the path's bounds, from c1 to c2 along the angle (degrees, as AppKit drew
        it in the flipped view: -60 runs from the lower left to the upper right)."""
        b = path.boundingRect()
        a = math.radians(angle)
        dx, dy = math.cos(a), math.sin(a)
        ext = (abs(b.width() * dx) + abs(b.height() * dy)) / 2
        c = b.center()
        g = QLinearGradient(c.x() - dx * ext, c.y() - dy * ext, c.x() + dx * ext, c.y() + dy * ext)
        g.setColorAt(0, c1)
        g.setColorAt(1, c2)
        p.fillPath(path, QBrush(g))

    def pen(c, width):
        q = QPen(c, width)
        q.setCapStyle(Qt.PenCapStyle.FlatCap)
        q.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        return q

    # -- device photos and scaled copies: cached, a resize rebuilds them ---------------------------------------
    photo_cache, scaled_cache = {}, {}

    def solid_inside(img):
        """The photo with whatever its cut-out left see-through inside the body (the glass's dark border,
        around the screen) made solid black: in each row, between the first and the last opaque pixel."""
        w, h = img.width(), img.height()
        px = np.frombuffer(img.constBits(), np.uint8, img.bytesPerLine() * h).reshape(h, -1)[:, :w * 4]
        argb = px.copy().view("<u4").reshape(h, w)
        opaque = (argb >> 24) > 200
        cols = np.arange(w)
        rows = opaque.any(axis=1)
        first = np.where(rows, opaque.argmax(axis=1), w)
        last = np.where(rows, w - 1 - opaque[:, ::-1].argmax(axis=1), -1)
        inside = (cols >= first[:, None]) & (cols <= last[:, None])
        argb = np.where(inside, argb | 0xFF000000, argb)   # premultiplied: a see-through pixel's colour is black
        out = QImage(argb.astype("<u4").tobytes(), w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)
        return out.copy()

    def photo_image(prof):
        """The profile's photo (turned with it) and the screen's rectangle in it, or None."""
        spec = prof.get("photo")
        if not spec or not spec.get("screen") or not devices_dir:
            return None
        path = os.path.join(devices_dir, spec["file"])
        key = (path, bool(prof.get("turned")))
        if key not in photo_cache:
            img = QImage(path) if os.path.isfile(path) else QImage()
            if img.isNull():
                photo_cache[key] = None
            else:
                img = solid_inside(img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))
                scr = tuple(spec["screen"])
                if prof.get("turned"):
                    iw = img.width()
                    img = img.transformed(QTransform().rotate(-90), Qt.TransformationMode.SmoothTransformation)
                    px, py, pw, ph = scr
                    scr = (py, iw - px - pw, ph, pw)
                photo_cache[key] = (img, scr)
        return photo_cache[key]

    def bounded(cache, key, make):
        if key not in cache:
            if len(cache) > 16:
                cache.clear()
            cache[key] = make()
        return cache[key]

    def scaled_photo(img, w, h, dpr):
        def make():
            out = img.scaled(max(1, round(w * dpr)), max(1, round(h * dpr)), Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            out.setDevicePixelRatio(dpr)
            return out
        return bounded(scaled_cache, (img.cacheKey(), round(w * dpr), round(h * dpr)), make)


    # -- the device's parts ---------------------------------------------------------------------------------
    def device_layout(look, s, ox, oy):
        """The device at s points per millimetre with its body's top left at (ox, oy): the body, the glass, the
        active area, the LED, the studs, and the buttons as shapes (centre, rotation in degrees where 0 is a
        vertical bar on the right edge, thickness w, length h, the outward direction, and where the label goes)."""
        prof, cw, ch = look["profile"], look["w"], look["h"]
        body, glass = prof["body"], prof["glass"]
        screen_mm = prof["screen_mm"] or (cw / look["ppi"] * 25.4 if look["ppi"] else 40.0)
        mm = screen_mm / cw                                   # millimetres per screen pixel
        aw, ah = cw * mm * s, ch * mm * s
        disc = body["shape"] == "round"
        if disc:
            d = (body["d"] or max(cw, ch) * mm + 2 * body["x"]) * s
            brect, body_r = (ox, oy, d, d), d / 2
            ax, ay = ox + (d - aw) / 2, oy + (d - ah) / 2
        else:
            bw = (body["w"] or cw * mm + 2 * body["x"]) * s
            bh = (body["h"] or ch * mm + 2 * body["y"]) * s
            brect, body_r = (ox, oy, bw, bh), body["r"] * s
            ax, ay = ox + body["x"] * s, oy + body["y"] * s
        photo, hole = photo_image(prof), None
        if photo:
            img, (px, py, pw, ph) = photo
            bw_mm = body.get("w") or body["d"]
            bh_mm = body.get("h") or body.get("d") or bw_mm * img.height() / img.width()
            fx = bw_mm * s / img.width()                      # photo pixels to points, each way: the drawing's
            fy = bh_mm * s / img.height()                     # millimetres win over the picture's proportions
            brect = (ox, oy, img.width() * fx, img.height() * fy)
            hole = (ox + px * fx, oy + py * fy, pw * fx, ph * fy)
            ax, ay, aw, ah = hole                             # the board's pixels keep their proportions, centred
            if ah / aw > ch / cw:
                ah = aw * ch / cw
                ay = hole[1] + (hole[3] - ah) / 2
            else:
                aw = ah * cw / ch
                ax = hole[0] + (hole[2] - aw) / 2
            s = aw / screen_mm                                # the screen's own scale, from the photo
        g = glass["border"] * s
        grect = hole or (ax - g, ay - g, aw + 2 * g, ah + 2 * g)
        glass_r = (aw + 2 * g) / 2 if look["round"] else glass["r"] * s
        active_r = aw / 2 if look["round"] else 0
        keys = place_keys(look, brect, body_r, disc, (ax, ay, aw, ah), grect, s)
        led = None
        if prof.get("led"):
            o = prof["led"]
            dd = o["d"] * s
            led = dict(rect=(brect[0] + o["x"] * s - dd / 2, brect[1] + o["y"] * s - dd / 2, dd, dd),
                       colour=o["colour"])
        studs = [(brect[0] + x * s, brect[1] + y * s, prof["studs"]["d"] * s)
                 for x, y in (prof.get("studs") or {}).get("at", ())]
        L = dict(body=brect, body_r=body_r, glass=grect, glass_r=glass_r, active=(ax, ay, aw, ah),
                 active_r=active_r, keys=keys, s=s, led=led, studs=studs, photo=photo[0] if photo else None)
        bx, by, bw, bh = brect
        reach = [key_path(k, False).boundingRect() for k in keys]
        L["over"] = (max([bx - r.left() for r in reach] + [0]), max([by - r.top() for r in reach] + [0]),
                     max([r.right() - bx - bw for r in reach] + [0]), max([r.bottom() - by - bh for r in reach] + [0]))
        return L

    def place_keys(look, brect, body_r, disc, active, grect, s):
        """Each button on the body's edge nearest its hint, at the hint's place along it (on the rim, at its
        angle, for a disc); buttons without a hint along the bottom."""
        bx, by, bw, bh = brect
        ax, ay, aw, ah = active
        cw, ch = look["w"], look["h"]
        cx, cy = bx + bw / 2, by + bh / 2
        T, Lk = KEY_MM * s * .2, KEY_MM * s * .75
        edges = {"right": [], "left": [], "top": [], "bottom": []}
        rim, loose, face = [], [], []
        defaults = {k[1]: k for k in look["profile"].get("keys", [])}
        for b in look["buttons"]:
            x, y = b.get("x", -1), b.get("y", -1)
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or x < 0 or y < 0:
                key = defaults.get(b["role"])
                if key:
                    label, role, edge, pos, diameter = key
                    if edge == "angle":
                        rim.append([pos, b])
                    elif edge == "face":
                        face.append(dict(cx=bx + pos[0] * s, cy=by + pos[1] * s, rot=0, dx=0, dy=1,
                                         rim=0, border=0, role=role, label=b.get("name") or label,
                                         w=diameter * s, h=diameter * s, face=True))
                    else:
                        edges[edge].append([pos * s, b])
                else:
                    loose.append(b)
                continue
            hx, hy = ax + min(x + .5, cw) / cw * aw, ay + min(y + .5, ch) / ch * ah
            if disc:
                rim.append([math.degrees(math.atan2(hx - cx, cy - hy)) % 360, b])
                continue
            # the screen edge the hint hugs, not the nearest body edge: bezels differ (the AIPI's screen sits
            # high, its buttons below it would land on the sides)
            edge = min((ax + aw - hx, "right"), (hx - ax, "left"), (ay + ah - hy, "bottom"), (hy - ay, "top"))[1]
            edges[edge].append([hy - by if edge in ("left", "right") else hx - bx, b])
        for i, b in enumerate(loose):
            off = i - (len(loose) - 1) / 2
            if disc:
                rim.append([(180 - off * 20) % 360, b])
            else:
                edges["bottom"].append([bw / 2 + off * Lk * 1.6, b])
        keys = face
        for edge, items in edges.items():
            if not items:
                continue
            length = bh if edge in ("left", "right") else bw
            lo, hi = body_r + Lk / 2, length - body_r - Lk / 2
            if lo > hi:
                lo = hi = length / 2
            items.sort(key=lambda it: it[0])
            for j, it in enumerate(items):                  # keep them apart, inside the straight part
                it[0] = min(max(it[0], lo), hi)
                if j:
                    it[0] = max(it[0], items[j - 1][0] + Lk * 1.3)
            over = items[-1][0] - hi
            for it in items:
                it[0] -= max(0, over)
            for pos, b in items:
                if edge == "right":
                    k = dict(cx=bx + bw + T / 2 - 1, cy=by + pos, rot=0, dx=1, dy=0,
                             rim=bx + bw - grect[0] - grect[2], border=grect[0] + grect[2] - ax - aw)
                elif edge == "left":
                    k = dict(cx=bx - T / 2 + 1, cy=by + pos, rot=0, dx=-1, dy=0, rim=grect[0] - bx,
                             border=ax - grect[0])
                elif edge == "top":
                    k = dict(cx=bx + pos, cy=by - T / 2 + 1, rot=90, dx=0, dy=-1, rim=grect[1] - by,
                             border=ay - grect[1])
                else:
                    k = dict(cx=bx + pos, cy=by + bh + T / 2 - 1, rot=90, dx=0, dy=1,
                             rim=by + bh - grect[1] - grect[3], border=grect[1] + grect[3] - ay - ah)
                keys.append(dict(k, role=b["role"], label=str(b.get("name") or ROLES.get(b["role"], "")), w=T, h=Lk))
        if rim:
            rr = body_r + T / 2 - 1
            step = math.degrees(Lk * 1.3 / max(1.0, rr))
            rim.sort(key=lambda it: it[0])
            for j, it in enumerate(rim):
                if j:
                    it[0] = max(it[0], rim[j - 1][0] + step)
            for angle, b in rim:
                a = math.radians(angle)
                keys.append(dict(cx=cx + rr * math.sin(a), cy=cy - rr * math.cos(a), rot=angle - 90,
                                 dx=math.sin(a), dy=-math.cos(a), rim=body_r - grect[2] / 2,
                                 border=(grect[2] - aw) / 2, role=b["role"],
                                 label=str(b.get("name") or ROLES.get(b["role"], "")), w=T, h=Lk))
        for k in keys:            # the label beside it, reading along the edge: on the body's rim, or when that is
            rim_w, border = max(0.0, k["rim"]), max(0.0, k["border"])     # too thin, on the glass's dark border
            glassy = rim_w * .6 < 6.5 and border > rim_w
            size = max(6.0, min(12.0, 1.6 * s, (border if glassy else rim_w) * .6))
            off = (T / 2 - 1) + (rim_w + min(border / 2, .4 * s + size * .6) if glassy
                                 else min(rim_w / 2, .4 * s + size * .6))
            turn = math.degrees(math.atan2(-k["dx"], k["dy"]))
            turn = turn - 180 if turn > 90.5 else turn + 180 if turn < -90.5 else turn
            k.update(lx=k["cx"] - k["dx"] * off, ly=k["cy"] - k["dy"] * off, lrot=turn, lsize=size, glassy=glassy)
        return keys

    def key_path(k, pushed):
        """The button as a path; pushed in, it is half as thick and sits closer to the body."""
        w = k["w"] * (.5 if pushed else 1)
        path = QPainterPath()
        rad = min(2.5, w / 2)
        if k.get("face"):
            path.addEllipse(QRectF(-k["w"] / 2, -k["h"] / 2, k["w"], k["h"]))
        else:
            path.addRoundedRect(QRectF(-w / 2, -k["h"] / 2, w, k["h"]), rad, rad)
        t = QTransform()
        t.translate(k["cx"] - (k["w"] / 4 * k["dx"] if pushed else 0), k["cy"] - (k["w"] / 4 * k["dy"] if pushed else 0))
        t.rotate(k["rot"])
        return t.map(path)

    def key_hit(k, p):
        r = math.radians(k["rot"])
        dx, dy = p.x() - k["cx"], p.y() - k["cy"]
        lx = math.cos(r) * dx + math.sin(r) * dy
        ly = -math.sin(r) * dx + math.cos(r) * dy
        return abs(lx) <= k["w"] / 2 + 5 and abs(ly) <= k["h"] / 2

    def on_glass(L, p):
        gx, gy, gw, gh = L["active"]
        if not (gx <= p.x() <= gx + gw and gy <= p.y() <= gy + gh):
            return False
        rad = L["active_r"]
        cx = min(max(p.x(), gx + rad), gx + gw - rad)
        cy = min(max(p.y(), gy + rad), gy + gh - rad)
        return (p.x() - cx) ** 2 + (p.y() - cy) ** 2 <= rad * rad + 1e-9

    def paint_led(p, L, mode, now):
        """A small lens on the body: dark when off, the colour with a glow when on."""
        if not L["led"]:
            return
        lit = mode == 3 or (mode == 1 and now % 1.0 < .5) or (mode == 2 and now % .25 < .125)
        x, y, w, h = L["led"]["rect"]
        col = L["led"]["colour"]
        p.setPen(NOPEN)
        p.setBrush(rgb(0x9a, 0x9a, 0x96))
        p.drawEllipse(QRectF(x - .5, y - .5, w + 1, h + 1))
        p.setBrush(rgb(*col) if lit else rgb(0x3a, 0x3a, 0x3c))
        p.drawEllipse(QRectF(x, y, w, h))
        if lit:
            glow = QRadialGradient(QPointF(x + w / 2, y + h / 2), w * 1.5)
            glow.setColorAt(0, rgb(*col, .8))
            glow.setColorAt(1, rgb(*col, 0))
            p.setBrush(QBrush(glow))
            p.drawEllipse(QRectF(x - w, y - h, w * 3, h * 3))

    def paint_labels(p, keys, colour, pressed):
        """Each button's name beside it; a held one's in the accent colour."""
        for k in keys:
            p.setPen(rgb(*TOUCH) if k["role"] in pressed else colour)
            p.save()
            p.translate(k["lx"], k["ly"])
            p.rotate(k["lrot"])
            p.setFont(font(k["lsize"]))
            p.drawText(QRectF(-60, -k["lsize"], 120, 2 * k["lsize"]), CENTER, k["label"])
            p.restore()

    def paint_device(p, L, prof, img, pressed, veil, text, led_mode=0, labels=True):
        """The device from a layout: the body with its finish, the buttons, the glass, the screen."""
        style = FINISHES[prof["body"]["finish"]]
        bx, by, bw, bh = L["body"]
        dpr = p.device().devicePixelRatioF() if p.device() else 1.0
        photo = scaled_photo(L["photo"], bw, bh, dpr) if L["photo"] is not None else None
        for k in L["keys"]:                                   # the buttons sit behind the body's edge, so first
            pushed = k["role"] in pressed
            c1, c2 = style["key_down"] if pushed else style["key"]
            grad_fill(p, key_path(k, pushed), k["rot"] + 90, rgb(*c1), rgb(*c2))
        if photo is not None:
            p.drawImage(QPointF(bx, by), photo)
        else:
            path = rounded(L["body"], L["body_r"])
            grad_fill(p, path, -60, rgb(*style["fill"]), rgb(*style["fill2"]))
            p.strokePath(path, QPen(rgb(*style["edge"]), 1))
            for sx, sy, sd in L["studs"]:                     # the corner holes: a dark ring, a lighter floor
                p.setPen(NOPEN)
                p.setBrush(rgb(*style["edge"]))
                p.drawEllipse(QRectF(sx - sd / 2, sy - sd / 2, sd, sd))
                p.setBrush(rgb(*style["fill2"]))
                p.drawEllipse(QRectF(sx - sd / 2 + 1, sy - sd / 2 + 1, sd - 2, sd - 2))
        paint_led(p, L, led_mode, time.monotonic())
        if labels:
            paint_labels(p, [k for k in L["keys"] if not k["glassy"]], rgb(*style["label"]), pressed)
        p.fillPath(rounded(L["glass"], L["glass_r"]), rgb(0x08, 0x08, 0x0a))   # the cover glass, the screen in it
        if labels:
            paint_labels(p, [k for k in L["keys"] if k["glassy"]], rgb(0x9a, 0x9a, 0xa0), pressed)
        p.save()
        a = L["active"]
        p.setClipPath(rounded(a, L["active_r"]), Qt.ClipOperation.IntersectClip)
        p.fillRect(QRectF(*a), rgb(0, 0, 0))
        if img is not None:
            p.drawImage(QRectF(*a), img)
        if veil:
            p.fillRect(QRectF(*a), rgb(0, 0, 0, veil))
        if text:
            inset = a[2] * .15 if L["active_r"] else 8
            p.setPen(rgb(0xc9, 0xc9, 0xc9))
            p.setFont(font(12))
            p.drawText(QRectF(*a).adjusted(inset, inset, -inset, -inset), CENTER | int(Qt.TextFlag.TextWordWrap), text)
        p.restore()
        grad_fill(p, rounded(L["glass"], L["glass_r"]), -70, rgb(255, 255, 255, .07), rgb(255, 255, 255, 0))

    def paint_button(p, name, br, recording, lit, enabled):
        x, y, w, h = br
        cx, cy = x + w / 2, y + h / 2
        ink = rgb(0xdd, 0xdd, 0xdd)
        p.save()
        if not enabled:
            p.setOpacity(.4)
        p.setPen(NOPEN)
        p.setBrush(rgb(0xeb, 0x57, 0x57) if name == "rec" and lit else rgb(0, 0x9b, 0x6d) if lit
                   else rgb(0x3a, 0x3a, 0x40, .75))
        p.drawEllipse(QRectF(*br))
        if name == "close":
            p.setPen(rgb(0xc9, 0xc9, 0xc9))
            p.setFont(font(17))
            p.drawText(QRectF(*br), CENTER, "✕")
        elif name == "listen":                                # a speaker: a box and a cone, and a sound wave
            p.setBrush(ink)
            p.drawPolygon(QPolygonF([QPointF(cx - 8, cy - 3), QPointF(cx - 4, cy - 3), QPointF(cx + 1, cy - 7),
                                     QPointF(cx + 1, cy + 7), QPointF(cx - 4, cy + 3), QPointF(cx - 8, cy + 3)]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(pen(ink, 1.5))
            p.drawArc(QRectF(cx + 1 - 6, cy - 6, 12, 12), -40 * 16, 80 * 16)
        elif name == "talk":                                  # a microphone: a capsule on a stand
            p.setBrush(ink)
            p.drawEllipse(QRectF(cx - 3.5, cy - 9, 7, 11))
            p.fillRect(QRectF(cx - .75, cy + 4.5, 1.5, 4), ink)
            p.fillRect(QRectF(cx - 4, cy + 8, 8, 1.5), ink)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(pen(ink, 1.5))
            p.drawArc(QRectF(cx - 6.5, cy - 2 - 6.5, 13, 13), 180 * 16, 180 * 16)
        elif name == "move":                                  # four arrows from the middle
            p.setPen(pen(ink, 1.5))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                tip = QPointF(cx + dx * 8, cy + dy * 8)
                p.drawLine(QPointF(cx, cy), tip)
                for side in (1, -1):                          # the head: two short strokes back from the tip
                    p.drawLine(tip, QPointF(tip.x() - dx * 3.5 + side * dy * 3, tip.y() - dy * 3.5 + side * dx * 3))
        elif name == "rec":
            if recording:
                p.fillRect(QRectF(cx - 6, cy - 6, 12, 12), rgb(255, 255, 255))
            else:
                p.setBrush(rgb(0xeb, 0x57, 0x57))
                p.drawEllipse(QRectF(cx - 7, cy - 7, 14, 14))
        else:                                                 # a camera
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(pen(ink, 1.5))
            p.drawRoundedRect(QRectF(cx - 9, cy - 4, 18, 12), 2, 2)
            p.drawEllipse(QRectF(cx - 3.5, cy - 1.5, 7, 7))
            p.fillRect(QRectF(cx - 4, cy - 7, 8, 3), ink)
        p.restore()

    PLACEHOLDER = dict(name="", w=240, h=240, round=False, diag=0, touch=False, buttons=[])

    class Dev:
        def __init__(self, link):
            self.link = link
            self.look = board_look(PLACEHOLDER)
            self.image = None
            self.pressed = set()
            self.L = None
            self.slot = None                                  # the device's part of the window
            self.icon_dirty = True
            self.said = None

    class Bridge(QObject):
        board = Signal(int, object)
        frame = Signal(int)
        status = Signal(int, str)
        saved = Signal(str)

    class Window(QWidget):
        def __init__(self, links):
            flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
            if on_top:
                flags |= Qt.WindowType.WindowStaysOnTopHint
            super().__init__(None, flags)
            self.setWindowTitle("espmirror")
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.devs = [Dev(link) for link in links]
            self.zoom, self.ss, self.lay = scale, None, None
            self.zooming, self.zoomed_at = False, 0.0
            self.closing = False
            self.flash_until, self.toast = 0.0, None
            self.settings = settings if settings is not None else settings_store()
            self.preferences = read_preferences(self.settings)
            self.touches = []                                 # (device, x, y in screen pixels, time)
            self.touch_at = 0.0
            self.button_down = self.mouse_key = self.finger = self.grip = self.drag_from = None
            self.wheel = None                                 # two fingers down: [device, cursor x, y, finger x, y, axis, on glass]
            self.wheel_idle = QTimer(self, singleShot=True, timeout=self.wheel_up)
            self.held = {}                                    # keyboard role -> the device it went to
            self.rec = None
            self.anim = QTimer(self, interval=50, timeout=self.animate)
            self.rec_timer = QTimer(self, interval=20, timeout=self.rec_frame)
            self.icon_timer = QTimer(self, interval=1000, timeout=self.refresh_icon)
            self.active_dev = self.devs[0]
            self.audio_io = AudioSession(self, self.notice)
            self.audio_tick = QTimer(self, interval=100, timeout=self.update)
            self.audio_tick.start()
            self.build_actions()

        # -- geometry ---------------------------------------------------------------------------------------
        def px_mm(self):
            """Logical pixels per millimetre on the screen the window is on."""
            screen = self.screen() or QGuiApplication.primaryScreen()
            size = screen.physicalSize()
            if size.width() > 0:
                return screen.geometry().width() / size.width()
            return screen.logicalDotsPerInch() / 25.4

        def scales(self, zoom):
            """Points per millimetre for each device at zoom (a share of the real size on this display), each
            raised as far as its screen needs to be MIN_GLASS wide."""
            pxmm = self.px_mm()
            return [max(zoom * pxmm, MIN_GLASS / device_layout(d.look, 1.0, 0, 0)["active"][2]) for d in self.devs]

        def measure(self, ss):
            """The devices at their scales, in rows (up to three side by side, more in a grid), and the window
            around them: a thin margin, more above for the buttons, room for keys that stick out."""
            parts = [device_layout(d.look, s, 0, 0) for d, s in zip(self.devs, ss)]
            m = 4
            cells = [(L["over"][0] + L["body"][2] + L["over"][2], L["over"][1] + L["body"][3] + L["over"][3])
                     for L in parts]
            n = len(parts)
            cols = n if n <= 3 else math.ceil(math.sqrt(n))
            rows = [list(range(i, min(n, i + cols))) for i in range(0, n, cols)]
            row_w = [sum(cells[i][0] for i in row) + GAP * (len(row) - 1) for row in rows]
            row_h = [max(cells[i][1] for i in row) for row in rows]
            mt = max(m, BUTTON + 14)
            W = max(row_w) + 2 * m
            W += max(0, 6 * (BUTTON + 6) + 8 - W)             # the six buttons at the top never shrink
            return dict(W=W, H=mt + sum(row_h) + GAP * (len(rows) - 1) + m, m=m, mt=mt, parts=parts, cells=cells,
                        rows=rows, row_w=row_w, row_h=row_h)

        def apply_zoom(self, zoom, say=True):
            """The size as a share of the real size on the display the window is on, kept across displays."""
            if self.zooming:
                return
            self.zooming = True
            try:
                self.fit(zoom)
            finally:
                self.zooming, self.zoomed_at = False, time.monotonic()
            if say:
                self.notice("actual size" if abs(self.zoom - 1) < .02 else "%.0f %% of actual" % (self.zoom * 100))

        def fit(self, zoom):
            self.ss = self.scales(zoom)
            self.zoom = min(self.ss) / self.px_mm()           # after the floor: Bigger acts at once
            g = self.measure(self.ss)
            self.lay = None
            # Fixed: the system's edge drag would stretch the window off the devices' proportions; corners resize.
            self.setFixedSize(int(math.ceil(g["W"])), int(math.ceil(g["H"])))
            if self.isVisible() and not self.grip:            # grown past the screen's edge: back onto it
                area = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
                x = max(area.left(), min(self.x(), area.right() + 1 - self.width()))
                y = max(area.top(), min(self.y(), area.bottom() + 1 - self.height()))
                if (x, y) != (self.x(), self.y()):
                    self.move(x, y)
            self.update()

        def screen_changed(self, _screen):
            """Moved to another display, where a millimetre is another number of points: the same share of the
            real size there. Not while a resize of ours is settling, which can itself cross displays and back."""
            if time.monotonic() - self.zoomed_at > .5:
                QTimer.singleShot(0, lambda: self.apply_zoom(self.zoom, say=False))

        def arrange(self):
            """The parts at the current size: each device, the buttons, the handles."""
            if self.lay is not None:
                return self.lay
            if self.ss is None:
                self.ss = self.scales(self.zoom)
            g = self.measure(self.ss)
            W, H = self.width(), self.height()
            if abs(W - g["W"]) > 2 or abs(H - g["H"]) > 2:    # resized from outside: the devices fit the window
                zoom = self.zoom
                for _ in range(3):                            # the margins don't scale: a pass or two settles it
                    zoom *= min(W / g["W"], H / g["H"])
                    self.ss = self.scales(zoom)
                    g = self.measure(self.ss)
                self.zoom = min(self.ss) / self.px_mm()
            y = g["mt"] + (H - g["H"]) / 2
            for row, rw, rh in zip(g["rows"], g["row_w"], g["row_h"]):
                x = (W - rw) / 2
                for i in row:
                    d, P, (cw, ch) = self.devs[i], g["parts"][i], g["cells"][i]
                    d.L = device_layout(d.look, self.ss[i], x + P["over"][0], y + (rh - ch) / 2 + P["over"][1])
                    d.slot = QRectF(x - GAP / 2, y - GAP / 2, cw + GAP, rh + GAP)
                    x += cw + GAP
                y += rh + GAP
            x0 = (W - (6 * (BUTTON + 6) - 6)) / 2
            # The corners of the drawn devices resize, as a window's own do.
            bodies = [d.L["body"] for d in self.devs]
            left, top = min(b[0] for b in bodies), min(b[1] for b in bodies)
            right, bottom = max(b[0] + b[2] for b in bodies), max(b[1] + b[3] for b in bodies)
            self.lay = dict(buttons={name: (x0 + i * (BUTTON + 6), 4, BUTTON, BUTTON)
                                     for i, name in enumerate(("move", "talk", "listen", "rec", "shot", "close"))},
                            corners=[((sx, sy), (left if sx < 0 else right, top if sy < 0 else bottom))
                                     for sx in (-1, 1) for sy in (-1, 1)])
            return self.lay

        def resizeEvent(self, e):
            self.lay = None
            super().resizeEvent(e)

        # -- drawing ----------------------------------------------------------------------------------------
        def paintEvent(self, e):
            lay = self.arrange()
            p = QPainter(self)
            p.setRenderHint(AA)
            p.setRenderHint(SMOOTH)
            if wall:
                p.setPen(NOPEN)
                p.setBrush(rgb(0x14, 0x14, 0x18))
                p.drawRoundedRect(QRectF(self.rect()), 12, 12)
            now = time.monotonic()
            self.paint_devices(p, now, flash=True)
            for name, br in lay["buttons"].items():
                lit = name == "rec" and self.rec is not None or name == "shot" and now < self.flash_until + .4
                lit = lit or name == "talk" and self.audio_io.talk_link is not None or name == "listen" and self.audio_io.listen_link is not None
                enabled = name not in ("talk", "listen") or self.active_dev.link.live and self.active_dev.link.audio_capable
                paint_button(p, name, br, self.rec is not None, lit, enabled)
            if self.toast and now < self.toast[1]:            # the toast, under the buttons
                f = font(11)
                fm = QFontMetricsF(f)
                text = fm.elidedText(self.toast[0], Qt.TextElideMode.ElideMiddle, max(40, self.width() - 40))
                tw, th = fm.horizontalAdvance(text), fm.height()
                box = QRectF(self.width() - 8 - tw - 16, 4 + BUTTON + 6, tw + 16, th + 8)
                p.setPen(NOPEN)
                p.setBrush(rgb(0x2a, 0x2a, 0x2e, .92))
                p.drawRoundedRect(box, 6, 6)
                p.setPen(rgb(0xdd, 0xdd, 0xdd))
                p.setFont(f)
                p.drawText(box.adjusted(8, 4, -8, -4), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            elif self.toast:
                self.toast = None
            p.end()

        def paint_devices(self, p, now, flash):
            """The devices with their screens and the touches on them; the flash only in the window."""
            for d in self.devs:
                link = d.link
                veil = {1: .4, 2: .65}.get(link.state, 0)
                if not link.live:
                    veil = max(veil, .5)
                text = ("another viewer took the screen, click to take it back" if link.taken
                        else "connecting..." if not link.live else None)
                paint_device(p, d.L, d.look["profile"], d.image, d.pressed, veil, text, led_mode=link.led)
                if flash and now < self.flash_until:          # a screenshot: the glass flashes
                    p.save()
                    p.setClipPath(rounded(d.L["active"], d.L["active_r"]), Qt.ClipOperation.IntersectClip)
                    p.fillRect(QRectF(*d.L["active"]), rgb(255, 255, 255, .7 * (self.flash_until - now) / .35))
                    p.restore()
            self.touches = [t for t in self.touches if now - t[3] < TOUCH_S]
            p.setBrush(Qt.BrushStyle.NoBrush)
            for d, x, y, t in self.touches:
                a = 1 - (now - t) / TOUCH_S
                ax, ay, aw, ah = d.L["active"]
                rad = (3 + 5 * (1 - a)) * d.L["s"]
                p.setPen(QPen(rgb(*TOUCH, .8 * a), 2))
                p.drawEllipse(QPointF(ax + (x + .5) * aw / d.look["w"], ay + (y + .5) * ah / d.look["h"]), rad, rad)
            if self.wheel and (flash or self.touches_act.isChecked()):
                # Two fingers as iOS shows a trackpad scroll: one grey disc that stays under the cursor
                # while the screen moves beneath it.
                d, x, y = self.wheel[:3]
                bx, by = self.screen_xy(d, QPointF(x, y))
                ax, ay, aw, ah = d.L["active"]
                at = QPointF(ax + (bx + .5) * aw / d.look["w"], ay + (by + .5) * ah / d.look["h"])
                r = 4 * d.L["s"]
                p.save()
                p.setClipPath(rounded(d.L["active"], d.L["active_r"]), Qt.ClipOperation.IntersectClip)
                p.setPen(QPen(rgb(255, 255, 255, .55), 1.5))
                p.setBrush(rgb(140, 140, 140, .45))
                p.drawEllipse(at, r, r)
                p.restore()

        def device_bounds(self):
            """Where the devices are, their buttons included: (x0, y0, x1, y1)."""
            self.arrange()
            boxes = []
            for d in self.devs:
                (bx, by, bw, bh), (left, top, right, bottom) = d.L["body"], d.L["over"]
                boxes.append((bx - left, by - top, bx + bw + right, by + bh + bottom))
            return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                    max(b[3] for b in boxes))

        def devices_size(self):
            x0, y0, x1, y1 = self.device_bounds()
            dpr = self.devicePixelRatioF()
            return int(math.ceil((x1 - x0) * dpr)), int(math.ceil((y1 - y0) * dpr))

        def render_devices(self, size=None, ground=None):
            """The devices alone, through the window's own drawing, without its buttons, handles, toast or
            flash: an image of size (pixels; default their own) with them fitted and centred on ground
            (default transparent)."""
            x0, y0, x1, y1 = self.device_bounds()
            dpr = self.devicePixelRatioF()
            size = size or self.devices_size()
            img = QImage(size[0], size[1], QImage.Format.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(ground or Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(AA)
            p.setRenderHint(SMOOTH)
            lw, lh = size[0] / dpr, size[1] / dpr
            k = min(lw / (x1 - x0), lh / (y1 - y0))
            p.translate((lw - (x1 - x0) * k) / 2, (lh - (y1 - y0) * k) / 2)
            p.scale(k, k)
            p.translate(-x0, -y0)
            self.paint_devices(p, time.monotonic(), flash=False)
            p.end()
            return img

        def animate(self):
            """Redraws while a flash, a toast or a touch is on, then stops."""
            self.update()
            if time.monotonic() > max(self.flash_until + .4, self.toast[1] if self.toast else 0,
                                      self.touch_at + TOUCH_S):
                self.anim.stop()

        def touch(self, d, x, y, every=0.0):
            """A ring where the finger is, if touches are shown; while it moves, one every so often."""
            now = time.monotonic()
            if self.touches_act.isChecked() and now - self.touch_at >= every:
                self.touches.append((d, x, y, now))
                self.touch_at = now
                self.anim.start()

        def show_touches(self, on):
            self.settings.setValue("show_touches", on)
            if not on:
                self.touches.clear()
                self.update()

        def notice(self, text, flash=False):
            now = time.monotonic()
            if flash:
                self.flash_until = now + .35
            self.toast = (text, now + 2.5)
            self.anim.start()
            self.update()

        # -- from the links ---------------------------------------------------------------------------------
        def on_board(self, i, info):
            d = self.devs[i]
            look = board_look(info)
            if (look["w"], look["h"]) != (d.look["w"], d.look["h"]):
                d.image = None
            d.look, d.icon_dirty = look, True
            d.pressed.clear()
            print("%s %s: %s, %dx%d" % (time.strftime("%H:%M:%S"), d.link.host, look["name"], look["w"], look["h"]),
                  flush=True)
            self.apply_zoom(self.zoom, say=False)
            if sound and i == 0 and d.link.audio_capable and self.audio_io.listen_link is None:
                self.audio_io.listen(d.link, 2)

        def on_frame(self, i):
            d = self.devs[i]
            with d.link.lock:
                fb = d.link.fb
                if fb is None:
                    return
                d.image = QImage(fb.tobytes(), fb.shape[1], fb.shape[0], fb.shape[1] * 2,
                                 QImage.Format.Format_RGB16).copy()
            d.icon_dirty = True
            if d.L:
                self.update(QRectF(*d.L["glass"]).adjusted(-2, -2, 2, 2).toAlignedRect())
            else:
                self.update()

        def on_status(self, i, text):
            d = self.devs[i]
            if text and text != d.said and not text.startswith("connecting"):
                print("%s %s" % (time.strftime("%H:%M:%S"), text), flush=True)
            d.said = text
            self.update()

        def refresh_icon(self):
            """The Dock (taskbar) icon follows the first board's screen: once a second when its pixels changed."""
            d = self.devs[0]
            if not d.icon_dirty:
                return
            d.icon_dirty = False
            size, margin = 512, 44
            probe = device_layout(d.look, 1.0, 0, 0)
            left, top, right, bottom = probe["over"]
            fw, fh = left + probe["body"][2] + right, top + probe["body"][3] + bottom
            s = (size - 2 * margin) / max(fw, fh)
            L = device_layout(d.look, s, (size - fw * s) / 2 + left * s, (size - fh * s) / 2 + top * s)
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(AA)
            p.setRenderHint(SMOOTH)
            paint_device(p, L, d.look["profile"], d.image, set(), {1: .4, 2: .65}.get(d.link.state, 0), None,
                         led_mode=d.link.led, labels=False)
            p.end()
            app.setWindowIcon(QIcon(pm))

        # -- mouse ------------------------------------------------------------------------------------------
        def where(self, pos):
            lay = self.arrange()
            x, y = pos.x(), pos.y()
            for name, (bx, by, bw, bh) in lay["buttons"].items():
                if (x - bx - bw / 2) ** 2 + (y - by - bh / 2) ** 2 <= (bw / 2) ** 2:
                    return name, None, None
            for side, (cx, cy) in lay["corners"]:                # any corner resizes, as a window's does
                if abs(x - cx) <= CORNER and abs(y - cy) <= CORNER:
                    return "corner", side, None
            for d in self.devs:
                for k in d.L["keys"]:
                    if key_hit(k, pos):
                        return "key", k["role"], d
            for d in self.devs:
                if on_glass(d.L, pos):
                    return "glass", None, d
            for d in self.devs:
                if rounded(d.L["body"], d.L["body_r"]).contains(pos):
                    return "body", None, d
            return "none", None, None

        def screen_xy(self, d, pos):
            ax, ay, aw, ah = d.L["active"]
            w, h = d.look["w"], d.look["h"]
            x = int((pos.x() - ax) * w / aw)
            y = int((pos.y() - ay) * h / ah)
            return max(0, min(w - 1, x)), max(0, min(h - 1, y))

        def press(self, d, role, down):
            if down == (role in d.pressed):
                return
            (d.pressed.add if down else d.pressed.discard)(role)
            if d.look["profile"].get("hold", True):
                d.link.key(role, down)
            elif not down:
                d.link.tap(role)
            self.update()

        def start_move(self, e):
            self.activateWindow()                             # the system's move takes the click: keys come here
            handle = self.windowHandle()
            if not (handle and handle.startSystemMove()):
                self.drag_from = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mousePressEvent(self, e):
            if e.button() != Qt.MouseButton.LeftButton:
                return super().mousePressEvent(e)
            pos = e.position()
            self.wheel_up()
            what, role, d = self.where(pos)
            if d is not None:
                self.active_dev = d
            if what in ("talk", "listen"):
                self.audio_action(what, 2)
                return
            if what in ("rec", "shot", "close"):
                self.button_down = what
            elif what == "key":
                self.mouse_key = (d, role)
                self.press(d, role, True)
            elif what == "glass" and d.link.taken:
                d.link.take_back()
                self.update()
            elif what == "glass" and d.look["touch"]:
                self.finger = d
                at = self.screen_xy(d, pos)
                d.link.pointer(*at, True)
                self.touch(d, *at)
            elif what == "corner":
                at, corners = e.globalPosition(), dict(self.arrange()["corners"])
                (x0, y0), (x1, y1) = corners[(-1, -1)], corners[(1, 1)]
                ox, oy = corners[(-role[0], -role[1])]       # the opposite corner stays where it is on the desk
                self.grip = dict(side=role, x0=at.x(), y0=at.y(), zoom=self.zoom, w=x1 - x0, h=y1 - y0,
                                 anchor=(self.x() + ox, self.y() + oy))
            elif what in ("move", "body", "glass"):
                self.start_move(e)

        def mouseMoveEvent(self, e):
            pos = e.position()
            if self.finger:
                at = self.screen_xy(self.finger, pos)
                self.finger.link.pointer(*at, True)
                self.touch(self.finger, *at, every=.08)
            elif self.grip:
                g, at = self.grip, e.globalPosition()
                sx, sy = g["side"]
                # Only the drag along the corner's diagonal counts; across it, nothing changes.
                dx, dy = sx * (at.x() - g["x0"]), sy * (at.y() - g["y0"])
                grow = 1 + (dx * g["w"] + dy * g["h"]) / (g["w"] ** 2 + g["h"] ** 2)
                self.apply_zoom(g["zoom"] * grow, say=False)
                ox, oy = dict(self.arrange()["corners"])[(-sx, -sy)]
                self.move(round(g["anchor"][0] - ox), round(g["anchor"][1] - oy))
            elif self.drag_from is not None:
                self.move(e.globalPosition().toPoint() - self.drag_from)
            elif not e.buttons():
                what, _role, d = self.where(pos)
                shape = (Qt.CursorShape.PointingHandCursor if what == "key" or what == "glass" and
                         (d.look["touch"] or d.link.taken) else Qt.CursorShape.OpenHandCursor if what == "move"
                         else (Qt.CursorShape.SizeFDiagCursor if _role[0] == _role[1] else Qt.CursorShape.SizeBDiagCursor)
                         if what == "corner" else Qt.CursorShape.ArrowCursor)
                if self.cursor().shape() != shape:
                    self.setCursor(shape)

        def mouseReleaseEvent(self, e):
            if e.button() != Qt.MouseButton.LeftButton:
                return super().mouseReleaseEvent(e)
            if self.finger:
                self.finger.link.pointer(*self.screen_xy(self.finger, e.position()), False)
                self.finger = None
            if self.mouse_key:
                self.press(*self.mouse_key, False)
                self.mouse_key = None
            self.grip = self.drag_from = None
            down, self.button_down = self.button_down, None
            if down and self.where(e.position())[0] == down:
                {"rec": self.record, "shot": self.screenshot, "close": self.close}[down]()

        # -- two fingers on a trackpad (or a wheel) over a touch screen: one finger on it ------------------------
        def wheelEvent(self, e):
            phase = e.phase()
            if phase == Qt.ScrollPhase.ScrollMomentum:        # the fingers left the trackpad: the board's list coasts
                return self.wheel_up()
            if self.wheel is None:
                what, _role, d = self.where(e.position())
                if phase == Qt.ScrollPhase.ScrollEnd or what != "glass" or not d.look["touch"] or d.link.taken \
                        or self.finger:
                    return super().wheelEvent(e)
                cx, cy = e.position().x(), e.position().y()
                self.wheel = [d, cx, cy, cx, cy, None, False]
                self.update(QRectF(*d.L["glass"]).toAlignedRect())
            if phase == Qt.ScrollPhase.ScrollEnd:
                return self.wheel_up()
            d, cx, cy, x, y, axis, down = self.wheel
            # A pause ends the touch: a wheel or most touchpads off a Mac send no end, and a Mac's can come late.
            self.wheel_idle.start(150)
            # The content follows the delta as in any scrolled view, so the finger does: the system's
            # natural-scrolling setting is already in it. A wheel notch has no pixels: 40 points.
            px = e.pixelDelta()
            dx, dy = (px.x(), px.y()) if not px.isNull() else (e.angleDelta().x() / 3, e.angleDelta().y() / 3)
            if axis is None and max(abs(dx), abs(dy)) >= 2:   # the first clear move picks the axis, as scroll views do
                axis = "h" if abs(dx) > abs(dy) else "v"
                self.wheel[5] = axis
            if axis is None:
                return
            dx, dy = (dx, 0) if axis == "h" else (0, dy)
            if QRectF(*d.L["active"]).contains(QPointF(x + dx, y + dy)):
                x, y = x + dx, y + dy
                start, at = self.screen_xy(d, QPointF(cx, cy)), self.screen_xy(d, QPointF(x, y))
                if down:
                    d.link.pointer(*at, True)
                elif math.hypot(at[0] - start[0], at[1] - start[1]) >= WHEEL_SLOP:
                    # Down only once the move is past what the board takes for a tap, and the move right
                    # after it: a short scroll must not click what is under the cursor.
                    d.link.pointer(*start, True)
                    d.link.pointer(*at, True)
                    down = True
            else:                                             # off the glass: lift; the next stretch starts at the cursor
                if down:
                    d.link.pointer(*self.screen_xy(d, QPointF(x, y)), False)
                x, y, down = cx, cy, False
            self.wheel[3:] = [x, y, axis, down]
            self.update(QRectF(*d.L["glass"]).toAlignedRect())

        def wheel_up(self):
            self.wheel_idle.stop()
            if self.wheel:
                d, _cx, _cy, x, y, _axis, down = self.wheel
                if down:
                    d.link.pointer(*self.screen_xy(d, QPointF(x, y)), False)
                self.wheel = None
                self.update(QRectF(*d.L["glass"]).toAlignedRect())

        def event(self, e):
            if e.type() == QEvent.Type.ToolTip:
                what = self.where(QPointF(e.pos()))[0]
                tip = {"move": "Move the window (or drag a device)", "talk": "Toggle microphone to selected board", "listen": "Toggle selected board speaker audio",
                       "rec": "Record / Stop (%s)" % self.native("Ctrl+R"),
                       "shot": "Screenshot (%s)" % self.native("Ctrl+S"), "close": "Close"}.get(what)
                if tip:
                    QToolTip.showText(e.globalPos(), tip, self)
                else:
                    QToolTip.hideText()
                    e.ignore()
                return True
            return super().event(e)

        @staticmethod
        def native(keys):
            return QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText)

        # -- keyboard ---------------------------------------------------------------------------------------
        def device_under_mouse(self):
            at = self.mapFromGlobal(QCursor.pos())
            for d in self.devs:
                if d.slot and d.slot.contains(QPointF(at)):
                    return d
            return self.devs[0]

        def keyPressEvent(self, e):
            d = self.device_under_mouse()
            edits = {Qt.Key.Key_Backspace: "Backspace", Qt.Key.Key_Delete: "Delete",
                     Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right", Qt.Key.Key_Return: "Enter",
                     Qt.Key.Key_Enter: "Enter", Qt.Key.Key_Escape: "Escape"}
            paste = app.clipboard().text() if d.link.text_open and e.matches(QKeySequence.StandardKey.Paste) else None
            modified = bool(e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
            if route_text_key(d.link, edits.get(e.key()), e.text(), paste=paste, modified=modified):
                e.accept()
                return
            role = KEY_ROLES.get(e.key())
            mods = e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier |
                                    Qt.KeyboardModifier.AltModifier)
            if not role or mods:
                return super().keyPressEvent(e)
            if e.isAutoRepeat():
                return
            d = self.device_under_mouse()
            if role <= 4:
                self.held[role] = d
                self.press(d, role, True)
            else:
                d.link.key(role, True)
                d.link.key(role, False)

        def keyReleaseEvent(self, e):
            role = KEY_ROLES.get(e.key())
            if role in self.held and not e.isAutoRepeat():
                self.press(self.held.pop(role), role, False)
            elif not role:
                super().keyReleaseEvent(e)

        def let_go(self):
            """Every key and the finger let go, each on the board that got it."""
            for role, d in list(self.held.items()):
                self.press(d, role, False)
            self.held.clear()
            if self.mouse_key:
                self.press(*self.mouse_key, False)
                self.mouse_key = None
            for d in self.devs:
                for role in list(d.pressed):
                    self.press(d, role, False)
            if self.finger:
                at = QPointF(self.mapFromGlobal(QCursor.pos()))
                self.finger.link.pointer(*self.screen_xy(self.finger, at), False)
                self.finger = None
            self.wheel_up()

        def focusOutEvent(self, e):
            self.let_go()
            super().focusOutEvent(e)

        def changeEvent(self, e):
            if e.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
                self.let_go()
            super().changeEvent(e)

        # -- menu -------------------------------------------------------------------------------------------
        def audio_action(self, action, source=2):
            link = self.active_dev.link
            if not link.live or not link.audio_capable:
                self.notice("Selected board does not offer audio")
                return
            if action == "talk":
                self.audio_io.talk(link)
            else:
                self.audio_io.listen(link, source)
            self.update()

        def build_actions(self):
            def act(text, keys, fn, enabled=True, checkable=False):
                a = QAction(text, self)
                a.setShortcuts([QKeySequence(k) for k in keys])
                a.setEnabled(enabled)
                a.setCheckable(checkable)
                a.triggered.connect(fn)
                self.addAction(a)
                return a

            self.top_act = act("Float on Top", ["Ctrl+T"], self.toggle_top, checkable=True)
            self.top_act.setChecked(on_top)
            self.touches_act = act("Show touches", [], self.show_touches, checkable=True)
            self.touches_act.setChecked(self.preferences["show_touches"])
            self.settings_act = act("Settings…", ["Ctrl+,"], self.open_settings)
            self.settings_act.setMenuRole(QAction.MenuRole.PreferencesRole)
            self.items = [act("Screenshot", ["Ctrl+S"], self.screenshot), act("Record / Stop", ["Ctrl+R"], self.record),
                          None,
                          act("Sound: hear what the board plays", ["Ctrl+L"], lambda: self.audio_action("listen", 2)),
                          act("Mic: hear the board's mics", ["Ctrl+Shift+L"], lambda: self.audio_action("listen", 1)),
                          act("Talk: your microphone to the board's speaker", ["Ctrl+K"], lambda: self.audio_action("talk")),
                          None,
                          act("Bigger", ["Ctrl+=", "Ctrl++"], lambda: self.apply_zoom(self.zoom * 1.25)),
                          act("Smaller", ["Ctrl+-"], lambda: self.apply_zoom(self.zoom * .8)),
                          act("Actual size", ["Ctrl+0"], lambda: self.apply_zoom(1.0)),
                          None, self.touches_act, self.top_act, None, self.settings_act]
            self.quit_act = act("Quit espmirror mirror", ["Ctrl+Q"], self.close)
            self.quit_act.setMenuRole(QAction.MenuRole.QuitRole)

        def fill_menu(self, menu):
            menu.setToolTipsVisible(True)
            for a in self.items:
                if a is None:
                    menu.addSeparator()
                else:
                    menu.addAction(a)
            return menu

        def contextMenuEvent(self, e):
            menu = self.fill_menu(QMenu(self))
            menu.addSeparator()
            menu.addAction(self.quit_act)
            menu.exec(e.globalPos())

        def toggle_top(self, on):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            self.show()
            self.settings.setValue("on_top", on)

        def open_settings(self):
            before = read_preferences(self.settings)
            if not edit_preferences(self, self.settings):
                return
            self.preferences = read_preferences(self.settings)
            after = self.preferences
            if after["on_top"] != before["on_top"]:
                self.top_act.setChecked(after["on_top"])
                self.toggle_top(after["on_top"])
            self.touches_act.setChecked(after["show_touches"])
            self.show_touches(after["show_touches"])
            if after["scale"] != before["scale"]:
                self.apply_zoom(after["scale"], say=False)
            self.notice("settings saved")

        def restore_position(self):
            position = self.settings.value("position") if self.preferences["remember_position"] else None
            screen = QGuiApplication.screenAt(position) if isinstance(position, QPoint) else None
            area = (screen or QGuiApplication.primaryScreen()).availableGeometry()
            if not isinstance(position, QPoint):
                position = QPoint(area.center().x() - self.width() // 2,
                                  area.center().y() - self.height() // 2)
            self.move(max(area.left(), min(position.x(), area.right() + 1 - self.width())),
                      max(area.top(), min(position.y(), area.bottom() + 1 - self.height())))

        # -- screenshot, recording --------------------------------------------------------------------------
        def screenshot(self):
            shots = [d for d in self.devs if d.image is not None]
            if not shots:
                self.notice("no frame yet")
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            paths = []
            for i, d in enumerate(shots):
                path = capture_path("png", "" if len(shots) == 1 else "-%d" % (i + 1), stamp,
                                    self.preferences["screenshots"])
                if not d.image.save(path):
                    self.notice("could not save screenshot: " + path)
                    return
                paths.append(path)
            paths.append(capture_path("png", "-window", stamp, self.preferences["screenshots"]))
            if not self.render_devices().save(paths[-1]):
                self.notice("could not save screenshot: " + paths[-1])
                return
            for path in paths:
                print("saved", path, flush=True)
            self.notice("saved " + os.path.basename(paths[0]), flash=True)

        def video_frame(self):
            """The devices as one video frame: on a dark ground (mp4 has no transparency), at the recording's size."""
            return bytes(self.render_devices(self.rec["size"], rgb(0x1e, 0x1f, 0x22)).constBits())

        def record(self):
            if self.rec:
                self.rec_timer.stop()
                rec, self.rec = self.rec, None
                rec["queue"].put(None)
                self.notice("recording stopped, encoding...")
                return
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                self.notice("recording needs ffmpeg")
                return
            w, h = self.devices_size()
            size = (w + w % 2, h + h % 2)                     # x264 wants even sides; kept for the whole take
            path = capture_path("mp4", folder=self.preferences["recordings"])
            if not os.path.isdir(os.path.dirname(path)):
                self.notice("recording folder is unavailable: " + os.path.dirname(path))
                return
            try:
                proc = subprocess.Popen([ffmpeg, "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
                                         "-s", "%dx%d" % size, "-r", "25", "-i", "-", "-c:v", "libx264",
                                         "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", path],
                                        stdin=subprocess.PIPE)
            except OSError as error:
                self.notice("could not start recording: " + str(error))
                return
            q = queue.Queue(maxsize=50)

            def write():
                ok = True
                while True:
                    frame = q.get()
                    if frame is None:
                        break
                    if ok:
                        try:
                            proc.stdin.write(frame)
                        except OSError:
                            ok = False
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                good = proc.wait() == 0 and ok
                print(("saved " if good else "ffmpeg failed: ") + path, flush=True)
                bridge.saved.emit(("saved " if good else "ffmpeg failed, ") + os.path.basename(path))

            self.rec = dict(size=size, queue=q, t0=time.monotonic(), n=0, process=proc,
                            thread=threading.Thread(target=write))
            self.rec["thread"].start()
            self.rec_timer.start()
            self.notice("recording")

        def rec_frame(self):
            """25 frames a second by the clock: a late tick repeats the frame it has."""
            rec = self.rec
            if not rec:
                return
            if rec["process"].poll() is not None:
                self.record()
                return
            due = int((time.monotonic() - rec["t0"]) * 25) + 1
            if due <= rec["n"]:
                return
            frame = self.video_frame()
            for _ in range(min(10, due - rec["n"])):
                try:
                    rec["queue"].put_nowait(frame)
                except queue.Full:
                    break
            rec["n"] = due

        def closeEvent(self, e):
            if self.closing:
                e.accept()
                return
            self.closing = True
            stop_links(links)
            if self.preferences["remember_position"] and self.isVisible():
                self.settings.setValue("position", self.pos())
            self.settings.sync()
            self.audio_io.close()
            rec = self.rec
            if rec:
                self.record()
                rec["thread"].join(30)
            join_links(links, timeout=1)
            super().closeEvent(e)

    links = [make_link(h, p, log_file=log_file, gateway=iris_gateway) for h, p in targets]
    bridge = Bridge()
    win = Window(links)
    bridge.board.connect(win.on_board)
    bridge.frame.connect(win.on_frame)
    bridge.status.connect(win.on_status)
    bridge.saved.connect(win.notice)
    try:
        for i, link in enumerate(links):
            link.on_board = lambda info, i=i: bridge.board.emit(i, info)
            link.on_frame = lambda i=i: bridge.frame.emit(i)
            link.on_status = lambda text, i=i: bridge.status.emit(i, text)
            link.start()
        if sys.platform == "darwin":                          # the View menu in the menu bar too
            bar = QMenuBar(None)
            win.fill_menu(bar.addMenu("View")).addAction(win.quit_act)
            win.menu_bar = bar
        win.apply_zoom(scale, say=False)
        win.restore_position()
        win.show()
        win.raise_()
        win.activateWindow()
        win.windowHandle().screenChanged.connect(win.screen_changed)
        win.refresh_icon()
        win.icon_timer.start()
        return app.exec()
    finally:
        try:
            if not win.closing:
                win.close()
        finally:
            join_links(links)                # keep Qt objects and stdout alive until every worker has finished


def parse_host(text):
    if text.startswith("iris:"):
        if not re.fullmatch(r"iris:[0-9a-fA-F]{32}", text):
            raise ValueError("Iris target needs a 32-digit hexadecimal device ID")
        return text, PORT
    host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
    return (host, int(port)) if port else (text, PORT)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("hosts", nargs="*", help="board address[:port] or serial port; none: USB boards (add --wifi for mDNS)")
    ap.add_argument("--list", action="store_true", help="list USB boards; add --wifi for network boards")
    ap.add_argument("--sound", action=argparse.BooleanOptionalAction, default=None,
                    help="start listening to the first board's speaker")
    ap.add_argument("--settings", action="store_true", help="edit viewer preferences without connecting to boards")
    ap.add_argument("--stats", action="store_true", help="print headless frame statistics")
    ap.add_argument("--seconds", type=float, default=5, help="duration for --stats")
    ap.add_argument("--wifi", action=argparse.BooleanOptionalAction, default=None,
                    help="also discover network boards over mDNS")
    ap.add_argument("--usb", action=argparse.BooleanOptionalAction, default=None, help="discover USB boards")
    ap.add_argument("--iris", action="store_true", help="include connected Iris devices listed by the Gateway")
    ap.add_argument("--iris-gateway", default=os.environ.get("ESPMIRROR_IRIS_GATEWAY", "http://127.0.0.1:8443"),
                    help="Iris Gateway URL (or ESPMIRROR_IRIS_GATEWAY)")
    ap.add_argument("--log", help="append timestamped cable console lines to FILE")
    ap.add_argument("--png", help="save the first board's screen once it is still, and exit")
    ap.add_argument("--scale", type=float, help="times the real size")
    ap.add_argument("--on-top", action=argparse.BooleanOptionalAction, default=None, help="float on top")
    ap.add_argument("--wall", action="store_true", help="a dark backdrop behind the boards (default with several)")
    ap.add_argument("--devices", help="device photos (default: devices/ next to this file, if there)")
    args = ap.parse_args()

    from espmirror_settings import DEFAULTS, edit_preferences, read_preferences, validate_hosts
    preferences = DEFAULTS.copy()
    graphical = not (args.list or args.stats or args.png)
    if args.settings or graphical:
        if importlib.util.find_spec("PySide6") is None:
            print("the window needs PySide6: pip install PySide6 zeroconf numpy", file=sys.stderr)
            return 1
        if args.settings:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance() or QApplication(sys.argv[:1])
            app.setApplicationName("espmirror")
            edit_preferences()
            return 0
        preferences = read_preferences()
    for key in ("scale", "on_top", "sound", "wifi", "usb", "log"):
        if getattr(args, key) is None:
            setattr(args, key, preferences[key])
    if not math.isfinite(args.scale) or args.scale <= 0:
        ap.error("--scale must be finite and positive")

    explicit = list(args.hosts)
    if not explicit and graphical:
        try:
            explicit = validate_hosts(preferences["hosts"]).split()
        except ValueError as error:
            print("invalid saved targets: %s Use --settings to edit them." % error, file=sys.stderr)
            return 1
    if args.list or not args.hosts:
        usb = usb_boards() if args.usb else []
        on_usb = {mac for _port, mac in usb if mac}
        net = [b for b in (discover() if args.wifi else []) if not b[3] or b[3] not in on_usb]    # a board on both: the cable
        iris = []
        if args.iris:
            from espmirror_iris import discover as discover_iris
            try:
                iris = discover_iris(args.iris_gateway)
            except (OSError, ValueError) as error:
                print("Iris discovery: %s" % error, file=sys.stderr)
        if args.list:
            for port, mac in usb:
                print("%-40s %s" % ("USB " + mac, port))
            for name, addr, port, _mac in net:
                print("%-40s %s:%d" % (name, addr, port))
            for item in iris:
                print("%-40s iris:%s" % (item.get("alias", "ESP-Iris"), item["device_id"]))
            return 0
        if not usb and not net and not iris and not explicit:
            print("no board answers as a mirror; pass its serial port or address, add --wifi for mDNS, "
                  "or use --settings to edit viewer preferences", file=sys.stderr)
            return 1
        targets = [(port, PORT) for port, _mac in usb] + [(addr, port) for _name, addr, port, _mac in net]
        targets += [("iris:" + item["device_id"], PORT) for item in iris]
        targets += [(h, PORT) if is_port(h) else parse_host(h) for h in explicit]
        targets = list(dict.fromkeys(targets))
    else:
        targets = [(h, PORT) if is_port(h) else parse_host(h) for h in args.hosts]

    if args.stats:
        if args.seconds <= 0:
            ap.error("--seconds must be positive")
        link = make_link(*targets[0], log_file=args.log, gateway=args.iris_gateway)
        try:
            link.start()
            start = time.monotonic()
            time.sleep(args.seconds)
            elapsed = time.monotonic() - start
            print("%d updates in %.2f s, %.1f updates/s" % (link.changes, elapsed, link.changes / elapsed))
            if isinstance(link, IrisLink):
                print("%d RGB565 bytes, %.0f bytes/s" % (link.bytes_received, link.bytes_received / elapsed))
            if args.png:
                return save_png(link, args.png)
            return 0 if link.fb is not None else 1
        finally:
            link.close()
    if args.png:
        link = make_link(*targets[0], log_file=args.log, gateway=args.iris_gateway)
        link.on_status = lambda text: text and not text.startswith("connecting") and print(text, file=sys.stderr)
        try:
            return save_png(link.start(), args.png)
        finally:
            link.close()
    devices = args.devices or os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices")
    if args.devices and not os.path.isdir(args.devices):
        print("no such directory: %s" % args.devices, file=sys.stderr)
        return 1
    return run_window(targets, args.scale, args.on_top, args.wall or len(targets) > 1,
                      devices if os.path.isdir(devices) else None, log_file=args.log, sound=args.sound,
                      iris_gateway=args.iris_gateway)


if __name__ == "__main__":
    sys.exit(main())
