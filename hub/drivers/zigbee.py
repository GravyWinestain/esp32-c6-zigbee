#!/usr/bin/env python3
"""hub/drivers/zigbee.py - Zigbee (Z-Stack ZNP) driver for the CC2652P dongle.

Speaks the TI ZNP protocol over the coordinator's serial port. Frame layouts
here are the ones verified against firmware 20250321 on this exact dongle;
do not change the offsets without re-verifying against raw frames.

Key facts baked in:
  * UNPI frame = FE | len | mt | cmd | <len bytes of data> | fcs
    where len counts DATA bytes only (excludes mt/cmd/fcs).
  * SYS_* replies carry no separate status byte - byte 0 is the first field.
  * UTIL_GET_DEVICE_INFO payload = status | ieee(8, LE) | nwk(2, LE) | state,
    and may be 12 or 14 bytes depending on firmware build, so read by offset
    with a minimum-length guard, never an exact-length guard.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import serial

from .base import Driver, DriverError, NodeInfo

# -- ZNP constants -----------------------------------------------------------

SOF = 0xFE

MT_SYS = 0x41
MT_UTIL = 0x67
MT_AF = 0x44
MT_ZDO = 0x25

# ZNP response-type offsets. A synchronous response (SRSP) comes back with a
# type bit added to the request's type byte, but the bit differs by layer:
#   SYS/UTIL: 0x41 -> 0x61,  0x67 -> 0x67|0x20 = 0x67... see _matches_srsp()
#   ZDO:      0x25 -> 0x65   (0x40 added, NOT 0x20)
# Hardcoding 0x20 made every ZDO reply invisible, which is what made the
# coordinator look dead while it was answering correctly all along.
SRSP_TYPE_OFFSETS = {MT_ZDO: 0x40, MT_SYS: 0x20, MT_UTIL: 0x20, MT_AF: 0x20}

SYS_RESET_REQ = 0x00
SYS_VERSION = 0x02
SYS_NV_LENGTH = 0x13

UTIL_GET_DEVICE_INFO = 0x00
UTIL_ADDRMGR_NWK_ADDR_LOOKUP = 0x40
UTIL_ASSOC_LIST = 0x02
UTIL_GET_NV_INFO = 0x01

ZDO_STARTUP_FROM_APP = 0x40
ZDO_MGMT_PERMIT_JOIN_REQ = 0x36
ZDO_MGMT_LEAVE_REQ = 0x34

# Device state codes reported by UTIL_GET_DEVICE_INFO.
# Values match zigpy_znp.types.DeviceState exactly; do not hand-edit these
# offsets. A wrong table makes a healthy coordinator look dead: index 8 is
# StartingAsCoordinator and 9 is StartedAsCoordinator, so treating 8 as
# "running" reports success while the network is still coming up.
DEV_STATES = {
    0: "Initialised - not started",
    1: "Initialised - not connected",
    2: "Discovering PANs",
    3: "Joining",
    4: "Rejoining - secure scan, current channel",
    5: "Joined - not authenticated",
    6: "Joined as end device",
    7: "Joined as router",
    8: "Starting as coordinator",
    9: "Started as coordinator",
    10: "Lost parent",
    11: "Sending keep-alive to parent",
    12: "Backoff before rejoin",
    13: "Rejoin - secure scan, all channels",
    14: "Rejoin - insecure scan, current channel",
    15: "Rejoin - insecure scan, all channels",
}

# The states in which a coordinator genuinely owns a live network.
COORDINATOR_STARTED_STATE = 9
COORDINATOR_STARTING_STATE = 8


def _unpi(mt: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a UNPI frame. LEN counts DATA bytes only."""
    body = bytes([mt, cmd]) + payload
    fcs = len(payload)
    for byte in body:
        fcs ^= byte
    return bytes([SOF, len(payload)]) + body + bytes([fcs])


