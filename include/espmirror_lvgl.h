/*
 * espmirror: the LVGL 9 hook.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "esp_err.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Every flush of the display passes the mirror on its way to the panel; NULL
 * takes the default display. Call with the LVGL lock held, after
 * espmirror_start(). Creates a queued mirror pointer input for this display;
 * call once per display and do not add another mirror pointer. The display's resolution should be the mirror's; the
 * copy is clipped otherwise. RGB565 displays only, native or byte-swapped;
 * a display offset (lv_display_set_offset) is not taken out. LVGL 9.5+. */
esp_err_t espmirror_attach_lvgl(lv_display_t *disp);

#ifdef __cplusplus
}
#endif
