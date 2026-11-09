/*
 * espmirror: the board's screen, keys, LED and audio on a desktop, over USB or TCP.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include "sdkconfig.h"
#include "espmirror.h"
#include "espmirror_core.h"
#include "espmirror_usb.h"
#include <math.h>
#include <stdlib.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stddef.h>
#include "esp_mac.h"
#include "soc/soc_caps.h"
#include "esp_rom_crc.h"
#if CONFIG_ESPMIRROR_IRIS
#include "esp_iris.h"
#include "esp_iris_service_profiles.h"
#endif
#if CONFIG_ESPMIRROR_TCP
#include "mdns.h"
#endif

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#if CONFIG_ESPMIRROR_TCP
#include <sys/socket.h>
#include "lwip/sockets.h"
#endif

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#ifndef CONFIG_ESPMIRROR_TCP
#define CONFIG_ESPMIRROR_TCP 0
#endif
#ifndef CONFIG_ESPMIRROR_IRIS
#define CONFIG_ESPMIRROR_IRIS 0
#endif
#ifndef CONFIG_ESPMIRROR_AUDIO
#define CONFIG_ESPMIRROR_AUDIO 0
#endif
#ifndef CONFIG_ESPMIRROR_USB_AUTO
#define CONFIG_ESPMIRROR_USB_AUTO 0
#endif
#ifndef CONFIG_ESPMIRROR_USB_NETWORK
#define CONFIG_ESPMIRROR_USB_NETWORK 0
#endif

static const char *TAG = "espmirror";

/* The wire (docs/protocol.md): a 4-byte little-endian length, then the
 * message. The viewer says HELLO first. From the board: 'F' a frame (the
 * screen state, the rectangle count, width, height, then each rectangle: x,
 * y, w, h, coding, 0, length, pixels), 'B' then JSON naming the board, 'T'
 * when another viewer takes the screen, 'A' then the source (1 the
 * microphone, 2 the speaker) and PCM16 mono 16 kHz while the viewer listens,
 * 'L' then the LED's state. From the viewer: 1/0 and x, y (a pointer:
 * on_pointer), 3 and a key (a tap), 6, a key
 * and 1/0 (held down or let go), 7 and the source to listen to or 0, 8 and
 * 1/0 (talk through the speaker, or stop), 'S', a format byte and PCM16 mono
 * 16 kHz (the viewer's microphone), anything else keeps the connection. */
#define PORT        CONFIG_ESPMIRROR_PORT
#define HELLO       "MIRROR1\n"
#define MSG_MAX     (CONFIG_ESPMIRROR_MSG_KB * 1024)
#define BAND_PX     ((MSG_MAX - 64) / 2)   /* a band of a big rectangle always fits a message raw */
#define WHOLE_PX    60000      /* up to this a rectangle is coded in one piece */
#define RECT_HDR    14
#define MAX_DIRTY   8
#define FRAME_MS    CONFIG_ESPMIRROR_FRAME_MS
#define BEAT_MS     1000       /* an empty frame carries the screen state when nothing changes */
#define SEND_MS     3000       /* a viewer that takes longer for one message is dropped */
#define OPEN_GAP_MS 1000       /* at most one new viewer this often */
#define IN_MAX      1024       /* the viewer's longest message: 'S', the format, 20 ms of audio */
#define IN_MS       2000       /* a message, the hello included, arrives whole within this */
#define TAP_MS      150        /* a key tapped from the viewer is held this long */
#if CONFIG_ESPMIRROR_AUDIO
#define AUD_RING    (CONFIG_ESPMIRROR_AUDIO_RING_KB * 1024)
#define AUD_CHUNK   1280       /* bytes per 'A' message: 40 ms, as the frames */
#define AUD_BURST   4          /* at most this many 'A' messages between two frames, catching up */
_Static_assert((AUD_RING & (AUD_RING - 1)) == 0 && AUD_RING >= 4 * AUD_CHUNK,
               "CONFIG_ESPMIRROR_AUDIO_RING_KB: a power of two, 8 or more");
#endif
_Static_assert(MSG_MAX >= 8 * 1024, "CONFIG_ESPMIRROR_MSG_KB: 8 or more");

static espmirror_config_t s_cfg;
static char s_name[48];        /* the board's name, JSON-safe */
static int s_w, s_h;
static atomic_bool s_ready;     /* the task runs once everything is published */

static uint16_t *s_fb;         /* the screen copy, under s_lock */
static uint8_t *s_msg;
static SemaphoreHandle_t s_lock;
static TaskHandle_t s_task;
static atomic_uchar s_state;

static espmirror_core_t s_core;
static uint16_t *s_previous;
static char s_board[2048];
static size_t s_board_len;
static atomic_bool s_serial;
static atomic_bool s_serial_open, s_session_busy;
#define SERIAL_QUIET_MS 6000
#define SERIAL_HELLO 0xffff
#define SERIAL_BYE 0xfffe
#define LOG_WAIT_MS 50
#define KEYS 11
#define SERIAL_QUEUE 32
static const uint8_t s_mark[] = { 0, 'M', 'I', 'R', 'R', 'O', 'R', '1', '\n' };
typedef struct { uint16_t len; uint8_t b[IN_MAX]; } serial_msg_t;
#if CONFIG_ESPMIRROR_AUDIO
#define SERIAL_INPUT_MAX 642
#else
#define SERIAL_INPUT_MAX 64
#endif
/* The reader drains IN_MAX bytes; the queue keeps only inputs this build accepts. */
#define SERIAL_ITEM_SIZE (offsetof(serial_msg_t, b) + SERIAL_INPUT_MAX)
static QueueHandle_t s_serial_in;
static SemaphoreHandle_t s_serial_tx, s_serial_done;

#define PTR_Q 32
typedef struct { uint16_t x, y; bool down, edge; } ptr_t;
static portMUX_TYPE s_ptr_mux = portMUX_INITIALIZER_UNLOCKED;
static ptr_t s_ptr_q[PTR_Q], s_ptr;
static unsigned s_ptr_head, s_ptr_n;
static int s_ptr_x, s_ptr_y;

/* The TCP side, all on the mirror task: the viewer's connection with the
 * message being received, and one candidate still saying hello (the viewer
 * keeps the screen until the candidate is admitted). */
static uint8_t *s_in;
static int s_cfd = -1;
#if CONFIG_ESPMIRROR_TCP
static int s_listen = -1;
static size_t s_inn;
static int64_t s_in_until;
static int s_pfd = -1;
static uint8_t s_hb[sizeof(HELLO) - 1];
static size_t s_hn;
static int64_t s_p_until;
#endif
static volatile uint8_t s_led_mode, s_led_sent = 0xFF;   /* the LED as driven; what the viewer was last told */
static int s_tap_key;          /* a key tapped from the viewer, let go at s_tap_until */
static int64_t s_tap_until;
static unsigned s_held;        /* keys the viewer holds down, a bit per code */
#define TEXT_Q 32
static QueueHandle_t s_text_in;      /* the viewer's typing, for the UI task */
static volatile bool s_text_field;   /* a text field is open, as the UI task last said */
static bool s_text_viewer;           /* mirror task: this viewer types (it sent 'O') */
static int s_text_sent = -1;         /* mirror task: what this viewer was last told */

#if CONFIG_ESPMIRROR_AUDIO
/* Audio for the viewer: a ring the audio path writes (s_aud_w bytes so far)
 * and the mirror task reads (s_aud_r), under one spinlock. */
static uint8_t *s_aud, *s_aud_out;
static uint32_t s_aud_w, s_aud_r;
static volatile uint8_t s_aud_src;   /* 0 off, 1 the microphone, 2 the speaker */
static portMUX_TYPE s_aud_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_talk;                  /* the viewer's microphone plays through the speaker */
#endif

