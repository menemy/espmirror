#!/usr/bin/env python3
"""A board that is not there: serves the mirror protocol on port 7331 with a test pattern, so a device profile
can be looked at in the viewer without the hardware.

  tools/fake_board.py "Waveshare 1.54G" 200 200 [PPI] [--round] [--port N]
  tools/mirror_window.py 127.0.0.1

Sends 'B' with the name and size, one raw frame of a gradient with a border and a moving bar every 40 ms,
and prints the viewer's messages (keys, pointer, listen) as they come.
"""

import select
import socket
import struct
import sys
import time


def frame(w, h, t):
    px = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 128
            if x < 2 or y < 2 or x >= w - 2 or y >= h - 2 or abs(x - int((t * 60) % w)) < 2:
                r, g, b = 255, 255, 255
            px += struct.pack("<H", (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3)
    rect = struct.pack("<HHHHBBI", 0, 0, w, h, 0, 0, len(px)) + px
    return b"F" + bytes([0]) + struct.pack("<HHH", 1, w, h) + rect


def main():
    name, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    ppi = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else 0
    radius = w // 2 if "--round" in sys.argv else 0
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7331
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    print(f"{name}, {w} x {h}: waiting for the viewer on 127.0.0.1:{port}", flush=True)
    while True:
        c, _ = srv.accept()
        if c.recv(6) != b"MIRR1\n":
            c.close()
            continue
        board = ('B{"forms":[],"board":{"name":"%s","w":%d,"h":%d,"ppi":%d,"r":%d}}' % (name, w, h, ppi, radius)).encode()
        c.sendall(struct.pack("<I", len(board)) + board)
        t0 = time.time()
        try:
            while True:
                f = frame(w, h, time.time() - t0)
                c.sendall(struct.pack("<I", len(f)) + f)
                if select.select([c], [], [], 0)[0]:
                    data = c.recv(1024)
                    if not data:
                        break
                    print("from the viewer:", data[4:].hex(), flush=True)
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError):
            pass
        c.close()
        print("viewer left", flush=True)


if __name__ == "__main__":
    main()
