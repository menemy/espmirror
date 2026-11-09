<!-- Copyright (c) 2026 Maksim Nagaev -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# ESP-Mosaico through ESP-Iris

The first example uses the desktop mirror as its only display: a moving
white bar, three key indicators and a red touch marker over an RGB565
gradient. FN is key 1, BOOT key 2 and POWER key 3. The default build does not
initialize the physical peripherals. POWER toggles screen state and blanks
the framebuffer, so both Iris and the physical display reflect it.

The application registers espmirror before starting Espressif's
`iris_ota_support`, which provides inventory, return-to-recovery and health
acceptance. The native USB-C port belongs to Iris, with two CDC interfaces;
espmirror's USB Serial/JTAG, USB network and TCP transports are disabled.

## Build

Requires the ESP32-S31 preview toolchain in ESP-IDF `7b9cc1ac` and an
`esp-mosaico-utils` checkout containing ESP-Iris and Vibe Mode 0.2. Set
`MOSAICO_UTILS` to that checkout, source the IDF environment, then:

```sh
cd examples/mosaico
idf.py --preview -B /tmp/iris-build \
  -D SDKCONFIG=/tmp/iris-build/sdkconfig \
  -D MOSAICO_UTILS="$MOSAICO_UTILS" build
```

`partitions.csv` retains the board's first 2 MiB and its application/data
layout: `main_app` starts at `0x210000`, size `0x600000`; application NVS
starts at `0x200000`, size `0x10000`. The product contract is
`esp-mosaico/v2`, layout `mosaico-retained-test-2m-v2`, recovery ABI 2.
Use a fresh build directory when changing defaults: existing sdkconfig
values win over them.

After validating the mirror-only build, enable physical hardware with
`-D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.panel'` and a separate
build directory and sdkconfig, such as `/tmp/iris-panel-build`.
This variant uses Espressif's CO5300 2.2 driver and CST9220 0.1 driver. It
reads the hardware revision from eFuse: v1.0 uses I2C SDA/SCL 0/1 and LCD
clock/reset 44/42; v1.1 and v1.2 use 56/3 and 42/44. Unknown revisions stop
initialization. QSPI CS is 50, data lanes 36/51/35/9, touch IRQ 6, peripheral
power enable 60 (active low), FN 7 and BOOT 61 (active low).
The panel transfer buffer is eight rows, 7,680 bytes of internal DMA memory.
It is reused only after transfer completion; the mirror copy is unchanged.
The panel configuration also enables FreeRTOS trace metadata and Iris's
on-demand task memory snapshots for `iris memory`; it has no background
memory sampler. The mirror-only defaults expose heap totals through
`iris device-status` but do not enable task snapshots.
The physical ON/OFF switch is not exposed as a GPIO input. The viewer's
POWER does not request board shutdown. The V1.2 board has no programmable
LED; its charge LED is controlled by hardware. Codec output is not enabled.

## Bundle and install

Use Espressif's tools with an existing Python environment satisfying their
requirements (`IRIS_PYTHON`). These commands create a System Update bundle
with the application and the same partition table, without a bootloader or
Vibe Mode image:

```sh
"$IRIS_PYTHON" "$MOSAICO_UTILS/mosaico-tools/tools/prepare_system_update.py" \
  --partition-csv partitions.csv \
  --partition-table /tmp/iris-build/partition_table/partition-table.bin \
  --application /tmp/iris-build/espmirror_mosaico.bin \
  --stage-dir /tmp/iris-build/system-update --release 0.1.0
"$IRIS_PYTHON" "$MOSAICO_UTILS/ESP-Iris/components/esp_iris/tools/esp_iris.py" \
  bundle build /tmp/iris-build/system-update/manifest.json \
  --component-root /tmp/iris-build/system-update \
  --output /tmp/iris-build/espmirror-mosaico.irisfw
```

Before each installation, confirm the target device and bundle with the
board's owner. Start the dedicated Gateway described below and set
`IRIS_GATEWAY` to its printed URL. Create a local CLI profile for that
Gateway, then install through Vibe Mode using the workspace's `mosaico.py`.
The current CLI rejects `--bundle` together with `--project`, so the profile
selects the already running Gateway explicitly:

