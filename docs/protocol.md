# The wire

One TCP connection from the viewer to the board, port 7331 by default
(`CONFIG_ESPMIRROR_PORT`). Every message is a 4-byte little-endian length
followed by the bytes. The viewer speaks first.

```
viewer → board   "MIRR1\n"          the hello, no length prefix, within 2 s
board → viewer   'B' + JSON          the board: {"forms":[],"board":{"name","w","h","ppi","r"}}
board → viewer   'F' ...             frames, from then on
```

One viewer at a time. A second connection that says hello takes the screen:
the first one gets `'T'` and is closed. A new viewer is admitted at most once
a second.

## Board to viewer

| Byte | Then | Meaning |
|---|---|---|
| `F` | state u8, rects u16, width u16, height u16, then the rectangles | A frame: the changed rectangles since the last one. `state` is 0 bright, 1 dim, 2 dark (`espmirror_set_screen_state`). An empty frame goes out once a second while nothing changes, so the viewer knows the board is alive. |
| `B` | JSON | The board, once after the hello. `r` is the panel's corner radius in pixels; half the size or more means a round panel. |
| `T` | | Another viewer took the screen; this connection is closed after it. |
| `A` | source u8, PCM | Audio while the viewer listens: source 1 the microphone, 2 the speaker; 1280 bytes of PCM16 mono 16 kHz per message (40 ms). |
| `L` | mode u8 | The LED: 0 off, 1 blinking slowly, 2 blinking fast, 3 on. On each change and once when the viewer connects. |

A rectangle inside `F`:

```
x u16, y u16, w u16, h u16, coding u8, 0 u8, length u32, then `length` bytes
```

| coding | bytes |
|---|---|
| 0 | raw RGB565, little-endian, row by row |
| 1 | run-length pairs `count u16, colour u16` in row order |
| 2 | the same pairs in column order (vertical bars code far smaller that way) |

The board picks the shortest of the three for each rectangle. A rectangle
over 60 000 pixels, or one that does not fit the message, goes out in
horizontal bands; what does not fit one message waits for the next.

All integers are little-endian. One frame is one message; a message is at
most `CONFIG_ESPMIRROR_MSG_KB` KB.

## Viewer to board

| Bytes | Meaning |
|---|---|
| `1, x u16, y u16` / `0, x u16, y u16` | Pointer down / up at screen pixels. The board ignores these; a firmware with a touch screen may wire them. |
| `3, key u8` | A tap: the key goes down and comes up 150 ms later. Keys are 1, 2, 3 as the viewer draws them; 4, 5, 6 are the same keys held long on a firmware that tells them apart (here: a plain press). |
| `6, key u8, 1/0` | The key held down or let go, as the viewer's mouse does it. |
| `7, source u8` | Listen to the microphone (1), the speaker (2), or stop (0). |
| `8, 1/0` | Talk through the board's speaker, or stop. The board may refuse. |
| `S, format u8, PCM` | The viewer's microphone while it talks: format 1, PCM16 mono 16 kHz, up to 20 ms per message. |
| anything else | Keeps the connection alive. |

A message from the viewer is at most 1024 bytes and must arrive whole within
2 s; the connection is dropped otherwise. The board lets go of every key the
viewer held and stops the audio when the viewer leaves.
