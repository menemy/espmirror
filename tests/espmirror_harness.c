/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "espmirror_core.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

static unsigned read_le(int n)
{
    unsigned v = 0;
    for (int i = 0; i < n; i++) {
        int b = getchar();
        assert(b != EOF);
        v |= (unsigned)b << (8 * i);
    }
    return v;
}

static void emit(espmirror_core_t *c, uint16_t *fb, uint8_t *msg, size_t cap, int state)
{
    bool pending = espmirror_core_pending(c);
    size_t n = espmirror_core_build(c, fb, msg, cap, state);
    assert(n >= 8 && n <= cap);
    assert(!pending || msg[2] || msg[3]);
    for (int i = 0; i < 4; i++) putchar(n >> (8 * i) & 255);
    assert(fwrite(msg, 1, n, stdout) == n);
}

int main(void)
{
    int w = read_le(2), h = read_le(2);
    size_t cap = read_le(4);
    espmirror_core_t c;
    assert(!espmirror_core_init(&c, 0, h));
    assert(!espmirror_core_init(&c, 65536, h));
    assert(espmirror_core_init(&c, w, h));
    uint16_t *fb = calloc((size_t)w * h, sizeof(*fb));
    uint16_t *previous = NULL;
    uint8_t *msg = malloc(cap);
    assert(fb && msg);
    for (int cmd; (cmd = getchar()) != EOF;) {
        if (cmd == 'C') {
            unsigned flags = read_le(1);
            free(previous);
            previous = flags & ESPMIRROR_DELTA ? calloc((size_t)w * h, 2) : NULL;
            assert(espmirror_core_init_ex(&c, w, h, flags, previous));
        } else if (cmd == 'R') {
            espmirror_core_reset(&c);
        } else if (cmd == 'U') {
            int x = read_le(2), y = read_le(2), rw = read_le(2), rh = read_le(2);
            assert(x + rw <= w && y + rh <= h && rw && rh);
            for (int row = y; row < y + rh; row++) {
                for (int col = x; col < x + rw; col++) fb[(size_t)row * w + col] = read_le(2);
            }
            espmirror_core_dirty(&c, x, y, x + rw - 1, y + rh - 1);
        } else if (cmd == 'B') {
            emit(&c, fb, msg, cap, read_le(1));
        } else if (cmd == 'D') {
            int state = read_le(1), frames = 0;
            while (espmirror_core_pending(&c)) {
                assert(++frames < 100000);
                emit(&c, fb, msg, cap, state);
            }
        } else if (cmd == 'S') {
            espmirror_core_t saved = c;
            assert(espmirror_core_build(&c, fb, msg, 8, 0) == 0);
            assert(c.ndirty == saved.ndirty && c.tail_on == saved.tail_on);
        } else if (cmd == 'A') {
            espmirror_core_dirty(&c, -10, -10, w + 10, h + 10);
        } else {
            assert(0);
        }
    }
    assert(!espmirror_core_pending(&c));
    free(previous);
    free(msg);
    free(fb);
    return 0;
}
