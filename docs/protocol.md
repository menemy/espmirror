<!-- Copyright (c) 2026 Maksim Nagaev
SPDX-License-Identifier: Apache-2.0 -->
# Mirror protocol version 2

The same payloads work over USB and TCP. Integers and PCM samples are little
endian. One viewer owns the device across both transports. There is no
application authentication or encryption. TCP is an explicit developer option.

## Session and transport

### ESP-Iris

`CONFIG_ESPMIRROR_IRIS=y` registers a screen backend and four RPC methods
with the application's `esp_iris` component. Call `espmirror_start()` before
`esp_iris_start()`. The application owns Iris USB and recovery services.
The viewer uses the existing espmirror encoder/decoder through IMAGE channel
4 when board metadata advertises `iris_codec:1`. Firmware without that field
uses the legacy raw SCREEN path. Other codec versions are rejected.

#### Encoded IMAGE stream

The viewer subscribes to `/v2/devices/{id}/streams/image` before starting
`channel:image` through `/mirror/start`. The description is the board width
and height, zero origin and stride, private format `0x4553`, quality/version 1.
This is an espmirror application format, not a standard Iris pixel format.
A reused IMAGE stream is refused because one viewer owns its ACKs and reference.
The Gateway forwards each chunk unchanged as one binary WebSocket message:
`metadata_size:u32`, UTF-8 JSON metadata, then chunk bytes. Integers are little
endian. Gateway subscriber loss arrives separately as text JSON `history_gap`.

Each chunk is `ESM1[4], epoch:u32, message:u32, offset:u32, total:u32, bytes[]`.
It carries a fragment of an ordinary espmirror `F` payload, including its dirty
rectangles and selected raw/RLE/delta/QOI coding. `total` is 8..65536 bytes;
chunks are at most `iris_chunk` bytes including their 20-byte header. The
firmware advertises `CONFIG_ESP_IRIS_MEDIA_LATEST_BYTES` (256..3840), leaving
up to 3820 bytes for encoded data. The Iris media DATA header adds 36 bytes
outside this limit. Media `frame_id` equals the application message number.
Inner DATA flags bit 0 marks offset zero and bit 1 marks the final chunk;
all other bits are zero. These are application flags, separate from Iris
wire-envelope STREAM_BEGIN/END flags.

The viewer starts with a nonzero random epoch through the stream-control RPC.
The mirror task releases held input, resets its encoder reference and dirties the full screen;
messages start at 1 and advance without gaps. The viewer starts with a zero
reference and waits for full screen coverage before showing it. That coverage
can span several F messages. Only data matching the requested epoch and
negotiated Iris stream ID may update this reference.

Iris push has one latest-chunk slot and no public ready/completion API. The
mirror task therefore submits one chunk, retains the encoded message in its
existing buffer, and waits for an exact `(epoch, message, next_offset)` ACK.
It neither builds another message nor submits another chunk before this ACK.
The prefix of a later chunk occupies only previously acknowledged buffer bytes.
A three-second ACK timeout, submit failure or cable/TCP takeover releases
held input and clears this stream's encoder session. Only a new reset can resume it. RPC handlers enqueue
commands and never wait for encoding or transmission.

A changed device `dropped` counter, Gateway `history_gap`, missing/reordered
chunks, invalid frame, or two seconds without data makes the viewer discard
partial data and request a new epoch. Old queued epochs are ignored. Three
unsuccessful recovery attempts are allowed before reconnecting. Application
message numbers and offsets also detect losses not reflected in `dropped`.
No reference from a failed generation is used for subsequent deltas.

The firmware frame interval is `CONFIG_ESPMIRROR_FRAME_MS`; Iris push does not
pace submissions using the negotiated FPS. ACK round trips also limit throughput.
There is no claimed hardware FPS for this encoded path before device testing.
The transport adds no full framebuffer; optional delta coding uses the core's
existing reference allocation, two bytes per screen pixel.

#### Screenshot and input

The registered SCREEN backend remains available for Iris screenshots and raw
mirror clients. It serves little-endian RGB565, origin 0,0, stride `width * 2`.
`CONFIG_ESP_IRIS_MEDIA_LATEST_BYTES` must hold a full screen row. Reads copy
chunks from the existing framebuffer under its lock; a busy framebuffer returns
a timeout without blocking the Iris protocol task. Iris retries later. The
backend does not freeze drawing across chunks, so raw moving frames may tear.

| Service | Method | Request | Successful response |
| --- | --- | --- | --- |
| `0x1001` (Iris pointer/v1) | 1 | `<BBhhHI>`, 12 bytes | Accepted sample, unchanged |
| `0x4553` (espmirror keys) | 1 | `<BB>`, key code and down | Accepted pair, unchanged |
| `0x4553` (espmirror board) | 2 | Empty | Board JSON plus `iris_codec:1`, `iris_chunk` |
| `0x4553` (stream control) | 3 | `<BIII>`, op, epoch, message, offset | Accepted command, unchanged |
| `0x4553` (timing, optional) | 4 | Empty | Fixed `EMT1` snapshot, see below |

