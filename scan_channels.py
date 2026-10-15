#!/usr/bin/env python3
"""scan_channels.py - Energy-detect scan across every Zigbee channel.

Runs the coordinator's energy scan and prints the measured noise floor per
channel in dBm so we can see WHY formation is refused, and pick the quietest
channel. Higher (less negative) values mean more interference. Anything above
roughly -70 dBm is considered busy in a Zigbee energy scan.

Usage:
    python scan_channels.py
"""

from __future__ import annotations

import asyncio
import logging

import zigpy.config as conf
from zigpy_znp.zigbee.application import ControllerApplication

PORT = "/dev/ttyUSB0"
BAUD = 115200
ALL_CHANNELS = list(range(11, 27))


async def main() -> None:
    """Measure the noise floor on every channel and rank them."""
    config = {
        conf.CONF_DEVICE: {
            conf.CONF_DEVICE_PATH: PORT,
            conf.CONF_DEVICE_BAUDRATE: BAUD,
        },
        conf.CONF_NWK: {
            conf.CONF_NWK_CHANNELS: ALL_CHANNELS,
        },
    }

    app = await ControllerApplication.new(config)
    print(f"connected: nwk=0x{app.state.node_info.nwk:04x}")
    print(f"scanning channels {ALL_CHANNELS[0]}-{ALL_CHANNELS[-1]} ...\n")

    results = await app.energy_scan(ALL_CHANNELS)
    for ch in ALL_CHANNELS:
        print(f"  channel {ch:>2}: {results[ch]}")

    print("\n=== ranked quietest first ===")
    ranked = sorted(results, key=lambda c: results[c])
    for ch in ranked[:6]:
        print(f"  channel {ch:>2}: {results[ch]}")

    print(f"\nquietest channel: {ranked[0]}  ({results[ranked[0]]})")
    print(f"noisiest channel : {ranked[-1]} ({results[ranked[-1]]})")
    await app.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)
    asyncio.run(main())
