/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include <assert.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#if CONFIG_VIRTUAL_WIFI
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#endif
#include "espmirror.h"
#include "espmirror_lvgl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lvgl.h"

#if !CONFIG_IDF_TARGET_ESP32S3
#error "The virtual example uses the ESP32-S3 native USB port"
#endif

#define W CONFIG_VIRTUAL_WIDTH
#define H CONFIG_VIRTUAL_HEIGHT

static QueueHandle_t s_keys;
static lv_obj_t *s_pages, *s_tiles[3], *s_count, *s_typed;
static unsigned s_presses;
static uint8_t s_draw[W * 24 * 2] __attribute__((aligned(LV_DRAW_BUF_ALIGN)));

static uint32_t tick_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void on_key(int key, bool down, void *user) {
    (void)user;
    /* The mirror task never calls LVGL. Only presses need to cross tasks. */
    if (down) xQueueSend(s_keys, &key, 0);
}

static void boot_poll(uint32_t now) {
#if CONFIG_VIRTUAL_BOOT_GPIO >= 0
    static bool raw, stable;
    static uint32_t since;
    bool down = gpio_get_level(CONFIG_VIRTUAL_BOOT_GPIO) == 0;
    if (down != raw) { raw = down; since = now; }
    if (stable != raw && (uint32_t)(now - since) >= 30) {
        stable = raw;
        on_key(ESPMIRROR_KEY_MAIN, stable, NULL);
    }
#else
    (void)now;
#endif
}

static void flush(lv_display_t *display, const lv_area_t *area, uint8_t *pixels) {
    (void)area;
    (void)pixels;
    /* The mirror's FLUSH_START hook has already copied these pixels. */
    lv_display_flush_ready(display);
}

static void count_press(void) {
    lv_label_set_text_fmt(s_count, "BOOT: %u", ++s_presses);
    espmirror_set_led(s_presses % 2 ? ESPMIRROR_LED_ON : ESPMIRROR_LED_OFF);
}

/* POWER: the screen goes dark in the viewer and comes back. */
static void power_press(void) {
    static bool dark;
    dark = !dark;
    espmirror_set_screen_state(dark ? ESPMIRROR_SCREEN_DARK : ESPMIRROR_SCREEN_BRIGHT);
}

/* FN: the next page, round to the first. */
static void next_page(void) {
    lv_obj_t *active = lv_tileview_get_tile_active(s_pages);
    unsigned i = 0;
    while (i < 3 && s_tiles[i] != active) i++;
    lv_tileview_set_tile_by_index(s_pages, (i + 1) % 3, 0, LV_ANIM_ON);
}

static void key_press(int key) {
    if (key == ESPMIRROR_KEY_MAIN) count_press();
    else if (key == ESPMIRROR_KEY_POWER) power_press();
    else if (key == ESPMIRROR_KEY_AUX) next_page();
}

static void clicked(lv_event_t *event) {
    (void)event;
    count_press();
}

/* Enter, or the keyboard's OK: the line moves under the field. */
static void typed(lv_event_t *event) {
    lv_obj_t *field = lv_event_get_target(event);
    lv_label_set_text(s_typed, lv_textarea_get_text(field));
    lv_textarea_set_text(field, "");
}

static void bar_value(void *bar, int32_t value) {
    lv_bar_set_value(bar, value, LV_ANIM_OFF);
}

