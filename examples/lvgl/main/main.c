/*
 * espmirror LVGL example: an LVGL display with no panel behind it, the mirror
 * is the display. A bare devkit shows a user interface on your desktop:
 *
 *   - animations: a spinner, a ball on a spring, a bar
 *   - the keys: the viewer's three buttons, and the board's own, light three
 *     LEDs on screen; each also plays a sound
 *   - touch: the viewer's mouse on the glass presses the on-screen buttons
 *     and leaves a ripple
 *   - sound: three small sounds synthesised here (a click, a chime, a tune)
 *     play through the viewer (View > Sound) and through the board's speaker
 *     when it has one
 *   - the board's microphone, while the "mic" button is on or the main key
 *     is held: in the spectrogram, and in the viewer (View > Mic)
 *   - a spectrogram of what plays and of what the microphone hears, and of
 *     your voice when you talk into the board from the viewer (View > Talk)
 *   - the board's LED, reported to the viewer: on while a sound plays,
 *     blinking slowly otherwise
 *
 * On a board the example knows (menuconfig, "The board"), the user interface
 * is on the board's own panel too: board_aipi_lite.c drives the AIPI Lite's
 * ST7789. On any other devkit the mirror is the only display. With your own
 * panel driver, keep your display as it is and call espmirror_attach_lvgl()
 * once after espmirror_start().
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "board.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "espmirror.h"
#include "espmirror_lvgl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lvgl.h"
#include "nvs_flash.h"
#include "protocol_examples_common.h"
#include "sdkconfig.h"

static const char *TAG = "lvgl";

#define W CONFIG_EXAMPLE_MIRROR_WIDTH
#define H CONFIG_EXAMPLE_MIRROR_HEIGHT
#if CONFIG_EXAMPLE_MIRROR_ROUND
#define UI_W (W * 7 / 10)
#define UI_H (H * 7 / 10)
#else
#define UI_W W
#define UI_H H
#endif
#define RATE 16000
#define CHUNK 320                      /* 20 ms of audio */
#define SMALL (UI_W < 200 || UI_H < 200)   /* a 128 px screen: smaller type, icons for words, no status line */
#define LARGE (UI_W >= 400 && UI_H >= 400) /* a 480 px screen: everything about twice the size */
#if CONFIG_EXAMPLE_BOARD_WAVESHARE_216
#define CORNER 80                          /* the 2.16's panel hides pixels in a corner of about this radius */
#else
#define CORNER 0
#endif
#define SPEC_X (LARGE ? 28 : 8)
#define SPEC_W (UI_W - 2 * SPEC_X)
#define SPEC_BOTTOM (SMALL ? 4 : LARGE ? 34 : 6)
#define SPEC_H (SMALL ? 32 : LARGE ? UI_H - SPEC_BOTTOM - 330 : (UI_H - 156 > 40 ? UI_H - 156 : 40))   /* under the status line: 44 rows at 200, 84 at 240 */
#define RIPPLE (SMALL ? 18 : LARGE ? 44 : 28)
#if LARGE
#define FONT_TITLE (&lv_font_montserrat_28)
#define FONT_TEXT (&lv_font_montserrat_20)
#else
#define FONT_TITLE (SMALL ? &lv_font_montserrat_14 : &lv_font_montserrat_24)
#define FONT_TEXT (&lv_font_montserrat_14)
#endif
#define FFT_N 128

/* ---- Sound: three small sounds, synthesised, 16 kHz mono --------------------- */

enum { SOUND_NONE, SOUND_CLICK, SOUND_CHIME, SOUND_TUNE };

static QueueHandle_t s_sounds;         /* what to play next, from the keys and the buttons */
static int16_t s_tap[4096];            /* the latest audio, for the spectrogram: a ring */
static volatile uint32_t s_tap_w;
static portMUX_TYPE s_tap_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_playing;               /* atomic access */

static void tap_put(const int16_t *pcm, size_t n) {
    portENTER_CRITICAL(&s_tap_mux);
    for (size_t i = 0; i < n; i++) s_tap[(s_tap_w + i) % 4096] = pcm[i];
    s_tap_w += n;
    portEXIT_CRITICAL(&s_tap_mux);
}