With `CONFIG_ESPMIRROR_TIMING`, method 4 returns a read-only, coherent snapshot.
Counters are cumulative since boot and saturate at `UINT64_MAX`; reading does
not reset them. The header is `"EMT1", count:u8=8, record_bytes:u8=40,
reserved:u16=0, snapshot_monotonic_us:u64`, followed by eight little-endian
records of five u64 values: count, total duration, last event timestamp,
last duration, maximum duration (all times in microseconds). The response is
336 bytes, independent of runtime. Invalid request bodies and insufficient
response capacity fail without returning a partial snapshot.

Record order: frame ready, Iris encoder lock wait, Iris encoder build, chunk
publication, panel present, DMA band done, DMA band wake latency, DMA band wait.
Frame ready counts positive dirty rectangles submitted through frame_end;
its timestamp is before releasing the framebuffer lock, and duration is zero.
Encoder wait covers framebuffer lock acquisition for actual builds; build
covers the core encoder call. Publication covers a successful media_submit call,
with timestamp after it returns (Iris DATA timestamps inside the call).
Mosaico panel present covers a completed 60-band transfer. Band done is the ISR
timestamp before giving the semaphore, with zero duration; wake measures time
from that timestamp to the waiter's return, while band wait measures time spent
in xSemaphoreTake. The semaphore orders the ISR timestamp read; only one DMA
band is outstanding. Snapshot deltas give window counts and mean durations;
maxima and last values describe the lifetime/latest observation, not an exact
per-frame trace. Disabled timing adds no timer reads or counter storage.

Stream op 0 requests a new epoch; message and offset must be zero. Op 1 ACKs
the next byte offset after a received chunk. Epoch must be nonzero. ACKs from
old epochs, messages or offsets cannot advance the sender. RPC success means
queue acceptance; data carrying the new epoch confirms a reset was applied.

Pointer phases are 0 begin, 1 move, 2 end. Reserved fields must be zero;
coordinates must be within the display. Sequence is echoed unchanged. Keys
are 1..10 and down is 0 or 1. Malformed requests are rejected before enqueueing;
a full queue returns an error. Input RPCs require SCREEN or IMAGE streaming
and are rejected while a cable/TCP viewer owns input. Stream control requires
IMAGE. Observed stream end or takeover releases held input and invalidates
queued samples. `espmirror_connected()` includes either active mirror; a
one-shot screenshot does not make it connected.

This mapping carries screen, pointer and keys. LED, audio and text remain on
serial/TCP; the Iris viewer disables those controls. Board metadata is available
without an active stream. RPC_BODY_BYTES must accommodate its JSON and Iris
capability fields; startup checks the configured response capacity.

### Serial and TCP

TCP listens on port 7331 by default when `CONFIG_ESPMIRROR_TCP=y`. The app
establishes its network connection. mDNS advertises `_musemirror._tcp`, TXT
`version=2`, `width`, `height`, and `mac`. This shared service name and the hello
`MIRROR1\n` are retained from Muse for compatibility. The protocol version is
in `B`; it does not change that eight-byte greeting. The hello must arrive
within two seconds. Afterwards both directions use `length:u32 + payload`.

USB Serial/JTAG is the default on supported targets with the console configured
for native USB Serial/JTAG or disabled. UART and USB OTG CDC consoles are not
supported; `espmirror_serial_session()` returns `ESP_ERR_NOT_SUPPORTED`.
With no usable transport, start returns that error too.

With `CONFIG_ESPMIRROR_USB_NETWORK=y` the native USB port is a TinyUSB device
instead: its CDC-ACM serial port carries the serial session below unchanged,
and its CDC-NCM network reaches the TCP port at `192.168.N.1`. The component
owns TinyUSB and runs the line reader itself; `external_console` is refused.
The application's esp_tinyusb settings give the product string, which must
contain `espmirror` so a viewer tells the board from other Espressif VID
devices, and an empty serial string, which esp_tinyusb 2.4.0 and later turns
into the MAC.

A host opens the USB serial port, sends four FF bytes, waits 500 ms, and sends
`\n>mirror\n`. The FF bytes terminate an old session; they are not an electrical
BREAK. An application's console handler may recognize the line `mirror` and
call `espmirror_serial_session()` in its own task. While the call runs no other
task may read the USB console. With `CONFIG_ESPMIRROR_USB_AUTO=y`, the component
installs the USB driver and runs a small line reader accepting `mirror` and
`>mirror`; other lines are ignored. Set `external_console=true` when the app
installs the driver and owns console reads. With AUTO off and external_console
false the component installs the driver but leaves line reading to the app.

