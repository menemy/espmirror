/*
 * espmirror LVGL example: an LVGL display with no panel behind it. The mirror
 * takes every flush, so the user interface runs on a bare devkit and shows up
 * on your desktop; the viewer's keys move the slider.
 *
 * With a real panel, keep your display as it is and call
 * espmirror_attach_lvgl() once after espmirror_start().
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "espmirror.h"
#include "espmirror_lvgl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lvgl.h"
#include "nvs_flash.h"
#include "protocol_examples_common.h"

static const char *TAG = "lvgl";

#define W 240
#define H 240

static SemaphoreHandle_t s_lv_lock;
static lv_obj_t *s_slider, *s_label;
static volatile int s_step;

/* No panel: the flush is done as soon as it starts (the mirror has copied it). */
static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px) {
    (void)area;
    (void)px;
    lv_display_flush_ready(disp);
}

static void on_key(int key, bool down, void *user) {
    (void)user;
    if (!down) return;
    s_step = key == ESPMIRROR_KEY_AUX ? -10 : key == ESPMIRROR_KEY_MAIN ? 10 : 0;
}

static void lv_task(void *arg) {
    (void)arg;
    for (;;) {
        xSemaphoreTake(s_lv_lock, portMAX_DELAY);
        int step = s_step;
        s_step = 0;
        if (step) {
            int v = lv_slider_get_value(s_slider) + step;
            lv_slider_set_value(s_slider, v < 0 ? 0 : v > 100 ? 100 : v, LV_ANIM_ON);
        }
        lv_label_set_text_fmt(s_label, "%d %%  ·  %lu s", (int)lv_slider_get_value(s_slider),
                              (unsigned long)(esp_timer_get_time() / 1000000));
        uint32_t wait = lv_timer_handler();
        xSemaphoreGive(s_lv_lock);
        vTaskDelay(pdMS_TO_TICKS(wait < 5 ? 5 : wait > 50 ? 50 : wait));
    }
}

void app_main(void) {
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(example_connect());

    const espmirror_config_t cfg = {
        .name = "espmirror lvgl",
        .width = W,
        .height = H,
        .ppi = 200,
        .corner_radius = 0,
        .on_key = on_key,
    };
    ESP_ERROR_CHECK(espmirror_start(&cfg));

    lv_init();
    lv_tick_set_cb((lv_tick_get_cb_t)(void *)esp_timer_get_time);   /* microseconds: LVGL asks for ms */
    static uint8_t buf[W * 40 * 2];
    lv_display_t *disp = lv_display_create(W, H);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(disp, buf, NULL, sizeof(buf), LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(disp, flush_cb);
    ESP_ERROR_CHECK(espmirror_attach_lvgl(disp));

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x181a20), 0);
    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "LVGL with no panel");
    lv_obj_set_style_text_color(title, lv_color_white(), 0);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 24);
    s_slider = lv_slider_create(scr);
    lv_obj_set_width(s_slider, 180);
    lv_obj_align(s_slider, LV_ALIGN_CENTER, 0, 0);
    lv_slider_set_value(s_slider, 40, LV_ANIM_OFF);
    s_label = lv_label_create(scr);
    lv_obj_set_style_text_color(s_label, lv_color_hex(0xc8c8c8), 0);
    lv_obj_set_style_text_font(s_label, &lv_font_montserrat_14, 0);
    lv_obj_align(s_label, LV_ALIGN_BOTTOM_MID, 0, -24);
    lv_obj_t *hint = lv_label_create(scr);
    lv_label_set_text(hint, "keys 1 and 2 move the slider");
    lv_obj_set_style_text_color(hint, lv_color_hex(0x8a8a8a), 0);
    lv_obj_set_style_text_font(hint, &lv_font_montserrat_14, 0);
    lv_obj_align(hint, LV_ALIGN_CENTER, 0, 40);

    s_lv_lock = xSemaphoreCreateMutex();
    xTaskCreate(lv_task, "lvgl", 6144, NULL, 4, NULL);
    ESP_LOGI(TAG, "run tools/mirror_window.py <this board's IP> on your computer");
}
