/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "board.h"
#include "sdkconfig.h"

#if CONFIG_EXAMPLE_BOARD_WAVESHARE_P4_35

#include "board_keys.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "board_codec.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_st7796.h"
#include "esp_lcd_touch_ft5x06.h"
#include "esp_timer.h"
#include "espmirror.h"

static const char *TAG = "wavesharep435";
static i2c_master_bus_handle_t s_i2c;
static i2c_master_dev_handle_t s_pmu;
static esp_lcd_panel_handle_t s_panel;
static esp_lcd_touch_handle_t s_touch;

static esp_err_t pmu_read(uint8_t reg, uint8_t *value) {
    return i2c_master_transmit_receive(s_pmu, &reg, 1, value, 1, 20);
}

static esp_err_t pmu_write(uint8_t reg, uint8_t value) {
    uint8_t bytes[] = {reg, value};
    return i2c_master_transmit(s_pmu, bytes, sizeof(bytes), 20);
}

void board_init(void) {
    const gpio_config_t boot = {.pin_bit_mask = 1ULL << 35, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE};
    ESP_ERROR_CHECK(gpio_config(&boot));
    const i2c_master_bus_config_t bus = {
        .i2c_port = I2C_NUM_0, .sda_io_num = 7, .scl_io_num = 8, .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus, &s_i2c));
    const i2c_device_config_t pmu = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x34, .scl_speed_hz = 400000};
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c, &pmu, &s_pmu));
    uint8_t enable;
    ESP_ERROR_CHECK(pmu_read(0x41, &enable));
    ESP_ERROR_CHECK(pmu_write(0x41, enable | 3));
    ESP_ERROR_CHECK(pmu_write(0x49, 3));
}

unsigned board_keys_down(void) {
    static board_key_filter_t keys;
    static bool power, release_next;
    if (release_next) {
        power = false;
        release_next = false;
    } else {
        uint8_t edges;
        if (pmu_read(0x49, &edges) != ESP_OK) power = false;
        else if ((edges &= 3) && pmu_write(0x49, edges) == ESP_OK) {
            if (edges == 3) { power = true; release_next = true; }
            else power = (edges & 2) != 0;
        }
    }
    unsigned raw = gpio_get_level(35) == 0 ? 1u << ESPMIRROR_KEY_MAIN : 0;
    return board_key_filter(&keys, raw, esp_timer_get_time()) | (power ? 1u << ESPMIRROR_KEY_POWER : 0);
}

static bool flush_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *event, void *user) {
    (void)io;
    (void)event;
    lv_display_flush_ready(user);
    return false;
}

static void flush(lv_display_t *disp, const lv_area_t *area, uint8_t *pixels) {
    (void)disp;
    lv_draw_sw_rgb565_swap(pixels, lv_area_get_size(area));
    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(s_panel, area->x1, area->y1, area->x2 + 1, area->y2 + 1, pixels));
}

static void touch_read(lv_indev_t *input, lv_indev_data_t *data) {
    (void)input;
    esp_lcd_touch_point_data_t point;
    uint8_t count = 0;
    data->state = LV_INDEV_STATE_RELEASED;
    if (esp_lcd_touch_read_data(s_touch) == ESP_OK
        && esp_lcd_touch_get_data(s_touch, &point, &count, 1) == ESP_OK && count
        && point.x < 320 && point.y < 480) {
        data->point.x = point.y;
        data->point.y = 319 - point.x;
        data->state = LV_INDEV_STATE_PRESSED;
    }
}

esp_err_t board_display_start(lv_display_t *disp) {
    const ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_0, .freq_hz = 20000, .clk_cfg = LEDC_AUTO_CLK,
    };
    const ledc_channel_config_t light = {
        .gpio_num = 28, .speed_mode = LEDC_LOW_SPEED_MODE, .channel = LEDC_CHANNEL_0, .timer_sel = LEDC_TIMER_0,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "backlight timer");
    ESP_RETURN_ON_ERROR(ledc_channel_config(&light), TAG, "backlight channel");
    const spi_bus_config_t bus = {
        .sclk_io_num = 21, .mosi_io_num = 20, .miso_io_num = -1, .quadwp_io_num = -1,
        .quadhd_io_num = -1, .max_transfer_sz = 480 * 40 * 2,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "panel SPI");
    esp_lcd_panel_io_handle_t io;
    const esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num = 23, .dc_gpio_num = 26, .spi_mode = 3, .pclk_hz = 80000000,
        .trans_queue_depth = 2, .lcd_cmd_bits = 8, .lcd_param_bits = 8,
        .on_color_trans_done = flush_done, .user_ctx = disp,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_cfg, &io), TAG, "panel IO");
    const esp_lcd_panel_dev_config_t panel = {.reset_gpio_num = 27, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR, .bits_per_pixel = 16};
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7796(io, &panel, &s_panel), TAG, "ST7796");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "panel reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "panel init");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_invert_color(s_panel, true), TAG, "panel inversion");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_swap_xy(s_panel, true), TAG, "landscape axes");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_mirror(s_panel, false, false), TAG, "landscape orientation");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "panel on");
    lv_display_set_flush_cb(disp, flush);

    esp_lcd_panel_io_handle_t touch_io;
    esp_lcd_panel_io_i2c_config_t touch_bus = ESP_LCD_TOUCH_IO_I2C_FT5x06_CONFIG();
    touch_bus.scl_speed_hz = 400000;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i2c(s_i2c, &touch_bus, &touch_io), TAG, "touch IO");
    /* FT6336 holds INT low throughout a gesture; polling must keep reading. */
    const esp_lcd_touch_config_t touch = {.x_max = 320, .y_max = 480, .rst_gpio_num = 29, .int_gpio_num = -1};
    ESP_RETURN_ON_ERROR(esp_lcd_touch_new_i2c_ft5x06(touch_io, &touch, &s_touch), TAG, "FT6336");
    lv_indev_t *input = lv_indev_create();
    ESP_RETURN_ON_FALSE(input, ESP_ERR_NO_MEM, TAG, "touch input");
    lv_indev_set_type(input, LV_INDEV_TYPE_POINTER);
    lv_indev_set_display(input, disp);
    lv_indev_set_read_cb(input, touch_read);
    ESP_RETURN_ON_ERROR(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 800), TAG, "backlight duty");
    return ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

esp_err_t board_audio_start(void) {
    const board_codec_config_t audio = {
        .bus = s_i2c, .mclk = 13, .bclk = 12, .ws = 10, .dout = 9, .din = 11,
        .amplifier = 53, .es7210 = false,
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