The board sends marker `00 4d 49 52 52 4f 52 31 0a` (`\0MIRROR1\n`). Host timeout
is five seconds. Board-to-host messages then use:

| Field | Bytes |
|---|---|
| SYNC | A5 5A |
| Length | u32, payload bytes |
| Payload | length bytes |
| CRC32 | u32, over length bytes followed by payload |

CRC is reflected CRC32, polynomial EDB88320, initial/final complements, API
seed zero. It matches `zlib.crc32(length_bytes + payload)` and chained
`esp_rom_crc32_le`. Payload `R` encodes as `a55a010000005275ee277e`.
Host-to-board messages retain plain `length:u32 + payload`, maximum **1024**
bytes, to carry microphone PCM. All message fragments have a two-second
receive deadline. Six seconds without a complete host message ends USB;
host keepalive `02` is sent after about five idle seconds.
The USB input queue retains up to 64 payload bytes without audio, or 642 with
audio. Larger payloads within the 1024-byte framing limit are consumed with
the same receive deadline and ignored; they do not become console input.

Each USB frame and each formatted ESP_LOG record holds the same TX mutex.
USB writes are at most 128 bytes and account for partial acceptance. Logging
has a 50 ms lock wait and a 50 ms TX budget; dropped records are counted.
ROM output and direct printf are not covered by the log hook and can corrupt
a frame. Do not call mirror APIs from an ESP_LOG formatting callback.

The host demultiplexes logs between frames, prints timestamped port-prefixed
lines, and optionally appends them with `--log FILE`. It validates CRC before
applying a message, accepts cable payloads 1..65536, and bounds buffered logs.
Bad lengths, CRC or a frame incomplete after three seconds trigger SYNC
resynchronization and `R` refresh. If `B` is lost the host reconnects.

On admission the board sends `B`, its current LED, then full screen coverage.
An admitted replacement sends the old viewer `T` and releases its input and
audio. A pending TCP hello does not displace a viewer. TCP admissions are
limited to one per second. The mirror task alone performs admission, sends
and input processing. USB RX queues up to 32 messages; repeated motion may
be dropped under pressure, but press/release/key edges wait up to three seconds.
The console caller waits for the mirror task to acknowledge the final queued
BYE before draining trailing bytes and restoring the log hook. Normal cable
end sends `E`; broken TX may prevent delivery. `E` permits reconnect; `T`
requires explicit take-back. Host writers cannot outlive their session.

## Board metadata

`B` followed by UTF-8 JSON, no NUL:

```
{"version":2,"name":"Virtual S3","mac":"aa:bb:cc:dd:ee:ff",
 "w":320,"h":240,"round":false,"diag":2.0,"touch":true,
 "keyboard":false,"audio":true,
 "buttons":[{"role":2,"name":"BOOT","x":-1,"y":-1}]}
```

Required geometry is `w`, `h`. `diag` is in inches: the diameter for a round
screen, the diagonal otherwise. When deriving it from `ppi`, firmware uses
`min(w,h)/ppi` for round screens and `hypot(w,h)/ppi` otherwise.
`name`, `mac`, `round`, `diag`, `touch`,
`keyboard`, and `buttons` share Muse's flat layout. `version` and `audio` are
optional additions; absent audio means unavailable. `(-1,-1)` has no position
hint. Keys 1..10 are passed unchanged to callbacks. Labels identify board
functions. Existing espmirror API constants remain AUX=1, MAIN=2, POWER=3;
Muse uses talk=1, aux=2, power=3, speaker=4, navigation=5..10. The wire does not
remap these numbers. Applications should supply button labels and positions.

## Board payloads

| Type | Body |
|---|---|
| F | state:u8, rects:u16, width:u16, height:u16, rectangles |
| L | mode:u8: 0 off, 1 slow blink, 2 fast blink, 3 on |
| A | source:u8 (1 mic, 2 speaker), PCM16 signed mono 16000 Hz |
| T | no body; another viewer took ownership |
| E | no body; cable session ended |
| O | open:u8: 1 while a text field is open on the board, else 0 |

F state is 0 bright, 1 dim, 2 dark. Empty F heartbeats carry state each second.
LED state is sent when changed and on admission. A normally contains 1280 PCM
bytes (40 ms); the host accepts even-length chunks. Up to four audio messages
may be sent between frame passes. Audio is available only with
`CONFIG_ESPMIRROR_AUDIO`; the app supplies capture/playback samples.

Each F rectangle is `x:u16,y:u16,w:u16,h:u16,coding:u8,reserved=0:u8,length:u32`
then its payload. Coordinates are inclusive-origin, with nonzero dimensions
inside the screen. RGB565 is R bits 15..11, G 10..5, B 4..0.

