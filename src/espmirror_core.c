/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include "espmirror_core.h"

#include <string.h>

#define WHOLE_PX 60000
#define RECT_HDR ESPMIRROR_RECT_HDR

bool espmirror_core_init_ex(espmirror_core_t *c, int w, int h,
                              unsigned flags, uint16_t *previous)
{
    memset(c, 0, sizeof(*c));
    if (w <= 0 || h <= 0 || w > UINT16_MAX || h > UINT16_MAX
        || (size_t)w > SIZE_MAX / 2 / h
        || (flags & ~(ESPMIRROR_DELTA | ESPMIRROR_QOI))
        || ((flags & ESPMIRROR_DELTA) && !previous)) return false;
    c->w = w;
    c->h = h;
    c->flags = flags;
    c->previous = flags & ESPMIRROR_DELTA ? previous : NULL;
    espmirror_core_reset(c);
    return true;
}

bool espmirror_core_init(espmirror_core_t *c, int w, int h)
{
    return espmirror_core_init_ex(c, w, h, 0, NULL);
}

void espmirror_core_reset(espmirror_core_t *c)
{
    if (c->previous) memset(c->previous, 0, (size_t)c->w * c->h * 2);
}

bool espmirror_core_pending(const espmirror_core_t *c)
{
    return c->ndirty > 0 || c->tail_on;
}

void espmirror_core_dirty(espmirror_core_t *c, int x1, int y1, int x2, int y2)
{
    if (x1 < 0) x1 = 0;
    if (y1 < 0) y1 = 0;
    if (x2 >= c->w) x2 = c->w - 1;
    if (y2 >= c->h) y2 = c->h - 1;
    if (x1 > x2 || y1 > y2) return;
    for (int i = 0; i < c->ndirty; i++) {
        /* Touching or overlapping: one rectangle. */
        if (x1 <= c->dirty[i].x2 + 1 && x2 >= c->dirty[i].x1 - 1 && y1 <= c->dirty[i].y2 + 1 && y2 >= c->dirty[i].y1 - 1) {
            if (x1 < c->dirty[i].x1) c->dirty[i].x1 = x1;
            if (y1 < c->dirty[i].y1) c->dirty[i].y1 = y1;
            if (x2 > c->dirty[i].x2) c->dirty[i].x2 = x2;
            if (y2 > c->dirty[i].y2) c->dirty[i].y2 = y2;
            return;
        }
    }
    if (c->ndirty < ESPMIRROR_MAX_DIRTY) {
        c->dirty[c->ndirty].x1 = x1;
        c->dirty[c->ndirty].y1 = y1;
        c->dirty[c->ndirty].x2 = x2;
        c->dirty[c->ndirty].y2 = y2;
        c->ndirty++;
    } else {
        if (x1 < c->dirty[0].x1) c->dirty[0].x1 = x1;
        if (y1 < c->dirty[0].y1) c->dirty[0].y1 = y1;
        if (x2 > c->dirty[0].x2) c->dirty[0].x2 = x2;
        if (y2 > c->dirty[0].y2) c->dirty[0].y2 = y2;
    }
}

static inline void put16(uint8_t *p, uint32_t v)
{
    p[0] = v;
    p[1] = v >> 8;
}

static inline void put32(uint8_t *p, uint32_t v)
{
    put16(p, v);
    put16(p + 2, v >> 16);
}

/* Stop counting once the candidate cannot beat limit. No candidate scratch. */
static size_t rle(const uint16_t *fb, const uint16_t *previous, int stride,
                  uint8_t *out, size_t limit, int x, int y, int w, int h, bool cols)
{
    size_t n = 0;
    uint16_t colour = 0;
    unsigned run = 0;
    int outer = cols ? w : h, inner = cols ? h : w;
    for (int a = 0; a < outer; a++) {
        size_t base = cols ? (size_t)y * stride + x + a : (size_t)(y + a) * stride + x;
        int step = cols ? stride : 1;
        for (int b = 0; b < inner; b++) {
            size_t pos = base + (size_t)b * step;
            uint16_t px = fb[pos] ^ (previous ? previous[pos] : 0);
            if (run && px == colour && run < UINT16_MAX) {
                run++;
                continue;
            }
            if (run) {
                if (n + 4 > limit) return SIZE_MAX;
                if (out) { put16(out + n, run); put16(out + n + 2, colour); }
                n += 4;
            }
            colour = px;
            run = 1;
        }
    }
    if (n + 4 > limit) return SIZE_MAX;
    if (out) { put16(out + n, run); put16(out + n + 2, colour); }
    return n + 4;
}

