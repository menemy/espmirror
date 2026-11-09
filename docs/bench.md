<!-- Copyright (c) 2026 Maksim Nagaev
SPDX-License-Identifier: Apache-2.0 -->
# Encoder benchmark

Run `python3 tests/bench_espmirror.py > results.csv`. Requires numpy and a
C11 compiler; no external network or ESP-IDF. `CC` selects the compiler and
`--repeats` selects repetitions (default 10). The C executable includes the
production core only in this host build so the forced-coding measurements use
the actual static encoders without adding a firmware API or runtime override.

Pass `--size 240x240 --size 480x480` to measure those geometries instead.
Each dimension must be between 128 and 640 pixels. `select` enables both delta
and QOI; `select-base`, `select-delta`, and `select-qoi` measure no optional
codec, delta alone, and QOI alone. These selectors all drain 32 KiB messages.
In the production selector, enabling either optional codec also enables column
RLE on capacity-sized bands. The base selector tries column RLE only on whole
rectangle attempts. Comparisons between selector modes include that difference
in candidate scans and selected codings.
The original six rows retain the cycle-budget column; the added modes leave
it empty because they have no calibrated target model.

`copy` measures row-by-row `memcpy` of the whole synthetic screen into a
separate buffer. It excludes mutex wait, clipping and LVGL, and reads the
copied buffer after timing so the copy remains observable. `dirty` measures
marking a whole screen in full-width bands of `height / 10` rows (integer
division), starting with an empty dirty list. It repeats this operation 256
times inside each timing interval and divides the result by 256. This is
the touching-band case, not a disjoint-rectangle or overflow workload.
Both rows report zero wire bytes because neither emits a frame. Times use
the host monotonic clock and include timer overhead; these are elapsed
host measurements, not ESP32 CPU times or PSRAM bandwidth measurements.

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

`tests/data/espmirror_bench_240_480.csv` records a separate run on macOS
27.2 arm64, `cc -std=c11 -O2`, 10 repetitions, with the additional selector
and pipeline modes. All screens are fully dirty, including the sprite scene:

| Geometry | Scene | Base bytes/frame | QOI selector bytes/frame | Base host us/frame | QOI selector host us/frame |
|---|---|---:|---:|---:|---:|
| 240x240 | sprite | 32808.3 | 14524.2 | 238.508 | 492.321 |
| 240x240 | slide12 | 20893.3 | 9253.1 | 198.725 | 410.758 |
| 240x240 | list | 5903.7 | 3452.2 | 126.008 | 331.963 |
| 480x480 | sprite | 34393.2 | 15192.4 | 452.496 | 985.292 |
| 480x480 | slide12 | 27952.0 | 14098.2 | 439.858 | 1214.500 |
| 480x480 | list | 12472.0 | 8773.5 | 406.104 | 1129.900 |

In that run full-screen row copies took 2.396 to 2.775 us at 240x240 and
9.417 to 9.808 us at 480x480. The touching-band dirty measurement took
0.024 to 0.026 us per screen. These values do not include lock contention
or the production LVGL flush path. USB bytes/s, updates/s and target heap
allocation were not measured by this host benchmark.

### Selector trial output

`tests/data/espmirror_selector_240_480.csv` compares the core at `ef255bc`
with the selector that retains QOI trial output and row-only band RLE trial
output. Both versions use the same current benchmark harness, Apple clang
21.0.0, `-std=c11 -O2`, and the scenes above. The run used an exclusive quiet
interval on the shared Mac, three paired trials with alternating execution
order and ten repetitions per trial. The table shows medians of host elapsed
microseconds per frame, with no target CPU estimate:

| Geometry | Scene | Base before | Base after | QOI selector before | QOI selector after |
|---|---|---:|---:|---:|---:|
| 240x240 | sprite | 236.367 | 214.317 | 505.271 | 341.429 |
| 240x240 | slide12 | 195.650 | 180.500 | 429.300 | 288.708 |
| 240x240 | list | 125.546 | 124.283 | 344.942 | 229.117 |
| 480x480 | sprite | 448.125 | 176.054 | 982.192 | 826.254 |
| 480x480 | slide12 | 432.217 | 169.783 | 1232.508 | 1006.933 |
| 480x480 | list | 401.162 | 154.000 | 1173.325 | 917.904 |

The CSV also includes two random RGB565 frames per geometry (NumPy
`default_rng(849)`) and a black/white pair. The base selector's random-frame
median fell by 10.2% at 240x240 and 31.0% at 480x480; the QOI selector's fell
by 14.6% and 11.4%. On the 240x240 solid pair the QOI selector increased from
141.150 to 143.150 us (1.4%); the base selector increased from 140.150 to
140.850 us (0.5%). Delta-only selector medians changed by -2.0% to +0.1%
across these workloads. All paired wire sizes and output checksums matched.
These results do not establish an ESP32, PSRAM or USB throughput gain.

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
