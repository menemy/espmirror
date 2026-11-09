/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "board.h"
#include "sdkconfig.h"
#if CONFIG_MOSAICO_PANEL
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_efuse.h"
#include "esp_efuse_table.h"
#include "esp_heap_caps.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_touch_cst9220.h"
#include "esp_timer.h"
#include "espmirror.h"
#include "espmirror_timing.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define WIDTH 480
#define HEIGHT 480
#define BAND_ROWS 8
static const char *TAG = "mosaico";
static esp_lcd_panel_handle_t panel;
static esp_lcd_touch_handle_t touch;
static SemaphoreHandle_t transmitted;
static uint16_t *band;
#if CONFIG_ESPMIRROR_TIMING
/* One DMA transfer is outstanding; its semaphore publishes this ISR timestamp. */
static volatile uint64_t band_done_us;
#endif

/* CO5300 vendor initialization for the Mosaico's 480 x 480 panel. */
static const co5300_lcd_init_cmd_t commands[] = {
    {0x11, NULL, 0, 120},
    {0xfe, (uint8_t[]){0x20}, 1, 0},
    {0x19, (uint8_t[]){0x10}, 1, 0},
    {0x1c, (uint8_t[]){0xa0}, 1, 0},
    {0xfe, (uint8_t[]){0x00}, 1, 0},
    {0xc4, (uint8_t[]){0x80}, 1, 0},
    {0x3a, (uint8_t[]){0x55}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 0},
    {0x51, (uint8_t[]){0xff}, 1, 0},
    {0x63, (uint8_t[]){0xff}, 1, 0},
    {0x2a, (uint8_t[]){0x00, 0x00, 0x01, 0xdf}, 4, 0},
    {0x2b, (uint8_t[]){0x00, 0x00, 0x01, 0xdf}, 4, 0},
    {0x29, NULL, 0, 20},
};

static bool transfer_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *event, void *ctx) {
    (void)io; (void)event; (void)ctx;
#if CONFIG_ESPMIRROR_TIMING
    band_done_us = espmirror_timing_now();
    espmirror_timing_record(ESPMIRROR_TIMING_BAND_DONE, band_done_us, 0);
#endif
    BaseType_t wake = pdFALSE;
    xSemaphoreGiveFromISR(transmitted, &wake);
    /* The pinned SPI panel driver ignores the callback's return value. */
    if (wake == pdTRUE) portYIELD_FROM_ISR();
    return wake == pdTRUE;
}

esp_err_t mosaico_board_start(void) {
    uint16_t revision = 0;
    ESP_RETURN_ON_ERROR(esp_efuse_read_field_blob(ESP_EFUSE_USER_DATA, &revision, 16), TAG, "board revision");
    if (revision != 0x0100 && revision != 0x0101 && revision != 0x0102) {
        ESP_LOGE(TAG, "unknown board revision 0x%04x", revision);
        return ESP_ERR_NOT_SUPPORTED;
    }
    bool first = revision == 0x0100;
    ESP_LOGI(TAG, "board revision %u.%u", revision >> 8, revision & 255);
    const gpio_config_t rail = { .pin_bit_mask = 1ULL << 60, .mode = GPIO_MODE_OUTPUT };
    ESP_RETURN_ON_ERROR(gpio_set_level(60, 0), TAG, "peripheral power level");
    ESP_RETURN_ON_ERROR(gpio_config(&rail), TAG, "peripheral power");
    ESP_RETURN_ON_ERROR(gpio_hold_dis(60), TAG, "peripheral power hold");
    vTaskDelay(pdMS_TO_TICKS(10));
    const gpio_config_t buttons = { .pin_bit_mask = (1ULL << 7) | (1ULL << 61),
        .mode = GPIO_MODE_INPUT, .pull_up_en = true };
    ESP_RETURN_ON_ERROR(gpio_config(&buttons), TAG, "keys");
    transmitted = xSemaphoreCreateBinary();
    band = heap_caps_malloc(WIDTH * BAND_ROWS * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (!transmitted || !band) return ESP_ERR_NO_MEM;
    spi_bus_config_t bus = CO5300_PANEL_BUS_QSPI_CONFIG(first ? 44 : 42, 36, 51, 35, 9, WIDTH * BAND_ROWS * 2);
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "QSPI bus");
    esp_lcd_panel_io_handle_t io;
    esp_lcd_panel_io_spi_config_t io_config = CO5300_PANEL_IO_QSPI_CONFIG(50, transfer_done, NULL);
    io_config.trans_queue_depth = 1;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_config, &io), TAG, "panel IO");
    co5300_vendor_config_t vendor = { .init_cmds = commands,
        .init_cmds_size = sizeof(commands) / sizeof(commands[0]), .flags.use_qspi_interface = 1 };
    esp_lcd_panel_dev_config_t config = { .reset_gpio_num = first ? 42 : 44,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB, .bits_per_pixel = 16, .vendor_config = &vendor };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_co5300(io, &config, &panel), TAG, "panel");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(panel), TAG, "panel reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(panel), TAG, "panel init");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(panel, true), TAG, "panel on");
    i2c_master_bus_handle_t i2c;
    const i2c_master_bus_config_t i2c_config = { .i2c_port = I2C_NUM_0,
        .sda_io_num = first ? 0 : 56, .scl_io_num = first ? 1 : 3,
        .clk_source = I2C_CLK_SRC_DEFAULT, .flags.enable_internal_pullup = true };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&i2c_config, &i2c), TAG, "touch bus");
    esp_lcd_panel_io_handle_t touch_io;
    esp_lcd_panel_io_i2c_config_t touch_io_config = ESP_LCD_TOUCH_IO_I2C_CST9220_CONFIG();
    touch_io_config.scl_speed_hz = 400000;
    touch_io_config.transaction_timeout_ms = 100;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i2c(i2c, &touch_io_config, &touch_io), TAG, "touch IO");
    const esp_lcd_touch_config_t touch_config = { .x_max = WIDTH - 1, .y_max = HEIGHT - 1,
        .rst_gpio_num = GPIO_NUM_NC, .int_gpio_num = 6, .levels = { .reset = 0, .interrupt = 0 } };
    return esp_lcd_touch_new_i2c_cst9220(touch_io, &touch_config, &touch);
}

