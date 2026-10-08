# Device profiles

The viewer draws the board as the physical device: the body in millimetres,
the glass, the keys where they are on the case, the LED. The board names
itself in the `B` message (`espmirror_config_t.name`); the viewer looks for a
fragment of that name in `DEVICES` in `tools/mirror_window.py` and takes the
first match. No match: a plain frame around the screen, with no keys.

Built in:

| Fragment | Device | Body | Keys |
|---|---|---|---|
| `2.16` | Waveshare ESP32-S3-Touch-AMOLED-2.16 | white box, 44.6 mm | KEY, PWR, BOOT on the top edge |
| `1.54G` | Waveshare ESP32-S3 e-Paper 1.54G | white, 39.8 × 53 mm, green LED | BOOT and PWR on the right edge, held keys |
| `1.75C` | Waveshare ESP32-S3-Touch-AMOLED-1.75C | round, silver, 55 mm | KEY and BOOT on the rim |
| `AIPI` | XOrigin AIPI Lite | red slab, 55.5 × 47.5 mm, corner studs | Power and Talk on the bottom edge, held keys |
| `3.49` | Waveshare ESP32-S3-Touch-LCD-3.49 (case B) | blue bar, 98.5 × 34.8 mm, the 640 × 172 panel sideways | brightness and power, on the back of the case (drawn on the bottom edge) |
| `3.5` | Waveshare ESP32-P4-WIFI6-Touch-LCD-3.5 | black case, 95.11 × 63.67 mm | PWR and BOOT on the bottom edge (RST between them is not a key) |

## Adding one

One entry in `DEVICES`, all sizes in millimetres from the maker's drawing:

```python
("MYBOARD", dict(
    screen_mm=27.8,                    # the active area's width (the mirror's width in pixels maps to it)
    body=dict(shape="rect", w=39.8, h=53.0, x=6.0, y=10.9, r=4.5, finish="white"),
                                       # the case: size, where the active area's top left corner is, the
                                       # corner radius, a finish from FINISHES ("white", "silver", "red")
    glass=dict(border=0.8, r=0.6),     # the dark border around the active area, its corner radius
    keys=[("boot", 2, "right", 31.1, 5.5), ("pwr", 3, "right", 41.3, 5.5)],
                                       # label, key code (1, 2, 3), edge ("top", "bottom", "right" or
                                       # "angle" on a round body), position along the edge (or degrees),
                                       # length
    hold=True,                         # keys are held while the mouse is down (push-to-talk boards);
                                       # False taps them
    led=(5.5, 46.5, 1.6, (0x30, 0xd0, 0x60)),   # optional: centre x, y, diameter, colour
)),
```

A round body: `body=dict(shape="round", d=55.0, finish="silver")`, keys with
`"angle"` and their position in degrees clockwise from the top.

The screen's pixel size and corner radius come from the board (`width`,
`height`, `corner_radius` in `espmirror_config_t`); `ppi` lets View > Actual
size show the device at its real size.

## Looking at a profile without the board

`tools/fake_board.py "Waveshare 1.54G" 200 200` serves the protocol on
127.0.0.1 with a test pattern; `tools/mirror_window.py 127.0.0.1` then
shows the profile the name picks, and the fake board prints what the viewer
sends (keys, pointer, listen).

## A photograph instead of a drawing

Add `photo=dict(file="devices/<name>.png", screen=(x, y, w, h))` to the
profile: a front-on photograph of the device with the background cut out,
and the screen's active area in the photo's pixels. The six built-in profiles carry mock-ups an image model made from
photographs of the devices (straight on, screen off, background removed),
stretched to the drawings' millimetres; a photograph of the real thing is
welcome in their place. The viewer draws the
photograph as the body, scaled so its width is `body.w` millimetres, puts
the live screen into the rectangle, and keeps the keys and the LED where the
millimetres say (a pressed key is shaded over the photo). How to shoot one:
`tools/devices/README.md`.

## Finishes

`FINISHES` holds the colours of a body: the fill, a second fill for the
shading, the edge, and the keys up and down. Add one and name it in a
profile.
