# Zigbee Hub — Architecture & Plan

## Goal

Turn the existing single-device Sonoff dongle panel (port 8001) into a proper
IoT hub:

- A **device registry** — every node known to the hub, persisted in SQLite.
- A **driver layer** — pluggable protocol adapters (Zigbee coordinator today,
  more later) behind one uniform interface.
- **Add new device** — commissioning (permit-join) plus an interview/enrol
  flow that populates the registry.
- An **extensible REST API** — uniform `/api/devices/*` surface that works the
  same for every device regardless of transport.
- **ESP32-C6 nodes** as first-class devices: the C6 light currently flashed
  with HA_on_off_light joins the coordinator and appears as a registry device.

## Roles (settle this first — it defines everything)

    Sonoff ZBDongle-P (CC2652P)   COORDINATOR  — owns the Zigbee network,
                                                 one per network, never resets
                                                 the PAN it created
    ESP32-C6 (Super Mini)         END DEVICE   — joins the coordinator, does
                                                 not route for others
    ESP32-C6 (alt firmware)       ROUTER       — mains-powered C6 can relay
                                                 (optional, later)

The dongle is NOT a hub. It is a radio. The hub is this web service. That
distinction is the whole point of the architecture: the dongle speaks ZNP,
the C6s speak Zigbee, and this service is the only thing that knows about both.

