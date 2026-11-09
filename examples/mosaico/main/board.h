/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"
esp_err_t mosaico_board_start(void);
esp_err_t mosaico_board_present(bool on);
unsigned mosaico_board_keys(void);
bool mosaico_board_touch(int *x, int *y);
