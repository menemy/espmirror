/*
 * The board under the example: its panel, codec and buttons, when the example
 * knows them. Any other devkit gets the stubs: the mirror is the display, the
 * sounds play on the viewer, the keys come from the viewer.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "lvgl.h"

/* Rails, latches and buttons the board needs before anything else. */
void board_init(void);

/* Drives the board's own panel from the LVGL display: sets the display's
 * flush callback to one that sends the area to the panel. ESP_ERR_NOT_SUPPORTED
 * when the example has no driver for the selected board. The draw buffers must
 * be DMA-capable (internal RAM). */
esp_err_t board_display_start(lv_display_t *disp);

/* The codec, 16 kHz: ESP_ERR_NOT_SUPPORTED without one. */
esp_err_t board_audio_start(void);
/* Blocks for `frames` of the microphone, mono PCM16; 0 when there is none. */
size_t board_mic_read(int16_t *mono, size_t frames);
/* Plays mono PCM16 on the speaker; nothing without one. */
void board_speaker_write(const int16_t *mono, size_t frames);

/* The board's own buttons, a bit per key code (1 << key) while held. */
unsigned board_keys_down(void);
