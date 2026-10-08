/*
 * espmirror: the board's screen, keys, LED and audio on a desktop, over TCP.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The keys the viewer draws on the device, as it numbers them. Which physical
 * button each one is depends on the device profile in the viewer: on a
 * Waveshare board 1 is KEY, 2 is BOOT, 3 is PWR; on a two-button device 2 is
 * the main (talk) button and 3 the other one. */
#define ESPMIRROR_KEY_AUX   1
#define ESPMIRROR_KEY_MAIN  2
#define ESPMIRROR_KEY_POWER 3

/* What the viewer veils the screen with. */
#define ESPMIRROR_SCREEN_BRIGHT 0
#define ESPMIRROR_SCREEN_DIM    1
#define ESPMIRROR_SCREEN_DARK   2

/* The LED as the viewer shows it; the blinks are timed on the viewer. */
#define ESPMIRROR_LED_OFF        0
#define ESPMIRROR_LED_BLINK_SLOW 1
#define ESPMIRROR_LED_BLINK_FAST 2
#define ESPMIRROR_LED_ON         3

typedef struct {
    /* The board, as the viewer labels it and picks its look by: a name with
     * "2.16", "1.54G", "1.75C" or "AIPI" in it gets that device's body and
     * buttons, anything else a plain frame. */
    const char *name;
    int width, height;      /* the screen in pixels */
    int ppi;                /* pixels per inch, for the viewer's real-size mode; 0 unknown */
    int corner_radius;      /* of the panel, pixels; half the size or more means round */

    /* A key the viewer presses (down) or lets go. From the mirror task; keep
     * it short, or hand the event to your input path. NULL ignores the keys. */
    void (*on_key)(int key, bool down, void *user);

    /* The viewer's mouse on the glass, as a touch: down at x, y (screen
     * pixels), moved while down, or up. Up once more, at 0, 0, when the
     * viewer leaves. From the mirror task. NULL ignores it. */
    void (*on_pointer)(int x, int y, bool down, void *user);

    /* A viewer connected (true) or left (false): hold the radio awake, light
     * something, count. From the mirror task. NULL ignores it. */
    void (*on_viewer)(bool connected, void *user);

    /* The viewer listens to the microphone (1), the speaker (2) or stops
     * (0): a firmware that powers its microphone only when wanted does it
     * here. From the mirror task. NULL ignores it. CONFIG_ESPMIRROR_AUDIO. */
    void (*on_listen)(int source, void *user);

    /* The viewer wants to talk through the board's speaker (on), or stops;
     * then its microphone arrives as mono PCM16 at 16 kHz. Both from the
     * mirror task. NULL refuses. Only with CONFIG_ESPMIRROR_AUDIO. */
    bool (*on_talk)(bool on, void *user);
    void (*on_talk_audio)(const int16_t *mono, size_t samples, void *user);

    void *user;
} espmirror_config_t;

/* Starts the mirror task and listens on CONFIG_ESPMIRROR_PORT. After the
 * network is up (the viewer needs an IP to connect to, the socket does not).
 * Keeps a copy of the screen, width x height RGB565; copies the config (the
 * name too, 47 characters at most). Once; from one task. */
esp_err_t espmirror_start(const espmirror_config_t *config);

/* The screen copy to draw into, locked until espmirror_frame_end(); NULL
 * before espmirror_start(). Tell what changed, in pixels; the viewer gets it
 * on the next frame. Any task. */
uint16_t *espmirror_frame_begin(void);
void espmirror_frame_end(int x, int y, int width, int height);

/* The same in one call: a rectangle of RGB565 pixels, stride in pixels
 * (0 means width). Clipped to the screen. */
void espmirror_draw(int x, int y, int width, int height, const uint16_t *rgb565, int stride);

/* The screen in pixels, as given to espmirror_start(). */
void espmirror_size(int *width, int *height);

/* The viewer's veil over the screen (ESPMIRROR_SCREEN_*), sent with the next
 * frame. Any task. */
void espmirror_set_screen_state(uint8_t state);

/* The LED as the firmware drives it (ESPMIRROR_LED_*), sent on each change
 * and to every new viewer. Any task. */
void espmirror_set_led(uint8_t mode);

/* Copies for the viewer, taken only while it listens to that source (cheap
 * otherwise): what the microphone gives, mono PCM16 at 16 kHz, and what the
 * speaker gets, PCM16 at 16 kHz, 1 or 2 channels (the left one is sent).
 * Any task. Without CONFIG_ESPMIRROR_AUDIO they compile to nothing. */
void espmirror_audio_capture(const int16_t *mono, size_t samples);
void espmirror_audio_playback(const int16_t *pcm, size_t frames, int channels);

/* True while a viewer is connected. */
bool espmirror_connected(void);

#ifdef __cplusplus
}
#endif
