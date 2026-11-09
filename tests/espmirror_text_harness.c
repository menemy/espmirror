/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "espmirror_core.h"
#include <assert.h>
#include <string.h>

int main(void)
{
    uint8_t edit;
    char text[64];
    uint8_t msg[65];
    memset(msg, '1', sizeof(msg));
    msg[0] = 'I';
    assert(espmirror_core_text(msg, 64, &edit, text));
    assert(strlen(text) == 63 && edit == 0);
    assert(!espmirror_core_text(msg, 65, &edit, text) && !text[0]);
    assert(!espmirror_core_text(msg, 1, &edit, text));
    assert(!espmirror_core_text(msg, 0, &edit, text));
    const uint8_t good[] = { 'I', 0xC3, 0xA9, 0xF0, 0x9F, 0x99, 0x82 };
    assert(espmirror_core_text(good, sizeof(good), &edit, text) && !strcmp(text, "\xC3\xA9\xF0\x9F\x99\x82"));
    for (size_t n = 2; n < sizeof(good); n++) {
        if (n != 3) assert(!espmirror_core_text(good, n, &edit, text));
    }
    const uint8_t bad[][5] = {
        {'I', 0xC0, 0x80}, {'I', 0xED, 0xA0, 0x80}, {'I', 0xF4, 0x90, 0x80, 0x80},
        {'I', 0xE0, 0x80, 0x80}, {'I', 0x80}, {'I', 0}, {'K', 0}, {'K', 6}, {'O'}, {'R'}
    };
    const size_t lengths[] = {3, 4, 5, 4, 2, 2, 2, 2, 1, 1};
    for (size_t i = 0; i < sizeof(lengths) / sizeof(*lengths); i++) {
        assert(!espmirror_core_text(bad[i], lengths[i], &edit, text));
    }
    for (int k = 1; k <= 5; k++) {
        msg[0] = 'K'; msg[1] = k;
        assert(espmirror_core_text(msg, 2, &edit, text) && edit == k && !text[0]);
    }
    return 0;
}
