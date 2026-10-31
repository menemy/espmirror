# espmirror

The screen of your ESP32 board on your desktop, live, drawn as the device it
is: the body, the buttons, the LED. Click the buttons, hear what it plays,
talk into its speaker, record a video, take a screenshot at the real size.
An ESP-IDF component on the board, one Python window on the computer: a USB
cable by default, a network over the same cable or Wi-Fi as options.

<p align="center">
  <img src="docs/demo.gif" width="440" alt="The LVGL example on a Waveshare 1.54G devkit, mirrored on a Mac and drawn as the device: the keys light LEDs on screen, the on-screen buttons play sounds, the spectrogram scrolls, the board's LED on the body lights while a sound plays">
</p>

That is `examples/lvgl` on an XOrigin AIPI Lite. The viewer's keyboard
presses the device's keys, the mouse presses the buttons on screen, the
sounds the board synthesises play on the Mac (and on the board's speaker),
the board's microphone goes to the spectrogram and to the Mac, and the LED
on the drawn body follows the board's.

Why: a board on the desk is small, far from the screen you are reading logs
on, and impossible to show to someone on a call. With the mirror the device
is a window: you see what it shows, press what it has, and the recording
goes into the bug report.

## What it does

- **Frames** of changed rectangles, run-length coded, 25 a second at most;
  an idle board sends one empty 8-byte frame a second. A 240 × 240 user
  interface costs about 20 KB/s while it animates and next to nothing while
  it does not.
- **Keys and touch**: the viewer's drawn buttons and the keyboard press the
  board's buttons, tapped or held; the mouse on the glass is a touch. The
  firmware gets a callback for each.
- **LED**: the board reports it, the viewer lights it on the body.
- **Audio**: the viewer hears the board's speaker (clicks, cues, replies) or
  its microphone, and can talk into the board's speaker. Off at build time
  if you don't want it.
- **The viewer** is PySide6, written for Windows, Linux and macOS and tested on
  macOS so far: no title bar, the device and its buttons, real size on any
  display (it reads the display's millimetres), record to mp4, screenshot to
  png, float on top, Dock icon with the live screen.
- **Device profiles**: the viewer knows the Waveshare 2.16 and 1.75C AMOLED
  boards, the 1.54G e-paper, the 3.49 bar LCD, the P4 3.5 LCD and the
  XOrigin AIPI Lite, each drawn
  from an image of the device (mock-ups made from the makers' product
  photographs today; a photograph of yours drops in). Another device is a line of
  millimetres and a picture (docs/devices.md).

## Minimum requirements

| | |
|---|---|
| Chip | an ESP32 with native USB (Serial/JTAG, or USB OTG for the USB network; the S3 has both); any other ESP32 over TCP |
| ESP-IDF | 5.1 or later; the LVGL hook needs LVGL 9.5 or later (from the registry: ESP-IDF 5.4 or later, whose component manager reads LVGL's manifest) |
| RAM | the screen copy `width × height × 2` bytes, the message buffer (32 KB by default, 8 KB at least), 1 KB input, 16 KB for audio when on, 4 KB of task stack |
| PSRAM | not required; used when present (`CONFIG_ESPMIRROR_PREFER_PSRAM`) |
| Computer | Python 3.11+ and PySide6 on macOS (tested), Linux or Windows (not tested yet); `ffmpeg` for recordings |

Version 1 baseline RAM, excluding the v2 USB queue and optional delta reference:

| Screen | Copy | With defaults (32 KB messages, audio) | Fits without PSRAM? |
|---|---|---|---|
| 128 × 128 | 32 KB | 85 KB | yes |
| 240 × 240 | 113 KB | 166 KB | version 1 on an ESP32-S3 with Wi-Fi, usually (with 8 KB messages and no audio, 123 KB); version 2 with Wi-Fi needs PSRAM, below |
| 320 × 240 | 150 KB | 203 KB | PSRAM |
| 466 × 466 | 424 KB | 477 KB | PSRAM |

Measured with version 2 on a XIAO ESP32S3 (`examples/virtual`, 240 × 240, USB
and TCP, no audio): `espmirror_start()` takes 207 KB of internal RAM without
PSRAM, too much for Wi-Fi to start as well, and 55 KB with PSRAM.

Encoder cost depends on dirty area, selected codings, PSRAM and transport.
Host tests do not establish target frame rates.

## Add it to a project

```sh
idf.py add-dependency "menemy/espmirror"
```

or in `main/idf_component.yml`:

```yaml
dependencies:
  menemy/espmirror: "^0.2"
```

Then start the component (Wi-Fi is not required for USB):

```c
#include "espmirror.h"

static void on_key(int key, bool down, void *user) {
    /* 1, 2, 3 as the viewer draws them; hand it to your input path */
}

const espmirror_config_t cfg = {
    .name = "Waveshare 1.54G",   /* picks the device's look in the viewer */
    .width = 200, .height = 200, .ppi = 184,
    .on_key = on_key,
};
ESP_ERROR_CHECK(espmirror_start(&cfg));
```

**With LVGL 9** (the usual case), one more line with the LVGL lock held:

```c
#include "espmirror_lvgl.h"
espmirror_attach_lvgl(NULL);          /* the default display; every flush passes the mirror */
```

**Without LVGL**, draw into the copy yourself:

```c
uint16_t *fb = espmirror_frame_begin();      /* width × height RGB565, locked */
/* ... */
espmirror_frame_end(x, y, w, h);             /* what changed */
```

or `espmirror_draw(x, y, w, h, pixels, stride)` for a ready rectangle (an
e-paper frame, a camera image).

**Audio, LED, state**: `espmirror_audio_capture()` with what the microphone
gives, `espmirror_audio_playback()` with what the speaker gets (both PCM16 at
16 kHz), `espmirror_set_led()`, `espmirror_set_screen_state()` when the
panel dims or sleeps. The viewer's own microphone comes back through
`on_talk` and `on_talk_audio` in the config.

## Transports

Three ways to the viewer; a board can have the cable and a network at once.

**USB serial** (the default). Select `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`
(or console NONE) and `CONFIG_ESPMIRROR_USB=y`. UART and USB OTG CDC consoles
are not supported. The default `CONFIG_ESPMIRROR_USB_AUTO=y` installs the
driver and reads complete `mirror` / `>mirror` commands. It does not interpret
other console input. If your app installs the driver and reads the console,
set `external_console` in the config and call `espmirror_serial_session()`
after reading that line. Do not read the console concurrently with the
session. ESP_LOG records remain visible between CRC-protected frames.
`--log FILE` saves the host log.

**USB network** (`CONFIG_ESPMIRROR_USB_NETWORK=y`, chips with USB OTG such as
the S3). The port becomes a TinyUSB device with a serial port and a network on
one cable, both carrying the mirror. The viewer finds the board by itself on
the serial port; the network takes `python3 tools/espmirror.py 192.168.7.1` or
any other TCP client, at a similar rate. The board is `192.168.7.1`
(`CONFIG_ESPMIRROR_USB_NETWORK_SUBNET` picks the 7; two boards on one computer
need different values) and hands the computer an address with no route: a Mac
kept its default route on its own network. ESP-IDF's DHCP server always names
a DNS server, here the board, which answers no DNS queries; on Windows 11 that
was reported to break name resolution while such a device is plugged in
([esp-idf#19190](https://github.com/espressif/esp-idf/issues/19190)).
Microsoft lists its CDC-NCM driver for Windows 11 and Windows Server 2022, not
Windows 10. Tested on macOS with ESP-IDF 6.0.1 only.

Add `espressif/esp_tinyusb` 2.4.0 or later to your app with
`CONFIG_TINYUSB_CDC_ENABLED=y`, `CONFIG_TINYUSB_NET_MODE_NCM=y`, a product
string containing `espmirror`, which the viewer looks for, and an empty serial
string, which esp_tinyusb turns into the MAC; `examples/virtual/sdkconfig.usbnet`
has the full list. It turns on TCP, so the port also listens on Wi-Fi if the
app joins a network. USB Serial/JTAG is off the port while TinyUSB runs: flash
with BOOT held, and read the boot log on the UART.

**TCP** (`CONFIG_ESPMIRROR_TCP=y`, on port 7331 with mDNS) binds to every
address, so it listens on each network the app brings up, such as Wi-Fi or the
USB network. The app starts Wi-Fi itself. Existing configurations with the
earlier name, `CONFIG_ESPMIRROR_WIFI`, keep TCP through `sdkconfig.rename`.
The port has **no authentication**. Anyone who can reach it can
see the screen, press keys, listen and talk. Leave TCP off in deployed images
unless the network and these capabilities are intentionally trusted.

Delta and QOI are off by default (`CONFIG_ESPMIRROR_DELTA`,
`CONFIG_ESPMIRROR_QOI`). Delta requires another width*height*2 byte reference.
USB input queues add about 33 KiB with audio-sized messages, beyond v1 memory
figures below. Measure actual allocation and frame timing on your board.

## The viewer

```sh
python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt
.venv/bin/python tools/espmirror.py
# or: python tools/espmirror.py COM12
# or: python tools/espmirror.py /dev/ttyACM0
# explicit network target: python tools/espmirror.py 192.168.1.42
```

Drag by the body or the handle at the bottom left, resize by the one at
the bottom right. The View menu: Screenshot (Cmd S), Record (Cmd R), Sound (Cmd L),
Mic (Cmd Shift L), Talk (Cmd K), Bigger, Smaller, Actual size (Cmd 0),
Float on Top (Cmd T). Use Ctrl on Windows/Linux. The keys 1 through 4 press
board buttons, with arrows/Enter/Escape for navigation. Several boards share a
window. Click a board before using the shared audio toolbar. The listen button
selects its speaker; Ctrl+Shift+L selects its microphone. Talk toggles your
microphone; the board may refuse it. OS microphone permission is required.
QtMultimedia handles audio without sounddevice. Talk requests microphone
permission through Qt; denied permission is reported in the window. Device-native audio formats are
converted to/from mono PCM16 at 16 kHz with bounded queues.

Two-finger scrolling becomes a touch gesture with a pointer disc. Show touches
controls the fading touch marks. Screenshots and recordings contain the devices
without the toolbar. `ffmpeg` must be on PATH for mp4 recording. Photos default
to `tools/devices/`; `--devices DIR` overrides them. Discovery is USB-only and
passive. `--wifi` adds mDNS; an explicit address always selects TCP.

`--sound` starts with the board's sound on, `--on-top` floating,
`--stats` prints frame statistics with no window for `--seconds N` (5 by
default); with it, `--png out.png` saves the screen once it is still.
`--scale K` starts the window at K times the real size.

## Examples

- [`examples/virtual`](examples/virtual): bare ESP32-S3, USB only by default,
  LVGL animations, swipe pages and a list. No panel, audio hardware or Wi-Fi
  setup. BOOT and the viewer key count presses.
- `examples/basic`: no display at all; the board draws a counter, a bouncing
  square, the keys you press and where you touch into the mirror. Any devkit.
- `examples/lvgl`: the showcase. Animations, on-screen buttons the viewer's
  mouse presses, LEDs for the keys, three small sounds synthesised on the
  board, a spectrogram, the board's LED. On a board the example knows
  (`menuconfig` → espmirror example → The board) it drives the board's own
  panel, speaker, microphone and buttons too: the XOrigin AIPI Lite today
  (ST7789, ES8311, two buttons). On any other devkit the mirror is the only
  display and the sounds play on the viewer; the board file is one C file to
  add for yours.

```sh
cd examples/basic
idf.py set-target esp32s3
idf.py menuconfig          # Example Connection Configuration: your Wi-Fi
idf.py flash monitor       # then run the viewer with the IP it prints
```

`menuconfig` also picks the board, and the name the board gives the viewer
and the screen size for a devkit; name it after a device the viewer knows and
it is drawn as that device.

## Your device

The viewer draws a board it knows; one it does not gets a plain frame. A
profile is one line of millimetres from the maker's drawing and a
screenshot: see [CONTRIBUTING.md](CONTRIBUTING.md), or open an
[issue](https://github.com/menemy/espmirror/issues/new?template=device.yml)
with the drawing and somebody with the board will add it.

## How it works

The board keeps a copy of the screen (RGB565) and a list of dirty
rectangles. Every 40 ms the mirror task codes the dirty rectangles in the
shortest of raw, row runs and column runs, and sends one message. The viewer
applies them to its own copy and repaints the glass. While nothing changes,
one empty frame a second. One viewer at a time; a new one takes over, the
old one is told.

The LVGL hook listens to `LV_EVENT_FLUSH_START` and copies the flushed area
out of the active draw buffer before the display driver swaps bytes for the
panel, so it works with any driver that renders RGB565, in partial, direct
or full render mode, native or byte-swapped, and never touches the buffer
(a display offset set with `lv_display_set_offset` is not taken out).

The wire is in [docs/protocol.md](docs/protocol.md). Device profiles in
[docs/devices.md](docs/devices.md).

## Limits

- Audio from the board is a debugging feature: anything the microphone
  hears goes to whoever runs the viewer on the local network. Leave
  `CONFIG_ESPMIRROR_AUDIO` off in a production image, or the component out.
- No authentication: the mirror is for a board on your bench and your
  network.
- RGB565 only.

## License

Apache-2.0. The device images in the viewer are mock-ups derived from the
makers' product photographs and published dimensions; the device names and
designs are theirs.