/* One 20 ms chunk of the sound at time t (seconds); false once it is over. */
static bool synth(int sound, float t, int16_t *out) {
    static const float tune[] = {523.25f, 659.25f, 783.99f, 1046.5f, 783.99f, 659.25f, 523.25f, 392.0f};
    for (int i = 0; i < CHUNK; i++) {
        float tt = t + (float)i / RATE, v = 0;
        switch (sound) {
        case SOUND_CLICK:                      /* a burst of noise, gone in 15 ms */
            if (tt < 0.015f) v = ((int)(esp_random() % 2000) - 1000) / 1000.0f * (1 - tt / 0.015f) * 0.6f;
            break;
        case SOUND_CHIME:                      /* two partials, a slow decay */
            if (tt < 0.8f) v = (sinf(2 * (float)M_PI * 880 * tt) + 0.5f * sinf(2 * (float)M_PI * 1320 * tt)) * expf(-4 * tt) * 0.5f;
            break;
        case SOUND_TUNE: {                     /* eight notes, 150 ms each, a soft square */
            int n = (int)(tt / 0.15f);
            if (n < 8) {
                float ph = fmodf(tt * tune[n], 1.0f), env = 1 - fmodf(tt, 0.15f) / 0.15f;
                v = (ph < 0.5f ? 0.35f : -0.35f) * env * 0.8f + 0.2f * sinf(2 * (float)M_PI * tune[n] * tt) * env;
            }
            break;
        }
        }
        out[i] = (int16_t)(v * 32767);
    }
    float len = sound == SOUND_CLICK ? 0.03f : sound == SOUND_CHIME ? 0.8f : 1.2f;
    return t < len;
}

static bool s_has_audio;             /* the board's codec is up: the speaker plays, the microphone reads */
static bool s_mic_toggle;            /* the on-screen mic button */
static bool s_listen_mic, s_talking;
typedef struct { size_t count; int16_t pcm[CHUNK]; } talk_chunk_t;
static QueueHandle_t s_talk;
static volatile unsigned s_keys_held;   /* keys down, from the viewer and the board, a bit per code */

static void sound_task(void *arg) {
    (void)arg;
    talk_chunk_t audio;
    int sound = SOUND_NONE;
    float t = 0;
    TickType_t next = xTaskGetTickCount(), period = pdMS_TO_TICKS(20) ? pdMS_TO_TICKS(20) : 1;
    for (;;) {
        bool playing = false;
        bool talking = __atomic_load_n(&s_talking, __ATOMIC_RELAXED);
        if (talking) {
            sound = SOUND_NONE;
            if (xQueueReceive(s_talk, &audio, period) == pdTRUE
                && __atomic_load_n(&s_talking, __ATOMIC_RELAXED)) {
                espmirror_audio_output(audio.pcm, audio.count, 1);
                board_speaker_write(audio.pcm, audio.count);
                playing = true;
            }
        } else {
            if (sound == SOUND_NONE && xQueueReceive(s_sounds, &sound, 0) == pdTRUE) t = 0;
            playing = sound != SOUND_NONE && synth(sound, t, audio.pcm);
            if (playing) {
                tap_put(audio.pcm, CHUNK);
                espmirror_audio_output(audio.pcm, CHUNK, 1);
                if (s_has_audio) board_speaker_write(audio.pcm, CHUNK);
                t += (float)CHUNK / RATE;
            } else sound = SOUND_NONE;
        }
        if (playing != __atomic_load_n(&s_playing, __ATOMIC_RELAXED)) {
            __atomic_store_n(&s_playing, playing, __ATOMIC_RELAXED);
            espmirror_set_led(playing ? ESPMIRROR_LED_ON : ESPMIRROR_LED_BLINK_SLOW);
        }
        /* The codec paces Talk, including packets shorter than 20 ms. */
        if (talking) next = xTaskGetTickCount();
        else vTaskDelayUntil(&next, period);
    }
}

static void play(int sound) {
    xQueueSend(s_sounds, &sound, 0);
}

