# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
set(espmirror_requires esp_timer esp_hw_support esp_rom lwip)
# Requirements are expanded before sdkconfig is loaded, so they cannot depend on CONFIG_ESPMIRROR_USB.
idf_build_get_property(major IDF_VERSION_MAJOR)
idf_build_get_property(minor IDF_VERSION_MINOR)
if(major GREATER 5 OR (major EQUAL 5 AND minor GREATER_EQUAL 3))
    list(APPEND espmirror_requires esp_driver_usb_serial_jtag)
else()
    list(APPEND espmirror_requires driver)
endif()
idf_component_register(SRCS "src/espmirror.c" "src/espmirror_core.c"
                            "src/espmirror_usb.c" "src/espmirror_lvgl.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "src"
                       PRIV_REQUIRES ${espmirror_requires})
idf_component_optional_requires(PUBLIC lvgl__lvgl lvgl)
if(CONFIG_ESPMIRROR_WIFI)
    idf_component_optional_requires(PRIVATE espressif__mdns mdns)
endif()
