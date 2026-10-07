set(srcs "src/espmirror.c")
set(priv_reqs esp_timer lwip)

# The LVGL hook compiles when LVGL is part of the build (the registry's lvgl/lvgl
# or a local component named lvgl); the core needs no display library at all.
idf_build_get_property(build_components BUILD_COMPONENTS)
if("lvgl__lvgl" IN_LIST build_components OR "lvgl" IN_LIST build_components)
    list(APPEND srcs "src/espmirror_lvgl.c")
    if("lvgl__lvgl" IN_LIST build_components)
        list(APPEND priv_reqs lvgl__lvgl)
    else()
        list(APPEND priv_reqs lvgl)
    endif()
endif()

idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES ${priv_reqs})
