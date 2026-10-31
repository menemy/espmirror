/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ESPMIRROR_MAX_DIRTY 8
#define ESPMIRROR_RECT_HDR 14
#define ESPMIRROR_DELTA 1u
#define ESPMIRROR_QOI 2u

typedef struct {
    int x1, y1, x2, y2;
} espmirror_rect_t;

/* Caller serializes access to this state and the framebuffer. */
typedef struct {
    int w, h;
    unsigned flags;
    uint16_t *previous;
    espmirror_rect_t dirty[ESPMIRROR_MAX_DIRTY];
    int ndirty;
    espmirror_rect_t tail;
    bool tail_on;
} espmirror_core_t;

bool espmirror_core_init(espmirror_core_t *c, int w, int h);
/* previous: caller-owned w*h pixels, required only for DELTA, cleared at init.
 * On ESP32 allocate it in PSRAM. Reset before a new viewer (whose copy is zero).
 * A built message must be delivered in order or the session must be reset. */
bool espmirror_core_init_ex(espmirror_core_t *c, int w, int h,
                              unsigned flags, uint16_t *previous);
void espmirror_core_reset(espmirror_core_t *c);
/* F: type:u8, state:u8, rects:u16, screen_w:u16, screen_h:u16.
 * Each rectangle: x,y,w,h:u16, coding:u8, reserved=0:u8, bytes:u32, payload.
 * Integers are little endian. Coding 0: RGB565; 1/2: row/column order pairs
 * (count:u16, pixel:u16), nonzero counts, runs may cross row/column boundaries.
 * Coding 3: row-order pairs of (count, current XOR previous), same run rules.
 * Previous starts black and advances after EVERY rectangle, including absolute
 * rectangles and overlaps. Coding 4: QOI ops only (no header or end marker),
 * row order, fresh RGBA index (all zero), previous=(0,0,0,255). Expand RGB565
 * by bit replication; opaque pixels only. Exactly w*h pixels and bytes consumed.
 */
/* Inclusive coordinates, clipped to the screen. */
void espmirror_core_dirty(espmirror_core_t *c, int x1, int y1, int x2, int y2);
bool espmirror_core_pending(const espmirror_core_t *c);
/* fb: w*h native RGB565 pixels. Returns an F payload (no length prefix).
 * Unsent pixels stay pending, with the previous tail served first.
 * Returns 0 without consuming dirt if capacity cannot hold one raw row. */
size_t espmirror_core_build(espmirror_core_t *c, const uint16_t *fb,
                              uint8_t *out, size_t capacity, uint8_t state);
