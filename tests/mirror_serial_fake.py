# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0


"""The pyserial subset used by Link, backed by a real POSIX PTY for host tests."""

import array
import fcntl
import os
import select
import termios
import time
import tty


class Serial:
    def __init__(self, port=None, baudrate=115200, timeout=.1, write_timeout=2, **_flags):
        self.port, self.timeout, self.write_timeout = port, timeout, write_timeout
        self.fd = -1
        self.dtr = self.rts = False
        if port:
            self.open()

    def open(self):
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)

    @property
    def in_waiting(self):
        count = array.array('i', [0])
        fcntl.ioctl(self.fd, termios.FIONREAD, count, True)
        return count[0]

    def read(self, n):
        out = bytearray()
        until = time.monotonic() + self.timeout
        while len(out) < n:
            fd = self.fd
            if fd < 0:
                raise OSError('closed')
            left = max(0, until - time.monotonic())
            readable, _, _ = select.select([fd], [], [], left)
            if not readable:
                break
            try:
                data = os.read(fd, n - len(out))
            except BlockingIOError:
                continue
            if not data:
                raise OSError('closed')
            out += data
            if time.monotonic() >= until:
                break
        return bytes(out)

    def write(self, data):
        done = 0
        until = time.monotonic() + self.write_timeout
        while done < len(data):
            if self.fd < 0:
                raise OSError('closed')
            _, writable, _ = select.select([], [self.fd], [], max(0, until - time.monotonic()))
            if not writable:
                raise OSError('write timeout')
            try:
                done += os.write(self.fd, data[done:])
            except BlockingIOError:
                pass
        return done

    def fileno(self):
        return self.fd

    def close(self):
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)