```sh
export ESP_IRIS_PROFILE_FILE=/tmp/espmirror-iris-profile.json
"$IRIS_PYTHON" "$MOSAICO_UTILS/ESP-Iris/components/esp_iris/tools/esp_iris.py" \
  ctl --url "$IRIS_GATEWAY" --profile espmirror profile
python3 "$MOSAICO_WORKSPACE/mosaico.py" --workspace "$MOSAICO_WORKSPACE" iris \
  system-update --bundle /tmp/iris-build/espmirror-mosaico.irisfw \
  --gateway-profile espmirror --device-id "$DEVICE_ID"
```

Do not use `idf.py flash` or `esptool` for this board. Preserve the exact
partition table already on it. A board with another recovery ABI or layout
requires its matching application configuration and bundle.

## Viewer and measurements

Keep a dedicated project Gateway running in another terminal, from this
example directory. Always select the device explicitly when other boards
are connected:

```sh
python3 "$MOSAICO_WORKSPACE/mosaico.py" --workspace "$MOSAICO_WORKSPACE" iris \
  run --project "$PWD" --device-id "$DEVICE_ID"
```

Use the printed Gateway URL as `IRIS_GATEWAY`. Then, from the repository root:

```sh
python3 tools/espmirror.py "iris:$DEVICE_ID" --iris-gateway "$IRIS_GATEWAY"
python3 tools/espmirror.py "iris:$DEVICE_ID" --iris-gateway "$IRIS_GATEWAY" \
  --stats --seconds 10 --png /tmp/mosaico.png
```

The example enables delta and QOI coding. Delta uses a 460,800-byte reference
in PSRAM, in addition to the existing screen copy. Encoded F messages travel
through Iris IMAGE with one acknowledged chunk in flight. The viewer counts
published F updates and encoded payload bytes, excluding HTTP, WebSocket,
Iris and the application chunk header. A large update can span multiple F
messages. These counts are not interchangeable with complete raw frame counts.
Actual throughput must be measured on hardware. The initial encoded panel
build delivered 47 F updates in 30.689 seconds (1.531 updates/s), with
181,932 encoded payload bytes on 2026-11-09. This predates the panel ISR
wakeup change; its throughput must be measured separately.
Use `mosaico.py ... iris memory --device-id "$DEVICE_ID"` before and during
the stream to compare device allocations. Do not run streams while another
board sharing the Gateway is under a power measurement.

Host tests use a fake Gateway and sanitizer-enabled C stubs. They do not
establish panel operation, USB throughput or device memory use.

### Historical raw SCREEN measurements

Before the encoded IMAGE transport, on an ESP-Mosaico V1.2, ESP-IDF `7b9cc1ac`, USB Highspeed and the default
configuration, the viewer received 51 complete frames in 30 seconds
(1.7 frames/s), 23,635,200 RGB565 bytes (787,829 bytes/s), on 2026-11-09 UTC.
This is received throughput, not the requested 25 fps. The Mac was not
building firmware during this run. Key down/up, pointer down/up and POWER
off/on were verified in the received pixels.

A separate stream run used these `iris device-status` heap snapshots:

| Metric | Before stream | During stream |
| --- | ---: | ---: |
| Free internal heap | 343,023 bytes | 342,675 bytes |
| Free PSRAM | 16,016,444 bytes | 16,012,600 bytes |
| Iris internal total | 33,605 bytes | 33,949 bytes |

The screen backend reads the live framebuffer in chunks. Drawing can occur
between chunks, so moving content can tear within a received frame.


### Bounded timing diagnostics

Append `sdkconfig.timing` to `SDKCONFIG_DEFAULTS` in a fresh build to enable
read-only timing RPC 0x4553/4. For the panel use
`sdkconfig.defaults;sdkconfig.panel;sdkconfig.timing`; for mirror-only use
`sdkconfig.defaults;sdkconfig.timing`. The counters and wire format are in
[the protocol](../../docs/protocol.md). Compare snapshots around the same
measurement window, keeping initial reference transfer outside that window.
The records use fixed storage and do not log or allocate on each frame.

The panel callback explicitly yields when the DMA semaphore wakes its waiter:
the pinned SPI panel IO driver does not consume the callback's boolean result.
The single DMA band is still reused only after completion. Compare panel and
mirror-only producers separately when measuring the transport.
