/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "board.h"
#include "sdkconfig.h"

#if CONFIG_EXAMPLE_BOARD_WAVESHARE_349

#include <string.h>
#include "board_keys.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "board_codec.h"
#include "esp_heap_caps.h"
#include "esp_lcd_axs15231b.h"
#include "esp_lcd_panel_ops.h"
#include "esp_timer.h"
#include "espmirror.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "waveshare349";
static i2c_master_bus_handle_t s_i2c;
static i2c_master_dev_handle_t s_expander, s_touch;
static uint8_t s_outputs;
static esp_lcd_panel_handle_t s_panel;
static SemaphoreHandle_t s_sent;
static uint8_t *s_frame, *s_dma;

static esp_err_t expander_write(uint8_t reg, uint8_t value) {
    uint8_t bytes[] = {reg, value};
    return i2c_master_transmit(s_expander, bytes, sizeof(bytes), 20);
}

static esp_err_t output(unsigned bit, bool high) {
    s_outputs = high ? s_outputs | (1u << bit) : s_outputs & ~(1u << bit);
    return expander_write(1, s_outputs);
}

void board_init(void) {
    const gpio_config_t keys = {
        .pin_bit_mask = (1ULL << 0) | (1ULL << 16), .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&keys));
    const i2c_master_bus_config_t bus = {
        .i2c_port = I2C_NUM_0, .sda_io_num = 47, .scl_io_num = 48, .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus, &s_i2c));
    const i2c_device_config_t expander = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x20, .scl_speed_hz = 400000};
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c, &expander, &s_expander));
    /* SYS_EN holds battery power. Write levels before enabling output drivers. */
    const uint8_t mask = (1u << 1) | (1u << 5) | (1u << 6) | (1u << 7);
    s_outputs = (uint8_t)~mask | (1u << 5) | (1u << 6);
    ESP_ERROR_CHECK(expander_write(1, s_outputs));
    ESP_ERROR_CHECK(expander_write(3, (uint8_t)~mask));
}

unsigned board_keys_down(void) {
    static board_key_filter_t keys;
    unsigned raw = (gpio_get_level(0) == 0 ? 1u << ESPMIRROR_KEY_MAIN : 0)
         | (gpio_get_level(16) == 0 ? 1u << ESPMIRROR_KEY_POWER : 0);
    return board_key_filter(&keys, raw, esp_timer_get_time());
}

static bool chunk_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *event, void *user) {
    (void)io;
    (void)event;
    (void)user;
    BaseType_t wake = pdFALSE;
    xSemaphoreGiveFromISR(s_sent, &wake);
    if (wake) portYIELD_FROM_ISR();
    return wake == pdTRUE;
}

static void flush(lv_display_t *disp, const lv_area_t *area, uint8_t *pixels) {
    (void)area;
    (void)pixels;
    /* QSPI omits row addressing: start at row zero, then continue the full frame. */
    if (lv_display_flush_is_last(disp)) {
        for (int row = 0; row < 640; row += 16) {
            memcpy(s_dma, s_frame + row * 172 * 2, 172 * 16 * 2);
            ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(s_panel, 0, row, 172, row + 16, s_dma));
            ESP_ERROR_CHECK(xSemaphoreTake(s_sent, pdMS_TO_TICKS(1000)) == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT);
        }
    }
    lv_display_flush_ready(disp);
}

