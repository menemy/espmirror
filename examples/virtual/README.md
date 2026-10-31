<!-- Copyright (c) 2026 Maksim Nagaev -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Virtual display

A bare ESP32-S3 runs LVGL 9.5; the desktop mirror is its display and touch
input. No panel, codec or Wi-Fi setup. The first page has an animated bar
and a counter. Swipe left to a scrolling list; swipe right to return.
Tap a button, press BOOT on the module, or click the viewer's BOOT key to
increment the counter. The keyboard's 2 key does the same.

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

In `menuconfig`, Virtual display sets the width, height, round shape,
diagonal, name and BOOT GPIO. Diagonal uses hundredths of an inch: 200 is
2.00 inches. Zero leaves the physical size unspecified. Width and height
range from 128 to 640; round screens normally use equal dimensions.

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
can use the S3's internal RAM. Larger screens, especially 466 by 466 or
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

TCP is disabled. Enabling CONFIG_ESPMIRROR_WIFI alone does not establish a
network connection; applications wanting TCP must add their own network
setup, as in the other examples. Audio and optional delta/QOI are off.
CI builds this example on ESP-IDF 5.4 and 6.0.1 for ESP32-S3. Physical BOOT,
USB line behavior, memory headroom and animation timing require hardware
validation; host checks do not establish those properties.
