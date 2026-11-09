/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include <assert.h>
#include <stdatomic.h>
#include <string.h>
#include "espmirror.h"
#include "esp_iris.h"
#include "esp_iris_service_profiles.h"
#define PTR_Q 32
static espmirror_config_t s_cfg;
static int s_w = 3, s_h = 2, s_ptr_x, s_ptr_y;
static bool s_ready, streaming, cable, framebuffer_busy;
static uint16_t pixels[] = { 0x1234, 0xabcd, 0, 0xffff, 0x55aa, 0x0102 };
static uint16_t *s_fb = pixels;
static SemaphoreHandle_t s_lock;
static char s_board[] = "B{\"w\":3,\"h\":2}";
static size_t s_board_len = sizeof(s_board) - 1;
static int locks, inputs, releases, pointer_releases, fail_registration, registrations;
static uint8_t last_input[5];
static size_t last_length;
static esp_iris_screen_backend_t screen;
static esp_iris_rpc_handler_t pointer_handler, key_handler, board_handler;
static bool viewer_on(void) { return cable; }
bool esp_iris_media_is_streaming(int channel) { assert(channel == 3); return streaming; }
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
static void on_key(int code, bool down, void *ctx) { (void)code; (void)down; (void)ctx; }
esp_err_t esp_iris_screen_register(const esp_iris_screen_backend_t *backend) {
    if (++registrations == fail_registration) return ESP_FAIL;
    screen = *backend; return ESP_OK;
}
esp_err_t esp_iris_screen_unregister(void *ctx) {
    assert(screen.user_ctx == ctx); memset(&screen, 0, sizeof(screen)); return ESP_OK;
}
esp_err_t esp_iris_rpc_register(uint16_t service, uint16_t method, esp_iris_rpc_handler_t handler, void *ctx) {
    assert((method == 1 || method == 2) && ctx == &s_cfg);
    if (++registrations == fail_registration) return ESP_FAIL;
    if (service == 0x1001) pointer_handler = handler;
    else { assert(service == 0x4553); if (method == 1) key_handler = handler; else board_handler = handler; }
    return ESP_OK;
}
esp_err_t esp_iris_rpc_unregister(uint16_t service, uint16_t method) {
    assert(method == 1 || method == 2);
    if (service == 0x1001) pointer_handler = NULL;
    else if (method == 1) key_handler = NULL;
    else board_handler = NULL;
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
    for (int i = 1; i <= 4; i++) {
        registrations = 0; fail_registration = i;
        assert(iris_register() == ESP_FAIL);
        iris_unregister();
        assert(!screen.begin && !pointer_handler && !key_handler && !board_handler);
    }
    fail_registration = registrations = 0;
    assert(iris_register() == ESP_OK);
    esp_iris_media_desc_t requested = {0}, actual;
    uint32_t total;
    assert(screen.begin(&requested, &actual, &total, NULL) == ESP_ERR_INVALID_STATE);
    s_ready = true;
    assert(screen.begin(&requested, &actual, &total, NULL) == ESP_OK);
    assert(actual.width == 3 && actual.height == 2 && actual.stride == 6 && total == 12 && actual.format == 1);
    requested.format = 3;
    assert(screen.begin(&requested, &actual, &total, NULL) == ESP_ERR_NOT_SUPPORTED);
    uint8_t output[16] = {0}; size_t size;
    esp_iris_rpc_request_t board_request = {0};
    assert(board_handler(&board_request, output, sizeof(output), &size, NULL) == ESP_OK);
    assert(size == s_board_len - 1 && memcmp(output, s_board + 1, size) == 0);
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
    iris_unregister(); assert(!screen.begin && !pointer_handler && !key_handler);
    return 0;
}