static void touch_read(lv_indev_t *input, lv_indev_data_t *data) {
    (void)input;
    static lv_point_t point;
    static bool pressed;
    static int64_t last_report;
    const uint8_t request[] = {0xB5, 0xAB, 0xA5, 0x5A, 0, 0, 0, 0x0E, 0, 0, 0};
    uint8_t packet[32];
    bool fresh = false;
    esp_err_t err = i2c_master_transmit_receive(s_touch, request, sizeof(request), packet, sizeof(packet), 20);
    if (err == ESP_OK) {
        for (unsigned i = 0; i < 14; i++) fresh |= packet[i] != 0xD3;
    }
    if (fresh) {
        unsigned along = ((packet[2] & 15) << 8) | packet[3];
        unsigned across = ((packet[4] & 15) << 8) | packet[5];
        pressed = packet[1] > 0 && packet[1] < 5 && (packet[2] >> 6) != 1 && across < 172 && along < 640;
        if (pressed) { point.x = across; point.y = 639 - along; }
        last_report = esp_timer_get_time();
    } else if (err != ESP_OK || esp_timer_get_time() - last_report > 80000) pressed = false;
    data->point = point;
    data->state = pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

esp_err_t board_display_start(lv_display_t *disp) {
    const ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_8_BIT,
        .timer_num = LEDC_TIMER_0, .freq_hz = 50000, .clk_cfg = LEDC_AUTO_CLK,
    };
    const ledc_channel_config_t light = {
        .gpio_num = 42, .speed_mode = LEDC_LOW_SPEED_MODE, .channel = LEDC_CHANNEL_0,
        .timer_sel = LEDC_TIMER_0, .duty = 255,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "backlight timer");
    ESP_RETURN_ON_ERROR(ledc_channel_config(&light), TAG, "backlight channel");
    i2c_master_bus_handle_t touch_bus;
    const i2c_master_bus_config_t touch_cfg = {
        .i2c_port = I2C_NUM_1, .sda_io_num = 17, .scl_io_num = 18, .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&touch_cfg, &touch_bus), TAG, "touch bus");
    const i2c_device_config_t touch = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x3B,
        .scl_speed_hz = 300000, .scl_wait_us = 200000,
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(touch_bus, &touch, &s_touch), TAG, "touch device");
    s_frame = heap_caps_aligned_alloc(LV_DRAW_BUF_ALIGN, 172 * 640 * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_dma = heap_caps_malloc(172 * 16 * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    s_sent = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(s_frame && s_dma && s_sent, ESP_ERR_NO_MEM, TAG, "panel buffers");
    memset(s_frame, 0, 172 * 640 * 2);
    const spi_bus_config_t bus = AXS15231B_PANEL_BUS_QSPI_CONFIG(10, 11, 12, 13, 14, 172 * 16 * 2);
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "panel bus");
    esp_lcd_panel_io_handle_t io;
    const esp_lcd_panel_io_spi_config_t io_cfg = AXS15231B_PANEL_IO_QSPI_CONFIG(9, chunk_done, NULL);
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_cfg, &io), TAG, "panel IO");
    static const axs15231b_lcd_init_cmd_t init[] = {{0x11, NULL, 0, 100}, {0x29, NULL, 0, 100}};
    const axs15231b_vendor_config_t vendor = {
        .init_cmds = init, .init_cmds_size = 2, .flags.use_qspi_interface = true,
    };
    const esp_lcd_panel_dev_config_t panel = {
        .reset_gpio_num = -1, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16, .vendor_config = (void *)&vendor,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_axs15231b(io, &panel, &s_panel), TAG, "AXS15231B");
    ESP_RETURN_ON_ERROR(output(5, true), TAG, "reset high");
    vTaskDelay(pdMS_TO_TICKS(30));
    ESP_RETURN_ON_ERROR(output(5, false), TAG, "reset low");
    vTaskDelay(pdMS_TO_TICKS(250));
    ESP_RETURN_ON_ERROR(output(5, true), TAG, "reset release");
    vTaskDelay(pdMS_TO_TICKS(30));
    /* The vendor's 0x29 leaves touch scanning enabled; do not send 0x28. */
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "panel init");
    ESP_RETURN_ON_ERROR(i2c_master_probe(touch_bus, 0x3B, 50), TAG, "touch probe");
    void *old_buffer = lv_display_get_buf_active(disp)->data;
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565_SWAPPED);
    lv_display_set_buffers(disp, s_frame, NULL, 172 * 640 * 2, LV_DISPLAY_RENDER_MODE_DIRECT);
    heap_caps_free(old_buffer);
    lv_display_set_flush_cb(disp, flush);
    lv_indev_t *input = lv_indev_create();
    ESP_RETURN_ON_FALSE(input, ESP_ERR_NO_MEM, TAG, "touch input");
    lv_indev_set_type(input, LV_INDEV_TYPE_POINTER);
    lv_indev_set_display(input, disp);
    lv_indev_set_read_cb(input, touch_read);
    ESP_RETURN_ON_ERROR(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 80), TAG, "backlight duty");
    ESP_RETURN_ON_ERROR(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0), TAG, "backlight update");
    return output(1, true);
}

esp_err_t board_audio_start(void) {
    const board_codec_config_t audio = {
        .bus = s_i2c, .mclk = 7, .bclk = 15, .ws = 46, .dout = 45, .din = 6,
        .amplifier = -1, .es7210 = true,
    };
    ESP_RETURN_ON_ERROR(board_codec_start(&audio), TAG, "codec");
    return output(7, true);
}

size_t board_mic_read(int16_t *mono, size_t frames) {
    return board_codec_read(mono, frames);
}

void board_speaker_write(const int16_t *mono, size_t frames) {
    board_codec_write(mono, frames);
}

#endif
