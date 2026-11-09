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

Viewer preferences persist window defaults, capture folders, startup logging
and connection targets. CLI options override saved startup preferences.
Capture and log failures are isolated from the connection. Offscreen viewer
CI covers macOS, Linux and Windows; capture defaults use platform Desktop
paths. Virtual presets supply device names, geometry and keys.

LVGL board implementations add Waveshare AMOLED 2.16 and 1.75C, LCD 3.49 V2,
e-Paper 1.54G, and P4 LCD 3.5, with board-specific panel, input and audio paths.
The P4 example needs ESP-IDF 6.0.3. The round UI fits inside the panel.
Example READMEs distinguish builds from hardware validation. Round screen metadata and viewer sizing use active diameter.

QOI and row-only RLE reuse successful trial output. USB queue items retain
only payload sizes accepted by the configured build. Host benchmark results
at 240 and 480 pixels are documented separately from hardware measurements.

Optional ESP-Iris screen backend reuses the mirror framebuffer. Iris pointer
and key RPCs feed the existing input path; board metadata is available by RPC.
The viewer accepts `iris:<device-id>`, discovers devices with `--iris`, and
receives encoded dirty rectangles through a running Gateway's IMAGE stream.
Chunk ACKs prevent latest-slot overwrite; device/Gateway drops request a fresh
delta reference. The viewer retains raw SCREEN support for older firmware. Iris carries screen and input;
audio, LED and typing remain on serial/TCP. A mirror-only ESP-Mosaico example
targets ESP32-S31 and retains the Vibe Mode 0.2 partition layout.
Its optional physical variant drives the CO5300 panel, CST9220 touch, FN
and BOOT; viewer POWER toggles display state.

## 0.1.0

First release: the component (frames, keys, LED, audio both ways, the LVGL 9
hook), the desktop viewer for macOS with four device profiles, two examples.
