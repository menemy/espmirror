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

## Hardware checks

Before marking a board measured: verify cold boot over USB, physical panel
colors and orientation, touch corners, each physical and viewer key, speaker
playback, microphone capture, Talk playback, and reconnect after unplugging
USB. Repeat with `CONFIG_ESPMIRROR_AUDIO=n`. Build success and host tests do
not establish these hardware properties.
