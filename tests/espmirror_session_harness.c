/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stddef.h>
#include <string.h>
#include <stdarg.h>

#define CONFIG_ESPMIRROR_TCP 1
#define IN_MAX 1024
#define IN_MS 2000
#define SERIAL_QUIET_MS 6000
#define SERIAL_HELLO 0xffff
#define SERIAL_BYE 0xfffe
#define SEND_MS 3000
#define KEYS 11
#define pdTRUE 1
#define pdMS_TO_TICKS(ms) (ms)
#define portMAX_DELAY 0

#include "session_types.inc"
static serial_msg_t queue[32];
static int head, count, inputs, acknowledgements, boards, dirty, waited;
static bool queue_stalled;
static int s_serial_in, s_serial_done, s_lock, s_core;
static int s_cfd = -1, s_w = 10, s_h = 10, s_ptr_x, s_ptr_y;
static bool s_serial, send_ok = true, pointer_down;
static atomic_bool s_serial_open;
static atomic_bool s_session_busy;
static bool s_ready = true, pump_on_send;
static int s_serial_tx;
static const uint8_t s_mark[] = "MIRROR1\n";
static void serial_pump(void);
static unsigned held;
static const char s_board[] = "B{}";
static size_t s_board_len = sizeof(s_board) - 1;
static int xQueueReceive(int q, serial_msg_t *m, int ticks)
{
    (void)q; (void)ticks;
    if (!count) return 0;
    memset(m, 0xcc, sizeof(*m));
    memcpy(m, &queue[head++], SERIAL_ITEM_SIZE);
    count--;
    return pdTRUE;
}
static int xQueueSend(int q, const serial_msg_t *m, int ticks)
{
    (void)q;
    waited = ticks;
    if (queue_stalled && !ticks) return 0;
    queue_stalled = false;
    if (!count) head = 0;
    memcpy(&queue[head + count++], m, SERIAL_ITEM_SIZE);
    if (pump_on_send) serial_pump();
    return pdTRUE;
}
static void xSemaphoreGive(int sem) { if (sem == s_serial_done) acknowledgements++; }
static void xSemaphoreTake(int sem, int ticks) { (void)sem; (void)ticks; }
static void keys_release(void) { held = 0; }
#if CONFIG_ESPMIRROR_AUDIO
static void audio_set(int flags) { (void)flags; }
static void talk_set(bool enabled) { (void)enabled; }
#endif
static int text_resets;
static void text_reset(void) { text_resets++; }
static void pointer_put(int x, int y, bool down) { (void)x; (void)y; pointer_down = down; }
static bool viewer_send(const void *p, size_t n, int ms)
{
    (void)n; (void)ms;
    if (*(const char *)p == 'B') boards++;
    return send_ok;
}
static int s_led_sent;
static struct { void (*on_viewer)(bool, void *); void *user; } s_cfg;
static bool tcp_send(int fd, const void *a, size_t n, const void *b, size_t nb, int ms) {
    (void)fd; (void)a; (void)n; (void)b; (void)nb; (void)ms; return true;
}
static void tcp_drop(void) { s_cfd = -1; }
static size_t last_input;
static void input(const uint8_t *p, size_t n)
{
    assert(n <= SERIAL_INPUT_MAX);
    if (pump_on_send) {
        assert(n && p[0] == 'R');
        for (size_t i = 1; i < n; i++) assert(p[i] == (uint8_t)i);
    }
    last_input = n;
    inputs++;
}
static void espmirror_core_dirty(int *c, int x, int y, int w, int h)
{
    (void)c; (void)x; (void)y; (void)w; (void)h; dirty++;
}
static void espmirror_core_reset(int *c) { (void)c; }
typedef int esp_err_t;
typedef int (*vprintf_like_t)(const char *, va_list);
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED 1
#define ESP_ERR_INVALID_STATE 2
static int serial_log(const char *fmt, va_list ap) { (void)fmt; (void)ap; return 0; }
static vprintf_like_t esp_log_set_vprintf(vprintf_like_t f) { (void)f; return serial_log; }
static void vTaskDelay(int ticks) { (void)ticks; }
static bool espmirror_usb_supported(void) { return true; }
static int64_t now;
static int64_t esp_timer_get_time(void) { return now; }
static bool serial_write(const void *p, size_t n, int64_t until)
{
    (void)p; (void)n; (void)until; return true;
}
static uint8_t wire[IN_MAX + 9];
static size_t wire_size, wire_at, payload_size;
static bool payload_timeout;
static int payload_reads;
static bool espmirror_usb_read(void *p, size_t n, int ms)
{
    if (wire_at == 4) {
        assert(n == payload_size && ms == IN_MS);
        payload_reads++;
        if (payload_timeout) {
            now += ms * 1000LL;
            wire_at += n / 2;
            return false;
        }
    }
    if (wire_size - wire_at < n) {
        now += ms * 1000LL;
        return false;
    }
    memcpy(p, wire + wire_at, n);
    wire_at += n;
    return true;
}
#include "session_impl.inc"
static void put(int kind)
{
    if (!count) head = 0;
    queue[head + count++].len = kind;
}
int main(void)
{
    s_serial_in = 1; s_serial_done = 2; s_lock = 3;
    s_serial_open = true;
    put(SERIAL_HELLO);
    serial_pump();
    assert(s_serial && boards == 1 && dirty == 1 && text_resets == 1);
    held = 6; pointer_down = true;
    s_serial_open = false;
    put(3); put(SERIAL_BYE);
    serial_pump();
    assert(!s_serial && !held && !pointer_down && !inputs && acknowledgements == 1 && text_resets == 2);
    put(SERIAL_HELLO); put(SERIAL_BYE);
    serial_pump();
    assert(boards == 1 && acknowledgements == 2);
    s_serial_open = true; send_ok = false;
    put(SERIAL_HELLO); put(SERIAL_BYE);
    serial_pump();
    assert(!s_serial && !s_serial_open && acknowledgements == 3);
    s_serial = s_serial_open = true;
    for (int i = 0; i < 16; i++) put(3);
    serial_pump();
    assert(inputs == 8 && count == 8);
    serial_pump();
    assert(inputs == 16 && !count);
    bool down = false;
    serial_msg_t m = { .len = 5, .b = { 1, 10, 0, 20, 0 } };
    assert(serial_enqueue(&m, &down) && down && count == 1);
    queue_stalled = true;
    m.b[1] = 122;
    assert(serial_enqueue(&m, &down) && down && count == 1 && waited == 0);
    m.b[0] = 0;
    assert(serial_enqueue(&m, &down) && !down && count == 2 && waited == SEND_MS);
    assert(queue[1].b[1] == 122 && queue[1].b[0] == 0);
    queue_stalled = true;
    m.len = 3; m.b[0] = 6; m.b[1] = 1; m.b[2] = 0;
    assert(serial_enqueue(&m, &down) && count == 3 && waited == SEND_MS);
    count = head = 0;
    send_ok = true;
    pump_on_send = true;
    for (size_t n = 1; n <= IN_MAX; n++) {
        memset(wire, 0, sizeof(wire));
        wire[0] = n; wire[1] = n >> 8;
        wire[4] = 'R';
        for (size_t i = 1; i < n; i++) wire[4 + i] = i;
        wire[4 + n] = 1;
        wire[8 + n] = 'R';
        wire_size = n + 9; wire_at = 0; payload_size = n;
        payload_reads = 0;
        int before = inputs;
        assert(espmirror_serial_session() == ESP_OK);
        assert(payload_reads == 1 && wire_at == wire_size && !count);
        assert(inputs == before + 1 + (n <= SERIAL_INPUT_MAX));
        assert(last_input == 1 && !s_session_busy && !s_serial_open && !s_serial);
    }
    wire_at = 0; payload_timeout = true; payload_reads = 0;
    int before = inputs;
    assert(espmirror_serial_session() == ESP_OK);
    assert(payload_reads == 1 && inputs == before && !count && !s_session_busy);
    assert(wire_at == wire_size);
    return 0;
}
