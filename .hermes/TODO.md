# ESP32-C6 Zigbee + IoT Hub — TODO

Goal: a working Zigbee IoT hub on port 8001, managing the Sonoff ZBDongle-P
coordinator and the ESP32-C6 nodes that join it.

## Hardware
- [x] `/dev/ttyACM0` = ESP32-C6FH4 (QFN32), rev v0.2, 4 MB flash, USB-Serial/JTAG
- [x] `/dev/ttyUSB0` = Sonoff Zigbee 3.0 USB Dongle Plus (CC2652P, CP210x)

## Done
- [x] Dongle firmware verified: Z-Stack 20250321, transport rev 2 (ZNP),
      product 1 (coordinator). Launchpad variant is the correct one for the
      ZBDongle-P.
- [x] ESP-IDF cloned + esp32c6 toolchain installed; `HA_on_off_light` built
      and flashed to `/dev/ttyACM0`.
- [x] **Found and fixed the ZNP response-type bug.** The parser computed
      `mt | 0x20`, which for ZDO (0x25) is a no-op, so every ZDO reply was
      discarded while SYS kept working. ZDO now answers correctly:
      `ZDO_STARTUP_FROM_APP` returns status 0x00 (success). This was the
      reason the coordinator looked dead.
- [x] Hub architecture designed: `.hermes/HUB_ARCHITECTURE.md`
- [x] `hub/db.py` — SQLite registry + event journal (WAL)
- [x] `hub/drivers/base.py` — Driver ABC + NodeInfo
- [x] `hub/drivers/zigbee.py` — ZNP driver
- [x] `hub/core.py` — hub core, reconciliation, poll loop
- [x] `hub/api.py` — REST surface (devices, commissioning, events)
- [x] `hub/static/index.html` — UI: Devices / Add device / Events / Coordinator
- [x] `hub_server.py` — FastAPI entry point on 8001
- [x] `tests/test_hub.py` — 26 tests passing, no hardware required
- [x] Deployed as `dongle-web.service`, enabled at login
- [x] Verified live: every API endpoint exercised against the running service
- [x] README written, port 8001 registered
- [x] `ZIGBEE_FINDINGS.md` — full verified diagnosis + tool index
- [x] Hub now reports BOTH version numbers (`zstack_version` 2.7.1 and
      `zstack_version_reported` 3.3) plus `uses_bdb_commissioning`, so the
      discrepancy is visible instead of hidden behind one number.

## In progress
- (none — waiting on a physical action, see Blocked)

## THE TWO REAL BLOCKERS (verified this session, supersedes earlier theory)

- [x] **1. The SAPI subsystem does not exist on this firmware.** Proven
      directly, not inferred:
      ```
      SYS.Ping -> Capabilities = SYS|AF|ZDO|UTIL|GP|APP_CNF     (no SAPI)
      SAPI.ZBReadConfiguration -> CommandNotRecognized
          ErrorCode = InvalidSubsystem (1), subsystem = Subsystem.SAPI
      ```
      So `ZB_WRITE_CONFIGURATION` never worked here. Its earlier apparent
      "success" was a misread reply. **Do not use any `ZB_*` call on this chip.**
      The whole "Correct ZNP wire facts" block below about
      `ZB_WRITE_CONFIGURATION` is VOID FOR THIS BUILD — the command is rejected
      outright. Use zigpy-znp's NVRAM layer instead, which is verified working
      (`LOGICAL_TYPE` write -> readback `Coordinator`).

- [x] **2. The stack was never started.** `UTIL.GetDeviceInfo` reported
      `DeviceState = InitializedNotStarted (0)`. A stopped stack cannot form
      anything. `ZDO_STARTUP_FROM_APP` fixes it:
      ```
      before: InitializedNotStarted (0)
      after : StartingAsCoordinator (8), NWK = 0x0000     <- moves
      ```
      It reaches 8 but not `StartedAsCoordinator (9)`, because formation itself
      is what fails.

