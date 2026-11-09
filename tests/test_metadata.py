# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import espmirror as mirror


class GeometryTests(unittest.TestCase):
    def test_viewer_round_diameter_and_rectangular_diagonal(self):
        for info, ppi in ((dict(w=466, h=466, round=True, diag=466 / 270), 270),
                          (dict(w=480, h=320, round=True, diag=1.6), 200),
                          (dict(w=300, h=400, round=False, diag=2.5), 200),
                          (dict(w=466, h=466, round=True, diag=0), 0)):
            with self.subTest(info=info):
                self.assertAlmostEqual(mirror.board_look(info)['ppi'], ppi)

    @unittest.skipUnless(os.name == 'posix', 'C harness uses POSIX compiler and sanitizers')
    def test_firmware_geometry_metadata(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        start = source.index('    s_w = config->width;')
        end = source.index('    for (size_t i = 0;', start)
        harness = r'''#include <math.h>
#include <stdio.h>
#include "espmirror.h"
#include "esp_mac.h"
static int s_w, s_h;
static char s_board[2048], s_name[] = "geometry";
static size_t s_board_len;
esp_err_t esp_read_mac(uint8_t *mac, int type) {
    (void)type;
    for (int i = 0; i < 6; i++) mac[i] = (uint8_t)i;
    return ESP_OK;
}
static void describe(const espmirror_config_t *config) {
    uint8_t mac[6];
    esp_read_mac(mac, 0);
''' + source[start:end] + r'''
    if (s_board_len < sizeof(s_board)) printf("%s]}\n", s_board + 1);
}
int main(void) {
    espmirror_config_t config = {.width=466, .height=466, .ppi=270, .round=true};
    describe(&config);
    config.round = false; config.corner_radius = 233;
    describe(&config);
    config.diagonal = 1.75;
    describe(&config);
    config = (espmirror_config_t){.width=300, .height=400, .ppi=200};
    describe(&config);
    config.ppi = 0;
    describe(&config);
    config = (espmirror_config_t){.width=480, .height=320, .ppi=200, .round=true};
    describe(&config);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path, exe = Path(tmp) / 'geometry.c', Path(tmp) / 'geometry'
            path.write_text(harness)
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-DCONFIG_ESPMIRROR_AUDIO=1', '-I' + str(ROOT / 'tests/idf_stubs'),
                '-I' + str(ROOT / 'include'), str(path), '-lm', '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        boards = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([b['round'] for b in boards], [True, True, True, False, False, True])
        self.assertEqual([b['diag'] for b in boards], [1.726, 1.726, 1.75, 2.5, 0, 1.6])
        self.assertAlmostEqual(mirror.board_look(boards[0])['ppi'], 270, delta=.1)

    @unittest.skipUnless(os.name == 'posix', 'C harness uses POSIX compiler and sanitizers')
    def test_failed_start_leaves_no_screen(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        wrapper = source[source.index('esp_err_t espmirror_start('):
                         source.index('static esp_err_t start(const espmirror_config_t *config) {')]
        harness = r'''#include <assert.h>
#include <stdlib.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
typedef struct { int width; } espmirror_config_t;
static void *s_task, *s_previous;
static int s_w, s_h;
static void heap_caps_free(void *p) { free(p); }
static esp_err_t start(const espmirror_config_t *config) {
    if (s_task) return ESP_ERR_INVALID_STATE;
    s_w = config->width; s_h = 240; s_previous = malloc(16);
    return config->width > 1000 ? ESP_ERR_INVALID_ARG : ESP_OK;
}
''' + wrapper + r'''
int main(void) {
    espmirror_config_t bad = {2000}, good = {320};
    assert(espmirror_start(&bad) == ESP_ERR_INVALID_ARG && s_w == 0 && s_h == 0 && !s_previous);
    assert(espmirror_start(&good) == ESP_OK && s_w == 320);
    s_task = &good;
    assert(espmirror_start(&bad) == ESP_ERR_INVALID_STATE && s_w == 320 && s_previous);
    free(s_previous);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path, exe = Path(tmp) / 'start.c', Path(tmp) / 'start'
            path.write_text(harness)
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', str(path), '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == 'posix', 'C harness uses POSIX compiler and sanitizers')
    def test_board_name_stays_valid_json(self):
        source = (ROOT / 'src/espmirror.c').read_text()
        start = source.index('    size_t at = 0;')
        end = source.index('    s_name[at] = 0;', start) + len('    s_name[at] = 0;')
        harness = r'''#include <stdio.h>
#include <string.h>
static char s_name[48];
typedef struct { const char *name; } config_t;
static void name(const char *raw) {
    const config_t *config = &(config_t){raw};
''' + source[start:end] + r'''
    printf("{\"name\":\"%s\"}\n", s_name);
}
int main(void) {
    name("ab\xC3\",\"x\":1");
    name("ab\xE2\x82\\");
    name("\x80\xBF stray \xF8 high");
    name("caf\xC3\xA9 \xE2\x82\xAC \xF0\x9F\x98\x80 \"q\" \\b");
    name("\xC3");
    name("0123456789012345678901234567890123456789012345\xE2\x82\xAC");
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path, exe = Path(tmp) / 'name.c', Path(tmp) / 'name'
            path.write_text(harness)
            command = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', str(path), '-o', str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        names = [json.loads(line)['name'] for line in result.stdout.decode('utf-8').splitlines()]
        self.assertEqual(names, ['ab","x":1', 'ab\\', ' stray  high',
                                 'café € 😀 "q" \\b', '', '0123456789012345678901234567890123456789012345'])


if __name__ == '__main__':
    unittest.main()
