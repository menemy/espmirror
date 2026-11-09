<!-- Copyright (c) 2026 Maksim Nagaev -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# LVGL device example

LVGL runs on the board and mirrors its RGB565 display to the desktop viewer.
The UI has three sound buttons, key indicators and a spectrogram. Physical
touch and the viewer's pointer both operate the UI. View > Sound listens to
speaker playback; View > Mic starts microphone capture. View > Talk sends the
computer's microphone to the board's speaker. Talk requires a working codec
and `CONFIG_ESPMIRROR_AUDIO`.

| Device | Display | Touch | Keys (viewer code) | LED | Audio | Measured on hardware |
|---|---|---|---|---|---|---|
| Any devkit | Virtual, configurable | Viewer | Viewer 1, 2, 3 | Viewer indicator | Synthesised playback to viewer | Not yet |
| XOrigin AIPI Lite | ST7789, 128 × 128 SPI | None | BOOT/talk (2), PWR (3) | Viewer indicator only | ES8311 speaker and mic | Not yet for this revision |
| Waveshare ESP32-S3-Touch-AMOLED-2.16 | CO5300, 480 × 480 QSPI | CST9220 | KEY (1), BOOT (2), PWR (3) | No programmable board LED | ES8311 speaker, ES7210 dual mic downmixed to mono | Not yet |
| Waveshare ESP32-S3-Touch-AMOLED-1.75C | CO5300, 466 × 466 round QSPI | CST9217 | KEY (1), BOOT (2) | No user LED | ES8311 speaker, ES7210 dual mic downmixed to mono | Not yet |
| Waveshare ESP32-S3-Touch-LCD-3.49 V2 | AXS15231B, 172 × 640 QSPI | AXS15231B | BOOT (2), PWR (3) | No user LED | ES8311 speaker, ES7210 dual mic downmixed to mono | Not yet |
| Waveshare ESP32-S3 e-Paper 1.54G | 200 × 200, black/white/yellow/red | None | BOOT (2), PWR (3) | GPIO3, active-low user LED | ES8311 speaker and mic | Not yet |
| Waveshare ESP32-P4-WIFI6-Touch-LCD-3.5 | ST7796, 480 × 320 SPI | FT6336 | BOOT (2), PWR (3) | No user LED | ES8311 speaker and mic | Not yet |

## Waveshare AMOLED 2.16

Use ESP-IDF 5.4.3 or later; local builds use 6.0.1. CI includes 5.4.3 and 6.0.1
for this board. The board overlay enables octal PSRAM, native USB
Serial/JTAG and audio, and disables networking. No Wi-Fi credentials are
needed. The flash layout uses the first 4 MB of the board's 16 MB flash.

```sh
idf.py -B /tmp/devices-build/lvgl-216 \
  -D SDKCONFIG=/tmp/devices-build/lvgl-216/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.waveshare-216' \
  -D IDF_TARGET=esp32s3 build
```

Run from this example directory with a fresh build directory when changing
boards. In `menuconfig`, select the board under **espmirror example**. The
overlay is also needed for PSRAM and USB settings; selecting the board alone
does not configure those. The name `Waveshare 2.16` selects its viewer profile.

After flashing with the owner's approval, close `idf.py monitor` and run
`python tools/espmirror.py` from the repository root. The USB connector is
wired to the S3's native USB pins. PWR events come from AXP2101 I2C latches;
the PMU's hardware hold-to-power-off behavior is left unchanged. KEY and BOOT
are active-low GPIO18 and GPIO0, debounced independently for 30 ms. The example
enables ALDO1 at 3.3 V for audio and leaves the other PMU rails and charging
settings unchanged.

The panel requires even update coordinates. LVGL expands invalidated areas
before rendering. A 38,400-byte internal DMA buffer holds 40 display lines;
the spectrogram buffer is allocated in PSRAM when available. Only the sound
task writes to the codec. The mirror callback queues at most four 20 ms Talk
chunks without waiting; excess chunks are dropped. Talk takes precedence over
the example's synthesised sounds. The speaker worker waits for incoming Talk
chunks and lets the codec pace playback, including packets shorter than 20 ms.

