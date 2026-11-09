/*
 * XOrigin AIPI Lite: ESP32-S3, a 128 x 128 ST7789 over SPI (SPI3: SCLK 16,
 * MOSI 17, CS 15, DC 7, RST 18, backlight 3), one ES8311 for the microphone
 * and the speaker (I2S MCLK 6, BCLK 14, WS 12, DOUT 11, DIN 13; I2C SDA 5,
 * SCL 4; amplifier enable 9), two buttons (talk 42, power 1, active low) and
 * the battery latch on GPIO 10 the firmware must hold. Pins as in
 * xiaozhi-esp32's aipi-lite board. ESP-IDF 5.3 or later (the I2C master
 * driver esp_codec_dev takes).
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#include "board.h"
#include "sdkconfig.h"

#if CONFIG_EXAMPLE_BOARD_AIPI_LITE

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/ledc.h"
#include "driver/rtc_io.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_log.h"
#include "espmirror.h"

static const char *TAG = "aipi";

#define LCD_RES 128
#define LCD_HOST SPI3_HOST
#define PIN_SCLK 16
#define PIN_MOSI 17
#define PIN_CS 15
#define PIN_DC 7
#define PIN_RST 18
#define PIN_BL 3
#define PIN_POWER_HOLD 10
#define PIN_TALK 42
#define PIN_AUX 1
#define PIN_I2C_SDA 5
#define PIN_I2C_SCL 4
#define PIN_I2S_MCLK 6
#define PIN_I2S_BCLK 14
#define PIN_I2S_WS 12
#define PIN_I2S_DOUT 11
#define PIN_I2S_DIN 13
#define PIN_PA 9
#define RATE 16000

static esp_lcd_panel_handle_t s_panel;
static lv_display_t *s_disp;
static esp_codec_dev_handle_t s_spk, s_mic;

void board_init(void) {
    /* The latch: without it the board turns off when the power button is let go. */
    rtc_gpio_init(PIN_POWER_HOLD);
    rtc_gpio_set_direction(PIN_POWER_HOLD, RTC_GPIO_MODE_OUTPUT_ONLY);
    rtc_gpio_set_level(PIN_POWER_HOLD, 1);
    rtc_gpio_hold_en(PIN_POWER_HOLD);
    const gpio_config_t keys = {
        .pin_bit_mask = 1ULL << PIN_TALK | 1ULL << PIN_AUX, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&keys);
}

unsigned board_keys_down(void) {
    /* The talk button is the viewer's key 2, the power button its key 3. */
    return (gpio_get_level(PIN_TALK) == 0 ? 1u << ESPMIRROR_KEY_MAIN : 0)
         | (gpio_get_level(PIN_AUX) == 0 ? 1u << ESPMIRROR_KEY_POWER : 0);
}

/* ---- The panel ------------------------------------------------------------------ */

static bool trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *data, void *user) {
    (void)io;
    (void)data;
    (void)user;
    lv_display_flush_ready(s_disp);
    return false;
}

/* The mirror took its copy at LV_EVENT_FLUSH_START; the panel wants the bytes swapped. */
static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px) {
    (void)disp;
    lv_draw_sw_rgb565_swap(px, lv_area_get_size(area));
    esp_lcd_panel_draw_bitmap(s_panel, area->x1, area->y1, area->x2 + 1, area->y2 + 1, px);
}

esp_err_t board_display_start(lv_display_t *disp) {
    const ledc_timer_config_t bl_timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_10_BIT, .timer_num = LEDC_TIMER_0,
        .freq_hz = 20000, .clk_cfg = LEDC_AUTO_CLK,
    };
    const ledc_channel_config_t bl_ch = {
        .gpio_num = PIN_BL, .speed_mode = LEDC_LOW_SPEED_MODE, .channel = LEDC_CHANNEL_0, .timer_sel = LEDC_TIMER_0,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&bl_timer), TAG, "backlight timer");
    ESP_RETURN_ON_ERROR(ledc_channel_config(&bl_ch), TAG, "backlight channel");

    const spi_bus_config_t bus = {
        .sclk_io_num = PIN_SCLK, .mosi_io_num = PIN_MOSI, .miso_io_num = -1, .quadwp_io_num = -1, .quadhd_io_num = -1,
        .max_transfer_sz = LCD_RES * LCD_RES * 2,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");
    esp_lcd_panel_io_handle_t io;
    const esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num = PIN_CS, .dc_gpio_num = PIN_DC, .spi_mode = 0, .pclk_hz = 40 * 1000 * 1000,
        .trans_queue_depth = 10, .lcd_cmd_bits = 8, .lcd_param_bits = 8,
        .on_color_trans_done = trans_done,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(LCD_HOST, &io_cfg, &io), TAG, "panel io");
    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = PIN_RST, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR, .bits_per_pixel = 16,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7789(io, &panel_cfg, &s_panel), TAG, "st7789");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "init");
    esp_lcd_panel_invert_color(s_panel, false);
    esp_lcd_panel_swap_xy(s_panel, true);
    esp_lcd_panel_mirror(s_panel, true, false);
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "on");

    s_disp = disp;
    lv_display_set_flush_cb(disp, flush_cb);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 800);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    ESP_LOGI(TAG, "ST7789 %d x %d on", LCD_RES, LCD_RES);
    return ESP_OK;
}

