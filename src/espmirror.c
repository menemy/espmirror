/*
 * espmirror: the board's screen, keys, LED and audio on a desktop, over TCP.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include "espmirror.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "sdkconfig.h"

static const char *TAG = "espmirror";

/* The wire (docs/protocol.md): a 4-byte little-endian length, then the
 * message. The viewer says HELLO first. From the board: 'F' a frame (the
 * screen state, the rectangle count, width, height, then each rectangle: x,
 * y, w, h, coding, 0, length, pixels), 'B' then JSON naming the board, 'T'
 * when another viewer takes the screen, 'A' then the source (1 the
 * microphone, 2 the speaker) and PCM16 mono 16 kHz while the viewer listens,
 * 'L' then the LED's state. From the viewer: 1/0 and x, y (a pointer, for
 * touch screens; passed on as nothing here), 3 and a key (a tap), 6, a key
 * and 1/0 (held down or let go), 7 and the source to listen to or 0, 8 and
 * 1/0 (talk through the speaker, or stop), 'S', a format byte and PCM16 mono
 * 16 kHz (the viewer's microphone), anything else keeps the connection. */
#define PORT        CONFIG_ESPMIRROR_PORT
#define HELLO       "MIRR1\n"
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
#endif

static espmirror_config_t s_cfg;
static int s_w, s_h;

static uint16_t *s_fb;         /* the screen copy, under s_lock */
static uint8_t *s_msg;
static SemaphoreHandle_t s_lock;
static TaskHandle_t s_task;
static volatile uint8_t s_state;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static struct { int x1, y1, x2, y2; } s_dirty[MAX_DIRTY];
static int s_ndirty;

/* The TCP side, all on the mirror task: the viewer's connection with the
 * message being received, and one candidate still saying hello (the viewer
 * keeps the screen until the candidate is admitted). */
static int s_listen = -1;
static int s_cfd = -1;
static uint8_t *s_in;
static size_t s_inn;
static int64_t s_in_until;
static int s_pfd = -1;
static uint8_t s_hb[sizeof(HELLO) - 1];
static size_t s_hn;
static int64_t s_p_until;
static volatile uint8_t s_led_mode, s_led_sent = 0xFF;   /* the LED as driven; what the viewer was last told */
static int s_tap_key;          /* a key tapped from the viewer, let go at s_tap_until */
static int64_t s_tap_until;
static unsigned s_held;        /* keys the viewer holds down, a bit per code */

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
static void tcp_drop(void);

static void *alloc(size_t size, bool zero) {
    uint32_t caps = MALLOC_CAP_8BIT;
#if CONFIG_ESPMIRROR_PREFER_PSRAM
    void *p = zero ? heap_caps_calloc(1, size, caps | MALLOC_CAP_SPIRAM) : heap_caps_malloc(size, caps | MALLOC_CAP_SPIRAM);
    if (p) return p;
#endif
    return zero ? heap_caps_calloc(1, size, caps) : heap_caps_malloc(size, caps);
}

