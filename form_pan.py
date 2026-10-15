#!/usr/bin/env python3
"""form_pan.py - Create a Zigbee PAN from scratch on a blank coordinator.

Diagnosis that led here (all verified on this CC2652P, fw 20250321, via
diagnose_nib.py and raw_energy_scan.py):

    nwkLogicalChannel = 0      -> never had a channel
    nwkKeyLoaded      = false  -> never had a network key
    nwkDevAddress     = 0xfffe -> never formed or joined
    nwkPanId          = 0xffff -> no PAN
    channelList       = 0      -> no channel to form ON

The NIB (network record) simply does not exist yet, so zigpy's start_network()
cannot pass its own guard and gives up. The SAPI route
(ZB_WRITE_CONFIGURATION) reports success but writes nothing the stack reads -
proven by the config items still being empty afterwards.

The fix is to build the network record explicitly and hand it to
write_network_info(), which writes the NIB the stack actually reads. Then
start_network() has something to start.

Usage:
    python form_pan.py --channel 15
    python form_pan.py --channel 15 --pan 0x1a2b   # specify PAN ID
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import zigpy.config as conf
import zigpy.state as app_state
import zigpy.zdo.types as zdo_t
import zigpy_znp.types as zigpy_znp_types
from zigpy_znp.zigbee.application import ControllerApplication

PORT = "/dev/ttyUSB0"
KEY = "zigbee-development"


async def main(channel: int, pan_id: int | None) -> None:
    """Build a network record, write it, then form the PAN."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    config = {
        "device": {"path": PORT},
        "network": {"channels": [channel]},
    }
    app = ControllerApplication(config)
    await app.connect()
    print(f"connected to {PORT}, stack {app._znp.version}\n")

    # Read the chip's own IEEE (load_network_info raises on a blank chip because
    # the NIB does not exist, so it never gets as far as reading this).
    from zigpy_znp.types.nvids import OsalNvIds

    ieee = await app._znp.nvram.osal_read(OsalNvIds.EXTADDR, item_type=zigpy_znp_types.EUI64)
    print(f"chip ieee       : {ieee}")
    app.state.node_info.ieee = ieee

    try:
        await app.load_network_info(load_devices=False)
    except Exception as exc:  # noqa: BLE001
        print(f"load_network_info: {type(exc).__name__}: {exc}")
    print(f"current ieee    : {app.state.node_info.ieee}")
    print(f"current channel : {app.state.network_info.channel}")

    # --- build the network record the coordinator has never had ---
    network_info = app_state.NetworkInfo(
        extended_pan_id=ieee,
        pan_id=pan_id if pan_id is not None else 0x1A2B,
        nwk_update_id=0,
        nwk_manager_id=0x0000,
        channel=channel,
        channel_mask=1 << channel,
        security_level=5,
        network_key=app_state.Key(
            key=os.urandom(16),
            tx_counter=0,
            rx_counter=0,
            seq=0,
        ),
    )
    node_info = app_state.NodeInfo(
        nwk=0x0000,
        ieee=ieee,
        logical_type=zdo_t.LogicalType.Coordinator,
    )

    print(f"\nwriting network record: ch={channel} pan=0x{network_info.pan_id:04x}")
    await app.write_network_info(network_info=network_info, node_info=node_info)
    print("  write_network_info OK")

    # --- verify it landed ---
    await app.load_network_info(load_devices=False)
    print(f"  readback channel : {app.state.network_info.channel}")
    print(f"  readback pan_id  : 0x{app.state.network_info.pan_id:04x}")
    print(f"  readback nwk     : 0x{app.state.node_info.nwk:04x}")
    print(f"  readback key set : {app.state.network_info.network_key.key != bytes(16)}")

    # --- now the real formation ---
    print("\nstarting network ...")
    try:
        await app.start_network()
        print("  start_network OK")
    except Exception as exc:  # noqa: BLE001
        print(f"  start_network FAILED: {type(exc).__name__}: {exc}")

    await asyncio.sleep(3)
    await app.load_network_info(load_devices=False)
    formed = app.state.network_info.channel != 0
    print(f"\nFINAL: channel={app.state.network_info.channel} nwk=0x{app.state.node_info.nwk:04x}")
    print(f"NETWORK FORMED: {formed}")

    await app.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=15)
    ap.add_argument("--pan", type=lambda s: int(s, 0), default=None)
    args = ap.parse_args()
    asyncio.run(main(args.channel, args.pan))
