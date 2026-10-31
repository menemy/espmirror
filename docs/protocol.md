<!-- Copyright (c) 2026 Maksim Nagaev
SPDX-License-Identifier: Apache-2.0 -->
# Mirror protocol version 2

The same payloads work over USB and TCP. Integers and PCM samples are little
endian. One viewer owns the device across both transports. There is no
application authentication or encryption. TCP is an explicit developer option.

## Session and transport

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

Required geometry is `w`, `h`. `name`, `mac`, `round`, `diag` (inches), `touch`,
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
The encoder counts each enabled candidate with an early size cutoff and emits
the shortest. At most four linear candidate passes plus emission and reference
copy per rectangle attempt; no candidate-size scratch allocation. Whole attempts
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

The application may refuse talk. There is no talk-acceptance response in this
version; an accepted session calls on_talk_audio. Disconnect stops listen/talk,
releases held keys, and queues pointer release. Pointer press, first movement,
and release are separate LVGL samples; later motion samples may coalesce.
Out-of-range coordinates and unknown payloads have no input action.

## Changes from espmirror version 1

Hello changes from `MIRR1\n` to the shared `MIRROR1\n`. B changes from
`{"forms":[],"board":{name,w,h,ppi,r}}` to the flat object above. ppi converts
to diagonal and corner_radius to round when new config fields are omitted.
USB is the default; TCP and mDNS require an option. Cable framing, E, R,
codings 3/4, button hints, touch/keyboard capability, version and audio metadata
are new. Audio, LED and dim screen state retain their v1 payloads. Tap values
4..6 no longer alias legacy long-press keys; all 1..10 denote literal keys.
Use held key messages for long presses. The C key constants retain their values.
