# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'C harnesses use POSIX stubs and sanitizers')
class TimingTests(unittest.TestCase):
    def run_c(self, source, enabled=True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.c'
            exe = Path(tmp) / 'test'
            path.write_text(source)
            cmd = shlex.split(os.environ.get('CC', 'cc')) + [
                '-std=c11', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                '-DCONFIG_ESPMIRROR_TIMING=%d' % enabled,
                '-I' + str(ROOT / 'tests/idf_stubs'), '-I' + str(ROOT / 'include'),
                '-I' + str(ROOT / 'src'), str(path), '-o', str(exe)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bounded_snapshot_and_saturation(self):
        self.run_c('''
#include <assert.h>
#include <string.h>
#include "espmirror_timing.c"
int64_t esp_timer_get_time(void) { return 9000; }
static uint64_t get64(const uint8_t *p) {
    uint64_t n = 0; for (unsigned i = 0; i < 8; i++) n |= (uint64_t)p[i] << (8 * i); return n;
}
int main(void) {
    uint8_t out[ESPMIRROR_TIMING_BYTES + 1]; memset(out, 0xa5, sizeof(out));
    assert(!espmirror_timing_read(out, ESPMIRROR_TIMING_BYTES - 1));
    assert(out[0] == 0xa5 && !espmirror_timing_read(NULL, sizeof(out)));
    espmirror_timing_record(ESPMIRROR_TIMING_BAND_WAKE, 1000, 50);
    espmirror_timing_record(ESPMIRROR_TIMING_BAND_WAKE, 2000, 20);
    espmirror_timing_record((espmirror_timing_event_t)-1, 1, 1);
    espmirror_timing_record(ESPMIRROR_TIMING_COUNT, 1, 1);
    assert(espmirror_timing_read(out, sizeof(out)) == ESPMIRROR_TIMING_BYTES);
    assert(!memcmp(out, "EMT1", 4) && out[4] == 8 && out[5] == 40 && !out[6] && !out[7]);
    assert(get64(out + 8) == 9000 && out[ESPMIRROR_TIMING_BYTES] == 0xa5);
    const uint8_t *p = out + 16 + ESPMIRROR_TIMING_BAND_WAKE * 40;
    assert(get64(p) == 2 && get64(p + 8) == 70 && get64(p + 16) == 2000);
    assert(get64(p + 24) == 20 && get64(p + 32) == 50);
    assert(get64(out + 16) == 0);
    timings[ESPMIRROR_TIMING_BAND_WAKE].count = UINT64_MAX;
    espmirror_timing_record(ESPMIRROR_TIMING_BAND_WAKE, 3000, UINT64_MAX);
    espmirror_timing_record(ESPMIRROR_TIMING_BAND_WAKE, 4000, 1);
    espmirror_timing_read(out, sizeof(out));
    assert(get64(p) == UINT64_MAX && get64(p + 8) == UINT64_MAX && get64(p + 32) == UINT64_MAX);
    return 0;
}
''')

    def test_disabled_needs_no_timer_or_storage(self):
        self.run_c('''
#include <assert.h>
#include "espmirror_timing.h"
int main(void) {
    assert(espmirror_timing_now() == 0);
    espmirror_timing_record(ESPMIRROR_TIMING_FRAME_READY, 1, 2);
    assert(espmirror_timing_read(NULL, 0) == 0);
    return 0;
}
''', enabled=False)

    def test_panel_callback_yields_only_for_awakened_waiter(self):
        board = (ROOT / 'examples/mosaico/main/board.c').read_text()
        callback = board[board.index('static bool transfer_done('):board.index('\nesp_err_t mosaico_board_start')]
        self.run_c('''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include "espmirror_timing.h"
typedef int BaseType_t;
typedef void *esp_lcd_panel_io_handle_t;
typedef int esp_lcd_panel_io_event_data_t;
#define pdFALSE 0
#define pdTRUE 1
#define portYIELD_FROM_ISR() do { assert(given); yields++; } while (0)
static volatile uint64_t band_done_us;
static int transmitted, given, yields, should_wake, recorded;
int64_t esp_timer_get_time(void) { return 1234; }
void espmirror_timing_record(espmirror_timing_event_t event, uint64_t at, uint64_t duration) {
    assert(event == ESPMIRROR_TIMING_BAND_DONE && at == 1234 && duration == 0); recorded++;
}
static void xSemaphoreGiveFromISR(int semaphore, BaseType_t *wake) {
    assert(semaphore == transmitted && band_done_us == 1234 && recorded);
    given++; *wake = should_wake;
}
''' + callback + '''
int main(void) {
    should_wake = 0; assert(!transfer_done(NULL, NULL, NULL)); assert(given == 1 && yields == 0);
    should_wake = 1; assert(transfer_done(NULL, NULL, NULL)); assert(given == 2 && yields == 1);
    return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
