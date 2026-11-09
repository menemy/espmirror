/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "espmirror_timing.h"
#if CONFIG_ESPMIRROR_TIMING
#include <string.h>
#include "freertos/FreeRTOS.h"

typedef struct {
    uint64_t count, total_us, last_at_us, last_us, max_us;
} timing_t;
static timing_t timings[ESPMIRROR_TIMING_COUNT];
static portMUX_TYPE timing_lock = portMUX_INITIALIZER_UNLOCKED;

void espmirror_timing_record(espmirror_timing_event_t event, uint64_t at_us, uint64_t duration_us) {
    if ((unsigned)event >= ESPMIRROR_TIMING_COUNT) return;
    portENTER_CRITICAL_SAFE(&timing_lock);
    timing_t *t = &timings[event];
    if (t->count != UINT64_MAX) t->count++;
    t->total_us = duration_us > UINT64_MAX - t->total_us ? UINT64_MAX : t->total_us + duration_us;
    t->last_at_us = at_us;
    t->last_us = duration_us;
    if (duration_us > t->max_us) t->max_us = duration_us;
    portEXIT_CRITICAL_SAFE(&timing_lock);
}

static void put64(uint8_t *p, uint64_t value) {
    for (unsigned i = 0; i < 8; i++) p[i] = value >> (8 * i);
}

size_t espmirror_timing_read(uint8_t *out, size_t capacity) {
    if (!out || capacity < ESPMIRROR_TIMING_BYTES) return 0;
    timing_t copy[ESPMIRROR_TIMING_COUNT];
    portENTER_CRITICAL_SAFE(&timing_lock);
    uint64_t now = espmirror_timing_now();
    memcpy(copy, timings, sizeof(copy));
    portEXIT_CRITICAL_SAFE(&timing_lock);
    memcpy(out, "EMT1", 4);
    out[4] = ESPMIRROR_TIMING_COUNT; out[5] = 40; out[6] = out[7] = 0;
    put64(out + 8, now);
    for (unsigned i = 0; i < ESPMIRROR_TIMING_COUNT; i++) {
        uint8_t *p = out + 16 + i * 40;
        put64(p, copy[i].count); put64(p + 8, copy[i].total_us);
        put64(p + 16, copy[i].last_at_us); put64(p + 24, copy[i].last_us);
        put64(p + 32, copy[i].max_us);
    }
    return ESPMIRROR_TIMING_BYTES;
}
#endif