/* The board's microphone, while asked for: to the spectrogram and to the viewer's Mic. */
static bool mic_wanted(void) {
    return s_has_audio && (__atomic_load_n(&s_listen_mic, __ATOMIC_RELAXED)
                           || __atomic_load_n(&s_mic_toggle, __ATOMIC_RELAXED)
                           || espmirror_audio_input_wanted()
                           || (__atomic_load_n(&s_keys_held, __ATOMIC_RELAXED) & 1u << ESPMIRROR_KEY_MAIN));
}

static void mic_task(void *arg) {
    (void)arg;
    int16_t pcm[CHUNK];
    for (;;) {
        if (!mic_wanted()) {
            vTaskDelay(pdMS_TO_TICKS(50) ? pdMS_TO_TICKS(50) : 1);
            continue;
        }
        size_t n = board_mic_read(pcm, CHUNK);           /* blocks for 20 ms of audio */
        if (!n) {
            vTaskDelay(1);
            continue;
        }
        espmirror_audio_input(pcm, n);
        tap_put(pcm, n);
    }
}

static void on_listen(int source, void *user) {
    (void)user;
    __atomic_store_n(&s_listen_mic, source == 1, __ATOMIC_RELAXED);
}

static bool on_talk(bool on, void *user) {
    (void)user;
    ESP_LOGI(TAG, "viewer %s", on ? "talks" : "stopped talking");
    if (on && !s_has_audio) return false;
    __atomic_store_n(&s_talking, on, __ATOMIC_RELAXED);
    xQueueReset(s_talk);
    return true;
}

static void on_talk_audio(const int16_t *mono, size_t samples, void *user) {
    (void)user;
    tap_put(mono, samples);
    if (!__atomic_load_n(&s_talking, __ATOMIC_RELAXED) || samples > CHUNK) return;
    talk_chunk_t chunk = {.count = samples};
    memcpy(chunk.pcm, mono, samples * sizeof(*mono));
    /* The mirror task must never wait for the codec or an overloaded speaker. */
    xQueueSend(s_talk, &chunk, 0);
}

/* ---- Keys and touch from the viewer ------------------------------------------- */

static QueueHandle_t s_keys;           /* (key << 1 | down), in order */
static bool s_keys_lost;               /* the queue overflowed: every LED goes off, nothing stays pressed */
static struct { int x, y; bool down; } s_pointer;
static portMUX_TYPE s_pointer_mux = portMUX_INITIALIZER_UNLOCKED;

static void key_event(int key, bool down) {
    if (down) __atomic_fetch_or(&s_keys_held, 1u << key, __ATOMIC_RELAXED);
    else __atomic_fetch_and(&s_keys_held, ~(1u << key), __ATOMIC_RELAXED);
    int ev = key << 1 | (down ? 1 : 0);
    if (xQueueSend(s_keys, &ev, 0) != pdTRUE) __atomic_store_n(&s_keys_lost, true, __ATOMIC_RELAXED);
}

static void on_key(int key, bool down, void *user) {
    (void)user;
    key_event(key, down);
}

/* The board's own buttons, polled by the LVGL task: the same path as the viewer's keys. */
static void board_keys_poll(void) {
    static unsigned was;
    unsigned now = board_keys_down();
    for (int key = 1; key <= 3; key++) {
        if ((now ^ was) & 1u << key) key_event(key, (now & 1u << key) != 0);
    }
    was = now;
}

static void on_pointer(int x, int y, bool down, void *user) {
    (void)user;
    portENTER_CRITICAL(&s_pointer_mux);
    s_pointer.x = x;
    s_pointer.y = y;
    s_pointer.down = down;
    portEXIT_CRITICAL(&s_pointer_mux);
}

/* ---- The user interface ----------------------------------------------------------- */

static SemaphoreHandle_t s_lv_lock;
static lv_obj_t *s_leds[4], *s_ripple, *s_status, *s_canvas, *s_mic_btn;
static lv_draw_buf_t s_spec_buf;

static void button_event(lv_event_t *e) {
    play((int)(intptr_t)lv_event_get_user_data(e));
}

static void mic_button_event(lv_event_t *e) {
    __atomic_store_n(&s_mic_toggle, lv_obj_has_state(lv_event_get_target(e), LV_STATE_CHECKED), __ATOMIC_RELAXED);
}