esp_err_t mosaico_board_present(bool on) {
    static bool was_on = true;
    if (on != was_on) {
        ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(panel, on), TAG, "panel state");
        was_on = on;
    }
    if (!on) return ESP_OK;
    uint64_t present_start = espmirror_timing_now();
    for (int y = 0; y < HEIGHT; y += BAND_ROWS) {
        const uint16_t *fb = espmirror_frame_begin();
        for (int i = 0; i < WIDTH * BAND_ROWS; i++) band[i] = __builtin_bswap16(fb[y * WIDTH + i]);
        espmirror_frame_end(0, 0, 0, 0);
        ESP_RETURN_ON_ERROR(esp_lcd_panel_draw_bitmap(panel, 0, y, WIDTH, y + BAND_ROWS, band), TAG, "panel draw");
        /* The DMA buffer is reused only after the SPI completion callback. */
        uint64_t wait_start = espmirror_timing_now();
        if (xSemaphoreTake(transmitted, pdMS_TO_TICKS(500)) != pdTRUE) return ESP_ERR_TIMEOUT;
        uint64_t awake = espmirror_timing_now();
#if CONFIG_ESPMIRROR_TIMING
        espmirror_timing_record(ESPMIRROR_TIMING_BAND_WAKE, awake, awake - band_done_us);
#endif
        espmirror_timing_record(ESPMIRROR_TIMING_BAND_WAIT, awake, awake - wait_start);
    }
    uint64_t presented = espmirror_timing_now();
    espmirror_timing_record(ESPMIRROR_TIMING_PANEL_PRESENT, presented, presented - present_start);
    return ESP_OK;
}

unsigned mosaico_board_keys(void) {
    static unsigned sample, stable;
    static int64_t changed;
    unsigned now = (!gpio_get_level(7) ? 1u << 1 : 0) | (!gpio_get_level(61) ? 1u << 2 : 0);
    int64_t time = esp_timer_get_time();
    if (now != sample) { sample = now; changed = time; }
    if (time - changed >= 30000) stable = sample;
    return stable;
}

bool mosaico_board_touch(int *x, int *y) {
    if (esp_lcd_touch_read_data(touch) != ESP_OK) return false;
    esp_lcd_touch_point_data_t point;
    uint8_t count = 0;
    if (esp_lcd_touch_get_data(touch, &point, &count, 1) == ESP_OK && count) {
        *x = point.x; *y = point.y; return true;
    }
    return false;
}
#else
esp_err_t mosaico_board_start(void) { return ESP_OK; }
esp_err_t mosaico_board_present(bool on) { (void)on; return ESP_OK; }
unsigned mosaico_board_keys(void) { return 0; }
bool mosaico_board_touch(int *x, int *y) { (void)x; (void)y; return false; }
#endif
