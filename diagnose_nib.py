#!/usr/bin/env python3
"""diagnose_nib.py - Read the coordinator's NIB and say exactly why no network.

zigpy-znp's _load_network_info raises NetworkNotFormed when either
    nib.nwkLogicalChannel == 0   (no channel chosen)
    not nib.nwkKeyLoaded         (no network key)
This script reads the raw NIB plus the config items that feed it, so we can see
which condition fails - and whether the earlier SAPI config writes landed.

Usage:
    python diagnose_nib.py
"""

from __future__ import annotations

import asyncio
import logging

import zigpy.config as conf
import zigpy_znp.types as t
from zigpy_znp.api import ZNP
from zigpy_znp.types.nvids import OsalNvIds

PORT = "/dev/ttyUSB0"


async def main() -> None:
    """Dump the NIB and the key config NVRAM items."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    from zigpy_znp.config import CONFIG_SCHEMA

    config = CONFIG_SCHEMA({conf.CONF_DEVICE: {conf.CONF_DEVICE_PATH: PORT}})
    znp = ZNP(config)
    await znp.connect()
    print(f"connected to {PORT}\n")

    ieee = await znp.nvram.osal_read(OsalNvIds.EXTADDR, item_type=t.EUI64)
    print(f"IEEE address (EXTADDR) : {ieee}")

    logical = await znp.nvram.osal_read(OsalNvIds.LOGICAL_TYPE, item_type=t.DeviceLogicalType)
    print(f"LOGICAL_TYPE           : {logical}")

    print("\n--- the NIB (the network record) ---")
    try:
        nib = await znp.nvram.osal_read(OsalNvIds.NIB, item_type=t.NIB)
        print(f"  nwkLogicalChannel    : {nib.nwkLogicalChannel}")
        print(f"  nwkKeyLoaded         : {nib.nwkKeyLoaded}")
        print(f"  nwkDevAddress        : 0x{nib.nwkDevAddress:04x}")
        print(f"  nwkPanId             : 0x{nib.nwkPanId:04x}")
        print(f"  extendedPANID        : {nib.extendedPANID}")
        print(f"  nwkUpdateId          : {nib.nwkUpdateId}")
        print(f"  channelList          : {nib.channelList}")
        print()
        print("  === why NetworkNotFormed is raised ===")
        print(f"  nwkLogicalChannel == 0 ? {nib.nwkLogicalChannel == 0}")
        print(f"  not nwkKeyLoaded       ? {not nib.nwkKeyLoaded}")
        if nib.nwkLogicalChannel == 0:
            print("  -> NO CHANNEL. This is the blocker.")
        if not nib.nwkKeyLoaded:
            print("  -> NO NETWORK KEY. This is the blocker.")
    except Exception as exc:  # noqa: BLE001 - report whatever the read does
        print(f"  NIB read failed: {type(exc).__name__}: {exc}")

    print("\n--- config items that FEED the NIB ---")
    for name, nvid in (
        ("NWK_PANID", getattr(OsalNvIds, "NWK_PANID", None)),
        ("CHANLIST", getattr(OsalNvIds, "CHANLIST", None)),
        ("PRECFGKEY", getattr(OsalNvIds, "PRECFGKEY", None)),
        ("STARTUP_OPTION", getattr(OsalNvIds, "STARTUP_OPTION", None)),
        ("NWKKEY", getattr(OsalNvIds, "NWKKEY", None)),
    ):
        if nvid is None:
            print(f"  {name:16s}: no such OsalNvId in this build")
            continue
        try:
            raw = await znp.nvram.osal_read(nvid, item_type=t.LongBytes)
            print(f"  {name:16s}: len={len(raw)} {bytes(raw).hex(' ')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:16s}: read failed - {type(exc).__name__}: {exc}")

    await znp.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
