#!/usr/bin/env python3
"""hub/drivers/base.py - Abstract driver interface for the IoT hub.

A Driver is the hub's view of one radio/transport. The hub core talks only to
this interface, so adding Matter, MQTT or ESP-NOW later means writing one new
driver rather than touching the registry, the API or the UI.

Every driver must be usable from the FastAPI request threads and from the
background poll loop, so implementations are responsible for their own locking.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NodeInfo:
    """Transport-agnostic description of one node a driver can see."""

    ieee: str
    driver: str
    nwk: Optional[str] = None
    model: Optional[str] = None
    vendor: Optional[str] = None
    node_type: Optional[str] = None  # coordinator|router|enddevice
    power_source: Optional[str] = None  # mains|battery|unknown
    capabilities: List[str] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    online: bool = True


class DriverError(RuntimeError):
    """Raised when a driver cannot complete an operation."""


class Driver(ABC):
    """Base class for every hub transport driver."""

    name: str = "base"

    @abstractmethod
    def start(self) -> None:
        """Open the transport. Must be safe to call repeatedly."""

    @abstractmethod
    def stop(self) -> None:
        """Close the transport. Must be safe to call repeatedly."""

    @abstractmethod
    def is_online(self) -> bool:
        """True if the transport is currently reachable."""

    @abstractmethod
    def list_nodes(self) -> List[NodeInfo]:
        """Return every node currently visible to this driver."""

    @abstractmethod
    def permit_join(self, seconds: int) -> None:
        """Open the network for new devices for the given duration."""

    @abstractmethod
    def get_state(self, ieee: str) -> Dict[str, Any]:
        """Read the live state of one node."""

    @abstractmethod
    def set_state(self, ieee: str, key: str, value: Any) -> Dict[str, Any]:
        """Write one state attribute of one node; returns the new state."""

    @abstractmethod
    def remove_node(self, ieee: str) -> None:
        """Ask the network to forget a node."""

    def info(self) -> Dict[str, Any]:
        """Describe this driver for the hub info endpoint."""
        return {"name": self.name, "online": self.is_online()}
