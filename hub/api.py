#!/usr/bin/env python3
"""hub/api.py - REST API for the Zigbee IoT hub.

Exposes a uniform device surface that works the same regardless of which
transport is underneath, plus coordinator-specific operations (permit-join,
device removal) and the event journal.

Every response is JSON. Errors carry a stable {"ok": false, "error": "..."}
shape so clients can branch on the error text without parsing prose.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from .core import Hub
from .db import Device
from .drivers.base import DriverError

log = logging.getLogger("hub.api")


# -- request models ---------------------------------------------------------


class CommissionRequest(BaseModel):
    """Body for POST /api/commission."""

    seconds: int = Field(
        default=180, ge=0, le=254, description="Permit-join window length in seconds"
    )


class RenameRequest(BaseModel):
    """Body for PATCH /api/devices/{ieee}."""

    friendly_name: str = Field(min_length=1, max_length=64)


class StateRequest(BaseModel):
    """Body for POST /api/devices/{ieee}/state."""

    key: str = Field(min_length=1)
    value: Any = None


class RegisterRequest(BaseModel):
    """Body for POST /api/devices - manual registry entry."""

    ieee: str = Field(min_length=8, max_length=32)
    driver: str = "zigbee"
    friendly_name: Optional[str] = None
    model: Optional[str] = None
    vendor: Optional[str] = None
    node_type: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)


def _device_json(device: Device) -> Dict[str, Any]:
    """Serialise a Device for the API."""
    return {
        "ieee": device.ieee,
        "nwk": device.nwk,
        "driver": device.driver,
        "model": device.model,
        "vendor": device.vendor,
        "node_type": device.node_type,
        "power_source": device.power_source,
        "friendly_name": device.friendly_name,
        "capabilities": device.capabilities,
        "state": device.state,
        "first_seen": device.first_seen,
        "last_seen": device.last_seen,
        "online": device.online,
    }


def build_router(hub: Hub) -> APIRouter:
    """Build the API router bound to a live Hub instance."""
    router = APIRouter(prefix="/api")

    # -- hub ---------------------------------------------------------------

    @router.get("/hub")
    def hub_info() -> Dict[str, Any]:
        """Hub identity, driver status and device count."""
        return {"ok": True, **hub.info()}

    # -- devices -----------------------------------------------------------

    @router.get("/devices")
    def list_devices(
        online_only: bool = False, node_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """List every device in the registry, optionally filtered."""
        devices = hub.registry.list()
        if online_only:
            devices = [d for d in devices if d.online]
        if node_type:
            devices = [d for d in devices if d.node_type == node_type]
        return {
            "ok": True,
            "count": len(devices),
            "devices": [_device_json(d) for d in devices],
        }

    @router.post("/devices")
    def register_device(req: RegisterRequest) -> Dict[str, Any]:
        """Manually register a device (for nodes a driver cannot enumerate)."""
        device = Device(
            ieee=req.ieee.lower(),
            driver=req.driver,
            friendly_name=req.friendly_name,
            model=req.model,
            vendor=req.vendor,
            node_type=req.node_type,
            capabilities=req.capabilities,
            online=False,
        )
        saved = hub.registry.upsert(device)
        hub.registry.log_event("register", saved.ieee, {"source": "manual"})
        return {"ok": True, "device": _device_json(saved)}

    @router.get("/devices/{ieee}")
    def get_device(ieee: str) -> Dict[str, Any]:
        """One device, including its last known state."""
        device = hub.registry.get(ieee)
        if not device:
            raise HTTPException(404, f"unknown device {ieee}")
        return {"ok": True, "device": _device_json(device)}

    @router.patch("/devices/{ieee}")
    def rename_device(ieee: str, req: RenameRequest) -> Dict[str, Any]:
        """Set a device's friendly name."""
        device = hub.registry.rename(ieee, req.friendly_name)
        if not device:
            raise HTTPException(404, f"unknown device {ieee}")
        return {"ok": True, "device": _device_json(device)}

    @router.delete("/devices/{ieee}")
    def delete_device(ieee: str, forget_radio: bool = True) -> Dict[str, Any]:
        """Remove a device. Asks the driver to drop it, then deletes the row."""
        device = hub.registry.get(ieee)
        if not device:
            raise HTTPException(404, f"unknown device {ieee}")
        try:
            if forget_radio:
                hub.remove_device(ieee)
            else:
                hub.registry.delete(ieee)
                hub.registry.log_event("leave", ieee, {"source": "registry"})
        except DriverError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, "removed": ieee}

    @router.get("/devices/{ieee}/state")
    def get_device_state(ieee: str) -> Dict[str, Any]:
        """Read live state from the owning driver, falling back to stored."""
        try:
            state = hub.get_state(ieee)
        except DriverError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "ieee": ieee, "state": state}

    @router.post("/devices/{ieee}/state")
    def set_device_state(ieee: str, req: StateRequest = Body(...)) -> Dict[str, Any]:
        """Write one state attribute through the owning driver."""
        try:
            state = hub.set_state(ieee, req.key, req.value)
        except DriverError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, "ieee": ieee, "state": state}

    # -- commissioning -----------------------------------------------------

    @router.post("/commission")
    def commission(req: CommissionRequest = Body(...)) -> Dict[str, Any]:
        """Open the network for new devices (permit-join)."""
        try:
            result = hub.permit_join(req.seconds)
        except DriverError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, **result}

    @router.get("/commission")
    def commission_status() -> Dict[str, Any]:
        """Current permit-join window."""
        return {"ok": True, **hub.permit_status()}

    @router.post("/reconcile")
    def reconcile() -> Dict[str, Any]:
        """Force an immediate poll of every driver into the registry."""
        try:
            count = hub.reconcile()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, str(exc)) from exc
        return {
            "ok": True,
            "nodes_seen": count,
            "device_count": len(hub.registry.list()),
        }

    # -- events ------------------------------------------------------------

    @router.get("/events")
    def events(
        limit: int = Query(default=100, ge=1, le=1000), ieee: Optional[str] = None
    ) -> Dict[str, Any]:
        """Recent events, newest first."""
        rows = hub.registry.events(limit=limit, ieee=ieee)
        return {"ok": True, "count": len(rows), "events": rows}

    return router