static void build_ui(void) {
    lv_obj_t *screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x14161c), 0);
    lv_obj_set_style_text_color(screen, lv_color_white(), 0);
    lv_obj_set_style_text_font(screen, &lv_font_montserrat_14, 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    s_pages = lv_tileview_create(screen);
    lv_obj_set_size(s_pages, W, H);
    lv_obj_set_style_bg_opa(s_pages, LV_OPA_TRANSP, 0);
    lv_obj_t *home = s_tiles[0] = lv_tileview_add_tile(s_pages, 0, 0, LV_DIR_RIGHT);
    lv_obj_t *page = s_tiles[1] = lv_tileview_add_tile(s_pages, 1, 0, LV_DIR_HOR);
    lv_obj_t *typing = s_tiles[2] = lv_tileview_add_tile(s_pages, 2, 0, LV_DIR_LEFT);
    lv_obj_remove_flag(home, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(page, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(typing, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = lv_label_create(home);
    lv_label_set_text(title, "espmirror");
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, H / 8);
    s_count = lv_label_create(home);
    lv_label_set_text(s_count, "BOOT: 0");
    lv_obj_align(s_count, LV_ALIGN_CENTER, 0, -H / 8);

    lv_obj_t *button = lv_button_create(home);
    lv_obj_set_size(button, W / 2, H / 6);
    lv_obj_align(button, LV_ALIGN_CENTER, 0, H / 12);
    lv_obj_add_event_cb(button, clicked, LV_EVENT_CLICKED, NULL);
    lv_obj_t *label = lv_label_create(button);
    lv_label_set_text(label, "Tap");
    lv_obj_center(label);

    lv_obj_t *bar = lv_bar_create(home);
    lv_obj_set_size(bar, W / 2, 8);
    lv_obj_align(bar, LV_ALIGN_CENTER, 0, H / 3);
    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, bar);
    lv_anim_set_exec_cb(&animation, bar_value);
    lv_anim_set_values(&animation, 0, 100);
    lv_anim_set_duration(&animation, 1200);
    lv_anim_set_playback_duration(&animation, 1200);
    lv_anim_set_repeat_count(&animation, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&animation);

    lv_obj_t *list = lv_list_create(page);
    lv_obj_set_size(list, W * 2 / 3, H * 2 / 3);
    lv_obj_center(list);
    for (unsigned i = 0; i < 16; i++) {
        lv_obj_t *row = lv_button_create(list);
        lv_obj_set_width(row, LV_PCT(100));
        lv_obj_add_event_cb(row, clicked, LV_EVENT_CLICKED, NULL);
        label = lv_label_create(row);
        lv_label_set_text_fmt(label, "Item %u", i + 1);
        lv_obj_center(label);
    }

    /* Tap the field, then type on the computer: the viewer sends the keys while it is focused. */
    lv_obj_t *field = lv_textarea_create(typing);
    lv_textarea_set_one_line(field, true);
    lv_textarea_set_placeholder_text(field, "Tap, then type");
    lv_obj_set_width(field, W - W / 8);
    lv_obj_align(field, LV_ALIGN_TOP_MID, 0, H / 16);
    lv_obj_add_event_cb(field, typed, LV_EVENT_READY, NULL);
    s_typed = lv_label_create(typing);
    lv_label_set_text(s_typed, "");
    lv_obj_align(s_typed, LV_ALIGN_TOP_MID, 0, H / 16 + 44);
    lv_obj_t *keys = lv_keyboard_create(typing);
    lv_obj_set_size(keys, W, H / 2);
    lv_obj_align(keys, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_keyboard_set_textarea(keys, field);
}

#if CONFIG_VIRTUAL_WIFI
static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg; (void)base; (void)data;
    if (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
}

static void got_ip(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg; (void)base; (void)id;
    const ip_event_got_ip_t *event = data;
    ESP_LOGI("virtual", "Wi-Fi: the board is " IPSTR, IP2STR(&event->ip_info.ip));
}

/* The mirror's TCP port opens on the stack this starts, before espmirror_start(). */
static void net_init(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
}

/* After espmirror_start(): the screen copy takes its contiguous block before Wi-Fi splits internal RAM. */
static void wifi_start(void) {
    esp_netif_create_default_wifi_sta();
    const wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, got_ip, NULL));
    wifi_config_t config = { 0 };
    strlcpy((char *)config.sta.ssid, CONFIG_VIRTUAL_WIFI_SSID, sizeof(config.sta.ssid));
    strlcpy((char *)config.sta.password, CONFIG_VIRTUAL_WIFI_PASSWORD, sizeof(config.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
}
#endif

void app_main(void) {
    s_keys = xQueueCreate(16, sizeof(int));
    assert(s_keys);
#if CONFIG_VIRTUAL_WIFI
    net_init();
#endif
#if CONFIG_VIRTUAL_BOOT_GPIO >= 0
    if (!GPIO_IS_VALID_GPIO(CONFIG_VIRTUAL_BOOT_GPIO)) {
        ESP_LOGE("virtual", "invalid BOOT GPIO");
        return;
    }
    const gpio_config_t input = {
        .pin_bit_mask = UINT64_C(1) << CONFIG_VIRTUAL_BOOT_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&input));
#endif
    /* Keys the viewer draws, laid out as the ESP-Mosaico's: two either side of the middle of the bottom
     * edge, where its USB port is, and one at one o'clock. Only BOOT is also a key on the module. */
    const espmirror_button_t keys[] = {
        { .role = ESPMIRROR_KEY_MAIN, .name = "BOOT", .x = W / 2 - W / 5, .y = H - 1 },
        { .role = ESPMIRROR_KEY_POWER, .name = "POWER", .x = W / 2 + W / 5, .y = H - 1 },
        { .role = ESPMIRROR_KEY_AUX, .name = "FN", .x = W * 4 / 5, .y = 0 },
    };
    const espmirror_config_t config = {
        .name = CONFIG_VIRTUAL_NAME,
        .width = W, .height = H,
#ifdef CONFIG_VIRTUAL_ROUND
        .round = CONFIG_VIRTUAL_ROUND,
#endif
        .diagonal = CONFIG_VIRTUAL_DIAGONAL_100 / 100.0f,
        .touch = true,
        .buttons = keys, .button_count = 3,
        .on_key = on_key,
    };
    ESP_ERROR_CHECK(espmirror_start(&config));
#if CONFIG_VIRTUAL_WIFI
    wifi_start();
#endif
    lv_init();
    lv_tick_set_cb(tick_ms);
    lv_display_t *display = lv_display_create(W, H);
    assert(display);
    lv_display_set_color_format(display, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(display, s_draw, NULL, sizeof(s_draw), LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, flush);
    ESP_ERROR_CHECK(espmirror_attach_lvgl(display));
    build_ui();
    ESP_LOGI("virtual", "mirror ready; swipe or FN for the pages, BOOT or Tap counts presses, POWER darkens");
    for (;;) {
        boot_poll(tick_ms());
        int key;
        while (xQueueReceive(s_keys, &key, 0) == pdTRUE) key_press(key);
        uint32_t delay = lv_timer_handler();
        if (delay > 10) delay = 10;
        TickType_t ticks = pdMS_TO_TICKS(delay);
        vTaskDelay(ticks ? ticks : 1);
    }
}
