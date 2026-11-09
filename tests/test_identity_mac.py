# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Run the startup MAC read on Wi-Fi and non-Wi-Fi chip capabilities."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'C harness uses POSIX compiler and sanitizers')
class IdentityMacTests(unittest.TestCase):
    def test_identity_type_and_read_failure(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        startup = source[source.index('esp_err_t espmirror_start('):]
        block = startup[startup.index('    uint8_t mac[6];'):startup.index('    s_cfg = *config;')]
        harness = '''#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "common.h"
static esp_err_t result;
static int calls;
esp_err_t esp_read_mac(uint8_t *mac, int type) {
    assert(type == (SOC_WIFI_SUPPORTED ? ESP_MAC_WIFI_STA : ESP_MAC_BASE));
    calls++;
    if (result != ESP_OK) return result;
    const uint8_t value[6] = {2, 3, 4, 5, 6, 7};
    memcpy(mac, value, sizeof(value));
    return ESP_OK;
}
static esp_err_t read_identity(uint8_t output[6]) {
''' + block + '''    memcpy(output, mac, 6);
    return ESP_OK;
}
int main(void) {
    uint8_t output[6] = {0};
    assert(read_identity(output) == ESP_OK);
    const uint8_t expected[6] = {2, 3, 4, 5, 6, 7};
    assert(memcmp(output, expected, 6) == 0 && calls == 1);
    memset(output, 0xAA, sizeof(output));
    result = ESP_ERR_NOT_SUPPORTED;
    assert(read_identity(output) == ESP_ERR_NOT_SUPPORTED && calls == 2);
    for (int i = 0; i < 6; i++) assert(output[i] == 0xAA);
    result = ESP_ERR_INVALID_STATE;
    assert(read_identity(output) == ESP_ERR_INVALID_STATE && calls == 3);
    return 0;
}
'''
        for wifi in (0, 1):
            with self.subTest(wifi=wifi), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'mac.c'
                path.write_text(harness)
                exe = Path(tmp) / 'mac'
                command = shlex.split(os.environ.get('CC', 'cc')) + [
                    '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                    '-DSOC_WIFI_SUPPORTED=%d' % wifi,
                    '-I' + str(ROOT / 'tests/idf_stubs'), str(path), '-o', str(exe)]
                result = subprocess.run(command, capture_output=True, text=True,
                                        env={**os.environ, 'TMPDIR': tmp})
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([exe], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
