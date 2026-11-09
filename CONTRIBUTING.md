# Contributing

## A device profile

The most useful contribution: the viewer drawing your board as the device it
is. It is one entry in `DEVICES` in `tools/espmirror.py`
(docs/devices.md explains every field) and a screenshot.

1. Find the maker's dimensional drawing: the body, where the active area of
   the screen sits, the buttons on the edges, the LED. Millimetres, not
   pixels.
2. Add the entry. Pick a fragment of the name your firmware passes in
   `espmirror_config_t.name` that no other profile matches.
3. Run the viewer against the board, press every key, take a screenshot
   (Cmd S) and put it next to the others in `docs/images/`.
4. Open a pull request with the entry, the screenshot and a link to the
   drawing. Mention the key codes your firmware maps (1, 2, 3).

No board? Open an issue with the "Add a device" template and the drawing;
somebody with the board can finish it.

## Viewer

The PySide6 viewer has offscreen CI configured for macOS, Linux and Windows;
see the README's platform checks for coverage and hardware limits.
Keep its device profiles in one list. Run
`python -m unittest discover -s tests` before a pull request; PySide6 enables
the offscreen rendering test. Exercise microphone permission and playback on
the platforms your change affects.

## Firmware

Keep the component free of display libraries: the LVGL hook is a separate
file compiled only when LVGL is in the build; an e-paper or a camera path is
`espmirror_draw()` from your code. Changes to the wire need a note in
docs/protocol.md and must document compatibility and update the protocol version.
A change to the encoders gets a run of `tests/bench_espmirror.py`; docs/bench.md
says what it measures and what it does not.

Build basic and lvgl for `esp32s3` and `esp32`. CI is configured for basic on
ESP-IDF 5.1, 5.4 and 6.0, and lvgl on 5.4.3 and 6.0. The lvgl example needs
ESP-IDF 5.4.3 or later for Kconfig-conditional panel dependencies; its Waveshare S3
variants have 5.4.3 and 6.0.1 jobs. Virtual on `esp32s3` has 5.4 and 6.0.1
jobs, plus its USB network and ESP-Mosaico screen variants on 6.0.1. The virtual
example needs no network credentials or panel.

The Waveshare P4 3.5 example has an ESP-IDF 6.0.3 CI job for `esp32p4`.
