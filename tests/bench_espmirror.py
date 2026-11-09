#!/usr/bin/env python3
# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0

"""Benchmark production C encoders against synthetic full-screen LVGL-like UI frames.

No Qt, IDF, board or network required. Output is CSV; diagnostics go to stderr.
Individual codings encode one full-screen rectangle even if it exceeds the normal
message capacity; select uses production dirty/band scheduling with 32 KiB messages.
Bytes include F and rectangle headers, exclude the transport's four-byte length.
Timing includes emission and, for delta/select, reference updates. It excludes
fixture generation, process startup, input I/O, allocation and reference reset.
"""

import argparse
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from fake_board import text

NAMES = ('raw', 'row-rle', 'column-rle', 'delta-rle', 'qoi', 'select')


def sprite(phase):
    y, x = np.indices((120, 120))
    dither = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]])[y % 4, x % 4] / 16
    radius = np.sqrt((x - 59.5) ** 2 + (y - 59.5) ** 2)
    light = np.clip((1 - radius / 62) * .6 + .35 + .15 * np.sin(x / 12 + phase), 0, 1)
    red = np.clip(np.floor(light * 22 + dither), 0, 31).astype(np.uint16)
    green = np.clip(np.floor(light * 53 + dither), 0, 63).astype(np.uint16)
    blue = np.clip(np.floor(light * 31 + dither), 0, 31).astype(np.uint16)
    result = red << 11 | green << 5 | blue
    result[radius > 57 + 2 * np.sin(phase)] = 0x0841
    eye_y = 49 + int(3 * np.sin(phase))
    result[eye_y:eye_y + 7, 37:44] = 0xffff
    result[eye_y:eye_y + 7, 76:83] = 0xffff
    return result


def page(w, h, phase=0):
    fb = np.full((h, w), 0x0841, np.uint16)
    text(fb, 12, 12, 'MIRROR', 0xffff, 2)
    x, y = (w - 120) // 2, (h - 120) // 2
    fb[y:y + 120, x:x + 120] = sprite(phase)
    text(fb, 12, h - 26, 'READY', 0x7bef, 2)
    return fb


def list_page(w, h, offset):
    fb = np.full((h + 80, w), 0x0841, np.uint16)
    for y in range(-(offset % 64), h + 64, 64):
        top, bottom = max(0, y), min(h + 80, y + 54)
        if top < bottom:
            fb[top:bottom, 8:w - 8] = 0x18e3
        if y >= 0:
            text(fb, 18, y + 10, 'DEVICE %02d' % ((y + offset) // 64), 0xffff, 2)
            text(fb, 18, y + 32, 'CONNECTED', 0x7bef)
    return fb[:h].copy()


def scenes(w, h):
    yield 'sprite', np.stack([page(w, h, f * .16) for f in range(24)])
    strip = np.concatenate((page(w, h), list_page(w, h, 0)), axis=1)
    yield 'slide12', np.stack([strip[:, x:x + w] for x in np.linspace(0, w, 12, dtype=int)])
    yield 'list', np.stack([list_page(w, h, f * 3) for f in range(24)])


def s3_us(coding, pixels, size):
    """Uncalibrated scalar instruction/branch budget, at 240 cycles/us.

    raw: 8 cycles/pixel; row/column: 12/20 + 16/run; delta: 18 + 16/run
    + 4/pixel reference copy; QOI: 40/pixel + 8/output byte. Column's larger
    budget models strided loads. No PSRAM stalls, RTOS, radio or LVGL contention.
    The select value is a full-pass budget, ignoring early termination; it is
    deliberately pessimistic and includes another QOI-sized emission budget.
    """
    payload = max(0, size - 22)
    if coding == 0:
        cycles = 8 * pixels
    elif coding in (1, 2, 3):
        cycles = (12, 20, 22)[coding - 1] * pixels + 4 * payload
    elif coding == 4:
        cycles = 40 * pixels + 8 * payload
    else:
        # Counts: row + column + delta + QOI. Emission <= QOI budget;
        # raw/row/column/delta winner <= 2 bytes/pixel. Reference copy: 4/px.
        # Allow both whole-rectangle and band attempts (a conservative 2x).
        cycles = 2 * (28 + 36 + 34 + 72 + 56 + 4) * pixels
    return cycles / 240


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repeats', type=int, default=10)
    args = ap.parse_args()
    if args.repeats < 1:
        ap.error('--repeats must be positive')
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / 'mirror-bench'
        cc = shlex.split(os.environ.get('CC', 'cc'))
        command = cc + ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'tests/espmirror_bench.c'), '-o', str(exe)]
        subprocess.run(command, check=True, env={**os.environ, 'TMPDIR': tmp})
        print('Host: %s; compiler: %s; flags: -std=c11 -O2; repeats: %d' %
              (platform.platform(), shlex.join(cc), args.repeats), file=sys.stderr)
        print('S3 estimates are uncalibrated cycle budgets at 240 MHz, not measurements; '
              'allow at least 0.5x-2x for individual codecs, plus PSRAM/contention. '
              'Select is a pessimistic full-pass budget.', file=sys.stderr)
        print('width,height,scene,frames,coding,bytes_per_frame,host_us_per_frame,s3_budget_us_per_frame')
        for w, h in ((172, 640), (466, 466)):
            for name, frames in scenes(w, h):
                result = subprocess.run([str(exe), str(w), str(h), str(len(frames)), str(args.repeats)],
                                        input=frames.astype('<u2').tobytes(), capture_output=True, check=True)
                for line in result.stdout.decode().splitlines():
                    coding, size, us, _checksum = line.split(',')
                    coding, size = int(coding), float(size)
                    print('%d,%d,%s,%d,%s,%.1f,%s,%.1f' %
                          (w, h, name, len(frames), NAMES[coding], size, us, s3_us(coding, w*h, size)))


if __name__ == '__main__':
    main()