/* A microphone, drawn (LVGL's symbol font has none): a capsule on a stem over a base, centred in the button. */
static lv_obj_t *mic_part(lv_obj_t *parent, int w, int h, int dx, int dy, int radius) {
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_radius(o, radius, 0);
    lv_obj_set_style_bg_color(o, lv_color_white(), 0);
    lv_obj_set_style_border_width(o, 0, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(o, LV_ALIGN_CENTER, dx, dy);
    return o;
}

static void mic_icon(lv_obj_t *btn, int u) {               /* u: the unit, the icon is 12 u tall */
    mic_part(btn, 5 * u, 8 * u, 0, -2 * u, LV_RADIUS_CIRCLE); /* the capsule */
    mic_part(btn, 1 * u, 3 * u, 0, 3 * u, 0);                /* the stem */
    mic_part(btn, 6 * u, 1 * u, 0, 5 * u, 0);                /* the base */
}

static void ball_x(void *obj, int32_t v) {
    lv_obj_set_x(obj, v);
}

static void ball_y(void *obj, int32_t v) {
    lv_obj_set_y(obj, v);
}

static void bar_value(void *obj, int32_t v) {
    lv_bar_set_value(obj, v, LV_ANIM_OFF);
}

/* No panel: the flush is done as soon as it starts (the mirror has copied it). */
static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px) {
    (void)area;
    (void)px;
    lv_display_flush_ready(disp);
}

static uint32_t tick_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void build_ui(void) {
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x14161c), 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
#if CONFIG_EXAMPLE_MIRROR_ROUND
    lv_obj_t *content = lv_obj_create(scr);
    lv_obj_remove_style_all(content);
    lv_obj_set_size(content, UI_W, UI_H);
    lv_obj_center(content);
    lv_obj_clear_flag(content, LV_OBJ_FLAG_SCROLLABLE);
    scr = content;