static size_t qoi(const uint16_t *fb, int stride, uint8_t *out, size_t limit,
                  int x, int y, int w, int h)
{
    uint32_t index[64] = {0};
    uint32_t previous = 0xff000000;
    size_t n = 0;
    unsigned run = 0;
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            uint16_t v = fb[(size_t)(y + row) * stride + x + col];
            int r = (v >> 11) * 8 + (v >> 13);
            int g = ((v >> 5) & 63) * 4 + ((v >> 9) & 3);
            int b = (v & 31) * 8 + ((v >> 2) & 7);
            uint32_t px = 0xff000000u | (unsigned)r << 16 | (unsigned)g << 8 | b;
            uint8_t op[5];
            int bytes = 0;
            if (px == previous) {
                if (++run == 62 || (row == h - 1 && col == w - 1)) {
                    op[bytes++] = 0xc0 | (run - 1);
                    run = 0;
                }
            } else {
                if (run) { op[bytes++] = 0xc0 | (run - 1); run = 0; }
                unsigned hash = (r * 3 + g * 5 + b * 7 + 255 * 11) & 63;
                if (index[hash] == px) {
                    op[bytes++] = hash;
                } else {
                    int dr = r - (int)((previous >> 16) & 255);
                    int dg = g - (int)((previous >> 8) & 255);
                    int db = b - (int)(previous & 255);
                    if (dr >= -2 && dr <= 1 && dg >= -2 && dg <= 1 && db >= -2 && db <= 1) {
                        op[bytes++] = 0x40 | (dr + 2) << 4 | (dg + 2) << 2 | (db + 2);
                    } else if (dg >= -32 && dg <= 31 && dr - dg >= -8 && dr - dg <= 7
                               && db - dg >= -8 && db - dg <= 7) {
                        op[bytes++] = 0x80 | (dg + 32);
                        op[bytes++] = (dr - dg + 8) << 4 | (db - dg + 8);
                    } else {
                        op[bytes++] = 0xfe; op[bytes++] = r; op[bytes++] = g; op[bytes++] = b;
                    }
                    index[hash] = px;
                }
                previous = px;
            }
            if (n + bytes > limit) return SIZE_MAX;
            if (out) memcpy(out + n, op, bytes);
            n += bytes;
        }
    }
    return n;
}

static size_t code_rect(espmirror_core_t *c, const uint16_t *fb, uint8_t *out,
                        size_t room, int x, int y, int w, int h, bool try_cols)
{
    size_t n = (size_t)w * h * 2;
    int enc = 0;
    for (int candidate = 1; candidate <= 4; candidate++) {
        if ((candidate == 2 && !try_cols && !c->flags)
            || (candidate == 3 && !(c->flags & ESPMIRROR_DELTA))
            || (candidate == 4 && !(c->flags & ESPMIRROR_QOI))) continue;
        size_t size = candidate == 4 ? qoi(fb, c->w, NULL, n - 1, x, y, w, h)
            : rle(fb, candidate == 3 ? c->previous : NULL, c->w, NULL,
                  n - 1, x, y, w, h, candidate == 2);
        if (size < n) { n = size; enc = candidate; }
    }
    if (RECT_HDR + n > room) return 0;
    uint8_t *p = out + RECT_HDR;
    if (enc == 4) {
        qoi(fb, c->w, p, n, x, y, w, h);
    } else if (enc) {
        rle(fb, enc == 3 ? c->previous : NULL, c->w, p, n, x, y, w, h, enc == 2);
    } else {
        for (int r = 0; r < h; r++) {
            const uint16_t *row = fb + (size_t)(y + r) * c->w + x;
            for (int i = 0; i < w; i++) put16(p + ((size_t)r * w + i) * 2, row[i]);
        }
    }
    if (c->previous) {
        for (int r = 0; r < h; r++) {
            size_t pos = (size_t)(y + r) * c->w + x;
            memcpy(c->previous + pos, fb + pos, (size_t)w * 2);
        }
    }
    put16(out, x);
    put16(out + 2, y);
    put16(out + 4, w);
    put16(out + 6, h);
    out[8] = enc;
    out[9] = 0;
    put32(out + 10, n);
    return RECT_HDR + n;
}

