/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include "common.h"
#define ESP_IRIS_CHANNEL_SCREEN 3
#define ESP_IRIS_PIXEL_FORMAT_RGB565 1
typedef struct {
    uint16_t service_id, method_id;
    uint32_t request_id, deadline_ms;
    const uint8_t *payload;
    size_t payload_size;
} esp_iris_rpc_request_t;
typedef esp_err_t (*esp_iris_rpc_handler_t)(const esp_iris_rpc_request_t *, uint8_t *, size_t, size_t *, void *);
typedef struct {
    uint16_t x, y, width, height;
    uint32_t stride;
    uint16_t format, quality;
} esp_iris_media_desc_t;
typedef struct {
    esp_err_t (*begin)(const esp_iris_media_desc_t *, esp_iris_media_desc_t *, uint32_t *, void *);
    esp_err_t (*read)(uint32_t, uint8_t *, size_t, size_t *, void *);
    void (*end)(void *);
    void *user_ctx;
} esp_iris_screen_backend_t;
esp_err_t esp_iris_screen_register(const esp_iris_screen_backend_t *);
esp_err_t esp_iris_screen_unregister(void *);
esp_err_t esp_iris_rpc_register(uint16_t, uint16_t, esp_iris_rpc_handler_t, void *);
esp_err_t esp_iris_rpc_unregister(uint16_t, uint16_t);
bool esp_iris_media_is_streaming(int);
bool esp_iris_is_started(void);
