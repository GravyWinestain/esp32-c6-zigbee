#!/usr/bin/env python3
"""tests/test_hub.py - Unit tests for the Zigbee hub.

Covers the pieces that can be tested without hardware: the registry, the
UNPI frame builder, the response-type matching rule (the bug that made every
ZDO reply invisible), and the API surface driven against a fake driver.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.core import Hub  # noqa: E402
from hub.db import Device, Registry  # noqa: E402
from hub.drivers.base import Driver, DriverError, NodeInfo  # noqa: E402
from hub.drivers.zigbee import (  # noqa: E402
    MT_SYS,
    MT_UTIL,
    MT_ZDO,
    SRSP_TYPE_OFFSETS,
    _unpi,
)

# -- fixtures ---------------------------------------------------------------


@pytest.fixture()
def registry(tmp_path: Path) -> Registry:
    """A throwaway registry backed by a temp database."""
    reg = Registry(tmp_path / "test.db")
    yield reg
    reg.close()


class FakeDriver(Driver):
    """In-memory driver that stands in for a radio.

    The name is settable so tests can stand in for the "zigbee" driver, which
    Hub.permit_join looks up by name.
    """

    name = "fake"

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.nodes: Dict[str, NodeInfo] = {}
        self.permit_seconds = 0
        self.removed: List[str] = []
        self.online = True
        self.writes: List[tuple] = []

    def start(self) -> None:
        self.online = True

    def stop(self) -> None:
        self.online = False

    def is_online(self) -> bool:
        return self.online

    def list_nodes(self) -> List[NodeInfo]:
        return list(self.nodes.values())

    def permit_join(self, seconds: int) -> None:
        self.permit_seconds = seconds

    def permit_status(self) -> Dict[str, Any]:
        return {
            "open": self.permit_seconds > 0,
            "seconds_remaining": self.permit_seconds,
        }

    def get_state(self, ieee: str) -> Dict[str, Any]:
        if ieee.lower() not in self.nodes:
            raise DriverError(f"unknown {ieee}")
        return {"value": 1}

    def set_state(self, ieee: str, key: str, value: Any) -> Dict[str, Any]:
        self.writes.append((ieee, key, value))
        return {key: value}

    def remove_node(self, ieee: str) -> None:
        self.removed.append(ieee)
        self.nodes.pop(ieee.lower(), None)


@pytest.fixture()
def hub(registry: Registry) -> Hub:
    """A hub with one fake driver, not polled."""
    return Hub(registry, [FakeDriver()])


@pytest.fixture()
def hub_as_zigbee(registry: Registry) -> Hub:
    """A hub whose fake driver is named 'zigbee'.

    Hub.permit_join looks the driver up by that name, so commissioning tests
    need a driver registered under it.
    """
    return Hub(registry, [FakeDriver(name="zigbee")])


# -- registry ---------------------------------------------------------------


def test_registry_upsert_and_get(registry: Registry) -> None:
    dev = Device(
        ieee="00:11:22:33:44:55:66:77",
        driver="zigbee",
        node_type="enddevice",
        capabilities=["onoff"],
    )
    saved = registry.upsert(dev)
    assert saved.ieee == "00:11:22:33:44:55:66:77"
    assert saved.first_seen is not None
    fetched = registry.get("00:11:22:33:44:55:66:77")
    assert fetched is not None
    assert fetched.capabilities == ["onoff"]


def test_registry_upsert_preserves_first_seen_and_name(registry: Registry) -> None:
    ieee = "aa:bb:cc:dd:ee:ff:00:11"
    first = registry.upsert(Device(ieee=ieee, driver="zigbee"))
    registry.rename(ieee, "Kitchen plug")
    second = registry.upsert(Device(ieee=ieee, driver="zigbee", state={"x": 1}))
    assert second.first_seen == first.first_seen
    assert second.friendly_name == "Kitchen plug"
    assert second.state == {"x": 1}


def test_registry_update_state_merges(registry: Registry) -> None:
    ieee = "aa:bb:cc:dd:ee:ff:00:22"
    registry.upsert(Device(ieee=ieee, driver="zigbee", state={"a": 1}))
    registry.update_state(ieee, {"b": 2})
    dev = registry.get(ieee)
    assert dev is not None
    assert dev.state == {"a": 1, "b": 2}


def test_registry_delete(registry: Registry) -> None:
    ieee = "aa:bb:cc:dd:ee:ff:00:33"
    registry.upsert(Device(ieee=ieee, driver="zigbee"))
    assert registry.delete(ieee) is True
    assert registry.get(ieee) is None
    assert registry.delete(ieee) is False


def test_registry_events(registry: Registry) -> None:
    registry.log_event("join", "aa:bb:cc:dd:ee:ff:00:44", {"node_type": "router"})
    registry.log_event("leave", "aa:bb:cc:dd:ee:ff:00:44", {})
    events = registry.events(limit=10)
    assert len(events) == 2
    assert events[0]["kind"] == "leave"  # newest first
    assert events[1]["payload"]["node_type"] == "router"


def test_registry_lists_coordinator_first(registry: Registry) -> None:
    registry.upsert(Device(ieee="bb", driver="zigbee", node_type="enddevice"))
    registry.upsert(Device(ieee="aa", driver="zigbee", node_type="coordinator"))
    order = [d.ieee for d in registry.list()]
    assert order[0] == "aa"


# -- UNPI framing -----------------------------------------------------------


def test_unpi_frame_layout() -> None:
    """FE | len | mt | cmd | data | fcs, with len counting data bytes only."""
    frame = _unpi(MT_SYS, 0x02)
    assert frame == bytes([0xFE, 0x00, 0x41, 0x02, 0x43])


def test_unpi_fcs_is_xor_of_len_mt_cmd_data() -> None:
    frame = _unpi(MT_ZDO, 0x36, bytes([0xFF, 0xFC]))
    body = frame[2:-1]
    expected = len(frame) - 5
    for b in body:
        expected ^= b
    assert frame[-1] == expected


def test_unpi_payload_length() -> None:
    payload = bytes([0x01, 0x02, 0x03])
    frame = _unpi(MT_UTIL, 0x00, payload)
    assert frame[1] == 3
    assert frame[4:7] == payload


# -- the response-type bug --------------------------------------------------


def test_zdo_srsp_type_is_not_0x20() -> None:
    """ZDO replies use 0x25|0x40 = 0x65. OR-ing 0x20 is a no-op on 0x25.

    This is why the original bug was total rather than partial: 0x25 already
    has bit 0x20 set, so `mt | 0x20` == 0x25 == the *request* type. The parser
    therefore only ever accepted a reply whose type equalled the request's,
    which never happens, so every ZDO reply was discarded.
    """
    assert MT_ZDO | SRSP_TYPE_OFFSETS[MT_ZDO] == 0x65
    assert MT_ZDO | 0x20 == MT_ZDO  # no-op, not 0x45
    assert MT_ZDO | 0x20 != 0x65
    assert SRSP_TYPE_OFFSETS[MT_ZDO] != 0x20


def test_sys_srsp_type_is_0x20() -> None:
    """SYS and UTIL replies do use 0x20 (0x41 -> 0x61)."""
    assert MT_SYS | SRSP_TYPE_OFFSETS[MT_SYS] == 0x61
    assert MT_UTIL | SRSP_TYPE_OFFSETS[MT_UTIL] == 0x67


def test_observed_zdo_reply_parses() -> None:
    """A real permit-join SRSP captured from the dongle must match."""

    def matches(buf: bytes, mt: int, cmd: int) -> bool:
        srsp = mt | SRSP_TYPE_OFFSETS.get(mt, 0x20)
        idx = buf.find(bytes([0xFE]))
        if idx == -1 or idx + 4 > len(buf):
            return False
        length = buf[idx + 1]
        return (
            buf[idx + 2] == srsp
            and buf[idx + 3] == cmd
            and idx + 4 + length <= len(buf)
        )

    # fe 01 65 36 02 50  = permit-join SRSP, status 0x02
    assert matches(bytes.fromhex("fe0165360250"), MT_ZDO, 0x36)
    # fe 01 65 40 00 24  = startup SRSP, status 0x00 (success)
    assert matches(bytes.fromhex("fe0165400024"), MT_ZDO, 0x40)

    # The old rule computed `mt | 0x20`, which for ZDO (0x25) is a no-op, so
    # it compared the reply type against 0x25 - the request type - and never
    # matched. Assert that directly rather than through the fixed helper.
    old_srsp_rule = MT_ZDO | 0x20
    assert old_srsp_rule == 0x25
    assert bytes.fromhex("fe0165360250")[2] != old_srsp_rule


# -- hub reconciliation -----------------------------------------------------


def test_reconcile_absorbs_nodes(hub: Hub) -> None:
    drv = hub.drivers["fake"]
    drv.nodes["aa:bb:cc:dd:ee:ff:00:55"] = NodeInfo(
        ieee="aa:bb:cc:dd:ee:ff:00:55",
        driver="fake",
        node_type="enddevice",
        capabilities=["onoff"],
    )
    assert hub.reconcile() == 1
    dev = hub.registry.get("aa:bb:cc:dd:ee:ff:00:55")
    assert dev is not None
    assert dev.node_type == "enddevice"


def test_reconcile_logs_join_only_once(hub: Hub) -> None:
    drv = hub.drivers["fake"]
    drv.nodes["aa:bb:cc:dd:ee:ff:00:66"] = NodeInfo(
        ieee="aa:bb:cc:dd:ee:ff:00:66", driver="fake"
    )
    hub.reconcile()
    hub.reconcile()
    joins = [e for e in hub.registry.events() if e["kind"] == "join"]
    assert len(joins) == 1


def test_reconcile_marks_missing_offline(hub: Hub) -> None:
    drv = hub.drivers["fake"]
    drv.nodes["aa:bb:cc:dd:ee:ff:00:77"] = NodeInfo(
        ieee="aa:bb:cc:dd:ee:ff:00:77", driver="fake"
    )
    hub.reconcile()
    drv.nodes.clear()
    hub.reconcile()
    dev = hub.registry.get("aa:bb:cc:dd:ee:ff:00:77")
    assert dev is not None
    assert dev.online is False


def test_reconcile_skips_offline_driver(hub: Hub) -> None:
    drv = hub.drivers["fake"]
    drv.nodes["aa:bb:cc:dd:ee:ff:00:88"] = NodeInfo(
        ieee="aa:bb:cc:dd:ee:ff:00:88", driver="fake"
    )
    drv.online = False
    assert hub.reconcile() == 0


def test_permit_join_records_event(hub_as_zigbee: Hub) -> None:
    result = hub_as_zigbee.permit_join(120)
    assert result["seconds"] == 120
    assert hub_as_zigbee.drivers["zigbee"].permit_seconds == 120
    kinds = [e["kind"] for e in hub_as_zigbee.registry.events()]
    assert "permit_join" in kinds


def test_permit_join_errors_without_zigbee_driver(hub: Hub) -> None:
    """A hub with no 'zigbee' driver must raise, not silently succeed."""
    with pytest.raises(DriverError):
        hub.permit_join(60)


def test_set_state_routes_to_driver(hub: Hub) -> None:
    ieee = "aa:bb:cc:dd:ee:ff:00:99"
    hub.registry.upsert(Device(ieee=ieee, driver="fake"))
    state = hub.set_state(ieee, "onoff", True)
    assert state == {"onoff": True}
    assert hub.drivers["fake"].writes == [(ieee, "onoff", True)]


def test_remove_device_calls_driver(hub: Hub) -> None:
    ieee = "aa:bb:cc:dd:ee:ff:00:aa"
    hub.registry.upsert(Device(ieee=ieee, driver="fake"))
    hub.remove_device(ieee)
    assert ieee in hub.drivers["fake"].removed
    assert hub.registry.get(ieee) is None


def test_hub_info_shape(hub: Hub) -> None:
    hub.registry.upsert(Device(ieee="aa", driver="fake"))
    info = hub.info()
    assert info["hub"] == "zigbee-hub"
    assert info["device_count"] == 1
    assert isinstance(info["drivers"], list)


# -- API surface ------------------------------------------------------------


def test_api_device_lifecycle(hub_as_zigbee: Hub) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hub.api import build_router

    app = FastAPI()
    app.include_router(build_router(hub_as_zigbee))
    client = TestClient(app)

    r = client.post(
        "/api/devices",
        json={
            "ieee": "11:22:33:44:55:66:77:88",
            "friendly_name": "Test node",
            "node_type": "enddevice",
        },
    )
    assert r.status_code == 200
    assert r.json()["device"]["friendly_name"] == "Test node"

    r = client.get("/api/devices")
    assert r.json()["count"] == 1

    r = client.patch(
        "/api/devices/11:22:33:44:55:66:77:88", json={"friendly_name": "Renamed"}
    )
    assert r.json()["device"]["friendly_name"] == "Renamed"

    r = client.post(
        "/api/devices/11:22:33:44:55:66:77:88/state",
        json={"key": "onoff", "value": True},
    )
    assert r.status_code == 200

    r = client.get("/api/commission")
    assert r.json()["open"] is False

    r = client.post("/api/commission", json={"seconds": 60})
    assert r.json()["ok"] is True
    assert client.get("/api/commission").json()["open"] is True

    r = client.delete("/api/devices/11:22:33:44:55:66:77:88")
    assert r.json()["ok"] is True
    assert client.get("/api/devices").json()["count"] == 0


def test_api_404_for_unknown_device(hub: Hub) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hub.api import build_router

    app = FastAPI()
    app.include_router(build_router(hub))
    client = TestClient(app)
    assert client.get("/api/devices/does:not:exist").status_code == 404


def test_api_events_and_hub(hub: Hub) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hub.api import build_router

    app = FastAPI()
    app.include_router(build_router(hub))
    client = TestClient(app)
    hub.registry.log_event("join", "aa", {})
    assert client.get("/api/events").json()["count"] == 1
    assert client.get("/api/hub").json()["hub"] == "zigbee-hub"
    assert client.post("/api/reconcile").json()["ok"] is True


# -- ZDO permit-join wire format (regression) -------------------------------


def test_permit_join_payload_matches_verified_wire_format() -> None:
    """Pin the ZDO_MGMT_PERMIT_JOIN_REQ payload against captured hardware bytes.

    The payload is AddrMode(1) | DstAddr(2, LE) | Duration(1) | TCSignificance(1).
    An AddrMode of 0xFC/0xFF (or transposing AddrMode and DstAddr) makes the
    stack answer status 0x02 ZFailure and nothing can ever join. Verified on a
    CC2652P running firmware 20250321: AddrMode 0x02 with DstAddr 0x0000
    returns status 0x00.
    """
    import types

    from hub.drivers.zigbee import ZDO_MGMT_PERMIT_JOIN_REQ, ZigbeeDriver

    captured = {}

    def fake_request(self, mt, cmd, payload=b"", timeout=1.0):
        captured["mt"] = mt
        captured["cmd"] = cmd
        captured["payload"] = payload
        return b"\x00"

    driver = ZigbeeDriver.__new__(ZigbeeDriver)
    driver._permit_until = 0.0
    driver.request = types.MethodType(fake_request, driver)

    driver.permit_join(120)

    assert captured["cmd"] == ZDO_MGMT_PERMIT_JOIN_REQ
    payload = captured["payload"]
    assert len(payload) == 5, f"permit-join payload must be 5 bytes, got {payload!r}"
    assert payload[0] == 0x02, "AddrMode must be 0x02 (16-bit); 0xFC/0xFF is invalid"
    assert payload[1:3] == b"\x00\x00", "DstAddr must be 0x0000 (the coordinator)"
    assert payload[3] == 120, "Duration must sit at offset 3"
    assert payload != b"\xff\xfc\x00\x00\x00", "addressing mode / dst are transposed"


def test_permit_join_duration_is_clamped_below_0xff() -> None:
    """0xFF is capped at 255 s by the stack; the driver must clamp to 0xFE."""
    import types

    from hub.drivers.zigbee import ZigbeeDriver

    captured = {}

    def fake_request(self, mt, cmd, payload=b"", timeout=1.0):
        captured["payload"] = payload
        return b"\x00"

    driver = ZigbeeDriver.__new__(ZigbeeDriver)
    driver._permit_until = 0.0
    driver.request = types.MethodType(fake_request, driver)

    driver.permit_join(9999)
    assert captured["payload"][3] == 0xFE
