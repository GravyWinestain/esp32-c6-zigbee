#!/usr/bin/env python3
"""form_network.py - Form a Zigbee network on the CC2652P via zigpy-znp.

zigpy-znp uses its own ZDO/NV configuration sequence rather than the SAPI
ZB_WRITE_CONFIGURATION path, which this firmware build acknowledges but
ignores. Run with the hub service stopped so the serial port is free.

Usage:
    python form_network.py [--channels 15] [--pan 0x1a2b]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import zigpy.config as conf
from zigpy_znp.zigbee.application import ControllerApplication

PORT = "/dev/ttyUSB0"
BAUD = 115200


async def main(channels: list[int], pan_id: int) -> None:
    """Form (or rejoin) a Zigbee network and report what the radio reports."""
    config = {
        conf.CONF_DEVICE: {
            conf.CONF_DEVICE_PATH: PORT,
            conf.CONF_DEVICE_BAUDRATE: BAUD,
        },
        conf.CONF_NWK: {
            conf.CONF_NWK_CHANNELS: channels,
            conf.CONF_NWK_PAN_ID: pan_id,
        },
    }

    print(f"connecting to {PORT} @ {BAUD} ...")
    app = await ControllerApplication.new(config)
    print(f"  state after connect: {app.state}")

    info = app.state.node_info
    print(f"  nwk     : 0x{info.nwk:04x}")
    print(f"  ieee    : {info.ieee}")
    print(f"  logical : {info.logical_type}")

    # If there is no usable network yet, form one.
    if info.nwk in (0x0000, 0xFFFE):
        print("\nforming network ...")
        await app.form_network()
        await asyncio.sleep(3)
        info = app.state.node_info
        print(f"  nwk  now: 0x{info.nwk:04x}")
        print(f"  chan now: {info.channel}")

    # Open the join window so the C6 can associate.
    print("\nopening permit-join for 180 s ...")
    await app.permit(180)
    print("  permit-join open")

    print("\n=== summary ===")
    print(f"  network formed : {app.state.node_info.nwk not in (0x0000, 0xFFFE)}")
    print(f"  nwk            : 0x{app.state.node_info.nwk:04x}")
    print(f"  pan id         : 0x{app.state.node_info.pan_id:04x}")
    print(f"  channel        : {app.state.node_info.channel}")

    # Leave the network running; do not shut the stack down.
    await asyncio.sleep(2)
    await app.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", type=int, nargs="+", default=[15])
    ap.add_argument("--pan", type=lambda s: int(s, 0), default=0x1A2B)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    asyncio.run(main(args.channels, args.pan))
