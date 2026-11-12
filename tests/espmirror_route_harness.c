// Copyright (c) 2026 Maksim Nagaev
// SPDX-License-Identifier: Apache-2.0
/* The component's sound routing, cut from src/espmirror.c: the speaker silenced for the viewer,
 * the viewer's microphone mixed in or in place of the board's, a lagging ring, a reset. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define __ATOMIC_RELAXED 0
#define __atomic_load_n(p, o) (*(p))
#define __atomic_store_n(p, v, o) (*(p) = (v))
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(m) ((void)(m))
#define portEXIT_CRITICAL(m) ((void)(m))
#define ESP_LOGI(...) ((void)0)

static struct { bool audio_route; } s_cfg = {true};
static int16_t played[8], captured[8];
static size_t played_n, captured_n;

void espmirror_audio_playback(const int16_t *pcm, size_t frames, int channels) {
    (void)channels;
    memcpy(played, pcm, frames * sizeof(*pcm));
    played_n = frames;
}

void espmirror_audio_capture(const int16_t *mono, size_t samples) {
    memcpy(captured, mono, samples * sizeof(*mono));
    captured_n = samples;
}

#include "route_impl.inc"

static void lend(const int16_t *pcm, size_t n) {
    uint8_t bytes[64];
    for (size_t i = 0; i < n; i++) {
        bytes[2 * i] = (uint8_t)pcm[i];
        bytes[2 * i + 1] = (uint8_t)((uint16_t)pcm[i] >> 8);
    }
    mic_put(bytes, n);
}

int main(void) {
    int16_t out[4] = {1, -2, 3, -4};
    espmirror_audio_output(out, 4, 1);
    assert(out[0] == 1 && out[3] == -4);                      /* both: the board plays */
    route_set(1, 0);
    espmirror_audio_output(out, 2, 2);
    assert(played_n == 2 && played[0] == 1);                   /* the viewer still hears it */
    assert(out[0] == 0 && out[1] == 0 && out[2] == 0 && out[3] == 0);

    int16_t mic[4] = {100, 200, 300, 32000};
    const int16_t theirs[3] = {10, -20, 1000};
    route_set(0, 1);
    assert(espmirror_audio_input_wanted());
    lend(theirs, 3);
    espmirror_audio_input(mic, 4);
    assert(mic[0] == 110 && mic[1] == 180 && mic[2] == 1300 && mic[3] == 32000);   /* mixed; the rest as is */
    assert(captured_n == 4 && captured[2] == 1300);

    route_set(0, 2);                                           /* a new mode starts from an empty ring */
    int16_t own[3] = {5, 5, 5};
    espmirror_audio_input(own, 3);
    assert(own[0] == 0 && own[1] == 0 && own[2] == 0);         /* nothing lent yet: silence, not the board */
    lend(theirs, 2);
    espmirror_audio_input(own, 3);
    assert(own[0] == 10 && own[1] == -20 && own[2] == 0);

    int16_t loud[1] = {-30000};
    const int16_t more[1] = {-10000};
    route_set(0, 1);
    lend(more, 1);
    espmirror_audio_input(loud, 1);
    assert(loud[0] == -32768);                                 /* clipped, not wrapped */

    int16_t chunk[16] = {0};
    for (int i = 0; i < MIC_RING / 16 + 4; i++) {              /* far behind: only the newest 80 ms stay */
        for (int j = 0; j < 16; j++) chunk[j] = (int16_t)i;
        lend(chunk, 16);
    }
    assert(s_mic_w - s_mic_r <= MIC_RING && s_mic_w - s_mic_r >= MIC_KEEP);

    route_set(0, 0);
    assert(!espmirror_audio_input_wanted());
    int16_t still[2] = {7, 8};
    espmirror_audio_input(still, 2);
    assert(still[0] == 7 && still[1] == 8);
    s_cfg.audio_route = false;                                 /* a firmware that does not route ignores it */
    route_set(1, 2);
    assert(!s_route_out && !s_route_in);
    return 0;
}
