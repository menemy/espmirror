/*
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include "board.h"
#include "sdkconfig.h"

#if CONFIG_EXAMPLE_BOARD_WAVESHARE_175C

#include "board_keys.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "board_codec.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_touch_cst9217.h"
#include "esp_timer.h"
#include "espmirror.h"

static const char *TAG = "waveshare175c";
static i2c_master_bus_handle_t s_i2c;
static i2c_master_dev_handle_t s_pmu;
static esp_lcd_panel_handle_t s_panel;
static esp_lcd_touch_handle_t s_touch;

static esp_err_t pmu_read(uint8_t reg, uint8_t *value) {
    return i2c_master_transmit_receive(s_pmu, &reg, 1, value, 1, 20);
}

static esp_err_t pmu_write(uint8_t reg, uint8_t value) {
    const uint8_t bytes[] = {reg, value};
    return i2c_master_transmit(s_pmu, bytes, sizeof(bytes), 20);
}

void board_init(void) {
    const gpio_config_t keys = {
        .pin_bit_mask = 1ULL << 0, .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&keys));
    const gpio_config_t key = {.pin_bit_mask = 1ULL << 3, .mode = GPIO_MODE_INPUT};
    ESP_ERROR_CHECK(gpio_config(&key));
    const i2c_master_bus_config_t bus = {
        .i2c_port = I2C_NUM_0, .sda_io_num = 15, .scl_io_num = 14,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus, &s_i2c));
    const i2c_device_config_t pmu = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x34, .scl_speed_hz = 400000,
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c, &pmu, &s_pmu));
    uint8_t value;
    /* ALDO1 supplies the codecs at 3.3 V; keep the other rails unchanged. */
    ESP_ERROR_CHECK(pmu_read(0x92, &value));
    ESP_ERROR_CHECK(pmu_write(0x92, (value & 0xE0) | 28));
    ESP_ERROR_CHECK(pmu_read(0x90, &value));
    ESP_ERROR_CHECK(pmu_write(0x90, value | 1));
}

unsigned board_keys_down(void) {
    static board_key_filter_t keys;
    /* The inverter makes the physical PWR button high on GPIO3 (viewer KEY). */
    unsigned raw = (gpio_get_level(3) != 0 ? 1u << ESPMIRROR_KEY_AUX : 0)
         | (gpio_get_level(0) == 0 ? 1u << ESPMIRROR_KEY_MAIN : 0);
    return board_key_filter(&keys, raw, esp_timer_get_time());
}

/* Register values and delays from Waveshare's 1.75C BSP, revision 010c5fb. */
static const co5300_lcd_init_cmd_t s_init[] = {
    {0xFE, (uint8_t[]){0x20}, 1, 0},
    {0x19, (uint8_t[]){0x10}, 1, 0},
    {0x1C, (uint8_t[]){0xA0}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x3A, (uint8_t[]){0x55}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 0},
    {0x51, (uint8_t[]){0xFF}, 1, 0},
    {0x63, (uint8_t[]){0xFF}, 1, 0},
    {0x2A, (uint8_t[]){0, 6, 1, 0xD7}, 4, 0},
    {0x2B, (uint8_t[]){0, 0, 1, 0xD1}, 4, 600},
    {0x11, NULL, 0, 600},
    {0x29, NULL, 0, 0},
};

static bool flush_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *event, void *user) {
    (void)io;
    (void)event;
    lv_display_flush_ready(user);
    return false;
}

static void flush(lv_display_t *disp, const lv_area_t *area, uint8_t *pixels) {
    lv_draw_sw_rgb565_swap(pixels, lv_area_get_size(area));
    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(s_panel, area->x1, area->y1,
                                             area->x2 + 1, area->y2 + 1, pixels));
    (void)disp;
}

static void align_area(lv_event_t *event) {
    /* CO5300 update windows must start even and end odd. */
    lv_area_t *area = lv_event_get_param(event);
    area->x1 &= ~1;
    area->y1 &= ~1;
    area->x2 |= 1;
    area->y2 |= 1;
}

static void touch_read(lv_indev_t *indev, lv_indev_data_t *data) {
    (void)indev;
    esp_lcd_touch_point_data_t point;
    uint8_t count = 0;
    data->state = LV_INDEV_STATE_RELEASED;
    if (esp_lcd_touch_read_data(s_touch) == ESP_OK
        && esp_lcd_touch_get_data(s_touch, &point, &count, 1) == ESP_OK && count
        && point.x < 466 && point.y < 466) {
        data->point.x = point.x;
        data->point.y = point.y;
        data->state = LV_INDEV_STATE_PRESSED;
    }
}

esp_err_t board_display_start(lv_display_t *disp) {
    const spi_bus_config_t bus = CO5300_PANEL_BUS_QSPI_CONFIG(38, 4, 5, 6, 7, 466 * 40 * 2);
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "SPI");
    esp_lcd_panel_io_handle_t io;
    const esp_lcd_panel_io_spi_config_t io_cfg = CO5300_PANEL_IO_QSPI_CONFIG(12, flush_done, disp);
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_cfg, &io), TAG, "panel IO");
    const co5300_vendor_config_t vendor = {
        .init_cmds = s_init, .init_cmds_size = sizeof(s_init) / sizeof(s_init[0]),
        .flags.use_qspi_interface = true,
    };
    const esp_lcd_panel_dev_config_t panel = {
        .reset_gpio_num = 1, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16, .vendor_config = (void *)&vendor,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_co5300(io, &panel, &s_panel), TAG, "CO5300");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "panel reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_set_gap(s_panel, 6, 0), TAG, "panel offset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "panel init");
    lv_display_add_event_cb(disp, align_area, LV_EVENT_INVALIDATE_AREA, NULL);
    lv_display_set_flush_cb(disp, flush);

    esp_lcd_panel_io_handle_t touch_io;
    esp_lcd_panel_io_i2c_config_t touch_bus = ESP_LCD_TOUCH_IO_I2C_CST9217_CONFIG();
    touch_bus.scl_speed_hz = 400000;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i2c(s_i2c, &touch_bus, &touch_io), TAG, "touch IO");
    const esp_lcd_touch_config_t touch = {
        .x_max = 465, .y_max = 465, .rst_gpio_num = 2, .int_gpio_num = 11,
        .flags = {.mirror_x = true, .mirror_y = true},
    };
    ESP_RETURN_ON_ERROR(esp_lcd_touch_new_i2c_cst9217(touch_io, &touch, &s_touch), TAG, "CST9217");
    lv_indev_t *input = lv_indev_create();
    ESP_RETURN_ON_FALSE(input, ESP_ERR_NO_MEM, TAG, "touch input");
    lv_indev_set_type(input, LV_INDEV_TYPE_POINTER);
    lv_indev_set_display(input, disp);
    lv_indev_set_read_cb(input, touch_read);
    return ESP_OK;
}

esp_err_t board_audio_start(void) {
    const board_codec_config_t audio = {
        .bus = s_i2c, .mclk = 16, .bclk = 9, .ws = 45, .dout = 8, .din = 10,
        .amplifier = 46, .es7210 = true,
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
