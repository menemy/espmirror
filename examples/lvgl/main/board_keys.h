/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdint.h>

typedef struct {
    unsigned stable, candidate;
    int64_t changed_us[4];
} board_key_filter_t;

static inline unsigned board_key_filter(board_key_filter_t *filter, unsigned raw, int64_t now_us) {
    for (unsigned key = 1; key <= 3; key++) {
        unsigned bit = 1u << key;
        if ((filter->candidate ^ raw) & bit) {
            filter->candidate ^= bit;
            filter->changed_us[key] = now_us;
        } else if (((filter->stable ^ raw) & bit) && now_us - filter->changed_us[key] >= 30000) {
            filter->stable ^= bit;
        }
    }
    return filter->stable;
}