- [x] **Version discrepancy is real and matters.** Raw SYS_VERSION fields decode
      as 2.7.1, but zigpy-znp computes **3.3** from the identical frame, so it
      drives the Z-Stack 3.x BDB commissioning path (`BDBStartCommissioning`),
      not the legacy `StartupFromApp` path. Branch on what the stack *accepts*,
      not on either version number.

- [x] `write_network_info()` writes the NIB successfully — confirmed by
      `StartupFromApp.Rsp(State=RestoredNetworkState)`, i.e. the stack restored
      the pan_id/key we wrote. This is the correct provisioning path on a blank
      chip. The NVRAM layer works; the SAPI layer does not.

## Radio findings — verified this session
- [x] Firmware on the dongle is **byte-identical to the 20250321 release**.
      Proved by parsing the Intel HEX and comparing against a live flash dump:
      all 132,956 dumped bytes match, 0 differences. The release contains ONLY
      the 64 KB application image (0x0..0xFFFF); it does not touch CCFG or NV.
      => Corrupt-firmware theory is dead. Do not reflash again.
- [x] The partial 352 KB dump was aborted early (132,956 of 368,640 bytes) and
      is NOT a restore path. Kept at `dongle-fw/backups/` for reference only.
      Confirmed twice by a watcher: it stalls with
      `ERROR: Timeout waiting for ACK/NACK after 'Mem Read (0x2A)'` and never
      grows. The bytes it did capture are valid ARM Thumb code (e.g. `2de9fc41`
      = `push {r6,r7,r8,sl,fp,ip,lr}`), so this is a **transport stall, not
      corruption**. Do not re-run it; 352 KB at 500 kbaud takes ~18 min and it
      dies part-way every time. The hub service holding `/dev/ttyUSB0` is a
      plausible cause - stop `dongle-web.service` before any BSL operation.
- [x] The hub service holds `/dev/ttyUSB0` while running, by design. That is
      expected, NOT a stuck process. `fuser -v /dev/ttyUSB0` will always show
      the hub's MainPID. Stop the service before probing/flashing, start after.
- [ ] **SUPERSEDED — SEE "THE TWO REAL BLOCKERS" ABOVE.** These wire facts are
      correct *as frame decodings*, but the `ZB_*` commands below are **rejected
      outright on this build** (no SAPI subsystem). Kept only as a record of the
      frames observed. Do not act on the `ZB_WRITE_CONFIGURATION` guidance.
- [x] Correct ZNP wire facts confirmed against TI's own docs and live frames:
      * `ZB_WRITE_CONFIGURATION` is **0x26/0x05**, NOT 0x41/0x0F. Payload:
        `cfgId(1) | len(1) | value`. `ZCD_NV_STARTUP_OPTION` cfgId = **0x03**
        (not 0x0F00); `ZCD_NV_LOGICAL_TYPE` = 0x87; `CHANLIST` = 0x84;
        `PAN_ID` = 0x83. `STARTUP_OPTION` value 0x03 = clear state + config.
      * SAPI (0x26) SRSP arrives as type **0x60** with an echoed request tail,
        so it looks like a bad frame if you expect `0x26|0x40 = 0x66`.
      * `ZB_READ_CONFIGURATION` on this build is an **acknowledgement-only
        stub** — it returns status, not the value. You cannot read config back.
      * `ZDO_MGMT_PERMIT_JOIN_REQ` payload is
        `AddrMode(1) | DstAddr(2, LE) | Duration(1) | TCSignificance(1)`.
        AddrMode **0x02** (16-bit), DstAddr **0x0000** for the coordinator
        itself. Duration 0xFE = 254 s (0xFF is capped to 255 s, not forever).
      * Broadcast (DstAddr 0xFFFF) does **not** permit-join the coordinator
        itself — unicast to 0x0000 as well.
- [x] With the corrected permit-join, status went 0x02 (ZFailure) -> **0x00
      (SUCCESS)** for the first time.
- [x] `UTIL_GET_DEVICE_INFO` devstate field is stale/lagging; trust
      `ZDO_STATE_CHANGE_IND` and `UTIL_GET_NV_INFO` instead.

