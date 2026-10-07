# Contributing

## A device profile

The most useful contribution: the viewer drawing your board as the device it
is. It is one entry in `DEVICES` in `tools/mirror_window.py`
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

## A viewer for another platform

The protocol is in docs/protocol.md and fits on a page. A viewer for Linux
or Windows is welcome as `tools/mirror_window_<platform>.py` or a separate
project linked from the README; keep the device profiles in one place.

## Firmware

Keep the component free of display libraries: the LVGL hook is a separate
file compiled only when LVGL is in the build; an e-paper or a camera path is
`espmirror_draw()` from your code. Changes to the wire need a note in
docs/protocol.md and must keep older viewers working, or bump the hello.

Build both examples for `esp32s3` and `esp32` before a pull request; the
workflow does the same on ESP-IDF 5.4 and 6.0.
