/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdatomic.h>
#include "esp_iris.h"
#include "espmirror.h"
#include "iris_ota_support.h"
#include "board.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define WIDTH 480
#define HEIGHT 480
static atomic_uint keys;
static atomic_bool screen_off;

static void on_key(int code, bool down, void *ctx) {
    (void)ctx;
    if (down) atomic_fetch_or(&keys, 1u << code);
    else atomic_fetch_and(&keys, ~(1u << code));
    if (code == 3 && down) atomic_store(&screen_off, !atomic_load(&screen_off));
}

void app_main(void) {
    ESP_ERROR_CHECK(esp_iris_boot_probe());
    static const espmirror_button_t buttons[] = {
        { .role = 1, .name = "FN", .x = 400, .y = 24 },
        { .role = 2, .name = "BOOT", .x = 160, .y = 456 },
        { .role = 3, .name = "POWER", .x = 320, .y = 456 },
    };
    const espmirror_config_t config = {
        .name = "ESP-Mosaico", .width = WIDTH, .height = HEIGHT,
        .diagonal = 2.16f, .touch = true, .on_key = on_key,
        .buttons = buttons, .button_count = 3,
    };
    ESP_ERROR_CHECK(espmirror_start(&config));
    ESP_ERROR_CHECK(mosaico_board_start());
    iris_ota_support_start();
    unsigned frame = 0;
    bool was_off = false;
    for (;;) {
        int px, py; bool down;
        while (espmirror_pointer_read(&px, &py, &down)) {}
        if (mosaico_board_touch(&px, &py)) down = true;
        unsigned held = atomic_load(&keys) | mosaico_board_keys();
        bool off = atomic_load(&screen_off);
        if (off != was_off) { espmirror_set_screen_state(off ? 2 : 0); was_off = off; }
        int bar = frame % (WIDTH - 40);
        uint16_t *fb = espmirror_frame_begin();
        for (int y = 0; y < HEIGHT; y++) {
            for (int x = 0; x < WIDTH; x++) {
                uint16_t color = ((x / 16) << 11) | ((y / 8) << 5) | ((x + y) / 32);
                if (x >= bar && x < bar + 40 && y >= 180 && y < 300) color = 0xffff;
                for (int key = 1; key <= 3; key++) {
                    if (y >= 360 && y < 420 && x >= key * 110 - 80 && x < key * 110)
                        color = held & (1u << key) ? 0x07e0 : 0x2104;
                }
                if (down && x >= px - 8 && x <= px + 8 && y >= py - 8 && y <= py + 8) color = 0xf800;
                fb[y * WIDTH + x] = off ? 0 : color;
            }
        }
        espmirror_frame_end(0, 0, WIDTH, HEIGHT);
        ESP_ERROR_CHECK(mosaico_board_present(!off));
        frame += 5;
        vTaskDelay(pdMS_TO_TICKS(40));
    }
}