static bool tcp_send(int fd, const void *a, size_t na, const void *b, size_t nb, int ms);
static bool viewer_send(const void *buf, size_t len, int ms);
static void serial_end(void);
static void viewer_drop(void);
static bool viewer_on(void) { return s_serial || __atomic_load_n(&s_cfd, __ATOMIC_RELAXED) >= 0; }
static void tcp_drop(void);
#if CONFIG_ESPMIRROR_IRIS
static void iris_takeover(void);
#endif

static void *alloc(size_t size, bool zero) {
    uint32_t caps = MALLOC_CAP_8BIT;
#if CONFIG_ESPMIRROR_PREFER_PSRAM
    void *p = zero ? heap_caps_calloc(1, size, caps | MALLOC_CAP_SPIRAM) : heap_caps_malloc(size, caps | MALLOC_CAP_SPIRAM);
    if (p) return p;
#endif
    return zero ? heap_caps_calloc(1, size, caps) : heap_caps_malloc(size, caps);
}

static void dirty_add(int x1, int y1, int x2, int y2) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    espmirror_core_dirty(&s_core, x1, y1, x2, y2);
    xSemaphoreGive(s_lock);
}

static void refresh(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    espmirror_core_reset(&s_core);
    espmirror_core_dirty(&s_core, 0, 0, s_w - 1, s_h - 1);
    xSemaphoreGive(s_lock);
}

static void pointer_put(int x, int y, bool down) {
    portENTER_CRITICAL(&s_ptr_mux);
    bool was = s_ptr_n ? s_ptr_q[(s_ptr_head + s_ptr_n - 1) % PTR_Q].down : s_ptr.down;
    ptr_t value = { x, y, down, down != was };
    unsigned last = (s_ptr_head + s_ptr_n - 1) % PTR_Q;
    if (s_ptr_n && !value.edge && !s_ptr_q[last].edge) {
        s_ptr_q[last] = value;
    } else {
        if (s_ptr_n == PTR_Q) {
            /* Overflow cancels the gesture before accepting a new press. */
            s_ptr_head = 0;
            s_ptr_n = 1;
            s_ptr_q[0] = (ptr_t){ x, y, false, true };
        }
        s_ptr_q[(s_ptr_head + s_ptr_n++) % PTR_Q] = value;
    }
    portEXIT_CRITICAL(&s_ptr_mux);
    s_ptr_x = x; s_ptr_y = y;
    if (s_cfg.on_pointer) s_cfg.on_pointer(x, y, down, s_cfg.user);
}

bool espmirror_pointer_read(int *x, int *y, bool *down) {
    portENTER_CRITICAL(&s_ptr_mux);
    if (s_ptr_n) {
        s_ptr = s_ptr_q[s_ptr_head];
        s_ptr_head = (s_ptr_head + 1) % PTR_Q;
        s_ptr_n--;
    }
    *x = s_ptr.x; *y = s_ptr.y; *down = s_ptr.down;
    bool more = s_ptr_n > 0;
    portEXIT_CRITICAL(&s_ptr_mux);
    return more;
}

/* ---- The public side -------------------------------------------------------- */

uint16_t *espmirror_frame_begin(void) {
    uint16_t *fb = __atomic_load_n(&s_fb, __ATOMIC_ACQUIRE);
    if (!fb) return NULL;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    return fb;
}

/* The mirror task picks the change up on its next pass (its wait is on the
 * socket, not on a notification). */
/* The rectangle clipped to the screen, as x1, y1, x2, y2; false when nothing is left. */
static bool clip(int x, int y, int width, int height, int *x1, int *y1, int *x2, int *y2) {
    if (width <= 0 || height <= 0 || x >= s_w || y >= s_h) return false;
    int64_t xe = (int64_t)x + width - 1, ye = (int64_t)y + height - 1;
    *x1 = x < 0 ? 0 : x;
    *y1 = y < 0 ? 0 : y;
    *x2 = xe >= s_w ? s_w - 1 : (int)xe;
    *y2 = ye >= s_h ? s_h - 1 : (int)ye;
    return *x2 >= *x1 && *y2 >= *y1;
}

void espmirror_frame_end(int x, int y, int width, int height) {
    if (!__atomic_load_n(&s_fb, __ATOMIC_ACQUIRE)) return;
    int x1, y1, x2, y2;
    if (clip(x, y, width, height, &x1, &y1, &x2, &y2)) espmirror_core_dirty(&s_core, x1, y1, x2, y2);
    xSemaphoreGive(s_lock);
}

void espmirror_draw(int x, int y, int width, int height, const uint16_t *rgb565, int stride) {
    int x1, y1, x2, y2;
    if (!rgb565 || !s_w || !clip(x, y, width, height, &x1, &y1, &x2, &y2)) return;
    if (stride <= 0) stride = width;
    uint16_t *fb = espmirror_frame_begin();
    if (!fb) return;
    for (int r = y1; r <= y2; r++) {
        memcpy(fb + r * s_w + x1, rgb565 + (size_t)(r - y) * stride + (x1 - x), (x2 - x1 + 1) * 2);
    }
    espmirror_frame_end(x1, y1, x2 - x1 + 1, y2 - y1 + 1);
}

void espmirror_size(int *width, int *height) {
    if (width) *width = s_w;
    if (height) *height = s_h;
}

void espmirror_set_screen_state(uint8_t state) {
    atomic_store_explicit(&s_state, state > 2 ? 2 : state, memory_order_relaxed);
    if (s_w) dirty_add(0, 0, 0, 0);       /* a frame goes out with it; one pixel's worth of rectangle */
}

void espmirror_set_led(uint8_t mode) {
    __atomic_store_n(&s_led_mode, mode > 3 ? 3 : mode, __ATOMIC_RELAXED);   /* sent on the task's next pass */
}

bool espmirror_connected(void) {
    return s_serial || __atomic_load_n(&s_cfd, __ATOMIC_RELAXED) >= 0
#if CONFIG_ESPMIRROR_IRIS
        || (s_ready && esp_iris_media_is_streaming(ESP_IRIS_CHANNEL_SCREEN))
#endif
        ;
}

void espmirror_text_open(bool open) {
    __atomic_store_n(&s_text_field, open, __ATOMIC_RELAXED);
}

bool espmirror_text_read(espmirror_text_t *out) {
    memset(out, 0, sizeof(*out));
    return s_text_in && xQueueReceive(s_text_in, out, 0) == pdTRUE;
}

/* Mirror task: a new viewer, or none, starts without typing pending. */
static void text_reset(void) {
    s_text_viewer = false;
    s_text_sent = -1;
    if (s_text_in) xQueueReset(s_text_in);
}

/* Mirror task: whether a field is open, to a viewer that types, when it changed. */
static void text_pump(void) {
    if (!viewer_on() || !s_text_viewer) return;
    bool open = __atomic_load_n(&s_text_field, __ATOMIC_RELAXED);
    if (open == s_text_sent) return;
    uint8_t msg[2] = {'O', open};
    if (viewer_send(msg, 2, SEND_MS)) {
        s_text_sent = open;
    } else {
        viewer_drop();
    }
}

/* Mirror task: the LED's state to the viewer when it changed since it was last told. */
static void led_pump(void) {
    uint8_t mode = __atomic_load_n(&s_led_mode, __ATOMIC_RELAXED);
    if (!viewer_on() || mode == s_led_sent) return;
    uint8_t msg[2] = {'L', mode};
    if (viewer_send(msg, 2, SEND_MS)) {
        s_led_sent = mode;
    } else {
        viewer_drop();                     /* part of a message may be out: nothing may follow it */
    }
}

/* ---- Audio ------------------------------------------------------------------ */

#if CONFIG_ESPMIRROR_AUDIO
/* Mono samples into the ring, from whichever task has them, when src is the
 * source the viewer asked for. */
