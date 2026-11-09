/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include <assert.h>
#include <stdatomic.h>
#include <string.h>
#include <stdio.h>
#include "espmirror_core.h"
#include "espmirror.h"
#include "espmirror_timing.h"
#include "esp_iris.h"
#include "esp_iris_service_profiles.h"
#define PTR_Q 32
#define CONFIG_ESP_IRIS_MEDIA_LATEST_BYTES 256
#define MSG_MAX 8192
#define SEND_MS 3000
#define FRAME_MS 40
#define BEAT_MS 1000
static uint8_t s_msg[MSG_MAX], s_state, submitted[256];
static espmirror_core_t s_core;
static size_t submitted_size;
static int submit_count, submit_error;
static int64_t now_us = 100000;
int64_t esp_timer_get_time(void) { return now_us; }
esp_err_t esp_iris_media_submit(int channel, const esp_iris_media_desc_t *desc,
                               uint32_t frame, uint16_t flags, const void *data, size_t size) {
    assert(channel == 4 && desc->format == 0x4553 && desc->quality == 1 && frame && flags <= 3);
    assert(size <= sizeof(submitted));
    memcpy(submitted, data, size); submitted_size = size; submit_count++;
    return submit_error;
}
static espmirror_config_t s_cfg;
static int s_w = 3, s_h = 2, s_ptr_x, s_ptr_y;
static bool s_ready, streaming, image_streaming, cable, framebuffer_busy;
static uint16_t pixels[] = { 0x1234, 0xabcd, 0, 0xffff, 0x55aa, 0x0102 };
static uint16_t *s_fb = pixels;
static SemaphoreHandle_t s_lock;
static char s_board[] = "B{\"w\":3,\"h\":2}";
static size_t s_board_len = sizeof(s_board) - 1;
static int locks, inputs, releases, pointer_releases, fail_registration, registrations;
static uint8_t last_input[5];
static size_t last_length;
static esp_iris_screen_backend_t screen;
static esp_iris_rpc_handler_t pointer_handler, key_handler, board_handler, stream_handler, timing_handler;
static bool viewer_on(void) { return cable; }
bool esp_iris_media_is_streaming(int channel) { assert(channel == 3 || channel == 4); return channel == 3 ? streaming : image_streaming; }
int xSemaphoreTake(SemaphoreHandle_t lock, int ticks) {
    (void)lock; assert(ticks == 0);
    if (framebuffer_busy) return pdFALSE;
    locks++; return pdTRUE;
}
int xSemaphoreGive(SemaphoreHandle_t lock) { (void)lock; locks--; return pdTRUE; }
static void input(const uint8_t *data, size_t length) {
    inputs++; last_length = length; memcpy(last_input, data, length);
}
static void keys_release(void) { releases++; }
static void pointer_put(int x, int y, bool down) { (void)x; (void)y; assert(!down); pointer_releases++; }
static void refresh(void) {
    espmirror_core_reset(&s_core); espmirror_core_dirty(&s_core, 0, 0, s_w - 1, s_h - 1);
}
static void on_key(int code, bool down, void *ctx) { (void)code; (void)down; (void)ctx; }
esp_err_t esp_iris_screen_register(const esp_iris_screen_backend_t *backend) {
    if (++registrations == fail_registration) return ESP_FAIL;
    screen = *backend; return ESP_OK;
}
esp_err_t esp_iris_screen_unregister(void *ctx) {
    assert(screen.user_ctx == ctx); memset(&screen, 0, sizeof(screen)); return ESP_OK;
}
esp_err_t esp_iris_rpc_register(uint16_t service, uint16_t method, esp_iris_rpc_handler_t handler, void *ctx) {
    assert((method >= 1 && method <= 4) && ctx == &s_cfg);
    if (++registrations == fail_registration) return ESP_FAIL;
    if (service == 0x1001) pointer_handler = handler;
    else { assert(service == 0x4553); if (method == 1) key_handler = handler; else if (method == 2) board_handler = handler; else if (method == 3) stream_handler = handler; else timing_handler = handler; }
    return ESP_OK;
}
esp_err_t esp_iris_rpc_unregister(uint16_t service, uint16_t method) {
    assert(method >= 1 && method <= 4);
    if (service == 0x1001) pointer_handler = NULL;
    else if (method == 1) key_handler = NULL;
    else if (method == 2) board_handler = NULL;
    else if (method == 3) stream_handler = NULL;
    else timing_handler = NULL;
    return ESP_OK;
}
#include "iris_impl.inc"
static iris_input_t queue[PTR_Q];
static int count;
int xQueueSend(QueueHandle_t q, const void *event, int ticks) {
    assert(q == s_iris_in && ticks == 0);
    if (count == PTR_Q) return 0;
    queue[count++] = *(const iris_input_t *)event; return pdTRUE;
}
int xQueueReceive(QueueHandle_t q, void *event, int ticks) {
    assert(q == s_iris_in && ticks == 0);
    if (!count) return 0;
    *(iris_input_t *)event = queue[0];
    memmove(queue, queue + 1, --count * sizeof(*queue)); return pdTRUE;
}
int main(void) {
    s_iris_in = queue;
    for (int i = 1; i <= 6; i++) {
        registrations = 0; fail_registration = i;
        assert(iris_register() == ESP_FAIL);
        iris_unregister();
        assert(!screen.begin && !pointer_handler && !key_handler && !board_handler && !stream_handler && !timing_handler);
    }
    fail_registration = registrations = 0;
    assert(iris_register() == ESP_OK);
    esp_iris_media_desc_t requested = {0}, actual;
    uint32_t total;
    assert(screen.begin(&requested, &actual, &total, NULL) == ESP_ERR_INVALID_STATE);
    s_ready = true;
    uint8_t timing_output[ESPMIRROR_TIMING_BYTES]; size_t timing_size = 99;
    esp_iris_rpc_request_t timing_request = {0};
    assert(timing_handler(&timing_request, timing_output, sizeof(timing_output) - 1, &timing_size, NULL) == ESP_ERR_INVALID_ARG);
    assert(timing_size == 0);
    assert(timing_handler(&timing_request, timing_output, sizeof(timing_output), &timing_size, NULL) == ESP_OK);
    assert(timing_size == sizeof(timing_output) && memcmp(timing_output, "EMT1", 4) == 0);
    timing_request.payload_size = 1;
    assert(timing_handler(&timing_request, timing_output, sizeof(timing_output), &timing_size, NULL) == ESP_ERR_INVALID_ARG);
    assert(timing_size == 0);
    assert(screen.begin(&requested, &actual, &total, NULL) == ESP_OK);
    assert(actual.width == 3 && actual.height == 2 && actual.stride == 6 && total == 12 && actual.format == 1);
    requested.format = 3;
    assert(screen.begin(&requested, &actual, &total, NULL) == ESP_ERR_NOT_SUPPORTED);
    uint8_t output[128] = {0}; size_t size;
    esp_iris_rpc_request_t board_request = {0};
    assert(board_handler(&board_request, output, sizeof(output), &size, NULL) == ESP_OK);
    assert(strstr((char *)output, "\"iris_codec\":1") && strstr((char *)output, "\"iris_chunk\":256"));
    board_request.payload_size = 1;
    assert(board_handler(&board_request, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_ARG);
    assert(screen.read(1, output, 7, &size, NULL) == ESP_OK && size == 7 && !locks);
    assert(memcmp(output, (uint8_t *)pixels + 1, 7) == 0);
    pixels[4] = 0x9876;
    assert(screen.read(8, output, sizeof(output), &size, NULL) == ESP_OK && size == 4);
    assert(memcmp(output, (uint8_t *)pixels + 8, 4) == 0);
    assert(screen.read(12, NULL, 0, &size, NULL) == ESP_OK && size == 0);
    assert(screen.read(13, output, 1, &size, NULL) == ESP_ERR_INVALID_ARG && size == 0);
    framebuffer_busy = true;
    assert(screen.read(0, output, 6, &size, NULL) == ESP_ERR_TIMEOUT && size == 0 && !locks);
    framebuffer_busy = false;
    assert(screen.read(0, output, 6, &size, NULL) == ESP_OK && size == 6 && !locks);
    s_cfg.touch = true; s_cfg.on_key = on_key;
    uint8_t pointer[] = {0, 0, 2, 0, 1, 0, 0, 0, 0x78, 0x56, 0x34, 0x12};
    esp_iris_rpc_request_t req = { .payload = pointer, .payload_size = sizeof(pointer) };
    assert(pointer_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_STATE && !count);
    streaming = true;
    assert(pointer_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK && size == 12);
    assert(memcmp(pointer, output, size) == 0 && !inputs);
    iris_pump();
    assert(inputs == 1 && last_length == 5 && last_input[0] == 1 && last_input[1] == 2 && last_input[3] == 1);
    pointer[0] = 2;
    assert(pointer_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK);
    iris_pump(); assert(last_input[0] == 0);
    pointer[1] = 1;
    assert(pointer_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_ARG);
    pointer[1] = 0; pointer[2] = 0xff; pointer[3] = 0xff;
    assert(pointer_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_ARG);
    uint8_t key[] = {3, 1}; req.payload = key; req.payload_size = 2;
    assert(key_handler(&req, output, 1, &size, NULL) == ESP_ERR_INVALID_ARG);
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK && size == 2);
    iris_pump(); assert(last_length == 3 && last_input[0] == 6 && last_input[1] == 3 && last_input[2] == 1);
    key[0] = 11;
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_ARG);
    key[0] = 1; key[1] = 2;
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_ARG);
    key[1] = 0;
    for (int i = 0; i < PTR_Q; i++) assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK);
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_NO_MEM);
    int before = inputs;
    screen.end(NULL); iris_pump();
    assert(inputs == before && releases == 1 && pointer_releases == 1);
    while (count) iris_pump();
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK);
    iris_takeover(); cable = true;
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_STATE);
    cable = false; iris_pump(); assert(inputs == before);
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK);
    iris_pump(); streaming = false; iris_pump();
    assert(releases == 2 && pointer_releases == 2);
    /* Real core output survives fragmentation and cannot advance without an exact ACK. */
    uint16_t large[64 * 48], previous[64 * 48], reference[64 * 48];
    for (size_t i = 0; i < 64 * 48; i++) large[i] = (uint16_t)(i * 7919);
    s_w = 64; s_h = 48; s_fb = large;
    assert(espmirror_core_init_ex(&s_core, s_w, s_h, ESPMIRROR_DELTA | ESPMIRROR_QOI, previous));
    espmirror_core_t expected;
    assert(espmirror_core_init_ex(&expected, s_w, s_h, ESPMIRROR_DELTA | ESPMIRROR_QOI, reference));
    espmirror_core_dirty(&expected, 0, 0, 63, 47);
    uint8_t expected_msg[MSG_MAX], assembled[MSG_MAX];
    size_t expected_size = espmirror_core_build(&expected, large, expected_msg, MSG_MAX - 20, 0);
    assert(expected_size > 256);
    uint8_t control[13] = {0}; iris_put32(control + 1, 123);
    req.payload = control; req.payload_size = 13;
    assert(stream_handler(&req, output, sizeof(output), &size, NULL) == ESP_ERR_INVALID_STATE);
    image_streaming = true;
    assert(stream_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK);
    iris_pump();
    size_t offset = 0;
    while (offset < expected_size) {
        int calls = submit_count;
        iris_stream_pump();
        assert(submit_count == calls + 1 && s_iris_ack);
        assert(memcmp(submitted, "ESM1", 4) == 0 && iris_u32(submitted + 4) == 123);
        assert(iris_u32(submitted + 8) == 1 && iris_u32(submitted + 12) == offset);
        assert(iris_u32(submitted + 16) == expected_size);
        memcpy(assembled + offset, submitted + 20, submitted_size - 20);
        iris_stream_pump(); assert(submit_count == calls + 1);
        offset += submitted_size - 20;
        control[0] = 1; iris_put32(control + 5, 1); iris_put32(control + 9, offset + 1);
        iris_stream_command(control); assert(s_iris_ack);  /* Wrong offset. */
        iris_put32(control + 9, offset); iris_put32(control + 1, 122);
        iris_stream_command(control); assert(s_iris_ack);  /* Old epoch. */
        iris_put32(control + 1, 123);
        iris_stream_command(control); assert(!s_iris_ack);
    }
    assert(memcmp(assembled, expected_msg, expected_size) == 0 && s_iris_message == 2);
    /* ACK replay, timeout, reset and cable takeover cannot reuse a lost reference. */
    iris_stream_command(control); assert(s_iris_message == 2);
    now_us += 1100000; iris_stream_pump(); assert(s_iris_ack);
    now_us += 3100000; iris_stream_pump(); assert(!s_iris_epoch);
    control[0] = 0; iris_put32(control + 1, 124); memset(control + 5, 0, 8);
    iris_stream_command(control); assert(s_iris_epoch == 124 && s_iris_message == 1);
    assert(espmirror_core_pending(&s_core));
    for (size_t i = 0; i < 64 * 48; i++) assert(previous[i] == 0);
    submit_error = ESP_FAIL; iris_stream_pump(); assert(!s_iris_epoch);
    submit_error = ESP_OK; iris_stream_command(control); iris_takeover(); assert(!s_iris_epoch);
    iris_stream_command(control); image_streaming = false; iris_stream_pump(); assert(!s_iris_epoch);
    /* IMAGE end releases its input even if a raw SCREEN observer remains. */
    streaming = image_streaming = true; iris_pump();
    key[0] = 1; key[1] = 1; req.payload = key; req.payload_size = 2;
    assert(key_handler(&req, output, sizeof(output), &size, NULL) == ESP_OK);
    iris_pump(); int released_before = releases;
    image_streaming = false; iris_pump();
    assert(releases == released_before + 1 && !s_iris_epoch);
    iris_unregister(); assert(!screen.begin && !pointer_handler && !key_handler);
    return 0;
}
