# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_timer esp_driver_gpio)