Pin assignments, display register values and touch orientation come from the
[Waveshare manual](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-2.16) and
[Waveshare BSP revision bfeb6e6](https://github.com/waveshareteam/Waveshare-ESP32-components/tree/bfeb6e6d5737178cdde78b630c8118074da0a657/bsp/esp32_s3_touch_amoled_2_16).
The example uses Espressif's CO5300 2.2.0 and CST9220 0.1.1 drivers and
`esp_codec_dev`. It does not use the BSP's LVGL task or display buffers.

## Waveshare AMOLED 1.75C

The `sdkconfig.waveshare-175c` overlay selects the 466 × 466 round CO5300
panel and CST9217 touch controller. Use the build command above with that
overlay and a separate build directory. The viewer name is `Waveshare 1.75C`.
The UI occupies a centered 326 × 326 square so that its controls fit inside
the circular panel. The panel and mirror remain 466 × 466, with round geometry
advertised to the viewer. Fonts are the same as on the rectangular boards.

This is the 1.75C board. Its panel reset is GPIO1, touch reset GPIO2 and codec
MCLK GPIO16. The physical PWR button reaches GPIO3 through an inverter and
appears as KEY (1) in the viewer; BOOT on GPIO0 is code 2. GPIO keys are
debounced independently for 30 ms. The panel has a six-pixel column offset.
ES8311 speaker output and ES7210 microphone input share a 16 kHz stereo I2S
bus; the mirror uses mono samples. ALDO1 supplies the codecs at 3.3 V.

The hardware reference is Waveshare's
[1.75C BSP revision 010c5fb](https://github.com/waveshareteam/Waveshare-ESP32-components/tree/010c5fb39ea7f0e996efd1f8e162cc70e569e57f/bsp/esp32_s3_touch_amoled_1_75c).

## Waveshare LCD 3.49 V2

The `sdkconfig.waveshare-349` overlay selects the V2 board with its 172 × 640
AXS15231B panel. The name `Waveshare 3.49 V2` selects the viewer's 3.49 body.
This pin mapping does not support V1 boards.

AXS15231B QSPI transfers use sequential rows, so the example keeps a
220,160-byte full frame in PSRAM and sends it through a 5,504-byte internal
DMA buffer. LVGL renders RGB565 in the panel's byte order; the mirror copies
and converts it before transmission. The touch controller uses a separate
I2C bus. TCA9554 outputs control the power latch, panel reset, backlight enable
and amplifier. BOOT (GPIO0) and PWR (GPIO16) map to viewer codes 2 and 3.

The hardware reference is Waveshare's
[V2 factory example](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-3.49-V2/tree/main/ESP-IDF/11_FactoryProgram).

## Waveshare e-Paper 1.54G

The `sdkconfig.waveshare-154g` overlay selects the 200 × 200 four-color panel.
LVGL and the viewer retain the full RGB565 animation. Physical panel updates
run in a separate task: RGB565 pixels are mapped to black, white, yellow or
red in a 10,000-byte pending frame. Each completed LVGL frame is copied to a
second 10,000-byte buffer before notifying the panel task. That task snapshots
only completed frames into its DMA buffer and performs a full refresh. Updates
coalesce while a refresh is running. The next refresh starts at least 15
seconds after the previous refresh ends. The panel enters deep sleep between
refreshes. Each refresh logs its elapsed time; hardware duration is not yet
measured.

GPIO17 holds the power latch, GPIO6 enables panel power and GPIO42 enables
audio power. BOOT (GPIO0) and PWR (GPIO18) map to viewer codes 2 and 3. The
active-low user LED on GPIO3 follows the example's playback and idle blink
state. ES8311 supplies both speaker output and microphone input. No touch
controller is present; the viewer pointer operates the UI.

The hardware reference is Waveshare's
[ESP-IDF examples](https://github.com/waveshareteam/ESP32-S3-ePaper-1.54G/tree/main/Example/ESP-IDF_5.5.1).

## Waveshare P4 LCD 3.5

Use ESP-IDF 6.0.3 and `sdkconfig.waveshare-p4-35`, targeting `esp32p4`. The
overlay selects silicon revision 3.x, PSRAM and the existing TinyUSB CDC/NCM
mirror transport. Connect the board's USB OTG connector for the viewer; the
CH343 connector carries the UART console. The C6 firmware is not changed.

```sh
idf.py -B /tmp/devices-build/lvgl-p4-35 \
  -D SDKCONFIG=/tmp/devices-build/lvgl-p4-35/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.waveshare-p4-35' \
  -D IDF_TARGET=esp32p4 build
```

See the [virtual example's USB network instructions](../virtual/README.md)
for the host setup and discovery behavior of the CDC/NCM transport.

The ST7796 panel runs in 480 × 320 landscape orientation. FT6336 touch uses
the same coordinate transform. PWR events come from AXP2101 I2C latches;
BOOT uses GPIO35. ES8311 provides speaker and microphone audio. The mirror
name `Waveshare P4 3.5` selects the 3.5 viewer body.

The hardware reference is Waveshare's
[P4 3.5 BSP](https://github.com/waveshareteam/Waveshare-ESP32-components/tree/3812a4c6146ccba2448695f3df7f24b9d9be1d34/bsp/esp32_p4_wifi6_touch_lcd_3_5).

## Hardware checks

Before marking a board measured: verify cold boot over USB, physical panel
colors and orientation, touch corners, each physical and viewer key, speaker
playback, microphone capture, Talk playback, and reconnect after unplugging
USB. Repeat with `CONFIG_ESPMIRROR_AUDIO=n`. Build success and host tests do
not establish these hardware properties.
