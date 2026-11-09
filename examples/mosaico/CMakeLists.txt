# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.16)
if(NOT DEFINED MOSAICO_UTILS)
    set(MOSAICO_UTILS "$ENV{MOSAICO_UTILS}")
endif()
if(NOT EXISTS "${MOSAICO_UTILS}/ESP-Iris/components/esp_iris/CMakeLists.txt")
    message(FATAL_ERROR "Set MOSAICO_UTILS to an esp-mosaico-utils checkout with ESP-Iris and Vibe Mode 0.2")
endif()
get_filename_component(ESPMIRROR_ROOT "../.." ABSOLUTE)
set(EXTRA_COMPONENT_DIRS
    "${ESPMIRROR_ROOT}"
    "${MOSAICO_UTILS}/ESP-Iris/components/esp_iris"
    "${MOSAICO_UTILS}/esp-mosaico-vibe-mode/components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espmirror_mosaico VERSION 0.1.0)
