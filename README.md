# Zigbee IoT Hub

A self-hosted IoT hub for a Sonoff ZBDongle-P (CC2652P) coordinator and the
ESP32-C6 nodes that join it. Runs on port **8001**.

    http://localhost:8001/

## What it is

The dongle is a radio, not a hub. This service is the hub: it owns the serial
port, speaks Z-Stack ZNP to the coordinator, keeps a registry of every known
device, and exposes one uniform REST surface regardless of transport.

## Roles

    Sonoff ZBDongle-P (CC2652P)   COORDINATOR  owns the Zigbee network
    ESP32-C6 (Super Mini)         END DEVICE   joins, does not route
    ESP32-C6 (router firmware)    ROUTER       relays for others (optional)

## Layout

    hub/
      db.py              SQLite registry + event journal (WAL)
      core.py            Hub: drivers -> registry reconciliation, poll loop
      api.py             REST router
      static/index.html  UI: Devices / Add device / Events / Coordinator
      drivers/
        base.py          Driver ABC + NodeInfo
        zigbee.py        ZNP driver for the CC2652P
    hub_server.py        FastAPI entry point (port 8001)
    tests/test_hub.py    26 unit tests, no hardware needed
    .hermes/HUB_ARCHITECTURE.md   design notes
    ZIGBEE_FINDINGS.md    verified hardware/stack diagnosis + bring-up sequence

## API

    GET    /api/hub                     hub identity + driver status
    GET    /api/devices                 list devices (?online_only, ?node_type)
    POST   /api/devices                 register a device manually
    GET    /api/devices/{ieee}          one device
    PATCH  /api/devices/{ieee}          rename
    DELETE /api/devices/{ieee}          remove (?forget_radio=false for registry only)
    GET    /api/devices/{ieee}/state    live state from the driver
    POST   /api/devices/{ieee}/state    write state {key, value}
    POST   /api/commission              open the network {seconds}
    GET    /api/commission              permit-join status
    POST   /api/reconcile               force an immediate driver poll
    GET    /api/events                  event journal (?limit, ?ieee)
    GET    /health                      liveness (never touches serial)

Interactive docs at `/docs`.

## Adding a C6 device

1. Flash the C6 (see the `esp32-c6-zigbee` build dir), or press its reset
   if already flashed.
2. In the UI, *Add device* -> **Open network (180 s)**.
3. Put the C6 into pairing mode. The hub polls every 10 s and registers it
   automatically once it joins.

## ZNP protocol notes (verified against firmware 20250321)

Frame layout:

    FE | LEN | MT | CMD | <LEN data bytes> | FCS

* `LEN` counts **data bytes only** — not MT, CMD or FCS.
* `FCS` = XOR of `LEN`, `MT`, `CMD` and every data byte.
* SYS/UTIL replies have **no separate status byte**; byte 0 of the data is
  the first real field.
* `UTIL_GET_DEVICE_INFO` payload is `status | ieee(8, LE) | nwk(2, LE) | state`
  and may be 12 **or 14** bytes depending on the firmware build. Read by
  offset with a minimum-length guard; never require an exact length.

### The response-type trap (a real bug, fixed)

A synchronous reply (SRSP) carries the request's MT byte **plus a
layer-specific offset**:

| Layer    | Request MT | Reply MT | Offset |
|----------|-----------|----------|--------|
| SYS      | 0x41      | 0x61     | 0x20   |
| UTIL     | 0x67      | 0x67     | —      |
| **ZDO**  | **0x25**  | **0x65** | **0x40** |

Computing `mt | 0x20` for ZDO is a **no-op** (`0x25` already has bit `0x20`
set), so a parser that assumes 0x20 ends up comparing the reply type against
`0x25` — the request type — and **never matches**. Every ZDO reply is
silently discarded while SYS commands keep working, which makes the
coordinator look completely dead.

The driver keeps the offsets in `SRSP_TYPE_OFFSETS`; do not collapse them
back to a single constant.

## Running

    systemctl --user status dongle-web.service
    systemctl --user restart dongle-web.service
    journalctl --user -u dongle-web -f

Environment overrides: `HUB_PORT`, `ZIGBEE_PORT`.

The unit is enabled at login and survives reboots.

## Status of the coordinator (read this before troubleshooting)

**The hub software is complete and working. The coordinator does not yet have a
formed network**, so nothing can join. Two root causes were found and proven
against the hardware — see `ZIGBEE_FINDINGS.md` for the full evidence:

1. **This firmware has no SAPI subsystem.** `SYS.Ping` advertises
   `SYS|AF|ZDO|UTIL|GP|APP_CNF` only, and every `ZB_*` call is rejected with
   `InvalidSubsystem`. Provisioning must go through the NVRAM layer
   (`osal_write` / `write_network_info`), not `ZB_WRITE_CONFIGURATION`.
2. **Broadband 2.4 GHz interference.** Formation fails identically on channels
   15, 20, 25 and 26 — including the channels above both nearby APs, which
   rules out ordinary WiFi congestion. The dongle shares a USB controller with
   three 2.4 GHz radios and sits behind two cascaded hubs.

The fix for (2) is physical: move the dongle to a USB 2.0 extension cable on a
bare root hub, away from the wireless receivers. No ZNP command substitutes
for it.

Note the version discrepancy: the raw `SYS_VERSION` fields decode as **2.7.1**,
but `zigpy-znp` computes **3.3** from the identical frame and therefore drives
the Z-Stack 3.x BDB commissioning path. Both are reported by `/api/hub`. Branch
on what the stack *accepts*, not on either number.

## Tests

    cd ~/dev/esp32-c6-zigbee
    ./.venv/bin/python -m pytest tests/test_hub.py -q

All 24 tests run without hardware — the API suite drives a `FakeDriver`.
