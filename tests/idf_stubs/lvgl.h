/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include "common.h"
#define LV_VERSION_CHECK(a, b, c) 1
#define LV_DRAW_BUF_ALIGN 4
#define LV_PCT(x) (x)
enum { LV_COLOR_FORMAT_RGB565, LV_COLOR_FORMAT_RGB565_SWAPPED, LV_DISPLAY_RENDER_MODE_PARTIAL,
       LV_INDEV_TYPE_POINTER, LV_INDEV_STATE_PRESSED, LV_INDEV_STATE_RELEASED,
       LV_EVENT_FLUSH_START, LV_EVENT_CLICKED, LV_ANIM_OFF, LV_ANIM_REPEAT_INFINITE,
       LV_OPA_TRANSP, LV_OBJ_FLAG_SCROLLABLE, LV_DIR_LEFT, LV_DIR_RIGHT,
       LV_ALIGN_TOP_MID, LV_ALIGN_CENTER };
typedef struct lv_obj lv_obj_t;
typedef struct lv_display lv_display_t;
typedef struct lv_event lv_event_t;
typedef struct lv_indev lv_indev_t;
typedef struct { int x1, y1, x2, y2; } lv_area_t;
typedef struct { struct { int cf, stride; } header; uint8_t *data; } lv_draw_buf_t;
typedef struct { struct { int x, y; } point; int state; bool continue_reading; } lv_indev_data_t;
typedef struct { void *var; } lv_anim_t;
extern int lv_font_montserrat_14;
void lv_init(void);
void lv_tick_set_cb(uint32_t (*)(void));
uint32_t lv_timer_handler(void);
void *lv_event_get_target(lv_event_t *);
void *lv_event_get_param(lv_event_t *);
lv_draw_buf_t *lv_display_get_buf_active(lv_display_t *);
int lv_display_get_render_mode(lv_display_t *);
int lv_display_get_horizontal_resolution(lv_display_t *);
int lv_display_get_vertical_resolution(lv_display_t *);
int lv_area_get_width(const lv_area_t *);
int lv_area_get_height(const lv_area_t *);
lv_display_t *lv_display_get_default(void);
lv_display_t *lv_display_create(int, int);
lv_obj_t *lv_display_get_screen_active(lv_display_t *);
void lv_display_set_color_format(lv_display_t *, int);
void lv_display_set_buffers(lv_display_t *, void *, void *, size_t, int);
void lv_display_set_flush_cb(lv_display_t *, void (*)(lv_display_t *, const lv_area_t *, uint8_t *));
void lv_display_flush_ready(lv_display_t *);
void lv_display_add_event_cb(lv_display_t *, void (*)(lv_event_t *), int, void *);
lv_indev_t *lv_indev_create(void);
void lv_indev_set_type(lv_indev_t *, int);
void lv_indev_set_display(lv_indev_t *, lv_display_t *);
void lv_indev_set_read_cb(lv_indev_t *, void (*)(lv_indev_t *, lv_indev_data_t *));
lv_obj_t *lv_screen_active(void);
void lv_obj_invalidate(lv_obj_t *);
unsigned lv_color_hex(unsigned);
unsigned lv_color_white(void);
void lv_obj_set_style_bg_color(lv_obj_t *, unsigned, int);
void lv_obj_set_style_text_color(lv_obj_t *, unsigned, int);
void lv_obj_set_style_text_font(lv_obj_t *, const void *, int);
void lv_obj_set_style_bg_opa(lv_obj_t *, unsigned, int);
void lv_obj_remove_flag(lv_obj_t *, unsigned);
void lv_obj_set_size(lv_obj_t *, int, int);
void lv_obj_set_width(lv_obj_t *, int);
void lv_obj_align(lv_obj_t *, int, int, int);
void lv_obj_center(lv_obj_t *);
void lv_obj_add_event_cb(lv_obj_t *, void (*)(lv_event_t *), int, void *);
lv_obj_t *lv_tileview_create(lv_obj_t *);
lv_obj_t *lv_tileview_add_tile(lv_obj_t *, unsigned, unsigned, unsigned);
lv_obj_t *lv_label_create(lv_obj_t *);
void lv_label_set_text(lv_obj_t *, const char *);
void lv_label_set_text_fmt(lv_obj_t *, const char *, ...);
lv_obj_t *lv_button_create(lv_obj_t *);
lv_obj_t *lv_list_create(lv_obj_t *);
lv_obj_t *lv_bar_create(lv_obj_t *);
void lv_bar_set_value(lv_obj_t *, int, int);
void lv_anim_init(lv_anim_t *);
void lv_anim_set_var(lv_anim_t *, void *);
void lv_anim_set_exec_cb(lv_anim_t *, void (*)(void *, int32_t));
void lv_anim_set_values(lv_anim_t *, int, int);
void lv_anim_set_duration(lv_anim_t *, unsigned);
void lv_anim_set_playback_duration(lv_anim_t *, unsigned);
void lv_anim_set_repeat_count(lv_anim_t *, unsigned);
void lv_anim_start(lv_anim_t *);
