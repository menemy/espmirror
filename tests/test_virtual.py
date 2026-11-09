# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VirtualTests(unittest.TestCase):
    def compile(self, source, args, run=False):
        with tempfile.TemporaryDirectory() as tmp:
            if isinstance(source, str):
                path = Path(tmp) / 'test.c'
                path.write_text(source)
            else:
                path = source
            exe = Path(tmp) / 'test'
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-I' + str(ROOT / 'tests/idf_stubs'), '-I' + str(ROOT / 'include'),
                *args, str(path)]
            command += ['-fsanitize=address,undefined', '-o', str(exe)] if run else ['-fsyntax-only']
            result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, 'TMPDIR': tmp})
            self.assertEqual(result.returncode, 0, result.stderr)
            if run:
                result = subprocess.run([exe], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_dimensions_round_and_disabled_boot(self):
        for w, h, round_, gpio in ((240, 240, 0, 0), (172, 640, 0, -1), (466, 466, 1, 0)):
            with self.subTest(width=w, height=h, round=round_, gpio=gpio):
                self.compile(ROOT / 'examples/virtual/main/main.c', [
                    '-DCONFIG_IDF_TARGET_ESP32S3=1', '-DCONFIG_VIRTUAL_NAME="Virtual"',
                    '-DCONFIG_VIRTUAL_DIAGONAL_100=200', '-DCONFIG_VIRTUAL_BOOT_GPIO=%d' % gpio,
                    '-DCONFIG_VIRTUAL_WIDTH=%d' % w, '-DCONFIG_VIRTUAL_HEIGHT=%d' % h,
                    *(['-DCONFIG_VIRTUAL_ROUND=1'] if round_ else [])])

    def test_device_presets(self):
        for device in ('WAVESHARE_216', 'WAVESHARE_154G', 'WAVESHARE_175C', 'WAVESHARE_349',
                       'WAVESHARE_P4_35', 'AIPI_LITE', 'ESP_MOSAICO', 'SENSECAP_WATCHER'):
            with self.subTest(device=device):
                self.compile(ROOT / 'examples/virtual/main/main.c', [
                    '-DCONFIG_IDF_TARGET_ESP32S3=1', '-DCONFIG_VIRTUAL_BOOT_GPIO=0',
                    '-DCONFIG_VIRTUAL_DEVICE_%s=1' % device])

    def test_boot_debounce_and_wrap(self):
        source = (ROOT / 'examples/virtual/main/main.c').read_text()
        functions = source[source.index('static void on_key'):source.index('static void flush')]
        harness = '''#include <assert.h>
#include "driver/gpio.h"
#include "espmirror.h"
static QueueHandle_t s_keys;
static int level = 1, presses, last;
int gpio_get_level(int gpio) { assert(gpio == 0); return level; }
int xQueueSend(QueueHandle_t q, const void *data, int ticks) {
    (void)q; assert(ticks == 0); last = *(const int *)data;
    presses++; return pdTRUE;
}
''' + functions + '''
int main(void) {
    boot_poll(0);
    level = 0; boot_poll(10);
    level = 1; boot_poll(20);
    level = 0; boot_poll(22); boot_poll(51); assert(presses == 0);
    boot_poll(52); boot_poll(100); assert(presses == 1);
    level = 1; boot_poll(101); boot_poll(131);
    level = 0; boot_poll(140); boot_poll(170); assert(presses == 2);
    level = 1; boot_poll(200); boot_poll(230);
    level = 0; boot_poll(UINT32_MAX - 10); boot_poll(20); assert(presses == 3 && last == ESPMIRROR_KEY_MAIN);
    on_key(ESPMIRROR_KEY_AUX, true, NULL); assert(presses == 4 && last == ESPMIRROR_KEY_AUX);
    on_key(ESPMIRROR_KEY_POWER, false, NULL);
    on_key(ESPMIRROR_KEY_MAIN, false, NULL); assert(presses == 4);
    on_key(ESPMIRROR_KEY_POWER, true, NULL); assert(presses == 5 && last == ESPMIRROR_KEY_POWER);
    return 0;
}
'''
        self.compile(harness, ['-DCONFIG_VIRTUAL_BOOT_GPIO=0'], run=True)


if __name__ == '__main__':
    unittest.main()
