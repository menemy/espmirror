cmake_minimum_required(VERSION 3.16)
# Wi-Fi from menuconfig (Example Connection Configuration), as the IDF examples do.
set(EXTRA_COMPONENT_DIRS $ENV{IDF_PATH}/examples/common_components/protocol_examples_common)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(espmirror_basic)