static void audio_put(uint8_t src, const int16_t *pcm, size_t samples) {
    size_t n = samples * 2;
    if (__atomic_load_n(&s_aud_src, __ATOMIC_RELAXED) != src || !s_aud || n > AUD_RING) return;
    portENTER_CRITICAL(&s_aud_mux);
    if (s_aud_src == src) {
        uint32_t w = s_aud_w;
        size_t at = w % AUD_RING, first = AUD_RING - at < n ? AUD_RING - at : n;
        memcpy(s_aud + at, pcm, first);
        memcpy(s_aud, (const uint8_t *)pcm + first, n - first);
        s_aud_w = w + n;
    }
    portEXIT_CRITICAL(&s_aud_mux);
}

void espmirror_audio_capture(const int16_t *mono, size_t samples) {
    for (size_t at = 0; at < samples; at += 320) {              /* 20 ms at a time under the lock */
        audio_put(1, mono + at, samples - at < 320 ? samples - at : 320);
    }
}

void espmirror_audio_playback(const int16_t *pcm, size_t frames, int channels) {
    if (__atomic_load_n(&s_aud_src, __ATOMIC_RELAXED) != 2 || !s_aud) return;
    if (channels <= 1) {
        audio_put(2, pcm, frames);
        return;
    }
    int16_t mono[320];
    for (size_t at = 0; at < frames; at += 320) {
        size_t n = frames - at < 320 ? frames - at : 320;
        for (size_t i = 0; i < n; i++) mono[i] = pcm[channels * (at + i)];
        audio_put(2, mono, n);
    }
}

/* Mirror task: the viewer listens to a source, or stops. */
static void audio_set(uint8_t src) {
    if (src > 2 || src == __atomic_load_n(&s_aud_src, __ATOMIC_RELAXED)) return;
    portENTER_CRITICAL(&s_aud_mux);
    s_aud_r = s_aud_w;
    __atomic_store_n(&s_aud_src, src, __ATOMIC_RELAXED);
    portEXIT_CRITICAL(&s_aud_mux);
    ESP_LOGI(TAG, "viewer %s", src == 1 ? "listens to the microphone" : src == 2 ? "listens to the speaker" : "stopped listening");
    if (s_cfg.on_listen) s_cfg.on_listen(src, s_cfg.user);
}

/* Mirror task: the viewer talks through the speaker, or stops; the firmware may refuse. */
static void talk_set(bool on) {
    if (on == s_talk) return;
    if (!s_cfg.on_talk) {
        if (on) ESP_LOGW(TAG, "viewer asked for the speaker: nothing takes it here");
        return;
    }
    if (on && !s_cfg.on_talk(true, s_cfg.user)) {
        ESP_LOGW(TAG, "viewer asked for the speaker: refused");
        return;
    }
    if (!on) s_cfg.on_talk(false, s_cfg.user);
    s_talk = on;
    ESP_LOGI(TAG, "viewer %s", on ? "talks through the speaker" : "stopped talking");
}

/* Mirror task: the audio that has gathered, as 'A' messages, a few at a
 * time; a reader further than the ring behind skips to the newest half of it.
 * The chunk is taken under the ring's lock, sent after it. */
static void audio_pump(void) {
    uint8_t src = __atomic_load_n(&s_aud_src, __ATOMIC_RELAXED);
    if (!src || !viewer_on()) return;
    for (int i = 0; i < AUD_BURST; i++) {
        portENTER_CRITICAL(&s_aud_mux);
        uint32_t w = s_aud_w, r = s_aud_r;
        if (w - r > AUD_RING) {
            r = w - AUD_RING / 2;
            r -= r % 2;
        }
        bool have = w - r >= AUD_CHUNK;
        if (have) {
            size_t at = r % AUD_RING, first = AUD_RING - at < AUD_CHUNK ? AUD_RING - at : AUD_CHUNK;
            memcpy(s_aud_out + 2, s_aud + at, first);
            memcpy(s_aud_out + 2 + first, s_aud, AUD_CHUNK - first);
            r += AUD_CHUNK;
        }
        s_aud_r = r;
        portEXIT_CRITICAL(&s_aud_mux);
        if (!have) return;
        s_aud_out[0] = 'A';
        s_aud_out[1] = src;
        if (!viewer_send(s_aud_out, 2 + AUD_CHUNK, SEND_MS)) {
            ESP_LOGW(TAG, "viewer too slow or gone, closing");
            viewer_drop();
            return;
        }
    }
}
#else
void espmirror_audio_capture(const int16_t *mono, size_t samples) {
    (void)mono;
    (void)samples;
}

void espmirror_audio_playback(const int16_t *pcm, size_t frames, int channels) {
    (void)pcm;
    (void)frames;
    (void)channels;
}
#endif

/* ---- Coding ------------------------------------------------------------------ */

#if CONFIG_ESPMIRROR_TCP
static inline void put16(uint8_t *p, uint32_t v) {
    p[0] = v;
    p[1] = v >> 8;
}

static inline void put32(uint8_t *p, uint32_t v) {
    put16(p, v);
    put16(p + 2, v >> 16);
}

#endif

static size_t build_msg(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    size_t n = espmirror_core_build(&s_core, s_fb, s_msg, MSG_MAX, atomic_load_explicit(&s_state, memory_order_relaxed));
    xSemaphoreGive(s_lock);
    return n;
}

/* ---- The TCP side ------------------------------------------------------------- */

#if CONFIG_ESPMIRROR_TCP
/* The whole buffer by the deadline; short writes and a full socket buffer are retried. */
static bool send_until(int fd, const void *buf, size_t len, int64_t until) {
    size_t done = 0;
    while (done < len) {
        if (esp_timer_get_time() > until) return false;
        int r = send(fd, (const char *)buf + done, len - done, 0);
        if (r > 0) {
            done += r;
        } else if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            vTaskDelay(1);
        } else {
            return false;
        }
    }
    return true;
}

/* One message: its length, then the bytes, whole within ms. */
static bool tcp_send(int fd, const void *a, size_t na, const void *b, size_t nb, int ms) {
    int64_t until = esp_timer_get_time() + ms * 1000LL;
    uint8_t hdr[4];
    put32(hdr, na + nb);
    return send_until(fd, hdr, 4, until) && send_until(fd, a, na, until) && (!nb || send_until(fd, b, nb, until));
}

static bool send_board(int fd) {
    return tcp_send(fd, s_board, s_board_len, NULL, 0, SEND_MS);
}

#else
static bool tcp_send(int fd, const void *a, size_t na, const void *b, size_t nb, int ms) {
    (void)fd; (void)a; (void)na; (void)b; (void)nb; (void)ms;
    return false;
}
#endif

/* A key the viewer holds (bit in s_held) or taps (s_tap_key until
 * s_tap_until); the board sees it down while either says so. */
static void key_set(int code, bool down) {
    if (!s_cfg.on_key || code < 1 || code > 10) return;
    bool was = (s_held & 1u << code) || (s_tap_until && s_tap_key == code);
    if (down) s_held |= 1u << code;
    else s_held &= ~(1u << code);
    bool now = (s_held & 1u << code) || (s_tap_until && s_tap_key == code);
    if (now != was) s_cfg.on_key(code, now, s_cfg.user);
}

static void tap_end(void) {
    if (!s_tap_until) return;
    int code = s_tap_key;
    s_tap_until = 0;
    if (s_cfg.on_key && !(s_held & 1u << code)) s_cfg.on_key(code, false, s_cfg.user);
}

/* Every key the viewer had down is let go: on its way out, nothing may stay pressed. */
static void keys_release(void) {
    tap_end();
    for (int code = 1; code <= 10; code++) {
        if (s_held & 1u << code) key_set(code, false);
    }
}