| Coding | Payload |
|---|---|
| 0 | raw RGB565 u16, row order |
| 1 | (count:u16, pixel:u16) runs in row order |
| 2 | the same runs in column order |
| 3 | row-order runs of current XOR previous RGB565 |
| 4 | QOI operations on RGB888, no header or end marker |

Run counts must be nonzero and sum exactly to w*h. Runs may cross traversal
line boundaries. QOI expands RGB565 by bit replication, uses fresh zero RGBA
index entries and previous pixel (0,0,0,255), and supports RGB, RGBA, INDEX,
DIFF, LUMA and RUN. Output must be opaque. Exactly w*h pixels and all payload
bytes must be consumed. Delta and QOI are independent opt-in Kconfig options.
The encoder tests each enabled candidate with an early size cutoff and emits
the shortest. QOI and row-only band attempts write into the available output
space during their trial. If that trial wins, no second encoding pass is needed;
otherwise the selected codec overwrites the trial. At most four linear candidate passes plus
one emission and reference copy per rectangle attempt; no candidate-size scratch allocation. Whole attempts
are limited to 60000 pixels before capacity-sized bands are used.

Delta reference starts black and advances after every emitted rectangle,
including absolute and overlapping rectangles. A lost cable frame invalidates
that reference. On `R`, board and host reset their copies to black and the board
marks the whole screen dirty and repeats B before sending more F messages.
That B is the reference boundary after any old in-flight frames. If framing cannot recover, reconnect and a new B reinitializes it.
There is no frame sequence or full-screen marker; the host tracks coverage.
Unsent tails precede new dirty rectangles. Default frames are at most 32 KiB,
configurable 8..64 KiB; TCP host limit is 1 MiB. A send deadline is three seconds.

## Host payloads

| Bytes | Action |
|---|---|
| 00 x:u16 y:u16 | pointer release |
| 01 x:u16 y:u16 | pointer press or move |
| 02 | keepalive |
| 03 key:u8 | tap, released after about 150 ms |
| 06 key:u8 down:u8 | held/released, independently of pending tap |
| 07 source:u8 | listen: 0 off, 1 microphone, 2 speaker |
| 08 enabled:u8 | request talk through the speaker |
| S 01 PCM | signed PCM16 mono 16000 Hz, at most 320 samples (20 ms) |
| R | reset reference and refresh entire screen |
| O | this viewer types: report text fields with O |
| I UTF-8 | insert 1..63 bytes of whole UTF-8 characters, no NUL, at the field's cursor |
| K edit:u8 | 1 Backspace, 2 Delete, 3 Left, 4 Right, 5 Enter |

The application may refuse talk. There is no talk-acceptance response in this
version; an accepted session calls on_talk_audio. Disconnect stops listen/talk,
releases held keys, and queues pointer release. Pointer press, first movement,
and release are separate LVGL samples; later motion samples may coalesce.
Out-of-range coordinates and unknown payloads have no input action.

## Typing into a text field

A viewer that types sends `O` after each `B`. Until then the board sends it no
`O`, so viewers that do not type never see one, and boards that do not know
`O` ignore it. The board then sends `O` with the current state and again on
every change. While a field is open the viewer sends printable keys (digits
included), Backspace, Delete, Left, Right, Enter and a paste (Cmd/Ctrl+V,
split at character boundaries) to the board under the mouse, as `I` and `K`;
with no field open the keys press the board's buttons as before. Esc is taken while a
field is open and does nothing. The viewer logs no typed or pasted text.

The board drops `I` and `K` while no field is open, and malformed ones always.
They wait in a queue of 32 for the UI task; a full queue for three seconds
ends the session, and a new or departing viewer empties it. The LVGL adapter
looks for a visible focused textarea on the top layer, then on the active
screen, every 20 ms while a viewer is connected. It inserts with LVGL's own
accepted-character and length rules. Enter sends `LV_EVENT_READY` to a visible
keyboard attached to the field and then to the field, as the keyboard's OK
key does; without one, a one-line field gets `LV_EVENT_READY` and others a new
line. An application without LVGL calls `espmirror_text_open()` and
`espmirror_text_read()` from its UI task.

## Changes from espmirror version 1

Hello changes from `MIRR1\n` to the shared `MIRROR1\n`. B changes from
`{"forms":[],"board":{name,w,h,ppi,r}}` to the flat object above. ppi converts
to diagonal and corner_radius to round when new config fields are omitted.
USB is the default; TCP and mDNS require an option. Cable framing, E, R,
codings 3/4, button hints, touch/keyboard capability, version and audio metadata
are new. Audio, LED and dim screen state retain their v1 payloads. Tap values
4..6 no longer alias legacy long-press keys; all 1..10 denote literal keys.
Use held key messages for long presses. The C key constants retain their values.
