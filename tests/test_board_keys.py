# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class BoardKeyFilterTest(unittest.TestCase):
    def test_bounce_and_independent_held_keys(self):
        source = r'''
#include <assert.h>
#include "board_keys.h"

int main(void) {
    board_key_filter_t keys = {0};
    const unsigned key = 1u << 1, boot = 1u << 2, power = 1u << 3;
    assert(board_key_filter(&keys, key, 0) == 0);
    assert(board_key_filter(&keys, 0, 10000) == 0);
    assert(board_key_filter(&keys, key, 20000) == 0);
    assert(board_key_filter(&keys, key, 49999) == 0);
    assert(board_key_filter(&keys, key, 50000) == key);

    assert(board_key_filter(&keys, key | boot, 60000) == key);
    assert(board_key_filter(&keys, key | boot, 90000) == (key | boot));
    assert(board_key_filter(&keys, boot, 100000) == (key | boot));
    assert(board_key_filter(&keys, key | boot, 110000) == (key | boot));
    assert(board_key_filter(&keys, boot, 120000) == (key | boot));
    assert(board_key_filter(&keys, boot, 150000) == boot);

    assert(board_key_filter(&keys, boot | power, 160000) == boot);
    assert(board_key_filter(&keys, power, 180000) == boot);
    assert(board_key_filter(&keys, power, 190000) == (boot | power));
    assert(board_key_filter(&keys, power, 210000) == power);
    assert(board_key_filter(&keys, 0, 220000) == power);
    assert(board_key_filter(&keys, 0, 250000) == 0);
    assert(board_key_filter(&keys, 0x100, 300000) == 0);
    assert(board_key_filter(&keys, 0x100, 330000) == 0);
    return 0;
}
'''
        include = Path(__file__).resolve().parents[1] / "examples/lvgl/main"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "keys.c").write_text(source)
            subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(include),
                str(path / "keys.c"), "-o", str(path / "keys"),
            ], check=True, capture_output=True, text=True)
            subprocess.run([str(path / "keys")], check=True, capture_output=True, text=True)