## Layering

    ┌──────────────────────────────────────────────────────┐
    │  HTTP / REST  +  Web UI        (FastAPI, port 8001)  │
    │    /api/devices/*  /api/commission  /api/events      │
    ├──────────────────────────────────────────────────────┤
    │  Hub core                                            │
    │    DeviceRegistry (SQLite)   EventBus   Scheduler    │
    ├──────────────────────────────────────────────────────┤
    │  Drivers (pluggable, one per transport)              │
    │    ZigbeeDriver  (ZNP over serial -> CC2652P)        │
    │    <future>      (Matter, WiFi/ESP-NOW, MQTT)        │
    └──────────────────────────────────────────────────────┘

### Driver interface

Every driver implements the same abstract base so the hub core never needs to
know which radio is underneath:

```python
class Driver(ABC):
    name: str
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_online(self) -> bool: ...
    def list_nodes(self) -> list[NodeInfo]: ...
    def permit_join(self, seconds: int) -> None: ...
    def get_state(self, ieee: str) -> dict: ...
    def set_state(self, ieee: str, key: str, value) -> dict: ...
    def remove_node(self, ieee: str) -> None: ...
```

`NodeInfo` = ieee, nwk, driver, model, vendor, node_type, power_source,
last_seen, online, capabilities.

### Registry schema (SQLite, WAL)

```sql
CREATE TABLE devices (
  ieee          TEXT PRIMARY KEY,
  nwk           TEXT,
  driver        TEXT NOT NULL,          -- 'zigbee'
  model         TEXT,
  vendor        TEXT,
  node_type     TEXT,                   -- coordinator|router|enddevice
  power_source  TEXT,                   -- mains|battery|unknown
  friendly_name TEXT,
  capabilities  TEXT,                   -- JSON array
  state         TEXT,                   -- JSON object, last known
  first_seen    TEXT,
  last_seen     TEXT,
  online        INTEGER DEFAULT 0
);

CREATE TABLE events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  ieee      TEXT,
  kind      TEXT NOT NULL,              -- join|leave|state|online|offline
  payload   TEXT
);
```

## REST API surface

    GET    /api/hub                     hub identity + driver status
    GET    /api/devices                 list all devices
    POST   /api/devices                 register a device manually
    GET    /api/devices/{ieee}          one device + its state
    PATCH  /api/devices/{ieee}          rename / retag
    DELETE /api/devices/{ieee}          remove from registry
    GET    /api/devices/{ieee}/state    read live state from the driver
    POST   /api/devices/{ieee}/state    write state (on/off, brightness…)
    POST   /api/commission              open/close permit-join
    GET    /api/commission              current permit-join window
    GET    /api/events?limit=N          recent events
    GET    /api/debug/logs              ZNP frame ring buffer (existing)

Legacy endpoints (`/api/status`, `/api/nv`, `/api/reset`) stay for the dongle
view so the current page keeps working.

## Build order

1. `hub/db.py` — SQLite registry + events.
2. `hub/drivers/base.py` — Driver ABC + NodeInfo dataclass.
3. `hub/drivers/zigbee.py` — wrap the existing Znp class as a Driver.
4. `hub/core.py` — Hub: owns drivers, registry, event bus, poll loop.
5. `hub/api.py` — REST surface above.
6. `hub/static` — UI: Devices tab, Add Device tab, Events tab.
7. Tests + live verification against the real dongle.

## Constraints

- Reuse the existing `Znp` framing (already verified against firmware
  20250321 — do not re-derive the frame layout).
- Poll only when a driver is online; never spin on a dead serial port.
- Registry writes are the source of truth; live reads refresh `state`.
- Port 8001 stays the hub port; register in the port registry.

## The C6 side: how ESP32-C6 nodes fit

Each C6 runs Espressif's **ESP-Zigbee-SDK**, not ZNP, so it is a *different kind
of thing* from the dongle and never speaks to the hub directly. It appears in the
hub only through the coordinator, exactly as a commercial Zigbee bulb would.

```
ESP32-C6 (end device)  --Zigbee 2.4 GHz-->  CC2652P (coordinator)  --UART/ZNP-->  hub
```

Practical consequences:

- **Build target**: `idf.py set-target esp32c6`, example `HA_on_off_light` from
  `esp-idf/examples/zigbee/`. The C6 joins whatever coordinator has permit-join
  open; it needs no IP address, credentials or hub URL.
- **The hub is not in the C6's firmware.** Pairing is a radio event, so a C6 has
  no idea the hub exists. Provisioning is therefore entirely a hub-side flow
  ("Add new device" opens the join window, the device appears).
- **Device identity** in the registry is the C6's Zigbee IEEE address, which
  ESP-Zigbee-SDK derives from the chip's factory MAC — stable and unique per
  board. Use it as the `ieee` primary key, unchanged.
- **A C6 is an end device by default.** `HA_on_off_light` does not route. It must
  be in radio range of the coordinator, or join via a router. Making a
  mains-powered C6 a router requires a different example and is optional.
- **On/off control maps cleanly**: the C6's On/Off cluster is what the hub's
  `POST /api/devices/{ieee}/command` should address once the driver implements
  ZCL `genOnOff` (toggle/on/off) rather than raw ZDO.

### Firmware flashing notes (ESP32-C6 Super Mini)

- Chip: **ESP32-C6FH4**, appears as `/dev/ttyACM0` (native USB-Serial/JTAG, not a
  UART bridge). It is *not* in the `dialout` group problem the dongle has.
- Use `esptool` v5.x and `idf.py -p /dev/ttyACM0 flash`.
- If the port is busy, another process holds it — same one-process-per-port rule
  as the dongle.

## Honest build status

| Component | State |
|-----------|-------|
| `hub/db.py` registry + events (WAL) | done, tested |
| `hub/drivers/base.py` ABC + NodeInfo | done, tested |
| `hub/drivers/zigbee.py` ZNP driver | done, talks to real hardware |
| `hub/core.py` reconciliation + poll | done, tested |
| `hub/api.py` REST surface | done, verified live |
| `hub/static/index.html` UI | done, dark theme, tabbed |
| `hub_server.py` + systemd unit | done, running on :8001 |
| `tests/test_hub.py` | 24 passing, hardware-free |
| C6 `HA_on_off_light` flashed | done |
| C6 joined to coordinator | **not yet** |
| Coordinator forms a network | **not yet** — see `.hermes/TODO.md` |

The hub is complete and verified. The blocker is upstream of it: the coordinator
will not form a PAN, so there is nothing for the C6 to join. Fixing that is a
radio-configuration problem (CHANLIST / PAN_ID via `ZB_WRITE_CONFIGURATION`),
not a hub problem.