/* A message from the viewer. */
static void input(const uint8_t *buf, size_t len) {
    if (len == 1 && buf[0] == 'O') {   /* this viewer types, and hears when a field opens */
        s_text_viewer = true;
        s_text_sent = -1;
        return;
    }
    espmirror_text_t text;
    if (espmirror_core_text(buf, len, &text.edit, text.text)) {
        /* With no field open the typing has nowhere to go; a full queue means the UI task stalled. */
        bool ok = !__atomic_load_n(&s_text_field, __ATOMIC_RELAXED)
                  || xQueueSend(s_text_in, &text, pdMS_TO_TICKS(SEND_MS)) == pdTRUE;
        memset(&text, 0, sizeof(text));
        if (!ok) viewer_drop();
        return;
    }
    if (len == 1 && buf[0] == 'R') {
        refresh();
        if (!viewer_send(s_board, s_board_len, SEND_MS)) viewer_drop();
        return;
    }
#if CONFIG_ESPMIRROR_AUDIO
    if (len == 2 && buf[0] == 7) {
        audio_set(buf[1]);
        return;
    }
    if (len == 2 && buf[0] == 8) {
        talk_set(buf[1] != 0);
        return;
    }
    if (len > 2 && len <= 642 && buf[0] == 'S') {
        if (s_talk && s_cfg.on_talk_audio && buf[1] == 1 && (len - 2) % 2 == 0) {
            int16_t pcm[(IN_MAX - 2) / 2];               /* aligned: the message buffer's offset is odd */
            memcpy(pcm, buf + 2, len - 2);
            s_cfg.on_talk_audio(pcm, (len - 2) / 2, s_cfg.user);
        }
        return;
    }
#endif
    if (len == 2 && buf[0] == 3 && buf[1] >= 1 && buf[1] <= 10) {
        int code = buf[1];
        tap_end();
        bool was = (s_held & 1u << code) != 0;
        s_tap_key = code;
        s_tap_until = esp_timer_get_time() + TAP_MS * 1000LL;
        if (s_cfg.on_key && !was) s_cfg.on_key(code, true, s_cfg.user);
    } else if (len == 3 && buf[0] == 6) {
        key_set(buf[1], buf[2] != 0);
    } else if (len == 5 && buf[0] <= 1 && (s_cfg.touch || s_cfg.on_pointer)) {
        int x = buf[1] | buf[2] << 8, y = buf[3] | buf[4] << 8;
        if (x < s_w && y < s_h) pointer_put(x, y, buf[0] != 0);
    }
}

/* The viewer's connection is over; its keys are let go and its audio ends before the socket. */
static void tcp_drop(void) {
    if (s_cfd < 0) return;
    keys_release();
    text_reset();
    pointer_put(s_ptr_x, s_ptr_y, false);
#if CONFIG_ESPMIRROR_AUDIO
    audio_set(0);
    talk_set(false);
#endif
#if CONFIG_ESPMIRROR_TCP
    close(s_cfd);
#endif
    __atomic_store_n(&s_cfd, -1, __ATOMIC_RELAXED);
#if CONFIG_ESPMIRROR_TCP
    s_inn = 0;
#endif
    ESP_LOGI(TAG, "viewer left");
    if (s_cfg.on_viewer) s_cfg.on_viewer(false, s_cfg.user);
}

#if CONFIG_ESPMIRROR_TCP
static void tcp_unpend(void) {
    s_pfd = -1;
    s_hn = 0;
}

/* The viewer's bytes, a few messages per pass so frames get their turn. */
static void tcp_read(void) {
    for (int msgs = 0; msgs < 8;) {
        if (s_inn && esp_timer_get_time() > s_in_until) {
            tcp_drop();
            return;
        }
        size_t need = s_inn < 4 ? 4 : 4 + (s_in[0] | s_in[1] << 8);
        if (s_inn >= 4 && (s_in[2] || s_in[3] || need > 4 + IN_MAX)) {
            tcp_drop();
            return;
        }
        if (s_inn == need) {
            input(s_in + 4, need - 4);
            s_inn = 0;
            msgs++;
            continue;
        }
        int r = recv(s_cfd, s_in + s_inn, need - s_inn, 0);
        if (r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            tcp_drop();
            return;
        }
        if (r < 0) return;
        if (!s_inn) s_in_until = esp_timer_get_time() + IN_MS * 1000LL;
        s_inn += r;
    }
}

/* The candidate becomes the viewer once its hello is right and the rate
 * limit allows; the viewer before is told 'T'. Else it is closed. */
static void tcp_admit(int fd) {
    static int64_t last_open_us;
    int64_t now = esp_timer_get_time();
    if (last_open_us && now - last_open_us < OPEN_GAP_MS * 1000) {
        ESP_LOGW(TAG, "viewer refused, the last one opened %lld ms ago", (long long)((now - last_open_us) / 1000));
        close(fd);
        return;
    }
    last_open_us = now;
#if CONFIG_ESPMIRROR_IRIS
    iris_takeover();
#endif
    if (s_cfd >= 0) {
        tcp_send(s_cfd, "T", 1, NULL, 0, 500);
        tcp_drop();                        /* its keys and audio end with it */
    }
    if (s_serial) { viewer_send("T", 1, 500); serial_end(); }
    __atomic_store_n(&s_cfd, fd, __ATOMIC_RELAXED);
    s_inn = 0;
    if (!send_board(fd)) {
        tcp_drop();
        return;
    }
    s_led_sent = 0xFF;                     /* the new viewer gets the LED's state at once */
    text_reset();
    refresh();
    ESP_LOGI(TAG, "viewer connected");
    if (s_cfg.on_viewer) s_cfg.on_viewer(true, s_cfg.user);
}

static void tcp_hello(void) {
    if (esp_timer_get_time() > s_p_until) {
        close(s_pfd);
        tcp_unpend();
        return;
    }
    int r = recv(s_pfd, s_hb + s_hn, sizeof(s_hb) - s_hn, 0);
    if (r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
        close(s_pfd);
        tcp_unpend();
        return;
    }
    if (r < 0) return;
    s_hn += r;
    if (s_hn < sizeof(s_hb)) return;
    int fd = s_pfd;
    tcp_unpend();
    if (memcmp(s_hb, HELLO, sizeof(s_hb)) != 0) {
        close(fd);
        return;
    }
    tcp_admit(fd);
}

/* A connection on the port becomes the candidate; one still saying hello makes way. */
static void tcp_accept(void) {
    int fd = accept(s_listen, NULL, NULL);
    if (fd < 0) return;
    if (s_pfd >= 0) {
        close(s_pfd);
        tcp_unpend();
    }
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        close(fd);                         /* a blocking read would stall the frames */
        return;
    }
    s_pfd = fd;
    s_hn = 0;
    s_p_until = esp_timer_get_time() + IN_MS * 1000LL;
}

/* The wait between frames, on the port: a connection or its bytes end it
 * early. With no viewer, candidate or tap pending it is open-ended, so an
 * idle board sleeps undisturbed. */
static void tcp_wait(int ms) {
    fd_set rf;
    FD_ZERO(&rf);
    if (s_listen < 0) { vTaskDelay(pdMS_TO_TICKS(ms) ? pdMS_TO_TICKS(ms) : 1); return; }
    FD_SET(s_listen, &rf);
    int mx = s_listen;
    if (s_cfd >= 0) {
        FD_SET(s_cfd, &rf);
        if (s_cfd > mx) mx = s_cfd;
    }
    if (s_pfd >= 0) {
        FD_SET(s_pfd, &rf);
        if (s_pfd > mx) mx = s_pfd;
    }
    struct timeval tv = {.tv_sec = ms / 1000, .tv_usec = (ms % 1000) * 1000};
    if (select(mx + 1, &rf, NULL, NULL, &tv) < 0) {
        vTaskDelay(pdMS_TO_TICKS(ms) ? pdMS_TO_TICKS(ms) : 1);
        return;
    }
    int64_t now = esp_timer_get_time();
    if (s_cfd >= 0 && (FD_ISSET(s_cfd, &rf) || (s_inn && now > s_in_until))) tcp_read();
    if (s_pfd >= 0 && (FD_ISSET(s_pfd, &rf) || now > s_p_until)) tcp_hello();
    if (FD_ISSET(s_listen, &rf)) tcp_accept();
    if (s_tap_until && now > s_tap_until) tap_end();
}

