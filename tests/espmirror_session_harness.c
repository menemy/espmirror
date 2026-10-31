/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stddef.h>

#define CONFIG_ESPMIRROR_WIFI 1
#define IN_MAX 64
#define SERIAL_HELLO 0xffff
#define SERIAL_BYE 0xfffe
#define SEND_MS 3000
#define KEYS 11
#define pdTRUE 1
#define pdMS_TO_TICKS(ms) (ms)
#define portMAX_DELAY 0

typedef struct { uint16_t len; uint8_t b[IN_MAX]; } serial_msg_t;
static serial_msg_t queue[32];
static int head, count, inputs, acknowledgements, boards, dirty, waited;
static bool queue_stalled;
static int s_serial_in, s_serial_done, s_lock, s_core;
static int s_cfd = -1, s_w = 10, s_h = 10, s_ptr_x, s_ptr_y;
static bool s_serial, send_ok = true, pointer_down;
static atomic_bool s_serial_open;
static unsigned held;
static const char s_board[] = "B{}";
static size_t s_board_len = sizeof(s_board) - 1;
static int xQueueReceive(int q, serial_msg_t *m, int ticks)
{
    (void)q; (void)ticks;
    if (!count) return 0;
    *m = queue[head++];
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
    queue[head + count++] = *m;
    return pdTRUE;
}
static void xSemaphoreGive(int sem) { if (sem == s_serial_done) acknowledgements++; }
static void xSemaphoreTake(int sem, int ticks) { (void)sem; (void)ticks; }
static void keys_release(void) { held = 0; }
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
static void input(const uint8_t *p, size_t n) { (void)p; (void)n; inputs++; }
static void espmirror_core_dirty(int *c, int x, int y, int w, int h)
{
    (void)c; (void)x; (void)y; (void)w; (void)h; dirty++;
}
static void espmirror_core_reset(int *c) { (void)c; }
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
    assert(s_serial && boards == 1 && dirty == 1);
    held = 6; pointer_down = true;
    s_serial_open = false;
    put(3); put(SERIAL_BYE);
    serial_pump();
    assert(!s_serial && !held && !pointer_down && !inputs && acknowledgements == 1);
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
    return 0;
}
