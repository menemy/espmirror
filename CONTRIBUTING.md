# Contributing

## A new device

New devices are welcome. A pull request with the parts below is quick to
review; one device per pull request.

1. **The drawing.** Sizes come from the maker's dimensional drawing: the body,
   where the screen's active area sits, the buttons on the edges, the LED.
   Millimetres, not pixels. Link the drawing in the pull request. A profile
   measured by eye or from a product photo is not merged.
2. **The profile.** One entry in `DEVICES` in `tools/espmirror.py`;
   docs/devices.md explains every field. Its fragment is a piece of the name
   your firmware passes in `espmirror_config_t.name` that picks this profile
   and no other.
3. **A preset.** One entry in `PRESETS` in `tools/fake_board.py`: the same
   name, the resolution, the diagonal, touch and the keys. CI draws every
   preset as the viewer does (the `profile-previews` artifact of the `viewer`
   job), and the job summary lists which profile each name picks. Locally:
   `python tests/preview_profiles.py previews`.
4. **An image, if you have one.** Your own photograph shot as
   tools/devices/README.md describes, or an AI-generated image; say which in
   the pull request. Under 1.5 MB. Not a picture from a shop or a maker's
   site. Without an image the viewer draws the body from the millimetres.
5. **Checked on the board, or not.** Either run the viewer against the board,
   press every key and put a screenshot in the pull request (not in the
   repository), or say it was drawn with `fake_board.py` only. Both are
   merged.

Not in a device pull request: changes to the viewer, the protocol or the
firmware, a second device, refactoring. Send them separately. Arduino
support and new transports: open an issue first.

No board? Open an issue with the "Add a device" template and the drawing;
somebody with the board can finish it.

## Bugs

Open an issue with the "Bug" template: the computer's OS, the connection
(USB serial, USB network, TCP or ESP-Iris), the chip, the ESP-IDF version and
the viewer's log (`--log FILE`).

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
