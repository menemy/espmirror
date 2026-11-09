/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "board.h"
#include "sdkconfig.h"

#if CONFIG_EXAMPLE_BOARD_WAVESHARE_154G

#include <limits.h>
#include <string.h>
#include "board_keys.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "board_codec.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "espmirror.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "waveshare154g";
static spi_device_handle_t s_spi;
static SemaphoreHandle_t s_lock;
static TaskHandle_t s_refresh;
static uint8_t *s_pending, *s_complete, *s_snapshot;

void board_init(void) {
    ESP_ERROR_CHECK(gpio_set_level(17, 1));
    ESP_ERROR_CHECK(gpio_set_level(6, 0));
#if CONFIG_ESPMIRROR_AUDIO
    ESP_ERROR_CHECK(gpio_set_level(42, 0));
#else
    ESP_ERROR_CHECK(gpio_set_level(42, 1));
#endif
    ESP_ERROR_CHECK(gpio_set_level(3, 1));
    const gpio_config_t outputs = {
        .pin_bit_mask = (1ULL << 17) | (1ULL << 6) | (1ULL << 42) | (1ULL << 3),
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&outputs));
    ESP_ERROR_CHECK(gpio_hold_en(17));
    const gpio_config_t keys = {
        .pin_bit_mask = (1ULL << 0) | (1ULL << 18), .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&keys));
}

unsigned board_keys_down(void) {
    static board_key_filter_t keys;
    unsigned raw = (gpio_get_level(0) == 0 ? 1u << ESPMIRROR_KEY_MAIN : 0)
         | (gpio_get_level(18) == 0 ? 1u << ESPMIRROR_KEY_POWER : 0);
    return board_key_filter(&keys, raw, esp_timer_get_time());
}

void board_led_set(bool on) {
    gpio_set_level(3, !on);
}

static esp_err_t send_bytes(bool data, const uint8_t *bytes, size_t length) {
    ESP_RETURN_ON_ERROR(gpio_set_level(10, data), TAG, "EPD DC");
    spi_transaction_t transaction = {.length = length * 8, .tx_buffer = bytes};
    return spi_device_polling_transmit(s_spi, &transaction);
}

static esp_err_t command(uint8_t cmd, const uint8_t *data, size_t length) {
    ESP_RETURN_ON_ERROR(send_bytes(false, &cmd, 1), TAG, "EPD command");
    return length ? send_bytes(true, data, length) : ESP_OK;
}

static esp_err_t wait_idle(void) {
    /* BUSY may assert after the command transaction has finished. */
    vTaskDelay(pdMS_TO_TICKS(100));
    int64_t deadline = esp_timer_get_time() + 45000000;
    while (!gpio_get_level(8)) {
        if (esp_timer_get_time() >= deadline) return ESP_ERR_TIMEOUT;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    return ESP_OK;
}

static esp_err_t refresh_panel(void) {
    gpio_set_level(9, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(9, 1);
    vTaskDelay(pdMS_TO_TICKS(200));
    static const struct { uint8_t cmd, count, data[7]; } init[] = {
        {0x4D, 1, {0x78}}, {0x00, 2, {0x0F, 0x29}},
        {0x06, 7, {0x0D, 0x12, 0x30, 0x20, 0x19, 0x2A, 0x22}},
        {0x50, 1, {0x37}}, {0x61, 4, {0, 200, 0, 200}},
        {0xE9, 1, {1}}, {0x30, 1, {8}},
    };
    for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); i++) {
        ESP_RETURN_ON_ERROR(command(init[i].cmd, init[i].data, init[i].count), TAG, "EPD init");
    }
    ESP_RETURN_ON_ERROR(command(0x04, NULL, 0), TAG, "EPD power on");
    ESP_RETURN_ON_ERROR(wait_idle(), TAG, "EPD power wait");
    ESP_RETURN_ON_ERROR(command(0x10, s_snapshot, 10000), TAG, "EPD frame");
    const uint8_t zero = 0, sleep = 0xA5;
    ESP_RETURN_ON_ERROR(command(0x12, &zero, 1), TAG, "EPD refresh");
    ESP_RETURN_ON_ERROR(wait_idle(), TAG, "EPD refresh wait");
    ESP_RETURN_ON_ERROR(command(0x02, &zero, 1), TAG, "EPD power off");
    ESP_RETURN_ON_ERROR(wait_idle(), TAG, "EPD power off wait");
    return command(0x07, &sleep, 1);
}

