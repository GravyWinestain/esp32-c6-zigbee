#!/usr/bin/env python3
"""hub/core.py - Hub core: owns drivers, the registry and the poll loop.

The Hub is the single object the API layer talks to. It holds one or more
drivers, reconciles what they report into the SQLite registry, and logs the
transitions worth knowing about (a node appearing, vanishing, or going
offline).

Polling is deliberately gentle: a driver that is offline is skipped, and a
driver that throws is marked offline rather than killing the loop.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .db import Device, Registry
from .drivers.base import Driver, DriverError, NodeInfo

log = logging.getLogger("hub.core")


class Hub:
    """The hub: a set of drivers plus a device registry."""

    def __init__(self, registry: Registry, drivers: List[Driver]) -> None:
        self.registry = registry
        self.drivers: Dict[str, Driver] = {d.name: d for d in drivers}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._poll_interval = 10.0
        self.last_poll: Optional[float] = None
        self.last_poll_error: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Start every driver, then optionally the background poll loop."""
        for name, driver in self.drivers.items():
            try:
                driver.start()
                log.info("driver %s started", name)
            except Exception as exc:  # noqa: BLE001 - one bad driver is not fatal
                log.warning("driver %s failed to start: %s", name, exc)
        if poll and self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="hub-poll"
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the poll loop and every driver."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        for driver in self.drivers.values():
            try:
                driver.stop()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass

    def _loop(self) -> None:
        """Reconcile drivers into the registry every poll interval."""
        while not self._stop.is_set():
            try:
                self.reconcile()
                self.last_poll_error = None
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self.last_poll_error = str(exc)
                log.warning("poll failed: %s", exc)
            self.last_poll = time.time()
            self._stop.wait(self._poll_interval)

    # -- reconciliation -----------------------------------------------------

    def reconcile(self) -> int:
        """Pull fresh node lists from all online drivers into the registry."""
        seen = 0
        for driver in self.drivers.values():
            if not driver.is_online():
                continue
            try:
                nodes = driver.list_nodes()
            except (DriverError, Exception):  # noqa: BLE001
                continue
            for node in nodes:
                self._absorb(node)
                seen += 1
        self._mark_missing_offline()
        return seen

    def _absorb(self, node: NodeInfo) -> Device:
        """Insert or refresh one node from a driver, logging transitions."""
        existing = self.registry.get(node.ieee)
        device = Device(
            ieee=node.ieee.lower(),
            driver=node.driver,
            nwk=node.nwk,
            model=node.model,
            vendor=node.vendor,
            node_type=node.node_type,
            power_source=node.power_source,
            friendly_name=existing.friendly_name if existing else None,
            capabilities=node.capabilities
            or (existing.capabilities if existing else []),
            state={**(existing.state if existing else {}), **node.state},
            online=node.online,
        )
        saved = self.registry.upsert(device)
        if existing is None:
            self.registry.log_event(
                "join",
                node.ieee,
                {
                    "node_type": node.node_type,
                    "driver": node.driver,
                    "model": node.model,
                },
            )
            log.info("new device: %s (%s)", node.ieee, node.node_type)
        elif not existing.online and node.online:
            self.registry.log_event("online", node.ieee, {})
        return saved

    def _mark_missing_offline(self) -> None:
        """Flag devices as offline when no online driver currently sees them."""
        live: set[str] = set()
        any_online = False
        for driver in self.drivers.values():
            if not driver.is_online():
                continue
            any_online = True
            try:
                live.update(n.ieee.lower() for n in driver.list_nodes())
            except Exception:  # noqa: BLE001
                continue
        if not any_online:
            return
        for dev in self.registry.list():
            if dev.ieee not in live and dev.online:
                self.registry.set_online(dev.ieee, False)
                self.registry.log_event("offline", dev.ieee, {})

    # -- operations ---------------------------------------------------------

    def permit_join(self, seconds: int = 180) -> Dict[str, Any]:
        """Open the Zigbee network for new devices."""
        driver = self.drivers.get("zigbee")
        if not driver:
            raise DriverError("no zigbee driver configured")
        driver.permit_join(seconds)
        self.registry.log_event("permit_join", None, {"seconds": seconds})
        return {
            "driver": driver.name,
            "seconds": seconds,
            "permit": driver.permit_status(),
        }  # type: ignore[attr-defined]

    def permit_status(self) -> Dict[str, Any]:
        """Current permit-join window, if a zigbee driver exists."""
        driver = self.drivers.get("zigbee")
        if not driver:
            return {"open": False, "seconds_remaining": 0}
        return driver.permit_status()  # type: ignore[attr-defined]

    def get_state(self, ieee: str) -> Dict[str, Any]:
        """Live state from the owning driver, else the stored state."""
        device = self.registry.get(ieee)
        if not device:
            raise DriverError(f"unknown device {ieee}")
        driver = self.drivers.get(device.driver)
        if driver and driver.is_online():
            try:
                state = driver.get_state(ieee)
                self.registry.update_state(ieee, state)
                return state
            except DriverError:
                pass
        return device.state

    def set_state(self, ieee: str, key: str, value: Any) -> Dict[str, Any]:
        """Write state through the owning driver and record it."""
        device = self.registry.get(ieee)
        if not device:
            raise DriverError(f"unknown device {ieee}")
        driver = self.drivers.get(device.driver)
        if not driver:
            raise DriverError(f"no driver {device.driver}")
        state = driver.set_state(ieee, key, value)
        self.registry.update_state(ieee, state)
        self.registry.log_event("state", ieee, {"key": key, "value": value})
        return state

    def remove_device(self, ieee: str) -> None:
        """Ask the driver to drop the node, then delete it from the registry."""
        device = self.registry.get(ieee)
        if not device:
            raise DriverError(f"unknown device {ieee}")
        driver = self.drivers.get(device.driver)
        if driver and driver.is_online():
            driver.remove_node(ieee)
        self.registry.delete(ieee)
        self.registry.log_event("leave", ieee, {})

    # -- description --------------------------------------------------------

    def info(self) -> Dict[str, Any]:
        """Hub identity and per-driver status for /api/hub."""
        return {
            "hub": "zigbee-hub",
            "version": "1.0.0",
            "drivers": [d.info() for d in self.drivers.values()],
            "device_count": len(self.registry.list()),
            "permit": self.permit_status(),
            "last_poll": self.last_poll,
            "last_poll_error": self.last_poll_error,
        }
