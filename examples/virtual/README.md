<!-- Copyright (c) 2026 Maksim Nagaev -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Virtual display

A bare ESP32-S3 runs LVGL 9.5; the desktop mirror is its display and touch
input. No panel, codec or Wi-Fi setup. The first page has an animated bar
and a counter. Swipe left to a scrolling list, and again to a text field
with LVGL's keyboard: tap the field and type on the computer, or on the
keyboard on the screen. Enter moves the line under the field.

The keys exist in the viewer: BOOT (2) counts, as do the on-screen Tap
button and BOOT on the module, the only key the module has. POWER (3) darkens
the screen and brings it back, the third key (1) turns to the next page. The
keyboard's 1, 2 and 3 press them too.

## Emulating a device

One S3 stands in for any device of the same family: its screen at its size
and shape, its keys, LVGL and ESP-IDF as on the real thing. In `menuconfig`,
Virtual display, Device picks one:

| Device | Screen | Touch | Keys |
|---|---|---|---|
| Your own | width, height, round, diagonal, name you set | yes | BOOT, POWER and FN, laid out as the ESP-Mosaico's |
| Waveshare ESP32-S3-Touch-AMOLED-2.16 | 480 × 480 | yes | KEY, BOOT, PWR |
| Waveshare ESP32-S3 e-Paper 1.54G | 200 × 200 | no | BOOT, PWR |
| Waveshare ESP32-S3-Touch-AMOLED-1.75C | 466 × 466, round | yes | KEY, BOOT |
| Waveshare ESP32-S3-Touch-LCD-3.49 | 172 × 640 | yes | BOOT, PWR |
| Waveshare ESP32-P4-WIFI6-Touch-LCD-3.5 | 480 × 320 | yes | PWR, BOOT |
| XOrigin AIPI Lite | 128 × 128 | no | PWR, BOOT |
| Espressif ESP-Mosaico | 480 × 480 | yes | BOOT, POWER at the bottom, FN at one o'clock |
| Seeed SenseCAP Watcher | 412 × 412, round | yes | WHEEL, SCROLL |

The name a device gets (`Virtual Waveshare 1.75C`, for instance) makes the
viewer draw its body from `tools/espmirror.py` `DEVICES`, its keys where the
body has them; View, Actual size (Cmd 0) shows it at its real size. The
ESP-Mosaico and the SenseCAP Watcher have no body there yet and get a plain
frame with the keys at their pixel hints. For your own device set the screen
in Virtual display and edit the `KEYS` of the last case in `main/main.c`:
each key is a code (1 to 3 here), a label, and either `-1, -1` for the body's
place or a pixel near the edge where the viewer should draw it. A body of
your own is one entry in `DEVICES` (docs/devices.md).

Screens bigger than 240 × 240 need PSRAM: add `sdkconfig.psram` (octal, as on
the XIAO ESP32S3 and the DevKitC-1 N8R8). `sdkconfig.mosaico` picks the
ESP-Mosaico with PSRAM in one go:

```sh
rm -f sdkconfig
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.mosaico" build
```

Use the XIAO ESP32S3's USB connector. On the ESP32-S3-DevKitC-1 N8R8, use
the connector marked USB, connected directly to the S3, not the UART bridge.
GPIO0 is BOOT on both boards. Release it while resetting so the application
boots instead of the ROM downloader.

```sh
cd examples/virtual
idf.py set-target esp32s3
idf.py menuconfig
idf.py -p /dev/cu.usbmodemXXXX flash
```

In `menuconfig`, Virtual display picks the device and the BOOT GPIO; for
your own it sets the width, height, round shape, diagonal and name.
Diagonal uses hundredths of an inch: 200 is 2.00 inches. Zero leaves the
physical size unspecified. Width and height range from 128 to 640; round
screens normally use equal dimensions.

Then, from the repository root:

```sh
python3 -m pip install -r tools/requirements.txt
python3 tools/espmirror.py --log virtual.log
```

Or pass the native USB port explicitly, such as `COM7` on Windows or
`/dev/ttyACM0` on Linux. Close `idf.py monitor` before connecting the viewer.
The component owns the console reader and recognizes `mirror` and `>mirror`.
Logs appear in the viewer's terminal and in the optional log file.

The default 240 by 240 screen uses a 115,200-byte mirror framebuffer and an
11,520-byte partial LVGL draw buffer. It does not enable PSRAM; these defaults
can use the S3's internal RAM. Measured on a XIAO ESP32S3 at 240 by 240 with
USB and TCP: `espmirror_start()` takes 207 KB of internal RAM without PSRAM,
which leaves Wi-Fi too little to start, and 55 KB with PSRAM, which runs
Wi-Fi and the mirror together. Larger screens, especially 466 by 466 or
640 by 640, need PSRAM enabled for the particular module under Component
config, ESP PSRAM. The component prefers PSRAM when available. Enabling delta
adds another width times height times two bytes. Allocation failure stops
startup instead of advertising a display with missing buffers.

The flash layout uses the first 4 MB, so it also works with the larger flash
on these modules. Adjust flash and PSRAM settings to match your module before
raising the display size. No board pin map or panel driver is used.

The example keeps every LVGL call in the application task. Mirror key
callbacks enqueue presses; BOOT is polled and debounced for 30 ms there.
The LVGL adapter consumes the mirror's queued press, movement and release
edges. The flush callback only acknowledges the buffer after the mirror
has copied it.

## Other transports

The default is the USB Serial/JTAG port. Two more, alone or together:

```sh
# USB network: TinyUSB serial port plus a network on the same cable, the board at 192.168.7.1.
# Remove an earlier sdkconfig first: its values win over the new defaults.
rm -f sdkconfig
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.usbnet" build
# Wi-Fi: Component config > ESP PSRAM on, then Virtual display > Join a Wi-Fi network
idf.py menuconfig
```

With the USB network the viewer still finds the board by itself, over its
TinyUSB serial port; `python3 tools/espmirror.py 192.168.7.1` goes over the
network instead. That build takes the port away from USB Serial/JTAG: hold
BOOT while plugging in to flash it again, and the boot log is on the UART
pins. With Wi-Fi the board logs its address; pass it to the viewer, or add
`--wifi` for mDNS. Audio and optional delta/QOI are off.

CI builds this example on ESP-IDF 5.4 and 6.0.1 for ESP32-S3, and the USB
network and ESP-Mosaico screen builds on 6.0.1. Physical BOOT, USB line behavior, memory headroom and
animation timing require hardware validation; host checks do not establish
those properties.