static void refresh_task(void *arg) {
    (void)arg;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        xSemaphoreTake(s_lock, portMAX_DELAY);
        memcpy(s_snapshot, s_complete, 10000);
        xSemaphoreGive(s_lock);
        int64_t start = esp_timer_get_time();
        esp_err_t err = refresh_panel();
        ESP_LOGI(TAG, "panel refresh: %s, %lld ms", esp_err_to_name(err),
                 (long long)((esp_timer_get_time() - start) / 1000));
        /* The next full refresh starts at least 15 s after this one ends. */
        vTaskDelay(pdMS_TO_TICKS(15000));
    }
}

static unsigned ink(uint16_t rgb) {
    const int r = (rgb >> 11) * 255 / 31;
    const int g = ((rgb >> 5) & 63) * 255 / 63;
    const int b = (rgb & 31) * 255 / 31;
    static const uint8_t palette[4][3] = {{0, 0, 0}, {255, 255, 255}, {255, 255, 0}, {255, 0, 0}};
    unsigned best = 0;
    int distance = INT_MAX;
    for (unsigned i = 0; i < 4; i++) {
        int dr = r - palette[i][0], dg = g - palette[i][1], db = b - palette[i][2];
        int d = dr * dr + dg * dg + db * db;
        if (d < distance) { distance = d; best = i; }
    }
    return best;
}

static void flush(lv_display_t *disp, const lv_area_t *area, uint8_t *pixels) {
    const uint16_t *rgb = (const uint16_t *)pixels;
    uint32_t stride = lv_draw_buf_width_to_stride(lv_area_get_width(area), LV_COLOR_FORMAT_RGB565) / 2;
    for (int y = area->y1; y <= area->y2; y++) {
        for (int x = area->x1; x <= area->x2; x++) {
            unsigned at = y * 50 + x / 4, shift = 6 - (x % 4) * 2;
            unsigned color = ink(rgb[(y - area->y1) * stride + x - area->x1]);
            s_pending[at] = (s_pending[at] & ~(3u << shift)) | (color << shift);
        }
    }
    if (lv_display_flush_is_last(disp)) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
        memcpy(s_complete, s_pending, 10000);
        xSemaphoreGive(s_lock);
        xTaskNotifyGive(s_refresh);
    }
    lv_display_flush_ready(disp);
}

esp_err_t board_display_start(lv_display_t *disp) {
    s_pending = heap_caps_malloc(10000, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_complete = heap_caps_malloc(10000, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_snapshot = heap_caps_malloc(10000, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_pending && s_complete && s_snapshot && s_lock, ESP_ERR_NO_MEM, TAG, "EPD buffers");
    memset(s_pending, 0x55, 10000);
    const gpio_config_t outputs = {.pin_bit_mask = (1ULL << 9) | (1ULL << 10), .mode = GPIO_MODE_OUTPUT};
    const gpio_config_t busy = {.pin_bit_mask = 1ULL << 8, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE};
    ESP_RETURN_ON_ERROR(gpio_config(&outputs), TAG, "EPD outputs");
    ESP_RETURN_ON_ERROR(gpio_config(&busy), TAG, "EPD busy");
    const spi_bus_config_t bus = {
        .sclk_io_num = 12, .mosi_io_num = 13, .miso_io_num = -1,
        .quadwp_io_num = -1, .quadhd_io_num = -1, .max_transfer_sz = 10000,
    };
    const spi_device_interface_config_t panel = {.clock_speed_hz = 10000000, .mode = 0, .spics_io_num = 11, .queue_size = 1};
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "EPD bus");
    ESP_RETURN_ON_ERROR(spi_bus_add_device(SPI2_HOST, &panel, &s_spi), TAG, "EPD device");
    ESP_RETURN_ON_FALSE(xTaskCreate(refresh_task, "epaper", 4096, NULL, 3, &s_refresh) == pdPASS,
                        ESP_ERR_NO_MEM, TAG, "EPD task");
    lv_display_set_flush_cb(disp, flush);
    return ESP_OK;
}

esp_err_t board_audio_start(void) {
    i2c_master_bus_handle_t i2c;
    const i2c_master_bus_config_t bus = {
        .i2c_port = I2C_NUM_0, .sda_io_num = 47, .scl_io_num = 48,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus, &i2c), TAG, "audio I2C");
    const board_codec_config_t audio = {
        .bus = i2c, .mclk = 14, .bclk = 15, .ws = 38, .dout = 45, .din = 16,
        .amplifier = 46, .es7210 = false,
    };
    return board_codec_start(&audio);
}

size_t board_mic_read(int16_t *mono, size_t frames) {
    return board_codec_read(mono, frames);
}

void board_speaker_write(const int16_t *mono, size_t frames) {
    board_codec_write(mono, frames);
}

#endif