#else
static void tcp_wait(int ms) { vTaskDelay(pdMS_TO_TICKS(ms) ? pdMS_TO_TICKS(ms) : 1); }
#endif

#define SERIAL_CHUNK 128

static bool serial_write(const void *buf, size_t len, int64_t until)
{
    for (const uint8_t *p = buf; len;) {
        int64_t left = until - esp_timer_get_time();
        if (left <= 0) return false;
        size_t n = len < SERIAL_CHUNK ? len : SERIAL_CHUNK;
        int ms = left / 1000;
        int written = espmirror_usb_write(p, n, ms < 200 ? ms : 200);
        if (written <= 0) continue;
        p += written;
        len -= written;
    }
    return true;
}

static bool serial_send(const void *buf, size_t len, int ms)
{
    uint8_t hdr[6] = { 0xA5, 0x5A, len, len >> 8, len >> 16, len >> 24 };
    uint32_t crc = esp_rom_crc32_le(0, hdr + 2, 4);
    crc = esp_rom_crc32_le(crc, buf, len);
    uint8_t tail[4] = { crc, crc >> 8, crc >> 16, crc >> 24 };
    int64_t until = esp_timer_get_time() + ms * 1000LL;
    if (xSemaphoreTake(s_serial_tx, pdMS_TO_TICKS(ms)) != pdTRUE) return false;
    bool ok = serial_write(hdr, sizeof(hdr), until) && serial_write(buf, len, until)
              && serial_write(tail, sizeof(tail), until);
    xSemaphoreGive(s_serial_tx);
    return ok;
}

static bool viewer_send(const void *buf, size_t len, int ms)
{
    return s_serial ? serial_send(buf, len, ms) : tcp_send(s_cfd, buf, len, NULL, 0, ms);
}

static void serial_end(void)
{
    if (!s_serial) return;
    keys_release();
    text_reset();
#if CONFIG_ESPMIRROR_AUDIO
    audio_set(0);
    talk_set(false);
#endif
    if (s_cfg.on_viewer) s_cfg.on_viewer(false, s_cfg.user);
    pointer_put(s_ptr_x, s_ptr_y, false);
    s_serial = false;
    s_serial_open = false;             /* the console's session stops within a second */
}

