/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include "common.h"
typedef struct {const char *key, *value;} mdns_txt_item_t;
int mdns_init(void);
int mdns_hostname_set(const char *);
int mdns_service_add(const char *, const char *, const char *, int, mdns_txt_item_t *, int);
