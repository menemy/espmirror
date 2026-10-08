# The core needs no display library. The LVGL hook is compiled too, and compiles
# to nothing unless lvgl.h is there; the optional requirement makes it there
# (and public, since espmirror_lvgl.h includes it) when LVGL is in the build.
idf_component_register(SRCS "src/espmirror.c" "src/espmirror_lvgl.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES esp_timer lwip)
idf_component_optional_requires(PUBLIC lvgl__lvgl lvgl)