static void serial_admit(void)
{
    if (!s_serial_open) return;
#if CONFIG_ESPMIRROR_IRIS
    iris_takeover();
#endif
    if (s_cfd >= 0) { tcp_send(s_cfd, "T", 1, NULL, 0, 500); tcp_drop(); }
    s_serial = true;
    s_led_sent = 0xff;
    text_reset();
    if (s_cfg.on_viewer) s_cfg.on_viewer(true, s_cfg.user);
    if (!viewer_send(s_board, s_board_len, SEND_MS)) {
        serial_end();
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    espmirror_core_reset(&s_core);
    espmirror_core_dirty(&s_core, 0, 0, s_w - 1, s_h - 1);
    xSemaphoreGive(s_lock);
}

static void serial_pump(void)
{
    serial_msg_t m;
    for (int i = 0; i < 8 && s_serial_in && xQueueReceive(s_serial_in, &m, 0) == pdTRUE; i++) {
        if (m.len == SERIAL_HELLO) {
            serial_admit();
        } else if (m.len == SERIAL_BYE) {
            if (s_serial) viewer_send("E", 1, 500);   /* the viewer stops sending and asks again */
            serial_end();
            xSemaphoreGive(s_serial_done);
        } else if (s_serial && s_serial_open && m.len <= SERIAL_INPUT_MAX) {
            input(m.b, m.len);
        }
    }
}

static char s_log_line[512];  /* protected by s_serial_tx; logging tasks can have small stacks */
static atomic_uint s_log_dropped;

static int serial_log(const char *fmt, va_list ap)
{
    if (xSemaphoreTake(s_serial_tx, pdMS_TO_TICKS(LOG_WAIT_MS)) != pdTRUE) {
        atomic_fetch_add(&s_log_dropped, 1);
        return 0;
    }
    int64_t until = esp_timer_get_time() + LOG_WAIT_MS * 1000LL;
    unsigned dropped = atomic_exchange(&s_log_dropped, 0);
    if (dropped) {
        int n = snprintf(s_log_line, sizeof(s_log_line), "\nmirror: %u log records dropped (TX busy)\n", dropped);
        if (!serial_write(s_log_line, n, until)) atomic_fetch_add(&s_log_dropped, dropped);
    }
    va_list copy;
    va_copy(copy, ap);
    int n = vsnprintf(s_log_line, sizeof(s_log_line), fmt, copy);
    va_end(copy);
    char *line = s_log_line;
    bool ok = n <= 0;
    if (n > 0) {
        if ((size_t)n >= sizeof(s_log_line)) {
            line = malloc((size_t)n + 1);
            if (line) vsnprintf(line, (size_t)n + 1, fmt, ap);
        }
        ok = line && serial_write(line, n, until);
        if (!ok) atomic_fetch_add(&s_log_dropped, 1);
    }
    if (line != s_log_line) free(line);
    xSemaphoreGive(s_serial_tx);
    return ok ? n : 0;
}

static bool serial_enqueue(const serial_msg_t *m, bool *pointer_down)
{
    bool pointer = m->len == 5 && m->b[0] <= 1;
    bool move = pointer && m->b[0] && *pointer_down;
    if (xQueueSend(s_serial_in, m, move ? 0 : pdMS_TO_TICKS(SEND_MS)) != pdTRUE) return move;
    if (pointer) *pointer_down = m->b[0] != 0;
    return true;
}

esp_err_t espmirror_serial_session(void)
{
    if (!espmirror_usb_supported()) return ESP_ERR_NOT_SUPPORTED;
    if (!s_ready || !s_serial_in || atomic_exchange(&s_session_busy, true)) return ESP_ERR_INVALID_STATE;
    vprintf_like_t log = esp_log_set_vprintf(serial_log);
    vTaskDelay(pdMS_TO_TICKS(50));     /* a log line already on its way finishes first */
    serial_msg_t m = { .len = SERIAL_HELLO };
    s_serial_open = true;
    xSemaphoreTake(s_serial_tx, portMAX_DELAY);
    bool marked = serial_write(s_mark, sizeof(s_mark), esp_timer_get_time() + SEND_MS * 1000LL);
    xSemaphoreGive(s_serial_tx);
    if (marked && xQueueSend(s_serial_in, &m, pdMS_TO_TICKS(500)) == pdTRUE) {
        int64_t heard = esp_timer_get_time();
        bool pointer_down = false;
        while (s_serial_open) {
            uint8_t hdr[4];
            if (!espmirror_usb_read(hdr, 1, 1000)) {
                if (esp_timer_get_time() - heard > SERIAL_QUIET_MS * 1000LL) break;
                continue;
            }
            if (!s_serial_open || !espmirror_usb_read(hdr + 1, 3, IN_MS)) break;
            size_t n = hdr[0] | hdr[1] << 8;
            if (hdr[2] || hdr[3] || n > IN_MAX) break;   /* not a viewer's message: the console is the console again */
            if (n && !espmirror_usb_read(m.b, n, IN_MS)) break;
            m.len = n;
            heard = esp_timer_get_time();
            if (!serial_enqueue(&m, &pointer_down)) break;
        }
    }
    s_serial_open = false;
    m.len = SERIAL_BYE;
    xQueueSend(s_serial_in, &m, portMAX_DELAY);
    /* No console reuse or hook restoration until the mirror task has consumed the final BYE. */
    xSemaphoreTake(s_serial_done, portMAX_DELAY);
    /* What the viewer sent meanwhile is no console command: a finger's x of 122 is 'z', sleep. */
    uint8_t c;
    for (int64_t until = esp_timer_get_time() + 2000000; esp_timer_get_time() < until && espmirror_usb_read(&c, 1, 150); ) {
    }
    esp_log_set_vprintf(log);
    s_session_busy = false;
    return ESP_OK;
}


static void viewer_drop(void) {
    if (s_serial) serial_end(); else tcp_drop();
}

static void console_task(void *arg) {
    (void)arg;
    while (!s_ready) vTaskDelay(1);
    char line[64];
    size_t used = 0;
    bool bad = false;
    for (;;) {
        uint8_t ch;
        if (!espmirror_usb_read(&ch, 1, 1000)) continue;
        if (ch == '\n' || ch == '\r') {
            line[used] = 0;
            if (!bad && (!strcmp(line, "mirror") || !strcmp(line, ">mirror"))) espmirror_serial_session();
            used = 0; bad = false;
        } else if (ch >= 32 && ch < 127 && used < sizeof(line) - 1) {
            line[used++] = ch;
        } else {
            bad = true;
        }
    }
}

#if CONFIG_ESPMIRROR_IRIS
#define IRIS_KEY_SERVICE 0x4553
#define IRIS_KEY_METHOD 1
#define IRIS_BOARD_METHOD 2
typedef struct {
    unsigned generation;
    uint8_t len, data[5];
} iris_input_t;
static QueueHandle_t s_iris_in;
static atomic_uint s_iris_generation;
static unsigned s_iris_seen;
static bool s_iris_input_owned;
static bool s_iris_screen_registered, s_iris_pointer_registered, s_iris_key_registered, s_iris_board_registered;

static esp_err_t iris_screen_begin(const esp_iris_media_desc_t *requested,
                                   esp_iris_media_desc_t *actual, uint32_t *total,
                                   void *ctx) {
    (void)ctx;
    if (!s_ready) return ESP_ERR_INVALID_STATE;
    if (requested->format && requested->format != ESP_IRIS_PIXEL_FORMAT_RGB565) return ESP_ERR_NOT_SUPPORTED;
    *actual = (esp_iris_media_desc_t){ .width = s_w, .height = s_h,
        .stride = (uint32_t)s_w * 2, .format = ESP_IRIS_PIXEL_FORMAT_RGB565 };
    *total = actual->stride * actual->height;
    return ESP_OK;
}

static esp_err_t iris_screen_read(uint32_t offset, uint8_t *out, size_t capacity,
                                  size_t *size, void *ctx) {
    (void)ctx;
    *size = 0;
    if (!s_ready) return ESP_ERR_INVALID_STATE;
    size_t total = (size_t)s_w * s_h * 2;
    if (offset > total || (!out && capacity)) return ESP_ERR_INVALID_ARG;
    size_t n = total - offset;
    if (n > capacity) n = capacity;
    /* Iris reads chunks across ticks; locking each copy permits drawing between chunks. */
    if (xSemaphoreTake(s_lock, 0) != pdTRUE) return ESP_ERR_TIMEOUT;
    if (n) memcpy(out, (const uint8_t *)s_fb + offset, n);
    xSemaphoreGive(s_lock);
    *size = n;
    return ESP_OK;
}

static void iris_screen_end(void *ctx) {
    (void)ctx;
    atomic_fetch_add(&s_iris_generation, 1);
}

static esp_err_t iris_enqueue(iris_input_t *event) {
    event->generation = atomic_load(&s_iris_generation);
    if (!s_ready || viewer_on() || !esp_iris_media_is_streaming(ESP_IRIS_CHANNEL_SCREEN)) return ESP_ERR_INVALID_STATE;
    if (event->generation != atomic_load(&s_iris_generation)) return ESP_ERR_INVALID_STATE;
    return xQueueSend(s_iris_in, event, 0) == pdTRUE ? ESP_OK : ESP_ERR_NO_MEM;
}

static esp_err_t iris_pointer_rpc(const esp_iris_rpc_request_t *request, uint8_t *response,
                                  size_t capacity, size_t *size, void *ctx) {
    (void)ctx;
    *size = 0;
    if (!request || request->payload_size != ESP_IRIS_POINTER_MESSAGE_SIZE || !request->payload
        || capacity < ESP_IRIS_POINTER_MESSAGE_SIZE || !response) return ESP_ERR_INVALID_ARG;
    const uint8_t *p = request->payload;
    if (p[0] > 2 || p[1] || p[6] || p[7]) return ESP_ERR_INVALID_ARG;
    if (!s_cfg.touch && !s_cfg.on_pointer) return ESP_ERR_NOT_SUPPORTED;
    int x = (int16_t)(p[2] | p[3] << 8), y = (int16_t)(p[4] | p[5] << 8);
    if (x < 0 || y < 0 || x >= s_w || y >= s_h) return ESP_ERR_INVALID_ARG;
    iris_input_t event = { .len = 5, .data = { p[0] != 2, p[2], p[3], p[4], p[5] } };
    esp_err_t err = iris_enqueue(&event);
    if (err != ESP_OK) return err;
    memcpy(response, p, ESP_IRIS_POINTER_MESSAGE_SIZE);
    *size = ESP_IRIS_POINTER_MESSAGE_SIZE;
    return ESP_OK;
}

static esp_err_t iris_key_rpc(const esp_iris_rpc_request_t *request, uint8_t *response,
                              size_t capacity, size_t *size, void *ctx) {
    (void)ctx;
    *size = 0;
    if (!request || request->payload_size != 2 || !request->payload || !response || capacity < 2) return ESP_ERR_INVALID_ARG;
    const uint8_t *p = request->payload;
    if (p[0] < 1 || p[0] > 10 || p[1] > 1) return ESP_ERR_INVALID_ARG;
    if (!s_cfg.on_key) return ESP_ERR_NOT_SUPPORTED;
    iris_input_t event = { .len = 3, .data = { 6, p[0], p[1] } };
    esp_err_t err = iris_enqueue(&event);
    if (err != ESP_OK) return err;
    memcpy(response, p, 2);
    *size = 2;
    return ESP_OK;
}

static void iris_release(void) {
    if (!s_iris_input_owned) return;
    keys_release();
    pointer_put(s_ptr_x, s_ptr_y, false);
    s_iris_input_owned = false;
}

static esp_err_t iris_board_rpc(const esp_iris_rpc_request_t *request, uint8_t *response,
                                size_t capacity, size_t *size, void *ctx) {
    (void)ctx;
    *size = 0;
    if (!s_ready) return ESP_ERR_INVALID_STATE;
    if (!request || request->payload_size || !response || capacity < s_board_len - 1) return ESP_ERR_INVALID_ARG;
    memcpy(response, s_board + 1, s_board_len - 1);
    *size = s_board_len - 1;
    return ESP_OK;
}

static void iris_takeover(void) {
    atomic_fetch_add(&s_iris_generation, 1);
    iris_release();
}

static void iris_pump(void) {
    unsigned generation = atomic_load(&s_iris_generation);
    bool active = esp_iris_media_is_streaming(ESP_IRIS_CHANNEL_SCREEN) && !viewer_on();
    if (!active || generation != s_iris_seen) iris_release();
    s_iris_seen = generation;
    iris_input_t event;
    for (int i = 0; i < 8 && xQueueReceive(s_iris_in, &event, 0) == pdTRUE; i++) {
        if (!active || event.generation != generation) continue;
        s_iris_input_owned = true;
        input(event.data, event.len);
    }
}

static void iris_unregister(void) {
    if (s_iris_board_registered) esp_iris_rpc_unregister(IRIS_KEY_SERVICE, IRIS_BOARD_METHOD);
    if (s_iris_key_registered) esp_iris_rpc_unregister(IRIS_KEY_SERVICE, IRIS_KEY_METHOD);
    if (s_iris_pointer_registered) esp_iris_rpc_unregister(ESP_IRIS_POINTER_SERVICE_ID, ESP_IRIS_POINTER_METHOD_ID);
    if (s_iris_screen_registered) esp_iris_screen_unregister(&s_cfg);
    s_iris_key_registered = s_iris_pointer_registered = s_iris_screen_registered = false;
    s_iris_board_registered = false;
}

static esp_err_t iris_register(void) {
    const esp_iris_screen_backend_t backend = { .begin = iris_screen_begin,
        .read = iris_screen_read, .end = iris_screen_end, .user_ctx = &s_cfg };
    esp_err_t err = esp_iris_screen_register(&backend);
    if (err != ESP_OK) return err;
    s_iris_screen_registered = true;
    err = esp_iris_rpc_register(ESP_IRIS_POINTER_SERVICE_ID, ESP_IRIS_POINTER_METHOD_ID, iris_pointer_rpc, &s_cfg);
    if (err != ESP_OK) return err;
    s_iris_pointer_registered = true;
    err = esp_iris_rpc_register(IRIS_KEY_SERVICE, IRIS_KEY_METHOD, iris_key_rpc, &s_cfg);
    if (err != ESP_OK) return err;
    s_iris_key_registered = true;
    err = esp_iris_rpc_register(IRIS_KEY_SERVICE, IRIS_BOARD_METHOD, iris_board_rpc, &s_cfg);
    if (err == ESP_OK) s_iris_board_registered = true;
    return err;
}
#endif

static void mirror_task(void *arg) {
    (void)arg;
    int64_t last_beat = 0, last_frame = 0;
    while (!s_ready) vTaskDelay(1);
    for (;;) {
#if CONFIG_ESPMIRROR_IRIS
        iris_pump();
#endif
        serial_pump();
        tcp_wait(10);
        serial_pump();
        if (s_tap_until && esp_timer_get_time() > s_tap_until) tap_end();
        led_pump();
        text_pump();
#if CONFIG_ESPMIRROR_AUDIO
        audio_pump();
#endif
        if (!viewer_on()) continue;
        int64_t now = esp_timer_get_time() / 1000;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        bool dirty = espmirror_core_pending(&s_core);
        xSemaphoreGive(s_lock);
        if (now - last_frame < FRAME_MS || (!dirty && now - last_beat < BEAT_MS)) continue;
        last_beat = last_frame = now;
        size_t len = build_msg();
        if (!viewer_send(s_msg, len, SEND_MS)) viewer_drop();
    }
}

static esp_err_t start(const espmirror_config_t *config);

esp_err_t espmirror_start(const espmirror_config_t *config) {
    esp_err_t err = start(config);
    if (err != ESP_OK && !s_task) {
        s_w = s_h = 0;                    /* the screen calls check s_w; without a task there is no lock either */
        heap_caps_free(s_previous); s_previous = NULL;
    }
    return err;
}

static esp_err_t start(const espmirror_config_t *config) {
    if (!config || config->width <= 0 || config->height <= 0 || config->width > 4096 || config->height > 4096) {
        return ESP_ERR_INVALID_ARG;
    }
    if ((size_t)config->width * 2 + RECT_HDR + 8 > MSG_MAX) {
        ESP_LOGE(TAG, "a raw row of %d pixels does not fit a %d KB message", config->width, CONFIG_ESPMIRROR_MSG_KB);
        return ESP_ERR_INVALID_ARG;
    }
    if (s_task) return ESP_ERR_INVALID_STATE;
    if (config->button_count > 10 || (config->button_count && !config->buttons)) return ESP_ERR_INVALID_ARG;
    if (!CONFIG_ESPMIRROR_TCP && !CONFIG_ESPMIRROR_IRIS && !espmirror_usb_supported()) return ESP_ERR_NOT_SUPPORTED;
#if CONFIG_ESPMIRROR_IRIS
    if (esp_iris_is_started()) return ESP_ERR_INVALID_STATE;
    if ((size_t)config->width * 2 > CONFIG_ESP_IRIS_MEDIA_LATEST_BYTES) return ESP_ERR_INVALID_ARG;
#endif
    if (CONFIG_ESPMIRROR_USB_NETWORK && config->external_console) return ESP_ERR_INVALID_ARG;   /* the component owns TinyUSB */
    uint8_t mac[6];
#if SOC_WIFI_SUPPORTED
    esp_err_t mac_err = esp_read_mac(mac, ESP_MAC_WIFI_STA);
#else
    esp_err_t mac_err = esp_read_mac(mac, ESP_MAC_BASE);
#endif
    if (mac_err != ESP_OK) return mac_err;
    s_cfg = *config;
    /* The name as JSON needs it: a bounded copy, quotes and backslashes escaped, control characters out. */
    size_t at = 0;
    for (const char *p = config->name ? config->name : ""; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c < 0x20 || (c >= 0x80 && c < 0xC2) || c > 0xF4) continue;
        size_t need = c == '"' || c == '\\' ? 2 : c < 0x80 ? 1 : c >= 0xF0 ? 4 : c >= 0xE0 ? 3 : 2;   /* whole characters */
        if (c >= 0x80) {
            size_t k = 1;
            while (k < need && ((unsigned char)p[k] & 0xC0) == 0x80) k++;
            if (k < need) continue;       /* a broken sequence: its lead byte goes, the rest is read afresh */
        }
        if (at + need >= sizeof(s_name)) break;
        if (need == 2 && c < 0x80) s_name[at++] = '\\';
        s_name[at++] = (char)c;
        for (size_t k = 1; k < need && c >= 0x80; k++) s_name[at++] = *++p;
    }
    s_name[at] = 0;
    s_w = config->width;
    s_h = config->height;
    bool is_round = config->round || config->corner_radius * 2 >= (s_w < s_h ? s_w : s_h);
    float diagonal = config->diagonal > 0 ? config->diagonal
        : config->ppi > 0 ? (is_round ? (s_w < s_h ? s_w : s_h) : hypotf(s_w, s_h)) / config->ppi : 0;
    s_board_len = snprintf(s_board, sizeof(s_board),
        "B{\"version\":2,\"name\":\"%s\",\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
        "\"w\":%d,\"h\":%d,\"round\":%s,\"diag\":%.3f,\"touch\":%s,\"keyboard\":%s,"
        "\"audio\":%s,\"buttons\":[",
        s_name, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], s_w, s_h,
        is_round ? "true" : "false",
        (double)diagonal, (config->touch || config->on_pointer) ? "true" : "false",
        config->keyboard ? "true" : "false", CONFIG_ESPMIRROR_AUDIO ? "true" : "false");
    for (size_t i = 0; i < (config->button_count ? config->button_count : 3); i++) {
        int role = config->button_count ? config->buttons[i].role : (int)i + 1;
        int x = config->button_count ? config->buttons[i].x : -1;
        int y = config->button_count ? config->buttons[i].y : -1;
        if (role < 1 || role > 10) return ESP_ERR_INVALID_ARG;
        char label[96]; size_t n = 0;
        const char *name = config->button_count ? config->buttons[i].name : i == 0 ? "AUX" : i == 1 ? "MAIN" : "POWER";
        for (const unsigned char *p = (const unsigned char *)(name ? name : ""); *p && n + 2 < sizeof(label); p++) {
            if (*p < 32) continue;
            if (*p == '"' || *p == '\\') label[n++] = '\\';
            label[n++] = *p;
        }
        label[n] = 0;
        int added = snprintf(s_board + s_board_len, sizeof(s_board) - s_board_len,
            "%s{\"role\":%d,\"name\":\"%s\",\"x\":%d,\"y\":%d}", i ? "," : "", role, label, x, y);
        if (added < 0 || (size_t)added >= sizeof(s_board) - s_board_len) return ESP_ERR_INVALID_ARG;
        s_board_len += added;
    }
    if (s_board_len + 3 > sizeof(s_board)) return ESP_ERR_INVALID_ARG;
    memcpy(s_board + s_board_len, "]}", 3); s_board_len += 2;
