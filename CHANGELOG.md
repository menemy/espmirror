# Changelog

## 0.2.0

Protocol v2: the mirror streams over the USB Serial/JTAG console by default,
with a sync word and a CRC on each frame between the log lines. The viewer is
PySide6 and finds USB boards by itself. `CONFIG_ESPMIRROR_USB_NETWORK` puts a
serial port and a network on the cable of a chip with USB OTG.
`CONFIG_ESPMIRROR_WIFI` is now `CONFIG_ESPMIRROR_TCP`; `sdkconfig.rename` maps
the old name, so existing configurations keep TCP. Delta and QOI codings, off
by default. Typing from the viewer into a text field on the board
(`O`, `I`, `K`; the LVGL hook finds the focused textarea).
`examples/virtual` runs LVGL on a bare ESP32-S3.

## 0.1.0

First release: the component (frames, keys, LED, audio both ways, the LVGL 9
hook), the desktop viewer for macOS with four device profiles, two examples.
