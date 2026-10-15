# Zigbee Coordinator Bring-Up — Verified Findings

Host: Groucho. Dongle: Sonoff ZBDongle-P (CC2652P), `/dev/ttyUSB0`.
ESP32-C6 target: `/dev/ttyACM0` (end device).

## The short version

The coordinator is healthy and correctly flashed, but **refuses to form a
Zigbee network on every channel**, reporting RF interference. This is a
physical placement problem, not a software one. It is sitting behind two
cascaded USB hubs on a controller shared with a 2.4 GHz radio.

## Verified hardware facts

| Fact | Value | How verified |
|---|---|---|
| Chip | CC2652P (Sonoff ZBDongle-P) | `cp210x` bridge, `10c4:ea60` |
| Firmware | 20250321 | binary match vs `CC1352P2_CC2652P_launchpad_coordinator_20250321.hex` |
| Stack version | **3.3** | `znp.version` (zigpy-znp detection) |
| IEEE address | `00:12:4b:00:39:29:7e:c6` | `UTIL_GET_DEVICE_INFO`, NVRAM `EXTADDR` |
| SYS_VERSION raw | transp 2, product 1, major 2, minor 7, maint 1 | `SYS.Version.Req` |

Note the discrepancy: SYS_VERSION *fields* decode as 2.7.1, but zigpy-znp
computes **3.3** from the same frame. zigpy-znp is authoritative for how the
stack behaves, and it takes the Z-Stack 3.x BDB commission path.

## The two real blockers

### 1. The SAPI subsystem does not exist on this firmware

```
SAPI.ZBReadConfiguration -> CommandNotRecognized
    ErrorCode = InvalidSubsystem (1), subsystem = Subsystem.SAPI
SYS.Ping -> Capabilities = SYS | AF | ZDO | UTIL | GP | APP_CNF
```

**SAPI is absent from the advertised capability set.** Every `ZB_*` SAPI call
is rejected outright. Consequence: `ZB_WRITE_CONFIGURATION` does not work here,
and any apparent success from it was a misread reply — the config items
(`CHANLIST`, `PRECFGKEY`, `NWKKEY`, `STARTUP_OPTION`) all read back empty.

**Do not use the SAPI path on this firmware.** Use the zigpy-znp NVRAM layer
(`znp.nvram.osal_read` / `osal_write`), which is proven to work:

```
LOGICAL_TYPE write -> readback: DeviceLogicalType.Coordinator   (works)
```

### 2. The stack was never started

```
UTIL.GetDeviceInfo -> DeviceState = InitializedNotStarted (0)
```

A stopped stack cannot form anything. `ZDO_STARTUP_FROM_APP` fixes this:

```
before: DeviceState = InitializedNotStarted (0)
after : DeviceState = StartingAsCoordinator (8)   <- moves
        NWK = 0x0000                                <- gets a short address
```

It reaches `StartingAsCoordinator` but **not** `StartedAsCoordinator (9)`,
because formation itself is what fails.

## The physical blocker (not fixable in software)

Formation refuses on **every** channel — 15, 20, 25 and 26 all fail
identically. If this were ordinary WiFi congestion, the high channels above
both nearby APs would have succeeded. Universal failure indicates a broadband,
constant source.

USB topology — the dongle is the worst-placed device on the machine:

```
usb1 (xhci, 480Mbps)
 ├─ 1-1    USB2.0 Hub
 │   └─ 1-1.2  USB2.1 Hub
 │       ├─ 1-1.2.1  248a:16bb  Wireless Receiver     <- 2.4 GHz
 │       ├─ 1-1.2.2  1ea7:0064  2.4G Mouse             <- 2.4 GHz
 │       └─ 1-1.2.4  10c4:ea60  Sonoff Dongle         <- the coordinator
 └─ 0e8d:c616  Wireless_Device                        <- BT/WiFi combo
```

Three 2.4 GHz radios sit on the same controller as the coordinator, which is
itself behind **two cascaded hubs**. Nearby 2.4 GHz APs: `SKY53USP` ch6
(2437 MHz), `BT-NJAFXK` ch8 (2447 MHz).

Three other USB controllers (`usb3`, `usb5`, `usb7`) are **completely idle with
free ports**, and the ESP32-C6 is on the bus too (`303a:1001`).

### The fix (requires physically moving the dongle)

1. Move the dongle to a **USB 2.0 extension cable**, plugged into one of the
   **bare root hubs** (`usb3`, `usb5` or `usb7`) — not a hub, not the current
   daisy-chain.
2. Keep it away from the 2.4 GHz mouse/keyboard receivers and the BT/WiFi combo.
3. Then re-run: `python start_then_form.py --channel 15`

Expect `DeviceState` to reach `StartedAsCoordinator` and `NWK` to stay `0x0000`.

## Tools in this directory

| Script | Purpose |
|---|---|
| `diagnose_nib.py` | Reads the NIB and says exactly why `NetworkNotFormed` is raised. Run this first. |
| `probe_sapi.py` | Proves which ZNP subsystems are actually implemented. |
| `start_then_form.py` | Starts the app, waits for state change, then forms. **The main tool.** |
| `form_pan.py` | Builds a network record and writes it via `write_network_info`. |
| `raw_energy_scan.py` | Energy-scans all channels even with no network formed. |
| `write_nib_and_form.py` | Direct NVRAM config writes, then form. |

## Correct bring-up sequence

1. `znp.connect()`
2. Read IEEE from NVRAM (`OsalNvIds.EXTADDR`, `item_type=t.EUI64`) — a blank
   chip raises `NetworkNotFormed` in `load_network_info`, so read it directly.
3. `write_network_info(network_info=..., node_info=...)` to build the NIB.
4. `ZDO_STARTUP_FROM_APP` → wait for `ZDO_STATE_CHANGE_IND`.
5. `start_network()` to form — **only now** is it meaningful.
6. Permit join: `MgmtPermitJoinReq` with `AddrMode=0x02` (16-bit) targeting
   `0x0000` (the coordinator). `0x0F/0xFFFF` broadcast does **not** permit-join
   the coordinator itself and returns `0xC2` / `ZG_NWK_INVALID_REQUEST`.

## Hub integration notes

- `dongle-web.service` holds `/dev/ttyUSB0`. **Stop it before any BSL or
  direct serial operation**, or the port is seized and the operation stalls.
- Partial flash dumps (~130 KB, ending `Timeout waiting for ACK/NACK`) are
  caused by port contention, not a bad chip.