class ZigbeeDriver(Driver):
    """Z-Stack coordinator driver for the Sonoff ZBDongle-P."""

    name = "zigbee"

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self._lock = threading.Lock()
        self._ser: Optional[serial.Serial] = None
        self._permit_until: float = 0.0
        self._coordinator_ieee: Optional[str] = None
        self.last_error: Optional[str] = None

    # -- transport ----------------------------------------------------------

    def _connect(self) -> serial.Serial:
        """Open the serial port if it is not already open."""
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.5)
            time.sleep(0.3)
            self._ser.reset_input_buffer()
        return self._ser

    def start(self) -> None:
        """Open the port, confirm the coordinator answers, and start the app.

        Opening the port is not enough: a fresh serial connection leaves the
        stack at InitializedNotStarted, where it reports devState=7 (the stale
        value from NVRAM) and cannot form or accept anything. Starting the
        application is what makes the network live, so do it here rather than
        leaving the hub to report a dead coordinator.
        """
        try:
            with self._lock:
                self._connect()
            self.version()
            self.bring_up()
            self.last_error = None
        except (serial.SerialException, OSError, DriverError) as exc:
            self.last_error = str(exc)
            raise DriverError(f"zigbee start failed: {exc}") from exc

    def device_state(self) -> int:
        """Return the coordinator's devState, or -1 if it cannot be read."""
        try:
            data = self.request(MT_UTIL, UTIL_GET_DEVICE_INFO, timeout=1.5)
        except DriverError:
            return -1
        # status(1) | ieee(8) | nwk(2) | unk(1) | devstate(1) | ...
        # devState is at offset 12, NOT 11. Offset 11 holds a stale/auxiliary
        # byte (observed 7 on a fully-formed coordinator), so reading it makes a
        # live network look like a router that never joined.
        if len(data) < 13:
            return -1
        return data[12]

    def bring_up(self) -> int:
        """Start the application so the coordinator owns a live network.

        Formation is not a separate command on this firmware - starting the app
        is the formation. Returns the resulting devState.
        """
        state = self.device_state()
        if state == COORDINATOR_STARTED_STATE:
            return state
        # ZDO_STARTUP_FROM_APP, payload StartDelay(uint16 LE) = 0
        self.request(MT_ZDO, ZDO_STARTUP_FROM_APP, b"\x00\x00", timeout=3.0)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            state = self.device_state()
            if state == COORDINATOR_STARTED_STATE:
                break
            time.sleep(0.5)
        return state

    def stop(self) -> None:
        """Close the serial port if open."""
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def is_online(self) -> bool:
        """True if the coordinator answers SYS_VERSION right now."""
        try:
            self.version()
            return True
        except Exception:  # noqa: BLE001 - any failure means offline
            return False

    def request(
        self,
        mt: int,
        cmd: int,
        payload: bytes = b"",
        timeout: float = 1.5,
        expect_areq: bool = False,
    ) -> bytes:
        """Send a request, return the matching SRSP data (len bytes).

        The response type byte is the request type plus a layer-specific
        offset (ZDO uses 0x40, SYS/UTIL use 0x20) - see SRSP_TYPE_OFFSETS.
        Matching only on 0x20 silently dropped every ZDO reply.
        """
        frame = _unpi(mt, cmd, payload)
        srsp_type = mt | SRSP_TYPE_OFFSETS.get(mt, 0x20)
        with self._lock:
            try:
                ser = self._connect()
            except (serial.SerialException, OSError) as exc:
                self.last_error = str(exc)
                raise DriverError(str(exc)) from exc
            ser.reset_input_buffer()
            ser.write(frame)
            ser.flush()
            deadline = time.time() + timeout
            buf = bytearray()
            while time.time() < deadline:
                chunk = ser.read(256)
                if not chunk:
                    continue
                buf.extend(chunk)
                idx = buf.find(bytes([SOF]))
                while idx != -1 and idx + 4 <= len(buf):
                    length = buf[idx + 1]
                    if idx + 4 + length + 1 > len(buf):
                        break
                    mt_r, cmd_r = buf[idx + 2], buf[idx + 3]
                    data = bytes(buf[idx + 4 : idx + 4 + length])
                    if mt_r == srsp_type and cmd_r == cmd:
                        return data
                    idx = buf.find(bytes([SOF]), idx + 1)
                deadline = time.time() + 0.3
            raise DriverError(
                f"no response for mt={mt:#04x} cmd={cmd:#04x} "
                f"(expected reply type {srsp_type:#04x})"
            )

    # -- identity -----------------------------------------------------------

    def version(self) -> Dict[str, Any]:
        """SYS_VERSION -> transport rev, product, Z-Stack version, revision.

        The raw fields decode as 2.7.1 on this CC2652P, but zigpy-znp computes
        3.3 from the identical frame and therefore uses the Z-Stack 3.x BDB
        commissioning path. That difference changes which commands the stack
        will accept, so report both rather than hiding the discrepancy behind
        a single number.
        """
        data = self.request(MT_SYS, SYS_VERSION)
        if len(data) < 8:
            raise DriverError(f"short SYS_VERSION reply: {data.hex(' ')}")
        revision = int.from_bytes(data[5:9] if len(data) >= 9 else data[5:8], "little")
        raw_version = f"{data[2]}.{data[3]}.{data[4]}"
        # zigpy-znp treats any transport rev >= 2 with major 2+ as Z-Stack 3.
        behaves_as_3x = data[0] >= 2 and data[2] >= 2
        return {
            "transportrev": data[0],
            "product": data[1],
            "majorrel": data[2],
            "minorrel": data[3],
            "maintrel": data[4],
            "revision": revision,
            "zstack_version": raw_version,
            "zstack_version_reported": "3.3" if behaves_as_3x else raw_version,
            "uses_bdb_commissioning": behaves_as_3x,
            "product_name": {0: "End device", 1: "Coordinator", 2: "Router"}.get(
                data[1], f"unknown ({data[1]})"
            ),
        }

    def coordinator_info(self) -> Dict[str, Any]:
        """UTIL_GET_DEVICE_INFO -> ieee, nwk, state (12 or 14 byte payloads)."""
        data = self.request(MT_UTIL, UTIL_GET_DEVICE_INFO)
        if len(data) < 13:
            raise DriverError(f"short device info reply: {data.hex(' ')}")
        # devState lives at offset 12 (see device_state() - offset 11 is stale).
        state = data[12]
        ieee = bytes(data[1:9])[::-1].hex(":")
        self._coordinator_ieee = ieee
        return {
            "status": data[0],
            "ieee_address": ieee,
            "short_address": f"0x{int.from_bytes(data[9:11], 'little'):04X}",
            "state_code": state,
            "state": DEV_STATES.get(state, f"unknown ({state})"),
        }

    # -- Driver interface ---------------------------------------------------

    def list_nodes(self) -> List[NodeInfo]:
        """Coordinator plus every node in the association table."""
        nodes: List[NodeInfo] = []
        info = self.coordinator_info()
        nodes.append(
            NodeInfo(
                ieee=info["ieee_address"],
                driver=self.name,
                nwk=info["short_address"],
                model="CC2652P (ZBDongle-P)",
                vendor="ITead Sonoff",
                node_type="coordinator",
                power_source="mains",
                capabilities=["coordinator", "permit_join"],
                state={
                    "network_state": info["state"],
                    "ieee_address": info["ieee_address"],
                },
                online=True,
            )
        )
        for node in self._association_table():
            nodes.append(node)
        return nodes

    def _association_table(self) -> List[NodeInfo]:
        """Read the coordinator's associated-device table via UTIL_ASSOC_LIST.

        Returns an empty list when the table is empty or the command is not
        supported, rather than raising - an empty network is a valid state.
        """
        out: List[NodeInfo] = []
        start_index = 0
        try:
            while True:
                data = self.request(
                    MT_UTIL,
                    UTIL_ASSOC_LIST,
                    bytes([start_index, 0x01]),
                )
                if len(data) < 4:
                    break
                status = data[0]
                if status != 0:
                    break
                count = data[1]
                if count == 0:
                    break
                # Each entry: ieee(8, LE) | nwk(2, LE) | type(1)
                off = 2
                for _ in range(count):
                    if off + 11 > len(data):
                        break
                    ieee = bytes(data[off : off + 8])[::-1].hex(":")
                    nwk = int.from_bytes(data[off + 8 : off + 10], "little")
                    ntype = data[off + 10]
                    out.append(
                        NodeInfo(
                            ieee=ieee,
                            driver=self.name,
                            nwk=f"0x{nwk:04X}",
                            node_type={
                                0: "coordinator",
                                1: "router",
                                2: "enddevice",
                            }.get(ntype, "unknown"),
                            power_source="unknown",
                            capabilities=self._guess_capabilities(ntype),
                            online=True,
                        )
                    )
                    off += 11
                start_index += count
                if count < 1:
                    break
        except DriverError:
            return out
        return out

    @staticmethod
    def _guess_capabilities(node_type: int) -> List[str]:
        """Best-effort capability list from the node type.

        The ZNP association table carries only the node type; the full simple
        descriptor (which lists real clusters) requires an active ZDO request.
        """
        if node_type == 1:
            return ["router"]
        if node_type == 2:
            return ["enddevice"]
        return ["coordinator"]

    def permit_join(self, seconds: int) -> None:
        """Open the network for new devices.

        Sends ZDO_MGMT_PERMIT_JOIN_REQ to the coordinator itself, then records
        the local expiry time.

        The payload is laid out AddrMode(1) | DstAddr(2, LE) | Duration(1) |
        TCSignificance(1). AddrMode MUST be 0x02 (16-bit address): 0xFC/0xFF are
        invalid and the stack answers status 0x02 (ZFailure). Broadcast to
        0xFFFF does not permit-join the coordinator itself, so we unicast to
        0x0000, which is what "Add new device" actually needs.

        Duration 0xFF is capped by the stack at 255 s rather than meaning
        "forever", so clamp to 0xFE and re-arm if a longer window is wanted.
        """
        seconds = max(0, min(int(seconds), 254))
        payload = bytes(
            [
                0x02,  # addressing mode: 16-bit short address
                0x00,  # destination low byte
                0x00,  # destination high byte -> 0x0000 = coordinator itself
                seconds & 0xFF,  # duration in seconds
                0x00,  # TC significance
            ]
        )
        try:
            data = self.request(MT_ZDO, ZDO_MGMT_PERMIT_JOIN_REQ, payload, timeout=3.0)
        except DriverError as exc:
            raise DriverError(f"permit_join failed: {exc}") from exc
        if data and data[0] != 0:
            raise DriverError(f"permit_join refused, status {data[0]:#04x}")
        self._permit_until = time.time() + seconds if seconds else 0.0

    def permit_status(self) -> Dict[str, Any]:
        """Seconds remaining in the current permit-join window."""
        remaining = max(0, int(self._permit_until - time.time()))
        return {"open": remaining > 0, "seconds_remaining": remaining}

    def get_state(self, ieee: str) -> Dict[str, Any]:
        """Live state of one node. For the coordinator, the network state."""
        ieee = ieee.lower()
        if self._coordinator_ieee and ieee == self._coordinator_ieee.lower():
            info = self.coordinator_info()
            return {
                "network_state": info["state"],
                "short_address": info["short_address"],
            }
        node = next(
            (n for n in self._association_table() if n.ieee.lower() == ieee), None
        )
        if not node:
            raise DriverError(f"node {ieee} not in association table")
        return {"nwk": node.nwk, "node_type": node.node_type, "online": node.online}

    def set_state(self, ieee: str, key: str, value: Any) -> Dict[str, Any]:
        """Write a state attribute.

        End-device cluster writes (On/Off, Level) need an AF_DATA_REQUEST with
        the target's endpoint and cluster; the association table does not carry
        those. Until an interview populates them, only coordinator-local
        attributes are writable.
        """
        ieee = ieee.lower()
        if self._coordinator_ieee and ieee == self._coordinator_ieee.lower():
            raise DriverError(
                "coordinator state is read-only (use /api/commission to "
                "open the network)"
            )
        raise DriverError(
            f"node {ieee} has no interviewed endpoint map yet - writes land "
            "once the device interview records its clusters"
        )

    def remove_node(self, ieee: str) -> None:
        """Ask the coordinator to remove a node from the network.

        ZDO_MGMT_LEAVE_REQ with the target's IEEE address.
        """
        raw = bytes.fromhex(ieee.replace(":", ""))
        if len(raw) != 8:
            raise DriverError(f"bad ieee address: {ieee}")
        payload = bytes([0x00, 0x00]) + raw[::-1] + bytes([0x00])
        data = self.request(MT_ZDO, ZDO_MGMT_LEAVE_REQ, payload, timeout=3.0)
        if data and data[0] != 0:
            raise DriverError(f"leave refused, status {data[0]:#04x}")

    def info(self) -> Dict[str, Any]:
        """Driver identity for /api/hub."""
        base = {"name": self.name, "type": "zigbee", "port": self.port}
        try:
            base["firmware"] = self.version()
            base["coordinator"] = self.coordinator_info()
            base["online"] = True
            base["error"] = None
        except (DriverError, serial.SerialException, OSError) as exc:
            base["online"] = False
            base["error"] = str(exc)
        return base
