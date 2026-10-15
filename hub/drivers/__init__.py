#!/usr/bin/env python3
"""hub/drivers/__init__.py - Driver registry for the Zigbee hub.

Drivers self-register here so the hub core can construct them by name from
configuration rather than hard-coding imports.
"""

from typing import Dict, Type

from .base import Driver, DriverError, NodeInfo
from .zigbee import ZigbeeDriver

DRIVERS: Dict[str, Type[Driver]] = {
    ZigbeeDriver.name: ZigbeeDriver,
}

__all__ = ["Driver", "DriverError", "NodeInfo", "ZigbeeDriver", "DRIVERS"]