static void dirty_add(int x1, int y1, int x2, int y2) {
    portENTER_CRITICAL(&s_mux);
    for (int i = 0; i < s_ndirty; i++) {
        /* Touching or overlapping: one rectangle. */
        if (x1 <= s_dirty[i].x2 + 1 && x2 >= s_dirty[i].x1 - 1 && y1 <= s_dirty[i].y2 + 1 && y2 >= s_dirty[i].y1 - 1) {
            if (x1 < s_dirty[i].x1) s_dirty[i].x1 = x1;
            if (y1 < s_dirty[i].y1) s_dirty[i].y1 = y1;
            if (x2 > s_dirty[i].x2) s_dirty[i].x2 = x2;
            if (y2 > s_dirty[i].y2) s_dirty[i].y2 = y2;
            portEXIT_CRITICAL(&s_mux);
            return;
        }
    }
    if (s_ndirty < MAX_DIRTY) {
        s_dirty[s_ndirty].x1 = x1;
        s_dirty[s_ndirty].y1 = y1;
        s_dirty[s_ndirty].x2 = x2;
        s_dirty[s_ndirty].y2 = y2;
        s_ndirty++;
    } else {
        if (x1 < s_dirty[0].x1) s_dirty[0].x1 = x1;
        if (y1 < s_dirty[0].y1) s_dirty[0].y1 = y1;
        if (x2 > s_dirty[0].x2) s_dirty[0].x2 = x2;
        if (y2 > s_dirty[0].y2) s_dirty[0].y2 = y2;
    }
    portEXIT_CRITICAL(&s_mux);
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
void espmirror_frame_end(int x, int y, int width, int height) {
    if (!__atomic_load_n(&s_fb, __ATOMIC_ACQUIRE)) return;
    xSemaphoreGive(s_lock);
    if (width > 0 && height > 0) dirty_add(x, y, x + width - 1, y + height - 1);
}

void espmirror_draw(int x, int y, int width, int height, const uint16_t *rgb565, int stride) {
    if (!rgb565 || width <= 0 || height <= 0) return;
    if (stride <= 0) stride = width;
    uint16_t *fb = espmirror_frame_begin();
    if (!fb) return;
    int x0 = x < 0 ? 0 : x, y0 = y < 0 ? 0 : y;
    int x1 = x + width - 1 < s_w ? x + width - 1 : s_w - 1, y1 = y + height - 1 < s_h ? y + height - 1 : s_h - 1;
    for (int r = y0; r <= y1 && x1 >= x0; r++) {
        memcpy(fb + r * s_w + x0, rgb565 + (r - y) * stride + (x0 - x), (x1 - x0 + 1) * 2);
    }
    espmirror_frame_end(x0, y0, x1 - x0 + 1, y1 - y0 + 1);
}

void espmirror_size(int *width, int *height) {
    if (width) *width = s_w;
    if (height) *height = s_h;
}

void espmirror_set_screen_state(uint8_t state) {
    s_state = state > 2 ? 2 : state;
    dirty_add(0, 0, 0, 0);                /* a frame goes out with it; an empty rectangle codes to nothing much */
}

void espmirror_set_led(uint8_t mode) {
    s_led_mode = mode > 3 ? 3 : mode;      /* the mirror task sends it on its next pass */
}

bool espmirror_connected(void) {
    return s_cfd >= 0;
}

/* Mirror task: the LED's state to the viewer when it changed since it was last told. */
static void led_pump(void) {
    uint8_t mode = s_led_mode;
    if (s_cfd < 0 || mode == s_led_sent) return;
    uint8_t msg[2] = {'L', mode};
    if (tcp_send(s_cfd, msg, 2, NULL, 0, SEND_MS)) {
        s_led_sent = mode;
    } else {
        tcp_drop();                        /* part of a message may be out: nothing may follow it */
    }
}

/* ---- Audio ------------------------------------------------------------------ */

#if CONFIG_ESPMIRROR_AUDIO
/* Mono samples into the ring, from whichever task has them, when src is the
 * source the viewer asked for. */
static void audio_put(uint8_t src, const int16_t *pcm, size_t samples) {
    size_t n = samples * 2;
    if (s_aud_src != src || !s_aud || n > AUD_RING) return;
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
    audio_put(1, mono, samples);
}

void espmirror_audio_playback(const int16_t *pcm, size_t frames, int channels) {
    if (s_aud_src != 2 || !s_aud) return;
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
    if (src > 2 || src == s_aud_src) return;
    portENTER_CRITICAL(&s_aud_mux);
    s_aud_r = s_aud_w;
    s_aud_src = src;
    portEXIT_CRITICAL(&s_aud_mux);
    ESP_LOGI(TAG, "viewer %s", src == 1 ? "listens to the microphone" : src == 2 ? "listens to the speaker" : "stopped listening");
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
    if (!s_aud_src || s_cfd < 0) return;
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
        s_aud_out[1] = s_aud_src;
        if (!tcp_send(s_cfd, s_aud_out, 2 + AUD_CHUNK, NULL, 0, SEND_MS)) {
            ESP_LOGW(TAG, "viewer too slow or gone, closing");
            tcp_drop();
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

static inline void put16(uint8_t *p, uint32_t v) {
    p[0] = v;
    p[1] = v >> 8;
}

static inline void put32(uint8_t *p, uint32_t v) {
    put16(p, v);
    put16(p + 2, v >> 16);
}

/* Run-length pairs (count, colour) over a rectangle in row order, or in
 * column order (vertical bars code several times smaller that way). out NULL
 * only counts. */
static size_t rle(uint8_t *out, int x, int y, int w, int h, bool cols) {
    size_t n = 0;
    uint16_t c = 0;
    uint32_t run = 0;
    int outer = cols ? w : h, inner = cols ? h : w;
    for (int a = 0; a < outer; a++) {
        const uint16_t *line = cols ? s_fb + y * s_w + x + a : s_fb + (y + a) * s_w + x;
        int step = cols ? s_w : 1;
        for (int b = 0; b < inner; b++) {
            uint16_t px = line[b * step];
            if (run && px == c && run < 0xFFFF) {
                run++;
                continue;
            }
            if (run) {
                if (out) {
                    put16(out + n, run);
                    put16(out + n + 2, c);
                }
                n += 4;
            }
            c = px;
            run = 1;
        }
    }
    if (out) {
        put16(out + n, run);
        put16(out + n + 2, c);
    }
    return n + 4;
}

/* One rectangle in the shortest of raw, row runs and (when asked) column
 * runs; 0 when it does not fit in room. */
static size_t code_rect(uint8_t *out, size_t room, int x, int y, int w, int h, bool try_cols) {
    size_t raw = (size_t)w * h * 2, rows = rle(NULL, x, y, w, h, false);
    size_t cols = try_cols ? rle(NULL, x, y, w, h, true) : SIZE_MAX;
    int enc = rows <= cols && rows < raw ? 1 : cols < raw ? 2 : 0;
    size_t n = enc == 1 ? rows : enc == 2 ? cols : raw;
    if (RECT_HDR + n > room) return 0;
    uint8_t *p = out + RECT_HDR;
    if (enc) {
        rle(p, x, y, w, h, enc == 2);
    } else {
        for (int r = 0; r < h; r++) {
            const uint16_t *row = s_fb + (y + r) * s_w + x;
            for (int i = 0; i < w; i++) put16(p + ((size_t)r * w + i) * 2, row[i]);
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

/* Fills s_msg from the dirty list under s_lock; what does not fit stays dirty
 * for the next message. */
static size_t build_msg(void) {
    struct { int x1, y1, x2, y2; } todo[MAX_DIRTY];
    int nt;
    portENTER_CRITICAL(&s_mux);
    nt = s_ndirty;
    memcpy(todo, s_dirty, sizeof(todo[0]) * nt);
    s_ndirty = 0;
    portEXIT_CRITICAL(&s_mux);

    s_msg[0] = 'F';
    s_msg[1] = s_state;
    put16(s_msg + 4, s_w);
    put16(s_msg + 6, s_h);
    size_t len = 8;
    int rects = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (int i = 0; i < nt; i++) {
        int x1 = todo[i].x1, y1 = todo[i].y1, x2 = todo[i].x2 < s_w ? todo[i].x2 : s_w - 1;
        int y2 = todo[i].y2 < s_h ? todo[i].y2 : s_h - 1;
        if (x1 > x2 || y1 > y2) continue;
        int w = x2 - x1 + 1, h_all = y2 - y1 + 1;
        size_t n = 0;
        if ((size_t)w * h_all <= WHOLE_PX) n = code_rect(s_msg + len, MSG_MAX - len, x1, y1, w, h_all, true);
        if (n) {
            len += n;
            rects++;
            continue;
        }
        /* Big, or too big for this message as a whole: bands whose raw size always fits an empty message. */
        int band = BAND_PX / w > 0 ? BAND_PX / w : 1;
        for (int y = y1; y <= y2;) {
            int h = y2 - y + 1 < band ? y2 - y + 1 : band;
            n = code_rect(s_msg + len, MSG_MAX - len, x1, y, w, h, false);
            if (!n) {
                dirty_add(x1, y, x2, y2);
                break;
            }
            len += n;
            rects++;
            y += h;
        }
    }
    xSemaphoreGive(s_lock);
    put16(s_msg + 2, rects);
    return len;
}

/* ---- The TCP side ------------------------------------------------------------- */

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
    char j[200];
    int n = snprintf(j, sizeof(j), "B{\"forms\":[],\"board\":{\"name\":\"%s\",\"w\":%d,\"h\":%d,\"ppi\":%d,\"r\":%d}}",
                     s_cfg.name ? s_cfg.name : "", s_w, s_h, s_cfg.ppi, s_cfg.corner_radius);
    return n > 0 && n < (int)sizeof(j) && tcp_send(fd, j, n, NULL, 0, SEND_MS);
}

/* A key the viewer holds (bit in s_held) or taps (s_tap_key until
 * s_tap_until); the board sees it down while either says so. */
static void key_set(int code, bool down) {
    if (!s_cfg.on_key || code < 1 || code > 3) return;
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
    for (int code = 1; code <= 3; code++) {
        if (s_held & 1u << code) key_set(code, false);
    }
}

/* A message from the viewer. */
static void input(const uint8_t *buf, size_t len) {
#if CONFIG_ESPMIRROR_AUDIO
    if (len == 2 && buf[0] == 7) {
        audio_set(buf[1]);
        return;
    }
    if (len == 2 && buf[0] == 8) {
        talk_set(buf[1] != 0);
        return;
    }
    if (len > 2 && buf[0] == 'S') {
        if (s_talk && s_cfg.on_talk_audio && (len - 2) % 2 == 0) {
            s_cfg.on_talk_audio((const int16_t *)(buf + 2), (len - 2) / 2, s_cfg.user);
        }
        return;
    }
#endif
    if (len == 2 && buf[0] == 3) {
        /* A tap, or the long press (4 PWR, 5 KEY3, 6 BOOT) as a plain press here. */
        int code = buf[1] <= 3 ? buf[1] : buf[1] == 4 ? 3 : buf[1] == 5 ? 1 : 2;
        tap_end();
        bool was = (s_held & 1u << code) != 0;
        s_tap_key = code;
        s_tap_until = esp_timer_get_time() + TAP_MS * 1000LL;
        if (s_cfg.on_key && !was && code >= 1 && code <= 3) s_cfg.on_key(code, true, s_cfg.user);
    } else if (len == 3 && buf[0] == 6) {
        key_set(buf[1], buf[2] != 0);
    }
}

/* The viewer's connection is over; its keys are let go and its audio ends before the socket. */
static void tcp_drop(void) {
    if (s_cfd < 0) return;
    keys_release();
#if CONFIG_ESPMIRROR_AUDIO
    audio_set(0);
    talk_set(false);
#endif
    close(s_cfd);
    s_cfd = -1;
    s_inn = 0;
    ESP_LOGI(TAG, "viewer left");
}

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
    if (s_cfd >= 0) {
        tcp_send(s_cfd, "T", 1, NULL, 0, 500);
        tcp_drop();                        /* its keys and audio end with it */
    }
    s_cfd = fd;
    s_inn = 0;
    if (!send_board(fd)) {
        tcp_drop();
        return;
    }
    s_led_sent = 0xFF;                     /* the new viewer gets the LED's state at once */
    dirty_add(0, 0, s_w - 1, s_h - 1);     /* the whole screen first */
    ESP_LOGI(TAG, "viewer connected");
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
    bool idle = s_cfd < 0 && s_pfd < 0 && !s_tap_until;
    if (select(mx + 1, &rf, NULL, NULL, idle ? NULL : &tv) < 0) {
        vTaskDelay(pdMS_TO_TICKS(ms));
        return;
    }
    int64_t now = esp_timer_get_time();
    if (s_cfd >= 0 && (FD_ISSET(s_cfd, &rf) || (s_inn && now > s_in_until))) tcp_read();
    if (s_pfd >= 0 && (FD_ISSET(s_pfd, &rf) || now > s_p_until)) tcp_hello();
    if (FD_ISSET(s_listen, &rf)) tcp_accept();
    if (s_tap_until && now > s_tap_until) tap_end();
}

static void mirror_task(void *arg) {
    (void)arg;
    int64_t last_beat = 0, last_frame = 0;
    for (;;) {
        tcp_wait(FRAME_MS);
        led_pump();
#if CONFIG_ESPMIRROR_AUDIO
        audio_pump();
#endif
        if (s_cfd < 0) continue;
        int64_t now = esp_timer_get_time() / 1000;
        portENTER_CRITICAL(&s_mux);
        bool dirty = s_ndirty > 0;
        portEXIT_CRITICAL(&s_mux);
        if (now - last_frame < FRAME_MS || (!dirty && now - last_beat < BEAT_MS)) continue;
        last_beat = last_frame = now;
        size_t len = build_msg();
        if (!tcp_send(s_cfd, s_msg, len, NULL, 0, SEND_MS)) {
            ESP_LOGW(TAG, "viewer too slow or gone, closing");
            tcp_drop();
        }
        /* A send that waited on the viewer may have outlasted a tap. */
        if (s_tap_until && esp_timer_get_time() > s_tap_until) tap_end();
    }
}

esp_err_t espmirror_start(const espmirror_config_t *config) {
    if (!config || config->width <= 0 || config->height <= 0 || config->width > 4096 || config->height > 4096) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_task) return ESP_ERR_INVALID_STATE;
    s_cfg = *config;
    s_w = config->width;
    s_h = config->height;
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
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons(PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    int one = 1;
    esp_err_t err = ESP_OK;
    const char *fail = NULL;
    if (!lock || !fb || !msg || !in || !audio_ok) {
        fail = "no memory for the screen copy";
        err = ESP_ERR_NO_MEM;
    } else if (fd < 0 || setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0
               || bind(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0 || listen(fd, 1) < 0) {
        fail = "no TCP port";
        err = ESP_FAIL;
    }
    if (!fail) {
        s_lock = lock;
        s_msg = msg;
        s_in = in;
#if CONFIG_ESPMIRROR_AUDIO
        s_aud = aud;
        s_aud_out = aud_out;
#endif
        s_listen = fd;
        BaseType_t ok = CONFIG_ESPMIRROR_TASK_CORE >= 0
            ? xTaskCreatePinnedToCore(mirror_task, "espmirror", CONFIG_ESPMIRROR_TASK_STACK, NULL,
                                      CONFIG_ESPMIRROR_TASK_PRIORITY, &s_task, CONFIG_ESPMIRROR_TASK_CORE)
            : xTaskCreate(mirror_task, "espmirror", CONFIG_ESPMIRROR_TASK_STACK, NULL,
                          CONFIG_ESPMIRROR_TASK_PRIORITY, &s_task);
        if (ok != pdPASS) {
            fail = "no task";
            err = ESP_ERR_NO_MEM;
            s_task = NULL;
            s_listen = -1;
            s_msg = NULL;
            s_in = NULL;
            s_lock = NULL;
#if CONFIG_ESPMIRROR_AUDIO
            s_aud = NULL;
            s_aud_out = NULL;
#endif
        }
    }
    if (fail) {
        ESP_LOGE(TAG, "%s (errno %d): no live screen", fail, errno);
        if (fd >= 0) close(fd);
        heap_caps_free(fb);
        heap_caps_free(msg);
        heap_caps_free(in);
#if CONFIG_ESPMIRROR_AUDIO
        heap_caps_free(aud);
        heap_caps_free(aud_out);
#endif
        if (lock) vSemaphoreDelete(lock);
        return err;
    }
    __atomic_store_n(&s_fb, fb, __ATOMIC_RELEASE);   /* the drawing side may call in from now on */
    ESP_LOGI(TAG, "%s, %d x %d, port %d", s_cfg.name ? s_cfg.name : "screen", s_w, s_h, PORT);
    return ESP_OK;
}
