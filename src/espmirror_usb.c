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
#elif CONFIG_ESPMIRROR_USB_NETWORK
#include <stdlib.h>
#include <string.h>
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/esp_netif_net_stack.h"
#include "tinyusb.h"
#include "tinyusb_cdc_acm.h"
#include "tinyusb_default_config.h"
#include "tinyusb_net.h"
#if !CONFIG_TINYUSB_CDC_ENABLED || !CONFIG_TINYUSB_NET_MODE_NCM
#error "CONFIG_ESPMIRROR_USB_NETWORK needs CONFIG_TINYUSB_CDC_ENABLED and CONFIG_TINYUSB_NET_MODE_NCM"
#endif
#define ACM TINYUSB_CDC_ACM_0
static SemaphoreHandle_t s_rx_ready;
static esp_netif_t *s_netif;
static bool s_driver, s_acm, s_net;

static void acm_rx(int itf, cdcacm_event_t *event) {
    (void)itf; (void)event;
    /* TinyUSB keeps the bytes in its FIFO and NAKs the host when it is full. */
    xSemaphoreGive(s_rx_ready);
}

static esp_err_t net_rx(void *buffer, uint16_t len, void *ctx) {
    (void)ctx;
    if (!s_netif) return ESP_OK;
    void *copy = malloc(len);
    if (!copy) return ESP_ERR_NO_MEM;
    memcpy(copy, buffer, len);
    return esp_netif_receive(s_netif, copy, len, NULL);
}

static esp_err_t net_tx(void *handle, void *buffer, size_t len) {
    (void)handle;
    /* A frame the computer does not take in time is dropped, as on a cable. */
    tinyusb_net_send_sync(buffer, len, NULL, pdMS_TO_TICKS(100));
    return ESP_OK;
}

static void net_free(void *handle, void *buffer) {
    (void)handle;
    free(buffer);
}

static esp_err_t netif_up(void) {
    uint8_t board[6];
    esp_read_mac(board, ESP_MAC_ETH);
    tinyusb_net_config_t net = { .on_recv_callback = net_rx };
    esp_derive_local_mac(net.mac_addr, board);    /* the computer's end of the link */
    esp_err_t err = tinyusb_net_init(&net);
    if (err != ESP_OK) return err;
    s_net = true;
    static const esp_netif_ip_info_t ip = {
        .ip = { .addr = ESP_IP4TOADDR(192, 168, CONFIG_ESPMIRROR_USB_NETWORK_SUBNET, 1) },
        .netmask = { .addr = ESP_IP4TOADDR(255, 255, 255, 0) },
        .gw = { .addr = ESP_IP4TOADDR(192, 168, CONFIG_ESPMIRROR_USB_NETWORK_SUBNET, 1) },
    };
    const esp_netif_inherent_config_t base = {
        .flags = ESP_NETIF_DHCP_SERVER | ESP_NETIF_FLAG_AUTOUP,
        .ip_info = &ip,
        .if_key = "espmirror_usb",
        .if_desc = "espmirror USB network",
        .route_prio = 5,
    };
    const esp_netif_driver_ifconfig_t driver = {
        .handle = (void *)1,                     /* TinyUSB's NCM is a singleton; esp_netif wants non-NULL */
        .transmit = net_tx,
        .driver_free_rx_buffer = net_free,
    };
    const struct esp_netif_netstack_config stack = { .lwip = { .init_fn = ethernetif_init, .input_fn = ethernetif_input } };
    const esp_netif_config_t config = { .base = &base, .driver = &driver, .stack = &stack };
    s_netif = esp_netif_new(&config);
    if (!s_netif) return ESP_ERR_NO_MEM;
    esp_netif_set_mac(s_netif, board);
    /* No router, so the computer keeps its default route. ESP-IDF's DHCP server always names a
     * DNS server, this board when none is set; it cannot leave the option out. */
    uint8_t off = 0;
    esp_netif_dhcps_option(s_netif, ESP_NETIF_OP_SET, ESP_NETIF_ROUTER_SOLICITATION_ADDRESS, &off, sizeof(off));
    esp_netif_action_start(s_netif, NULL, 0, NULL);
    return ESP_OK;
}

bool espmirror_usb_supported(void) { return true; }

esp_netif_t *espmirror_usb_netif(void) { return s_netif; }

void espmirror_usb_uninstall(void) {
    if (s_netif) { esp_netif_destroy(s_netif); s_netif = NULL; }
    if (s_net) { tinyusb_net_deinit(); s_net = false; }
    if (s_acm) { tinyusb_cdcacm_deinit(ACM); s_acm = false; }
    if (s_driver) { tinyusb_driver_uninstall(); s_driver = false; }
    if (s_rx_ready) { vSemaphoreDelete(s_rx_ready); s_rx_ready = NULL; }
}

esp_err_t espmirror_usb_install(void) {
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK) return err;
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;
    s_rx_ready = xSemaphoreCreateBinary();
    if (!s_rx_ready) return ESP_ERR_NO_MEM;
    const tinyusb_config_t usb = TINYUSB_DEFAULT_CONFIG();
    err = tinyusb_driver_install(&usb);
    s_driver = err == ESP_OK;
    if (err == ESP_OK) {
        const tinyusb_config_cdcacm_t acm = { .cdc_port = ACM, .callback_rx = acm_rx };
        err = tinyusb_cdcacm_init(&acm);
        s_acm = err == ESP_OK;
    }
    if (err == ESP_OK) err = netif_up();
    if (err != ESP_OK) espmirror_usb_uninstall();
    return err;
}

bool espmirror_usb_read(void *data, size_t size, int timeout_ms) {
    uint8_t *p = data;
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (size) {
        size_t n = 0;
        if (tinyusb_cdcacm_read(ACM, p, size, &n) == ESP_OK && n) {
            p += n; size -= n;
            continue;
        }
        int64_t left = deadline - esp_timer_get_time();
        if (left <= 0) return false;
        TickType_t ticks = pdMS_TO_TICKS((left + 999) / 1000);
        xSemaphoreTake(s_rx_ready, ticks ? ticks : 1);
    }
    return true;
}

int espmirror_usb_write(const void *data, size_t size, int timeout_ms) {
    size_t n = tinyusb_cdcacm_write_queue(ACM, data, size);
    /* esp_tinyusb logs a flush that times out, and the mirror's log hook would then wait on the
     * TX lock this task holds: poll the non-blocking flush instead, which does not log. */
    int64_t until = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (tinyusb_cdcacm_write_flush(ACM, 0) == ESP_ERR_NOT_FINISHED && esp_timer_get_time() < until) vTaskDelay(1);
    return (int)n;
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
