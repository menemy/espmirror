/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
/* Host-only access to the exact production encoders, including static helpers. */
#define _POSIX_C_SOURCE 200809L
#include "../src/espmirror_core.c"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static double now_us(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) abort();
    return t.tv_sec * 1000000.0 + t.tv_nsec / 1000.0;
}

int main(int argc, char **argv)
{
    assert(argc == 5);
    int w = atoi(argv[1]), h = atoi(argv[2]), frames = atoi(argv[3]), repeats = atoi(argv[4]);
    assert(w > 0 && h > 0 && frames > 0 && repeats > 0);
    size_t pixels = (size_t)w * h, bytes = pixels * 2;
    uint16_t *input = malloc(bytes * frames), *previous = calloc(pixels, 2);
    uint8_t *out = malloc(pixels * 5 + 1024);
    assert(input && previous && out);
    /* Fixtures are little endian, independently of the host's byte order. */
    for (size_t i = 0; i < pixels * frames; i++) {
        int lo = getchar(), hi = getchar();
        assert(lo != EOF && hi != EOF);
        input[i] = lo | hi << 8;
    }
    for (int coding = 0; coding <= 10; coding++) {
        double elapsed = 0;
        size_t total = 0, checksum = 0;
        for (int repeat = 0; repeat < repeats; repeat++) {
            espmirror_core_t core;
            unsigned flags = coding == 6 ? 0 : coding == 7 ? ESPMIRROR_DELTA
                : coding == 8 ? ESPMIRROR_QOI : ESPMIRROR_DELTA | ESPMIRROR_QOI;
            assert(espmirror_core_init_ex(&core, w, h, flags, previous));
            for (int f = 0; f < frames; f++) {
                const uint16_t *fb = input + pixels * f;
                size_t n = 0;
                double start = now_us();
                if (coding == 0) {
                    for (size_t i = 0; i < pixels; i++) put16(out + i * 2, fb[i]);
                    n = bytes;
                } else if (coding <= 3) {
                    n = rle(fb, coding == 3 ? previous : NULL, w, out, SIZE_MAX,
                            0, 0, w, h, coding == 2);
                } else if (coding == 4) {
                    n = qoi(fb, w, out, SIZE_MAX, 0, 0, w, h);
                } else if (coding <= 8) {
                    espmirror_core_dirty(&core, 0, 0, w - 1, h - 1);
                    do {
                        size_t part = espmirror_core_build(&core, fb, out, 32768, 0);
                        assert(part >= 8);
                        checksum += out[part - 1];
                        n += part;
                    } while (espmirror_core_pending(&core));
                } else if (coding == 9) {
                    for (int row = 0; row < h; row++) {
                        memcpy(out + (size_t)row * w * 2, fb + (size_t)row * w, (size_t)w * 2);
                    }
                } else {
                    /* Full-width h/10-row bands, repeated to resolve sub-us work. */
                    for (int batch = 0; batch < 256; batch++) {
                        core.ndirty = 0;
                        for (int y = 0; y < h; y += h / 10) {
                            espmirror_core_dirty(&core, 0, y, w - 1, y + h / 10 - 1);
                        }
                        checksum += core.ndirty + core.dirty[0].y2;
                    }
                }
                if (coding == 3) memcpy(previous, fb, bytes);
                elapsed += (now_us() - start) / (coding == 10 ? 256 : 1);
                if (coding == 9) {
                    for (size_t i = 0; i < bytes; i++) checksum += out[i];
                } else if (coding < 9) {
                    checksum += out[coding >= 5 ? 0 : n - 1];
                }
                total += n + (coding >= 5 ? 0 : 22);
            }
        }
        printf("%d,%.3f,%.3f,%zu\n", coding, (double)total / repeats / frames,
               elapsed / repeats / frames, checksum);
    }
    free(out);
    free(previous);
    free(input);
    return 0;
}
