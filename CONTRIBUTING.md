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

The PySide6 viewer is written for macOS, Linux and Windows; only macOS is
tested so far. Keep its device profiles in one list. Run
`python -m unittest discover -s tests` before a pull request; PySide6 enables
the offscreen rendering test. Exercise microphone permission and playback on
the platforms your change affects.

## Firmware

Keep the component free of display libraries: the LVGL hook is a separate
file compiled only when LVGL is in the build; an e-paper or a camera path is
`espmirror_draw()` from your code. Changes to the wire need a note in
docs/protocol.md and must document compatibility and update the protocol version.

Build basic and lvgl for `esp32s3` and `esp32`. CI checks those on ESP-IDF
5.1, 5.4 and 6.0 (LVGL needs 5.4+), and virtual on `esp32s3` with 5.4 and
6.0.1. The virtual example needs no network credentials or panel.
