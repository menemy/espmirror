# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Host compile checks use declarations, not an emulation of ESP-IDF."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'C harnesses use POSIX stubs and sanitizers')
class ComponentTests(unittest.TestCase):
    def test_iris_screen_rpc_and_lifecycle(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        start = source.index('#define IRIS_KEY_SERVICE')
        end = source.index('\n#endif\n\nstatic void mirror_task', start)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'iris_impl.inc').write_text(source[start:end])
            exe = folder / 'iris'
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-DCONFIG_ESPMIRROR_TIMING=1',
                '-I' + str(ROOT / 'tests/idf_stubs'), '-I' + str(ROOT / 'include'),
                '-I' + str(folder), '-I' + str(ROOT / 'src'), str(ROOT / 'tests/espmirror_iris_harness.c'),
                str(ROOT / 'src/espmirror_core.c'), str(ROOT / 'src/espmirror_timing.c'), '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'TMPDIR': tmp})
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_component_configurations(self):
        for wifi, audio, usb, iris in ((0, 0, 1, 0), (0, 1, 1, 0), (1, 0, 0, 0), (1, 1, 1, 0),
                                      (0, 0, 0, 1), (1, 1, 1, 1)):
            with self.subTest(wifi=wifi, audio=audio, usb=usb, iris=iris), tempfile.TemporaryDirectory() as tmp:
                command = shlex.split(os.environ.get('CC', 'cc')) + [
                    '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsyntax-only',
                    '-I' + str(ROOT / 'tests/idf_stubs'), '-I' + str(ROOT / 'include'),
                    '-I' + str(ROOT / 'src'), '-DCONFIG_ESPMIRROR_TCP=%d' % wifi,
                    '-DCONFIG_ESPMIRROR_AUDIO=%d' % audio, '-DCONFIG_ESPMIRROR_USB=%d' % usb,
                    '-DCONFIG_ESPMIRROR_TIMING=%d' % iris,
                    '-DCONFIG_ESPMIRROR_IRIS=%d' % iris, '-DCONFIG_ESP_IRIS_MEDIA_LATEST_BYTES=4096',
                    '-DCONFIG_ESP_IRIS_RPC_BODY_BYTES=2048',
                    '-DCONFIG_ESPMIRROR_DELTA=1', '-DCONFIG_ESPMIRROR_QOI=1',
                    str(ROOT / 'src/espmirror.c'), str(ROOT / 'src/espmirror_usb.c'),
                    str(ROOT / 'src/espmirror_lvgl.c'), str(ROOT / 'src/espmirror_timing.c')]
                result = subprocess.run(command, capture_output=True, text=True,
                                        env={**os.environ, 'TMPDIR': tmp})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_pointer_press_move_release_edges(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        declarations = source[source.index('#define PTR_Q'):source.index('/* The TCP side')]
        functions = source[source.index('static void pointer_put'):source.index('/* ---- The public side')]
        harness = '''#include <assert.h>
#include "espmirror.h"
#include "common.h"
static espmirror_config_t s_cfg;
''' + declarations + functions + '''
int main(void) {
    int x, y; bool down;
    pointer_put(10, 20, true);
    pointer_put(30, 40, true);
    pointer_put(50, 60, true);
    pointer_put(50, 60, false);
    assert(espmirror_pointer_read(&x, &y, &down) && x == 10 && y == 20 && down);
    assert(espmirror_pointer_read(&x, &y, &down) && x == 50 && y == 60 && down);
    assert(!espmirror_pointer_read(&x, &y, &down) && x == 50 && !down);
    for (int i = 0; i < 100; i++) pointer_put(i, i, i % 2 == 0);
    while (espmirror_pointer_read(&x, &y, &down)) {}
    assert(!down);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pointer.c'
            path.write_text(harness)
            exe = Path(tmp) / 'pointer'
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-I' + str(ROOT / 'tests/idf_stubs'), '-I' + str(ROOT / 'include'), str(path), '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'TMPDIR': tmp})
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
