/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "espmirror_usb.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"
#if CONFIG_ESPMIRROR_USB && SOC_USB_SERIAL_JTAG_SUPPORTED
#include "driver/usb_serial_jtag.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
static bool installed;
bool espmirror_usb_supported(void) { return true; }
esp_err_t espmirror_usb_install(void) {
    usb_serial_jtag_driver_config_t config = { .tx_buffer_size = 1024, .rx_buffer_size = 4096 };
    esp_err_t err = usb_serial_jtag_driver_install(&config);
    installed = err == ESP_OK;
    return err;
}
void espmirror_usb_uninstall(void) {
    if (installed) usb_serial_jtag_driver_uninstall();
    installed = false;
}
bool espmirror_usb_read(void *data, size_t size, int timeout_ms) {
    uint8_t *p = data;
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (size) {
        int64_t left = deadline - esp_timer_get_time();
        if (left <= 0) return false;
        TickType_t ticks = pdMS_TO_TICKS((left + 999) / 1000);
        int n = usb_serial_jtag_read_bytes(p, size, ticks ? ticks : 1);
        if (n <= 0) return false;
        p += n; size -= n;
    }
    return true;
}
int espmirror_usb_write(const void *data, size_t size, int timeout_ms) {
    TickType_t ticks = pdMS_TO_TICKS(timeout_ms);
    return usb_serial_jtag_write_bytes(data, size, ticks ? ticks : 1);
}
#else
bool espmirror_usb_supported(void) { return false; }
esp_err_t espmirror_usb_install(void) { return ESP_ERR_NOT_SUPPORTED; }
void espmirror_usb_uninstall(void) {}
bool espmirror_usb_read(void *data, size_t size, int timeout_ms) {
    (void)data; (void)size; (void)timeout_ms;
    return false;
}
int espmirror_usb_write(const void *data, size_t size, int timeout_ms) {
    (void)data; (void)size; (void)timeout_ms;
    return -1;
}
#endif