#endif

    /* Rows, from the top: the title, the LEDs, the buttons, the ball, the bar, (the status), the spectrogram. */
    const int pad = SMALL ? 6 : LARGE ? 24 : 10, gap = SMALL ? 6 : LARGE ? 14 : 10;
    const int y_led = SMALL ? 26 : LARGE ? 86 : 42, y_btn = SMALL ? 36 : LARGE ? 112 : 58, h_btn = SMALL ? 22 : LARGE ? 72 : 30;
    const int y_ball = SMALL ? 64 : LARGE ? 214 : 92, ball_px = SMALL ? 12 : LARGE ? 28 : 18;
    const int bounce = SMALL ? 10 : LARGE ? 22 : 14, y_bar = SMALL ? 84 : LARGE ? 268 : 124;
    const int led_px = SMALL ? 8 : LARGE ? 16 : 12, spin_px = SMALL ? 18 : LARGE ? 40 : 28;
    const int w_btn = (UI_W - 2 * pad - 3 * gap) / 4;   /* three sounds and the microphone */
    const int edge = CORNER ? CORNER * 11 / 20 : pad;   /* the top row, clear of rounded corners */

    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "espmirror");
    lv_obj_set_style_text_color(title, lv_color_white(), 0);
    lv_obj_set_style_text_font(title, FONT_TITLE, 0);
    lv_obj_set_pos(title, edge, SMALL ? 6 : LARGE ? 32 : 8);

    lv_obj_t *spinner = lv_spinner_create(scr);
    lv_spinner_set_anim_params(spinner, 1200, 90);
    lv_obj_set_size(spinner, spin_px, spin_px);
    lv_obj_set_style_arc_width(spinner, SMALL ? 3 : LARGE ? 6 : 4, LV_PART_MAIN);
    lv_obj_set_style_arc_width(spinner, SMALL ? 3 : LARGE ? 6 : 4, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(spinner, lv_color_hex(0x33d17a), LV_PART_INDICATOR);
    lv_obj_align(spinner, LV_ALIGN_TOP_RIGHT, -edge, SMALL ? 4 : LARGE ? 28 : 8);

    /* Three LEDs for the three keys; three buttons for the three sounds and one for the microphone
     * (icons where words do not fit). */
    static const char *names[] = {"click", "chime", "tune", NULL};
    static const char *icons[] = {LV_SYMBOL_VOLUME_MID, LV_SYMBOL_BELL, LV_SYMBOL_PLAY, NULL};
    for (int i = 0; i < 4; i++) {
        lv_obj_t *btn = lv_button_create(scr);
        lv_obj_set_size(btn, w_btn, h_btn);
        lv_obj_set_pos(btn, pad + i * (w_btn + gap), y_btn);
        lv_obj_set_style_bg_color(btn, lv_color_hex(0x2a2e3a), 0);
        lv_obj_set_style_bg_color(btn, lv_color_hex(0x33d17a), LV_STATE_PRESSED);
        lv_obj_set_style_bg_color(btn, lv_color_hex(0x33d17a), LV_STATE_CHECKED);
        lv_obj_set_style_pad_all(btn, 2, 0);
        if (i < 3) {
            lv_obj_add_event_cb(btn, button_event, LV_EVENT_CLICKED, (void *)(intptr_t)(SOUND_CLICK + i));
        } else {
            lv_obj_add_flag(btn, LV_OBJ_FLAG_CHECKABLE);
            lv_obj_add_event_cb(btn, mic_button_event, LV_EVENT_VALUE_CHANGED, NULL);
            s_mic_btn = btn;
        }
        if (i == 3) {
            mic_icon(btn, SMALL ? 1 : LARGE ? 4 : 2);
            continue;
        }
        lv_obj_t *l = lv_label_create(btn);
        lv_label_set_text(l, SMALL ? icons[i] : names[i]);
        lv_obj_set_style_text_font(l, FONT_TEXT, 0);
        lv_obj_center(l);

        s_leds[i + 1] = lv_led_create(scr);
        lv_obj_set_size(s_leds[i + 1], led_px, led_px);
        lv_obj_set_pos(s_leds[i + 1], pad + i * (w_btn + gap) + (w_btn - led_px) / 2, y_led);
        lv_led_set_color(s_leds[i + 1], lv_color_hex(0x33d17a));
        lv_led_off(s_leds[i + 1]);
    }

    /* The ball on a spring, and a bar that fills and empties. */
    lv_obj_t *ball = lv_obj_create(scr);
    lv_obj_set_size(ball, ball_px, ball_px);
    lv_obj_set_style_radius(ball, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ball, lv_color_hex(0xe95f3c), 0);
    lv_obj_set_style_border_width(ball, 0, 0);
    lv_obj_set_pos(ball, pad, y_ball);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, ball);
    lv_anim_set_exec_cb(&a, ball_x);
    lv_anim_set_values(&a, pad, UI_W - pad - ball_px);
    lv_anim_set_duration(&a, 1400);
    lv_anim_set_playback_duration(&a, 1400);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
    lv_anim_start(&a);
    lv_anim_init(&a);
    lv_anim_set_var(&a, ball);
    lv_anim_set_exec_cb(&a, ball_y);
    lv_anim_set_values(&a, y_ball - 2, y_ball - 2 + bounce);
    lv_anim_set_duration(&a, 450);
    lv_anim_set_playback_duration(&a, 450);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&a, lv_anim_path_bounce);
    lv_anim_start(&a);

    lv_obj_t *bar = lv_bar_create(scr);
    lv_obj_set_size(bar, UI_W - 2 * pad, SMALL ? 4 : LARGE ? 10 : 6);
    lv_obj_set_pos(bar, pad, y_bar);
    lv_obj_set_style_bg_color(bar, lv_color_hex(0x2a2e3a), LV_PART_MAIN);
    lv_obj_set_style_bg_color(bar, lv_color_hex(0x5b8def), LV_PART_INDICATOR);
    lv_anim_init(&a);
    lv_anim_set_var(&a, bar);
    lv_anim_set_exec_cb(&a, bar_value);
    lv_anim_set_values(&a, 0, 100);
    lv_anim_set_duration(&a, 2600);
    lv_anim_set_playback_duration(&a, 2600);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&a);

    /* The spectrogram, scrolling left. */
    uint32_t stride = lv_draw_buf_width_to_stride(SPEC_W, LV_COLOR_FORMAT_RGB565);
    size_t bytes = stride * SPEC_H;
    void *pixels = heap_caps_aligned_alloc(LV_DRAW_BUF_ALIGN, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!pixels) pixels = heap_caps_aligned_alloc(LV_DRAW_BUF_ALIGN, bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ESP_ERROR_CHECK(pixels ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(lv_draw_buf_init(&s_spec_buf, SPEC_W, SPEC_H, LV_COLOR_FORMAT_RGB565,
                                    stride, pixels, bytes) == LV_RESULT_OK ? ESP_OK : ESP_FAIL);
    s_canvas = lv_canvas_create(scr);
    lv_canvas_set_draw_buf(s_canvas, &s_spec_buf);
    lv_canvas_fill_bg(s_canvas, lv_color_hex(0x0b0c10), LV_OPA_COVER);
    lv_obj_set_pos(s_canvas, SPEC_X, UI_H - SPEC_H - SPEC_BOTTOM);

    s_status = lv_label_create(scr);
    lv_obj_set_style_text_color(s_status, lv_color_hex(0x8a8f9a), 0);
    lv_obj_set_style_text_font(s_status, FONT_TEXT, 0);
    lv_obj_set_pos(s_status, LARGE ? SPEC_X : 10, LARGE ? 292 : 130);
    if (SMALL) lv_obj_add_flag(s_status, LV_OBJ_FLAG_HIDDEN);   /* no room: the line stays in the log */

    /* The ripple under a touch, hidden until one. */
    s_ripple = lv_obj_create(lv_screen_active());
    lv_obj_set_size(s_ripple, RIPPLE, RIPPLE);
    lv_obj_set_style_radius(s_ripple, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_ripple, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_bg_opa(s_ripple, LV_OPA_40, 0);
    lv_obj_set_style_border_width(s_ripple, 0, 0);
    lv_obj_add_flag(s_ripple, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_ripple, LV_OBJ_FLAG_CLICKABLE);
}

/* ---- The spectrogram: a 128-point FFT of the latest audio, one column a tick ------- */

static void fft(float *re, float *im, int n) {
    for (int i = 1, j = 0; i < n; i++) {                      /* bit reversal */
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            float t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        float ang = -2 * (float)M_PI / len, wr = cosf(ang), wi = sinf(ang);
        for (int i = 0; i < n; i += len) {
            float cr = 1, ci = 0;
            for (int k = 0; k < len / 2; k++) {
                int a = i + k, b = i + k + len / 2;
                float tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
                re[b] = re[a] - tr; im[b] = im[a] - ti;
                re[a] += tr; im[a] += ti;
                float ncr = cr * wr - ci * wi;
                ci = cr * wi + ci * wr;
                cr = ncr;
            }
        }
    }
}

static uint16_t heat(float v) {                                 /* 0..1 to dark blue, blue, yellow, white */
    v = v < 0 ? 0 : v > 1 ? 1 : v;
    int r = v < .5f ? (int)(v * 2 * 255) : 255, g = v < .5f ? (int)(v * 2 * 120) : (int)(120 + (v - .5f) * 2 * 135);
    int b = v < .5f ? (int)(60 + v * 2 * 195) : (int)(255 - (v - .5f) * 2 * 255);
    return (uint16_t)((r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3);
}

static void spectrogram_tick(void) {
    static uint32_t seen;
    static float window[FFT_N];
    if (window[1] == 0) {
        for (int i = 0; i < FFT_N; i++) window[i] = 0.5f - 0.5f * cosf(2 * (float)M_PI * i / (FFT_N - 1));
    }
    float re[FFT_N], im[FFT_N];
    int16_t raw[FFT_N];
    portENTER_CRITICAL(&s_tap_mux);                             /* only the copy, not the arithmetic */
    uint32_t w = s_tap_w;
    if (w == seen) {                                            /* nothing new: the picture stands */
        portEXIT_CRITICAL(&s_tap_mux);
        return;
    }
    seen = w;
    for (int i = 0; i < FFT_N; i++) raw[i] = s_tap[(w - FFT_N + i) % 4096];
    portEXIT_CRITICAL(&s_tap_mux);
    for (int i = 0; i < FFT_N; i++) {
        re[i] = raw[i] / 32768.0f * window[i];
        im[i] = 0;
    }
    fft(re, im, FFT_N);
    uint8_t *data = s_spec_buf.data;
    uint32_t stride = s_spec_buf.header.stride;
    for (int y = 0; y < SPEC_H; y++) {
        uint16_t *row = (uint16_t *)(data + y * stride);
        memmove(row, row + 1, (SPEC_W - 1) * 2);
        int bin = 1 + (SPEC_H - 1 - y) * (FFT_N / 2 - 1) / (SPEC_H - 1);   /* low at the bottom */
        float mag = sqrtf(re[bin] * re[bin] + im[bin] * im[bin]) / (FFT_N / 4);
        float db = 20 * log10f(mag + 1e-5f);
        row[SPEC_W - 1] = heat((db + 60) / 60);
    }
    lv_obj_invalidate(s_canvas);
}

static void lv_task(void *arg) {
    (void)arg;
    TickType_t last = 0;
    for (;;) {
        xSemaphoreTake(s_lv_lock, portMAX_DELAY);
        board_keys_poll();
        board_led_set(__atomic_load_n(&s_playing, __ATOMIC_RELAXED) || (esp_timer_get_time() / 500000) % 2);
        int ev;
        if (__atomic_exchange_n(&s_keys_lost, false, __ATOMIC_RELAXED)) {
            while (xQueueReceive(s_keys, &ev, 0) == pdTRUE) {}     /* what is queued may lack its release */
            for (int k = 1; k <= 3; k++) lv_led_off(s_leds[k]);
        }
        while (xQueueReceive(s_keys, &ev, 0) == pdTRUE) {
            int key = ev >> 1;
            bool down = ev & 1;
            if (key >= 1 && key <= 3) {
                if (down) lv_led_on(s_leds[key]); else lv_led_off(s_leds[key]);
                if (down) play(SOUND_CLICK + key - 1);
            }
        }
        bool down;
        int px, py;
        portENTER_CRITICAL(&s_pointer_mux);
        down = s_pointer.down;
        px = s_pointer.x;
        py = s_pointer.y;
        portEXIT_CRITICAL(&s_pointer_mux);
        if (down) {
            lv_obj_set_pos(s_ripple, px - RIPPLE / 2, py - RIPPLE / 2);
            lv_obj_clear_flag(s_ripple, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(s_ripple, LV_OBJ_FLAG_HIDDEN);
        }
        spectrogram_tick();
        /* The mic button shows the microphone's state, held key included. */
        if (s_has_audio && mic_wanted() != lv_obj_has_state(s_mic_btn, LV_STATE_CHECKED)
            && !__atomic_load_n(&s_mic_toggle, __ATOMIC_RELAXED)) {
            if (mic_wanted()) lv_obj_add_state(s_mic_btn, LV_STATE_CHECKED); else lv_obj_clear_state(s_mic_btn, LV_STATE_CHECKED);
        }
        TickType_t now = xTaskGetTickCount();
        if (now - last >= pdMS_TO_TICKS(500)) {
            last = now;
            lv_label_set_text_fmt(s_status, "%s  |  %lu s  |  %s", espmirror_connected() ? "viewer on" : "no viewer",
                                  (unsigned long)(esp_timer_get_time() / 1000000),
                                  mic_wanted() ? "mic" : __atomic_load_n(&s_playing, __ATOMIC_RELAXED) ? "playing" : "quiet");
        }
        uint32_t wait = lv_timer_handler();
        xSemaphoreGive(s_lv_lock);
        TickType_t ticks = pdMS_TO_TICKS(wait < 20 ? 20 : wait > 40 ? 40 : wait);
        vTaskDelay(ticks ? ticks : 1);
    }
}

void app_main(void) {
    board_init();
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());         /* another image's NVS, or an old layout */
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
#if CONFIG_EXAMPLE_CONNECT_WIFI || CONFIG_EXAMPLE_CONNECT_ETHERNET
    ESP_ERROR_CHECK(example_connect());
#endif

    s_sounds = xQueueCreate(8, sizeof(int));
    s_talk = xQueueCreate(4, sizeof(talk_chunk_t));
    s_keys = xQueueCreate(16, sizeof(int));
    s_lv_lock = xSemaphoreCreateMutex();
    if (!s_sounds || !s_talk || !s_keys || !s_lv_lock) {
        ESP_LOGE(TAG, "no memory for the queues");
        return;
    }
#if CONFIG_ESPMIRROR_AUDIO
    err = board_audio_start();
    s_has_audio = err == ESP_OK;
    if (err != ESP_OK && err != ESP_ERR_NOT_SUPPORTED) ESP_ERROR_CHECK(err);
#endif
#if CONFIG_EXAMPLE_BOARD_WAVESHARE_216
    static const espmirror_button_t buttons[] = {
        {ESPMIRROR_KEY_AUX, "KEY", -1, -1},
        {ESPMIRROR_KEY_MAIN, "BOOT", -1, -1},
        {ESPMIRROR_KEY_POWER, "PWR", -1, -1},
    };
#elif CONFIG_EXAMPLE_BOARD_WAVESHARE_175C
    static const espmirror_button_t buttons[] = {
        {ESPMIRROR_KEY_AUX, "KEY", -1, -1},
        {ESPMIRROR_KEY_MAIN, "BOOT", -1, -1},
    };
#elif CONFIG_EXAMPLE_BOARD_WAVESHARE_349
    static const espmirror_button_t buttons[] = {
        {ESPMIRROR_KEY_MAIN, "BOOT", -1, -1},
        {ESPMIRROR_KEY_POWER, "PWR", -1, -1},
    };
#elif CONFIG_EXAMPLE_BOARD_WAVESHARE_154G
    static const espmirror_button_t buttons[] = {
        {ESPMIRROR_KEY_MAIN, "BOOT", -1, -1},
        {ESPMIRROR_KEY_POWER, "PWR", -1, -1},
    };
#elif CONFIG_EXAMPLE_BOARD_WAVESHARE_P4_35
    static const espmirror_button_t buttons[] = {
        {ESPMIRROR_KEY_MAIN, "BOOT", -1, -1},
        {ESPMIRROR_KEY_POWER, "PWR", -1, -1},
    };
#endif
    const espmirror_config_t cfg = {
        .name = CONFIG_EXAMPLE_MIRROR_NAME,
        .width = W,
        .height = H,
        .ppi = CONFIG_EXAMPLE_MIRROR_PPI,
        .corner_radius = 0,
#if CONFIG_EXAMPLE_MIRROR_ROUND
        .round = true,
#endif
        .on_key = on_key,
        .on_pointer = on_pointer,
        .on_listen = on_listen,
        .on_talk = on_talk,
        .on_talk_audio = on_talk_audio,
        .audio_route = true,
#if CONFIG_EXAMPLE_BOARD_WAVESHARE_216 || CONFIG_EXAMPLE_BOARD_WAVESHARE_175C || CONFIG_EXAMPLE_BOARD_WAVESHARE_349 || CONFIG_EXAMPLE_BOARD_WAVESHARE_154G || CONFIG_EXAMPLE_BOARD_WAVESHARE_P4_35
        .buttons = buttons,
        .button_count = sizeof(buttons) / sizeof(buttons[0]),
#endif
    };
    ESP_ERROR_CHECK(espmirror_start(&cfg));
    espmirror_set_led(ESPMIRROR_LED_BLINK_SLOW);

    lv_init();
    lv_tick_set_cb(tick_ms);
    size_t buf_bytes = W * 40 * 2;
    void *buf = heap_caps_aligned_alloc(LV_DRAW_BUF_ALIGN, buf_bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    ESP_ERROR_CHECK(buf ? ESP_OK : ESP_ERR_NO_MEM);
    lv_display_t *disp = lv_display_create(W, H);
    ESP_ERROR_CHECK(disp ? ESP_OK : ESP_ERR_NO_MEM);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(disp, buf, NULL, buf_bytes, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(disp, flush_cb);
    err = board_display_start(disp);                 /* the board's panel, when the example knows it */
    if (err != ESP_OK && err != ESP_ERR_NOT_SUPPORTED) ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(espmirror_attach_lvgl(disp));

    build_ui();
    if (xTaskCreate(sound_task, "sound", 4096, NULL, 5, NULL) != pdPASS
        || (s_has_audio && xTaskCreate(mic_task, "mic", 4096, NULL, 5, NULL) != pdPASS)
        || xTaskCreate(lv_task, "lvgl", 8192, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "no memory for the tasks");
        return;
    }
    ESP_LOGI(TAG, "run tools/espmirror.py --sound on your computer");
}
