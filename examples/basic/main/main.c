/*
 * espmirror basic example: no display at all. The board draws into the
 * mirror's screen copy and your desktop shows it, 240 x 240 at a few frames
 * a second: a clock, a bouncing square, and the keys you press in the viewer.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "espmirror.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "protocol_examples_common.h"
#include "sdkconfig.h"

static const char *TAG = "basic";

#define W CONFIG_EXAMPLE_MIRROR_WIDTH
#define H CONFIG_EXAMPLE_MIRROR_HEIGHT

/* RGB565 from 8-bit channels, in the byte order the mirror expects (native). */
static inline uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
    return (uint16_t)((r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3);
}

static void fill(uint16_t *fb, int x, int y, int w, int h, uint16_t c) {
    for (int j = y; j < y + h; j++) {
        if (j < 0 || j >= H) continue;
        for (int i = x; i < x + w; i++) {
            if (i >= 0 && i < W) fb[j * W + i] = c;
        }
    }
}

/* A 3 x 5 digit font, enough for a counter. */
static const uint16_t DIGITS[10] = {
    0x7B6F, 0x2492, 0x73E7, 0x73CF, 0x5BC9, 0x79CF, 0x79EF, 0x7249, 0x7BEF, 0x7BCF,
};

static void digit(uint16_t *fb, int x, int y, int d, int px, uint16_t c) {
    for (int row = 0; row < 5; row++) {
        for (int col = 0; col < 3; col++) {
            if (DIGITS[d] >> (14 - (row * 3 + col)) & 1) fill(fb, x + col * px, y + row * px, px, px, c);
        }
    }
}

static volatile int s_key_down[4];
static volatile int s_touch_x = -1, s_touch_y = -1;

static void on_key(int key, bool down, void *user) {
    (void)user;
    ESP_LOGI(TAG, "key %d %s", key, down ? "down" : "up");
    if (key >= 1 && key <= 3) s_key_down[key] = down;
}

static void on_pointer(int x, int y, bool down, void *user) {
    (void)user;
    s_touch_x = down ? x : -1;
    s_touch_y = down ? y : -1;
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());         /* another image's NVS, or an old layout */
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(example_connect());   /* Wi-Fi from menuconfig */

    const espmirror_config_t cfg = {
        .name = CONFIG_EXAMPLE_MIRROR_NAME,
        .width = W,
        .height = H,
        .ppi = CONFIG_EXAMPLE_MIRROR_PPI,
        .corner_radius = 0,
        .on_key = on_key,
        .on_pointer = on_pointer,
    };
    ESP_ERROR_CHECK(espmirror_start(&cfg));
    ESP_LOGI(TAG, "run tools/espmirror.py <this board's IP> on your computer");

    int x = 20, y = 60, dx = 3, dy = 2;
    unsigned frame = 0;
    for (;;) {
        uint16_t *fb = espmirror_frame_begin();
        if (fb) {
            /* The background, the square, the counter, the keys. */
            fill(fb, 0, 0, W, H, rgb(24, 26, 32));
            fill(fb, 0, 0, W, H * 3 / 20, rgb(40, 120, 90));
            fill(fb, x, y, 40, 40, rgb(230, 90, 60));
            unsigned n = frame;
            for (int i = 0; i < 6; i++) {
                digit(fb, W - 40 - i * 24, H - 50, n % 10, 5, rgb(240, 240, 240));
                n /= 10;
            }
            if (s_touch_x >= 0) fill(fb, s_touch_x - 8, s_touch_y - 8, 16, 16, rgb(250, 220, 80));
            for (int k = 1; k <= 3; k++) {
                int side = W * 5 / 24;
                fill(fb, W / 12 + (k - 1) * W * 7 / 24, H * 11 / 24, side, side,
                     s_key_down[k] ? rgb(80, 200, 120) : rgb(60, 64, 76));
            }
            espmirror_frame_end(0, 0, W, H);
        }
        x += dx;
        y += dy;
        if (x < 0 || x + 40 > W) dx = -dx;
        if (y < H * 3 / 20 || y + 40 > H * 11 / 24) dy = -dy;
        frame++;
        espmirror_set_led(frame / 25 % 2 ? ESPMIRROR_LED_ON : ESPMIRROR_LED_OFF);
        vTaskDelay(pdMS_TO_TICKS(40) ? pdMS_TO_TICKS(40) : 1);
    }
}