#if CONFIG_ESPMIRROR_IRIS
    if (s_board_len - 1 > CONFIG_ESP_IRIS_RPC_BODY_BYTES) return ESP_ERR_INVALID_ARG;
#endif
    unsigned flags = 0;
#if CONFIG_ESPMIRROR_DELTA
    flags |= ESPMIRROR_DELTA;
    s_previous = alloc((size_t)s_w * s_h * 2, true);
#endif
#if CONFIG_ESPMIRROR_QOI
    flags |= ESPMIRROR_QOI;
#endif
    if (!espmirror_core_init_ex(&s_core, s_w, s_h, flags, s_previous)) return ESP_ERR_NO_MEM;
    s_serial_in = xQueueCreate(SERIAL_QUEUE, SERIAL_ITEM_SIZE);
    s_text_in = xQueueCreate(TEXT_Q, sizeof(espmirror_text_t));
#if CONFIG_ESPMIRROR_IRIS
    s_iris_in = xQueueCreate(PTR_Q, sizeof(iris_input_t));
#endif
    s_serial_tx = xSemaphoreCreateMutex();
    s_serial_done = xSemaphoreCreateBinary();
    SemaphoreHandle_t lock = xSemaphoreCreateMutex();
    uint16_t *fb = alloc((size_t)s_w * s_h * sizeof(uint16_t), true);
    uint8_t *msg = alloc(MSG_MAX, false);
    uint8_t *in = alloc(4 + IN_MAX, false);
