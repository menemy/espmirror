/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
#include <sys/types.h>
#include <unistd.h>
typedef int esp_err_t;
#define ESP_ERROR_CHECK(e) ((void)(e))
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 1
#define ESP_ERR_INVALID_STATE 2
#define ESP_ERR_NO_MEM 3
#define ESP_ERR_NOT_SUPPORTED 4
#define ESP_ERR_TIMEOUT 5
#define ESP_MAC_WIFI_STA 0
#define ESP_MAC_BASE 1
#define MALLOC_CAP_8BIT 1
#define MALLOC_CAP_SPIRAM 2
#define portMUX_INITIALIZER_UNLOCKED 0
#define portMAX_DELAY 0
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define pdMS_TO_TICKS(x) (x)
#define portENTER_CRITICAL(x) ((void)(x))
#define portEXIT_CRITICAL(x) ((void)(x))
#define ESP_LOGI(tag, ...) ((void)(tag))
#define ESP_LOGW(tag, ...) ((void)(tag))
#define ESP_LOGE(tag, ...) ((void)(tag))
typedef int portMUX_TYPE, BaseType_t, TickType_t;
typedef void *TaskHandle_t, *SemaphoreHandle_t, *QueueHandle_t;
typedef int (*vprintf_like_t)(const char *, va_list);
void *heap_caps_malloc(size_t, unsigned);
void *heap_caps_calloc(size_t, size_t, unsigned);
void heap_caps_free(void *);
int64_t esp_timer_get_time(void);
uint32_t esp_rom_crc32_le(uint32_t, const uint8_t *, size_t);
esp_err_t esp_read_mac(uint8_t *, int);
vprintf_like_t esp_log_set_vprintf(vprintf_like_t);
SemaphoreHandle_t xSemaphoreCreateMutex(void);
SemaphoreHandle_t xSemaphoreCreateBinary(void);
int xSemaphoreTake(SemaphoreHandle_t, int);
int xSemaphoreGive(SemaphoreHandle_t);
void vSemaphoreDelete(SemaphoreHandle_t);
QueueHandle_t xQueueCreate(int, size_t);
int xQueueSend(QueueHandle_t, const void *, int);
int xQueueReceive(QueueHandle_t, void *, int);
void vQueueDelete(QueueHandle_t);
int xQueueReset(QueueHandle_t);
void vTaskDelay(int);
void vTaskDelete(TaskHandle_t);
int xTaskCreate(void (*)(void *), const char *, int, void *, int, TaskHandle_t *);
int xTaskCreatePinnedToCore(void (*)(void *), const char *, int, void *, int, TaskHandle_t *, int);
