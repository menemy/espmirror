<!-- Copyright (c) 2026 Maksim Nagaev
SPDX-License-Identifier: Apache-2.0 -->
# Encoder benchmark

Run `python3 tests/bench_espmirror.py > results.csv`. Requires numpy and a
C11 compiler; no external network or ESP-IDF. `CC` selects the compiler and
`--repeats` selects repetitions (default 10). The C executable includes the
production core only in this host build so the forced-coding measurements use
the actual static encoders without adding a firmware API or runtime override.

Fixtures are deterministic RGB565 full-screen frames at 172x640 and 466x466:

- Dark page with a 120x120 animated sprite, smooth shading quantized with Bayer
  dithering, moving eyes and lighting, and static text; 24 frames.
- Horizontal transition from that page to a list; 12 positions from the first
  page through the second page, moving the whole screen.
- Scrolling cards with labels and status text; 24 frames, three pixels per step.

All screens are marked fully dirty. This measures the cost of scanning an entire
screen and is conservative for a sprite whose actual dirty rectangle is small.
The first frame starts with a black delta reference. Each subsequent delta
references the preceding screen; reference-copy time is included. Individual
codings emit a single rectangle (including ones larger than a production
message); `select` runs the actual enabled selector, splitting/draining 32 KiB
messages. Sizes include frame and rectangle headers, exclude transport framing.
Splitting can make `select` smaller than every single whole-screen coding.
Times exclude generation, I/O, allocation and initialization. They include candidate
counting and reference copying where applicable, and are means over all frames
and repetitions. Full-frame results do not measure LVGL, scheduling, radio,
USB, decoder time, display time, dropped frames, or frame pacing.

`tests/data/espmirror_bench.csv` records a run on macOS 27.2 arm64, Apple clang
21.0.0, `-O2`, 10 repetitions. Selected results:

| Geometry | Scene | Row RLE bytes/frame | QOI bytes/frame | Selected bytes/frame | Selected host us/frame |
|---|---|---:|---:|---:|---:|
| 172x640 | sprite | 34144.7 | 15354.9 | 13662.0 | 650.629 |
| 172x640 | slide12 | 29410.0 | 13220.1 | 13116.0 | 795.175 |
| 172x640 | list | 16390.0 | 8785.7 | 8929.6 | 803.258 |
| 466x466 | sprite | 34152.7 | 17062.1 | 14419.5 | 1085.812 |
| 466x466 | slide12 | 27494.3 | 14080.8 | 13834.3 | 1236.400 |
| 466x466 | list | 11848.7 | 8622.8 | 8079.0 | 1176.025 |

## ESP32-S3 estimate

The CSV's `s3_budget_us_per_frame` is an **uncalibrated scalar cycle model** at
240 MHz, not host time scaled by CPU frequency and not a hardware measurement.
The assumed budgets are 8 cycles/pixel for raw; 12/20 for row/column RLE plus
16/run; 18 for delta plus 16/run and 4/pixel for its reference copy; and 40 for
QOI plus 8/output byte. These are explicit engineering assumptions for scalar
loads, comparisons, branches and stores; they are not disassembly-derived counts.
The output-size-dependent terms use the measured byte counts. Divide cycles by
240 to get microseconds. Allow at least 0.5x–2x uncertainty for each individual
codec, **plus** PSRAM stalls and contention. The script exposes this model in
`s3_us` so it can be replaced with a measured calibration.

For example the 172x640 slide budgets are raw 3.67 ms, row 5.99 ms, column
9.65 ms, delta 10.73 ms, QOI 18.79 ms. At 466x466 they are 7.24, 11.32, 18.66,
20.63 and 36.66 ms. This alone is insufficient to promise smooth target playback.

`select` is a deliberately pessimistic full-pass work budget (211/416 ms by
geometry), including failed-fit scans and ignoring early cutoffs. It is not an
expected encoder latency, nor a wall-time upper bound with PSRAM/RTOS stalls.
Hardware calibration should time each candidate and the complete selection/build
under the same PSRAM, compiler, framebuffer, LVGL and transport conditions, then
measure the frame gaps of a page swipe. The synthetic result suggests QOI can
roughly halve slide bytes relative to row RLE; it does not establish target FPS.