#if CONFIG_ESPMIRROR_AUDIO
    uint8_t *aud = alloc(AUD_RING, false);
    uint8_t *aud_out = alloc(2 + AUD_CHUNK, false);
    bool audio_ok = aud && aud_out;
#else
    bool audio_ok = true;
#endif
#if CONFIG_ESPMIRROR_TCP
    int fd = -1;
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons(PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    int one = 1;
#endif
    esp_err_t err = ESP_OK;
    const char *fail = NULL;
    if (!lock || !fb || !msg || !in || !audio_ok || !s_serial_in || !s_serial_tx || !s_serial_done || !s_text_in) {
        fail = "no memory for the screen copy";
        err = ESP_ERR_NO_MEM;
    }
#if CONFIG_ESPMIRROR_IRIS
    if (!s_iris_in) { fail = "no Iris input queue"; err = ESP_ERR_NO_MEM; }
    if (!fail) {
        err = iris_register();
        if (err != ESP_OK) fail = "Iris backend registration failed";
    }
#endif
    /* Before the socket: the USB network brings up the TCP/IP stack. */
    if (!fail && espmirror_usb_supported() && !config->external_console) {
        err = espmirror_usb_install();
        if (err != ESP_OK) fail = "USB driver unavailable";
    }
#if CONFIG_ESPMIRROR_TCP
    if (!fail && ((fd = socket(AF_INET, SOCK_STREAM, 0)) < 0
                  || setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0
                  || bind(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0 || listen(fd, 1) < 0)) {
        fail = "no TCP port";
        err = ESP_FAIL;
    }
#endif
    if (!fail) {
        s_lock = lock;
        s_msg = msg;
        s_in = in;
#if CONFIG_ESPMIRROR_AUDIO
        s_aud = aud;
        s_aud_out = aud_out;
#endif
#if CONFIG_ESPMIRROR_TCP
        s_listen = fd;
#endif
        BaseType_t ok = CONFIG_ESPMIRROR_TASK_CORE >= 0
            ? xTaskCreatePinnedToCore(mirror_task, "espmirror", CONFIG_ESPMIRROR_TASK_STACK, NULL,
                                      CONFIG_ESPMIRROR_TASK_PRIORITY, &s_task, CONFIG_ESPMIRROR_TASK_CORE)
            : xTaskCreate(mirror_task, "espmirror", CONFIG_ESPMIRROR_TASK_STACK, NULL,
                          CONFIG_ESPMIRROR_TASK_PRIORITY, &s_task);
        if (ok != pdPASS) {
            fail = "no task";
            err = ESP_ERR_NO_MEM;
            s_task = NULL;
#if CONFIG_ESPMIRROR_TCP
            s_listen = -1;
#endif
            s_msg = NULL;
            s_in = NULL;
            s_lock = NULL;
#if CONFIG_ESPMIRROR_AUDIO
            s_aud = NULL;
            s_aud_out = NULL;
#endif
        }
    }
    if (!fail && espmirror_usb_supported() && !config->external_console
        && (CONFIG_ESPMIRROR_USB_AUTO || CONFIG_ESPMIRROR_USB_NETWORK)) {
        if (xTaskCreate(console_task, "mirror_console", 4096, NULL, 3, NULL) != pdPASS) {
            vTaskDelete(s_task); s_task = NULL;
            fail = "no console task"; err = ESP_ERR_NO_MEM;
        }
    }
    if (fail) {
#if CONFIG_ESPMIRROR_IRIS
        iris_unregister();
        if (s_iris_in) vQueueDelete(s_iris_in);
        s_iris_in = NULL;
#endif
        ESP_LOGE(TAG, "%s (errno %d): no live screen", fail, errno);
#if CONFIG_ESPMIRROR_TCP
        if (fd >= 0) close(fd);
#endif
        heap_caps_free(fb);
        heap_caps_free(msg);
        heap_caps_free(in);
#if CONFIG_ESPMIRROR_AUDIO
        heap_caps_free(aud);
        heap_caps_free(aud_out);
#endif
        if (lock) vSemaphoreDelete(lock);
        if (s_serial_in) vQueueDelete(s_serial_in);
        if (s_serial_tx) vSemaphoreDelete(s_serial_tx);
        if (s_serial_done) vSemaphoreDelete(s_serial_done);
        if (s_text_in) vQueueDelete(s_text_in);
        s_serial_in = NULL; s_serial_tx = NULL; s_serial_done = NULL; s_text_in = NULL;
        heap_caps_free(s_previous); s_previous = NULL;
        espmirror_usb_uninstall();
        return err;
    }
    __atomic_store_n(&s_fb, fb, __ATOMIC_RELEASE);   /* the drawing side may call in from now on */
    atomic_store_explicit(&s_ready, true, memory_order_release);   /* and the task may serve */
#if CONFIG_ESPMIRROR_TCP
    esp_err_t mdns = mdns_init();      /* ESP_OK also when the app started mDNS already */
    char host[MDNS_NAME_BUF_LEN];
    if (mdns == ESP_OK && mdns_hostname_get(host) != ESP_OK) mdns_hostname_set("espmirror");
    if (mdns == ESP_OK || mdns == ESP_ERR_INVALID_STATE) {
        char width[8], height[8], address[18];
        snprintf(width, sizeof(width), "%d", s_w); snprintf(height, sizeof(height), "%d", s_h);
        snprintf(address, sizeof(address), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        mdns_txt_item_t txt[] = {{"version", "2"}, {"width", width}, {"height", height}, {"mac", address}};
        mdns_service_add(s_name, "_musemirror", "_tcp", PORT, txt, 4);
#if CONFIG_ESPMIRROR_USB_NETWORK
        esp_netif_t *usb = espmirror_usb_netif();
        if (mdns_register_netif(usb) == ESP_OK) {
            mdns_netif_action(usb, MDNS_EVENT_ENABLE_IP4 | MDNS_EVENT_ANNOUNCE_IP4);
        } else {
            ESP_LOGW(TAG, "no mDNS slot for the USB network: raise CONFIG_MDNS_MAX_INTERFACES");
        }
#endif
    }
#endif
    ESP_LOGI(TAG, "%s, %d x %d, port %d", s_name, s_w, s_h, PORT);
#if CONFIG_ESPMIRROR_USB_NETWORK
    ESP_LOGI(TAG, "USB network: the board is 192.168.%d.1", CONFIG_ESPMIRROR_USB_NETWORK_SUBNET);
#endif
    return ESP_OK;
}
