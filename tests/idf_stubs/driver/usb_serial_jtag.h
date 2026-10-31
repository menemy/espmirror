/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include "common.h"
typedef struct { int tx_buffer_size, rx_buffer_size; } usb_serial_jtag_driver_config_t;
int usb_serial_jtag_driver_install(const usb_serial_jtag_driver_config_t *);
void usb_serial_jtag_driver_uninstall(void);
int usb_serial_jtag_read_bytes(void *, size_t, int);
int usb_serial_jtag_write_bytes(const void *, size_t, int);