## Blocked / open
- [ ] **Coordinator still does not form a network — needs a PHYSICAL FIX.**
      Formation fails on **every** channel: 15, 20, 25 and 26 all refuse
      identically with `FormationFailure: too much RF interference`. Failing on
      the high channels too (which sit above both nearby 2.4 GHz APs) means the
      source is broadband and constant, not WiFi congestion.

      USB topology — the dongle is the worst-placed device on the machine:
      ```
      usb1 (xhci) -> 1-1 USB2.0 Hub -> 1-1.2 USB2.1 Hub
                        |- 1-1.2.1  248a:16bb  Wireless Receiver   <- 2.4 GHz
                        |- 1-1.2.2  1ea7:0064  2.4G Mouse          <- 2.4 GHz
                        `- 1-1.2.4  10c4:ea60  Sonoff Dongle       <- coordinator
        and 0e8d:c616 Wireless_Device (BT/WiFi combo) on the same controller
      ```
      Three 2.4 GHz radios share the controller; the coordinator also sits
      behind **two cascaded hubs**. Nearby 2.4 GHz APs: `SKY53USP` ch6,
      `BT-NJAFXK` ch8. Controllers `usb3`, `usb5`, `usb7` are completely idle
      with free ports.

      **Fix (needs Simon to move the dongle):** USB 2.0 extension cable into a
      bare root hub (`usb3`/`usb5`/`usb7`), away from the 2.4 GHz receivers.
      Then re-run `python start_then_form.py --channel 15`. Expect
      `StartedAsCoordinator` and NWK `0x0000`.
- [ ] C6 has not yet joined the network end-to-end (blocked by the above).

## Tools in this directory (all verified to run)
| Script | Purpose |
|---|---|
| `diagnose_nib.py` | Reads the NIB; says exactly why `NetworkNotFormed` is raised. Start here. |
| `probe_sapi.py` | Proves which ZNP subsystems are actually implemented. |
| `start_then_form.py` | **The main tool.** Starts the app, waits for state change, then forms. |
| `form_pan.py` | Builds a network record, writes it via `write_network_info`. |
| `raw_energy_scan.py` | Energy-scans all channels even with no network formed. |
| `write_nib_and_form.py` | Direct NVRAM config writes, then form. |

## Correct bring-up sequence (for a blank coordinator)
1. `znp.connect()`
2. Read IEEE from NVRAM (`OsalNvIds.EXTADDR`, `item_type=t.EUI64`) —
   `load_network_info()` raises `NetworkNotFormed` before reading it.
3. `write_network_info(...)` to build the NIB.
4. `ZDO_STARTUP_FROM_APP`, wait for `ZDO_STATE_CHANGE_IND`.
5. `start_network()` to form — only now is it meaningful.
6. Permit join: `MgmtPermitJoinReq`, `AddrMode=0x02`, `Dst=0x0000`.

## Not done yet
- [ ] Full association table / device interview, so end devices show real
      clusters instead of just a node type.
- [ ] On/off writes to end devices (`AF_DATA_REQUEST` needs the interview's
      endpoint map — currently refuses with a clear message).

## Notes
- ZNP wire format: SOF | LEN | MT | CMD | DATA | FCS; LEN counts DATA only
- SYS/UTIL SRSP reply type = request MT | 0x20; **ZDO SRSP = MT | 0x40**
- `esptool` is v5.4.0 at `~/.local/bin/esptool` (not `esptool.py`)
- Zigbee2MQTT UI: http://localhost:8081

## Mistakes made this session (keep visible, do not repeat)
- Claimed three times that the fault was hardware/firmware — wrong firmware
  variant, mass-erase bricking the chip, missing full image. Each was killed
  by evidence:
  * launchpad **is** the correct variant for the ZBDongle-P (maintainer says so)
  * the primary IEEE address is factory-burned and was never at risk
  * the release ships only a 64 KB application image — no full image exists
  The real fault was in my own frame parser.
  Lesson: verify the protocol layer against a known-good raw frame **before**
  theorising about hardware.
- `cc2538-bsl` needs decimal addresses; `-a 0x57FA8` is rejected.
- `-i IEEE` runs *after* verify and writes the **secondary** IEEE slot.