/* ---- The codec: one ES8311 both ways over a duplex I2S bus, clocked from BCLK -------- */

esp_err_t board_audio_start(void) {
    i2c_master_bus_handle_t i2c;
    const i2c_master_bus_config_t i2c_cfg = {
        .i2c_port = I2C_NUM_0, .sda_io_num = PIN_I2C_SDA, .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&i2c_cfg, &i2c), TAG, "i2c");

    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &tx, &rx), TAG, "i2s channel");
    const i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = PIN_I2S_MCLK, .bclk = PIN_I2S_BCLK, .ws = PIN_I2S_WS, .dout = PIN_I2S_DOUT, .din = PIN_I2S_DIN},
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(tx, &std_cfg), TAG, "i2s tx");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(rx, &std_cfg), TAG, "i2s rx");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(tx), TAG, "i2s tx on");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(rx), TAG, "i2s rx on");

    audio_codec_i2s_cfg_t i2s_cfg = {.port = I2C_NUM_0, .rx_handle = rx, .tx_handle = tx};
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);
    audio_codec_i2c_cfg_t i2c_if_cfg = {.port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = i2c};
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_if_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(data_if && ctrl_if && gpio_if, ESP_ERR_NO_MEM, TAG, "codec interfaces");
    es8311_codec_cfg_t es_cfg = {
        .ctrl_if = ctrl_if, .gpio_if = gpio_if, .codec_mode = ESP_CODEC_DEV_WORK_MODE_BOTH, .pa_pin = PIN_PA,
        .use_mclk = false, .hw_gain = {.pa_voltage = 5.0, .codec_dac_voltage = 3.3},
    };
    const audio_codec_if_t *codec = es8311_codec_new(&es_cfg);
    ESP_RETURN_ON_FALSE(codec, ESP_FAIL, TAG, "ES8311 not responding");
    esp_codec_dev_cfg_t out_cfg = {.dev_type = ESP_CODEC_DEV_TYPE_OUT, .codec_if = codec, .data_if = data_if};
    esp_codec_dev_cfg_t in_cfg = {.dev_type = ESP_CODEC_DEV_TYPE_IN, .codec_if = codec, .data_if = data_if};
    s_spk = esp_codec_dev_new(&out_cfg);
    s_mic = esp_codec_dev_new(&in_cfg);
    ESP_RETURN_ON_FALSE(s_spk && s_mic, ESP_FAIL, TAG, "codec devices");
    esp_codec_dev_sample_info_t fs = {.sample_rate = RATE, .channel = 2, .bits_per_sample = 16};
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_spk, &fs), TAG, "speaker open");
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_mic, &fs), TAG, "mic open");
    esp_codec_dev_set_out_vol(s_spk, 70);
    esp_codec_dev_set_in_gain(s_mic, 30.0f);
    ESP_LOGI(TAG, "ES8311 on, %d Hz", RATE);
    return ESP_OK;
}

size_t board_mic_read(int16_t *mono, size_t frames) {
    if (!s_mic || frames > 512) return 0;
    int16_t stereo[1024];
    if (esp_codec_dev_read(s_mic, stereo, frames * 4) != ESP_CODEC_DEV_OK) return 0;
    for (size_t i = 0; i < frames; i++) mono[i] = stereo[2 * i];   /* the ADC's left slot */
    return frames;
}

void board_speaker_write(const int16_t *mono, size_t frames) {
    if (!s_spk) return;
    int16_t stereo[640];
    for (size_t at = 0; at < frames; at += 320) {
        size_t n = frames - at < 320 ? frames - at : 320;
        for (size_t i = 0; i < n; i++) stereo[2 * i] = stereo[2 * i + 1] = mono[at + i];
        esp_codec_dev_write(s_spk, stereo, n * 4);
    }
}

#elif CONFIG_EXAMPLE_BOARD_NONE

void board_init(void) {}

esp_err_t board_display_start(lv_display_t *disp) {
    (void)disp;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t board_audio_start(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

size_t board_mic_read(int16_t *mono, size_t frames) {
    (void)mono;
    (void)frames;
    return 0;
}

void board_speaker_write(const int16_t *mono, size_t frames) {
    (void)mono;
    (void)frames;
}

unsigned board_keys_down(void) {
    return 0;
}

#endif

#if !CONFIG_EXAMPLE_BOARD_WAVESHARE_154G
void board_led_set(bool on) { (void)on; }
#endif