size_t espmirror_core_build(espmirror_core_t *c, const uint16_t *fb,
                              uint8_t *out, size_t capacity, uint8_t state)
{
    if (c->w <= 0 || c->h <= 0 || capacity < 64 ||
        capacity < (size_t)c->w * 2 + ESPMIRROR_RECT_HDR + 8) return 0;
    espmirror_rect_t todo[ESPMIRROR_MAX_DIRTY];
    int nt;
    nt = c->ndirty;
    memcpy(todo, c->dirty, sizeof(todo[0]) * nt);
    c->ndirty = 0;

    out[0] = 'F';
    out[1] = state;
    put16(out + 4, c->w);
    put16(out + 6, c->h);
    size_t len = 8;
    int rects = 0;
    /* The tail of the last message first (so a screen that changes all the time
     * still gets through to its bottom), then the dirty list; a tail that does
     * not fit stays for the next message. */
    bool tail_first = c->tail_on;
    c->tail_on = false;
    for (int i = tail_first ? -1 : 0; i < nt; i++) {
        int x1 = i < 0 ? c->tail.x1 : todo[i].x1, y1 = i < 0 ? c->tail.y1 : todo[i].y1;
        int x2 = i < 0 ? c->tail.x2 : todo[i].x2, y2 = i < 0 ? c->tail.y2 : todo[i].y2;
        int w = x2 - x1 + 1, h_all = y2 - y1 + 1;
        size_t n = 0;
        if ((size_t)w * h_all <= WHOLE_PX) n = code_rect(c, fb, out + len, capacity - len, x1, y1, w, h_all, true);
        if (n) {
            len += n;
            rects++;
            continue;
        }
        /* Big, or too big for this message as a whole: bands whose raw size always fits an empty message. */
        int band = (int)(((capacity - 64) / 2) / w);
        if (band < 1) band = 1;
        for (int y = y1; y <= y2;) {
            int h = y2 - y + 1 < band ? y2 - y + 1 : band;
            n = code_rect(c, fb, out + len, capacity - len, x1, y, w, h, false);
            if (!n) {
                if (c->tail_on) {
                    espmirror_core_dirty(c, x1, y, x2, y2);       /* a second tail waits its turn behind the first */
                } else {
                    c->tail.x1 = x1;
                    c->tail.y1 = y;
                    c->tail.x2 = x2;
                    c->tail.y2 = y2;
                    c->tail_on = true;
                }
                break;
            }
            len += n;
            rects++;
            y += h;
        }
    }
    put16(out + 2, rects);
    return len;
}


bool espmirror_core_text(const uint8_t *buf, size_t len, uint8_t *edit, char text[64])
{
    memset(text, 0, 64);
    *edit = 0;
    if (len == 2 && buf[0] == 'K' && buf[1] >= 1 && buf[1] <= 5) {
        *edit = buf[1];
        return true;
    }
    if (len < 2 || len > 64 || buf[0] != 'I') return false;
    for (size_t i = 1; i < len;) {
        uint32_t c = buf[i++], min = 0;
        unsigned more = 0;
        if (c >= 0xC2 && c <= 0xDF) { c &= 31; more = 1; min = 0x80; }
        else if (c >= 0xE0 && c <= 0xEF) { c &= 15; more = 2; min = 0x800; }
        else if (c >= 0xF0 && c <= 0xF4) { c &= 7; more = 3; min = 0x10000; }
        else if (c >= 0x80 || !c) return false;
        if (i + more > len) return false;
        while (more--) {
            if ((buf[i] & 0xC0) != 0x80) return false;
            c = c << 6 | (buf[i++] & 63);
        }
        if (c < min || c > 0x10FFFF || (c >= 0xD800 && c <= 0xDFFF)) return false;
    }
    memcpy(text, buf + 1, len - 1);
    return true;
}
