/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "sdkconfig.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    ESPMIRROR_TIMING_FRAME_READY,
    ESPMIRROR_TIMING_ENCODER_WAIT,
    ESPMIRROR_TIMING_ENCODER_BUILD,
    ESPMIRROR_TIMING_CHUNK_PUBLISH,
    ESPMIRROR_TIMING_PANEL_PRESENT,
    ESPMIRROR_TIMING_BAND_DONE,
    ESPMIRROR_TIMING_BAND_WAKE,
    ESPMIRROR_TIMING_BAND_WAIT,
    ESPMIRROR_TIMING_COUNT
} espmirror_timing_event_t;

#define ESPMIRROR_TIMING_BYTES (16 + ESPMIRROR_TIMING_COUNT * 40)

#if CONFIG_ESPMIRROR_TIMING
#include "esp_timer.h"
static inline uint64_t espmirror_timing_now(void) { return esp_timer_get_time(); }
/* Task and ISR safe. Counters are cumulative since boot, saturating at UINT64_MAX. */
void espmirror_timing_record(espmirror_timing_event_t event, uint64_t at_us, uint64_t duration_us);
/* A coherent little-endian snapshot; zero means the destination is too small. */
size_t espmirror_timing_read(uint8_t *out, size_t capacity);
#else
static inline uint64_t espmirror_timing_now(void) { return 0; }
static inline void espmirror_timing_record(espmirror_timing_event_t event, uint64_t at_us, uint64_t duration_us) {
    (void)event; (void)at_us; (void)duration_us;
}
static inline size_t espmirror_timing_read(uint8_t *out, size_t capacity) {
    (void)out; (void)capacity; return 0;
}
#endif

#ifdef __cplusplus
}
#endif
