# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
get_filename_component(espmirror_root "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
get_filename_component(espmirror_component "${espmirror_root}" NAME)
idf_component_register(SRCS "main.c" "board.c" INCLUDE_DIRS "."
    PRIV_REQUIRES ${espmirror_component} esp_iris esp_mosaico_app_recovery esp_timer
                  esp_driver_gpio esp_driver_i2c esp_driver_spi esp_lcd efuse)
