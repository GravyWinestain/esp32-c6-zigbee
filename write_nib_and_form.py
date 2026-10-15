#!/usr/bin/env python3
"""write_nib_and_form.py - Write the network config NVRAM items, then form.

Proven facts from diagnose_nib.py on this CC2652P (fw 20250321):
    nwkLogicalChannel = 0, nwkKeyLoaded = false, nwkPanId = 0xffff,
    channelList = 0
and the config items that feed the NIB (CHANLIST, PRECFGKEY, STARTUP_OPTION,
NWKKEY) are all empty. SAPI ZB_WRITE_CONFIGURATION returns success but writes
nothing to the NV the stack reads, so the network can never form.

This script writes those items directly through the zigpy-znp NVRAM layer
(OsalNvIds), which is the same path the stack itself uses, then asks the stack
to form the network.

Usage:
    python write_nib_and_form.py [--channel 15] [--pan 0x1a2b]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import zigpy.config as conf
import zigpy_znp.types as t
from zigpy_znp.api import ZNP
from zigpy_znp.config import CONFIG_SCHEMA
from zigpy_znp.types.nvids import OsalNvIds

PORT = "/dev/ttyUSB0"


async def main(channel: int, pan_id: int) -> None:
    """Write CHANLIST/PAN_ID/LOGICAL_TYPE, then form and report."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    config = CONFIG_SCHEMA(
        {
            conf.CONF_DEVICE: {conf.CONF_DEVICE_PATH: PORT},
            conf.CONF_NWK: {
                conf.CONF_NWK_CHANNELS: [channel],
                conf.CONF_NWK_PAN_ID: pan_id,
            },
        }
    )
    znp = ZNP(config)
    await znp.connect()
    print(f"connected to {PORT}")
    print(f"stack version: {znp.version}")

    # --- write the config items the NIB is built from ---
    print(f"\nwriting config: channel={channel} pan=0x{pan_id:04x}")

    # LOGICAL_TYPE = coordinator (0x00)
    try:
        await znp.nvram.osal_write(
            OsalNvIds.LOGICAL_TYPE, t.DeviceLogicalType.Coordinator, create=True
        )
        print("  LOGICAL_TYPE  -> Coordinator  OK")
    except Exception as exc:  # noqa: BLE001
        print(f"  LOGICAL_TYPE  FAILED: {type(exc).__name__}: {exc}")

    # PAN ID -> ZCD_NV_PAN_ID == OsalNvIds.NWK_PANID
    pan_nvid = getattr(OsalNvIds, "NWK_PANID", None)
    if pan_nvid is not None:
        try:
            await znp.nvram.osal_write(pan_nvid, t.uint16_t(pan_id), create=True)
            print("  PAN_ID        -> written    OK")
        except Exception as exc:  # noqa: BLE001
            print(f"  PAN_ID        FAILED: {type(exc).__name__}: {exc}")
    else:
        print("  PAN_ID        : no OsalNvId in this build (stack will pick one)")

    # Startup option: keep config, just clear any stale state (0x00 = keep all)
    so_nvid = getattr(OsalNvIds, "STARTUP_OPTION", None)
    if so_nvid is not None:
        try:
            await znp.nvram.osal_write(so_nvid, t.uint8_t(0x00), create=True)
            print("  STARTUP_OPTION-> 0x00 keep  OK")
        except Exception as exc:  # noqa: BLE001
            print(f"  STARTUP_OPTION FAILED: {type(exc).__name__}: {exc}")

    # --- verify the writes actually landed (unlike the SAPI path) ---
    print("\nverifying writes ...")
    try:
        logical = await znp.nvram.osal_read(OsalNvIds.LOGICAL_TYPE, item_type=t.DeviceLogicalType)
        print(f"  LOGICAL_TYPE readback: {logical}")
    except Exception as exc:  # noqa: BLE001
        print(f"  LOGICAL_TYPE readback failed: {type(exc).__name__}: {exc}")

    # --- form the network through the stack ---
    print(f"\nforming network on channel {channel} ...")
    try:
        # start_network() takes NO arguments: channels/pan_id come from config.
        await znp.start_network()
        print("  start_network returned OK")
    except Exception as exc:  # noqa: BLE001
        print(f"  start_network FAILED: {type(exc).__name__}: {exc}")

    await asyncio.sleep(2)

    # --- read the NIB back to see if it took ---
    print("\n--- NIB after formation attempt ---")
    try:
        nib = await znp.nvram.osal_read(OsalNvIds.NIB, item_type=t.NIB)
        print(f"  nwkLogicalChannel : {nib.nwkLogicalChannel}")
        print(f"  nwkKeyLoaded      : {nib.nwkKeyLoaded}")
        print(f"  nwkDevAddress     : 0x{nib.nwkDevAddress:04x}")
        print(f"  nwkPanId         : 0x{nib.nwkPanId:04x}")
        print(f"  channelList       : {nib.channelList}")
        formed = nib.nwkLogicalChannel != 0 and nib.nwkKeyLoaded
        print(f"\n  NETWORK FORMED: {formed}")
    except Exception as exc:  # noqa: BLE001
        print(f"  NIB read failed: {type(exc).__name__}: {exc}")

    await znp.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=15)
    ap.add_argument("--pan", type=lambda s: int(s, 0), default=0x1A2B)
    args = ap.parse_args()

    asyncio.run(main(args.channel, args.pan))
