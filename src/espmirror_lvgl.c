/*
 * espmirror: the LVGL 9 hook.
 *
 * Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */

#if __has_include("lvgl.h")

#include <string.h>

#include "esp_log.h"
#include "espmirror.h"
#include "espmirror_lvgl.h"
#include "lvgl.h"

#if !LV_VERSION_CHECK(9, 5, 0)
#error "espmirror's LVGL hook needs LVGL 9.5 or later (lv_display_get_buf_active, lv_display_get_render_mode)"
#endif

static const char *TAG = "espmirror";

/* LVGL announces each flush before the display's callback gets it; at that
 * moment the active draw buffer holds the area as rendered, at the buffer's
 * stride: the whole screen when the buffer is that big (direct and full
 * modes), the area alone otherwise (partial mode). RGB565 in native order, or
 * byte-swapped when the display was set up to render for its panel; a copy
 * either way, the buffer is left as it is. */
static void flush_start(lv_event_t *e) {
    lv_display_t *disp = lv_event_get_target(e);
    const lv_area_t *area = lv_event_get_param(e);
    lv_draw_buf_t *buf = lv_display_get_buf_active(disp);
    if (!buf || !area) return;
    bool swapped = buf->header.cf == LV_COLOR_FORMAT_RGB565_SWAPPED;
    if (buf->header.cf != LV_COLOR_FORMAT_RGB565 && !swapped) return;
    int sw, sh;
    espmirror_size(&sw, &sh);
    uint16_t *fb = espmirror_frame_begin();
    if (!fb) return;
    int w = lv_area_get_width(area), h = lv_area_get_height(area);
    bool whole = lv_display_get_render_mode(disp) != LV_DISPLAY_RENDER_MODE_PARTIAL;   /* a screen-sized buffer */
    int x0 = area->x1 < 0 ? 0 : area->x1, x1 = area->x2 >= sw ? sw - 1 : area->x2;
    for (int y = 0; y < h && x1 >= x0; y++) {
        int ty = area->y1 + y;
        if (ty < 0 || ty >= sh) continue;
        const uint8_t *row = buf->data + (whole ? ty : y) * buf->header.stride + (whole ? x0 : x0 - area->x1) * 2;
        uint16_t *to = fb + ty * sw + x0;
        if (!swapped) {
            memcpy(to, row, (x1 - x0 + 1) * 2);
        } else {
            for (int i = 0; i <= x1 - x0; i++) to[i] = (uint16_t)(row[2 * i] << 8 | row[2 * i + 1]);
        }
    }
    espmirror_frame_end(area->x1, area->y1, w, h);
}

static void pointer_read(lv_indev_t *indev, lv_indev_data_t *data) {
    (void)indev;
    int x, y; bool down;
    data->continue_reading = espmirror_pointer_read(&x, &y, &down);
    data->point.x = x; data->point.y = y;
    data->state = down ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

#if LV_USE_TEXTAREA
/* The focused, visible textarea under obj; with ta, the visible keyboard typing into ta. */
static lv_obj_t *text_find(lv_obj_t *obj, lv_obj_t *ta) {
    if (!obj || !lv_obj_is_visible(obj)) return NULL;
    if (!ta && lv_obj_check_type(obj, &lv_textarea_class) && lv_obj_has_state(obj, LV_STATE_FOCUSED)) return obj;
#if LV_USE_KEYBOARD
    if (ta && lv_obj_check_type(obj, &lv_keyboard_class) && lv_keyboard_get_textarea(obj) == ta) return obj;
#endif
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        lv_obj_t *found = text_find(lv_obj_get_child(obj, i), ta);
        if (found) return found;
    }
    return NULL;
}

static lv_obj_t *text_on_display(lv_display_t *disp, lv_obj_t *ta) {
    lv_obj_t *found = text_find(lv_display_get_layer_top(disp), ta);
    return found ? found : text_find(lv_display_get_screen_active(disp), ta);
}

/* Enter as the field's own keyboard would take it: its OK key, or a new line
 * unless the field is one line, which then reports it is done. */
static void text_enter(lv_display_t *disp, lv_obj_t *ta) {
#if LV_USE_KEYBOARD
    lv_obj_t *kb = text_on_display(disp, ta);
    if (kb) {
        if (lv_obj_send_event(kb, LV_EVENT_READY, NULL) == LV_RESULT_OK && (ta = lv_keyboard_get_textarea(kb))) {
            lv_obj_send_event(ta, LV_EVENT_READY, NULL);
        }
        return;
    }
#else
    (void)disp;
#endif
    if (lv_textarea_get_one_line(ta)) lv_obj_send_event(ta, LV_EVENT_READY, NULL);
    else lv_textarea_add_char(ta, '\n');
}

/* In LVGL's task, like every LVGL call: whether a field is open, and the
 * viewer's typing into it. No tree walks while nobody watches. */
static void text_timer(lv_timer_t *timer) {
    lv_display_t *disp = lv_timer_get_user_data(timer);
    bool watched = espmirror_connected();
    lv_timer_set_period(timer, watched ? 20 : 250);
    if (!watched) {
        espmirror_text_open(false);
        return;
    }
    for (int i = 0; i < 8; i++) {
        lv_obj_t *ta = text_on_display(disp, NULL);
        espmirror_text_open(ta != NULL);
        espmirror_text_t m;
        if (!espmirror_text_read(&m)) break;
        if (ta) {
            switch (m.edit) {
            case ESPMIRROR_TEXT_INSERT: lv_textarea_add_text(ta, m.text); break;
            case ESPMIRROR_TEXT_BACKSPACE: lv_textarea_delete_char(ta); break;
            case ESPMIRROR_TEXT_DELETE: lv_textarea_delete_char_forward(ta); break;
            case ESPMIRROR_TEXT_LEFT: lv_textarea_cursor_left(ta); break;
            case ESPMIRROR_TEXT_RIGHT: lv_textarea_cursor_right(ta); break;
            case ESPMIRROR_TEXT_ENTER: text_enter(disp, ta); break;
            }
        }
        memset(&m, 0, sizeof(m));
    }
}
#endif

esp_err_t espmirror_attach_lvgl(lv_display_t *disp) {
    if (!disp) disp = lv_display_get_default();
    if (!disp) return ESP_ERR_INVALID_STATE;
    int sw, sh;
    espmirror_size(&sw, &sh);
    if (!sw) return ESP_ERR_INVALID_STATE;
    if (lv_display_get_horizontal_resolution(disp) != sw || lv_display_get_vertical_resolution(disp) != sh) {
        ESP_LOGW(TAG, "the display is %d x %d, the mirror %d x %d", (int)lv_display_get_horizontal_resolution(disp),
                 (int)lv_display_get_vertical_resolution(disp), sw, sh);
    }
    lv_indev_t *pointer = lv_indev_create();
    if (!pointer) return ESP_ERR_NO_MEM;
    lv_indev_set_type(pointer, LV_INDEV_TYPE_POINTER);
    lv_indev_set_display(pointer, disp);
    lv_indev_set_read_cb(pointer, pointer_read);
    lv_display_add_event_cb(disp, flush_start, LV_EVENT_FLUSH_START, NULL);
#if LV_USE_TEXTAREA
    if (!lv_timer_create(text_timer, 250, disp)) return ESP_ERR_NO_MEM;
#endif
    lv_obj_invalidate(lv_display_get_screen_active(disp));   /* the whole screen once, for the copy */
    ESP_LOGI(TAG, "on the LVGL flush");
    return ESP_OK;
}

#endif /* lvgl.h */
