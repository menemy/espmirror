/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
bool espmirror_usb_supported(void);
esp_err_t espmirror_usb_install(void);
void espmirror_usb_uninstall(void);
bool espmirror_usb_read(void *data, size_t size, int timeout_ms);
int espmirror_usb_write(const void *data, size_t size, int timeout_ms);
