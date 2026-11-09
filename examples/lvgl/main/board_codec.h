/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "driver/i2c_master.h"
#include "esp_err.h"

typedef struct {
    i2c_master_bus_handle_t bus;
    int mclk, bclk, ws, dout, din, amplifier;
    bool es7210;
} board_codec_config_t;

esp_err_t board_codec_start(const board_codec_config_t *config);
size_t board_codec_read(int16_t *mono, size_t frames);
void board_codec_write(const int16_t *mono, size_t frames);
