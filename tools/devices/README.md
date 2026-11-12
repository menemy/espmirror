# Device photographs

A profile in `tools/espmirror.py` may point here: `photo=dict(file="<name>.png", screen=(x, y, w, h))`.
The viewer then draws the photograph as the body and puts the live screen into the rectangle; the keys and the
LED stay where the profile's millimetres say.

How to shoot one:

- straight on, the camera square to the front face, from far enough that the edges stay parallel (a longer lens,
  or crop the middle of a wide shot);
- even light, no reflections on the glass; the screen off or black;
- cut the background out (macOS Preview or Photos: Remove Background) and save a PNG with transparency;
- note the screen's active area in the PNG's pixels: `x, y, w, h` of the lit pixels, not of the glass;
- the PNG's width must be the body's width in the profile (`body.w` millimetres): the viewer scales by that.

The images here are AI-generated (gpt-image-1, 2026-10): straight on, the screen off, the background removed.
They are not photographs. A real photograph of your device, shot as above, is better; replace the file and the
screen rectangle and send a pull request.
