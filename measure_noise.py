#!/usr/bin/env python3
"""measure_noise.py - Measure the real 2.4 GHz noise floor on every channel.

The stack refuses to form a network claiming "too much RF interference". This
measures that claim directly instead of trusting the error message.

Key wire fact discovered by reading zigpy_znp/commands/zdo.py: both
MgmtNWKUpdateReq (0x37) and MgmtNwkDiscReq (0x30) reply with STATUS_SCHEMA only.
The scan RESULTS do not come back in the response - they arrive asynchronously
as MgmtNWKUpdateNotify (0x3B) / MgmtNwkDiscRsp (0x30+1) callbacks. So the script
must arm a listener BEFORE issuing the request.

Usage:
    python measure_noise.py
"""

from __future__ import annotations

import asyncio
import logging

import zigpy.config as conf
import zigpy_znp.commands as c
import zigpy_znp.types as t
from zigpy_znp.api import ZNP
from zigpy_znp.config import CONFIG_SCHEMA

PORT = "/dev/ttyUSB0"
ALL_CHANNELS = list(range(11, 27))


async def main() -> None:
    """Energy-scan all 2.4 GHz channels and rank them by quietness."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    config = CONFIG_SCHEMA(
        {
            conf.CONF_DEVICE: {conf.CONF_DEVICE_PATH: PORT},
            conf.CONF_NWK: {conf.CONF_NWK_CHANNELS: ALL_CHANNELS},
        }
    )
    znp = ZNP(config)
    await znp.connect()
    print(f"connected to {PORT}, stack {znp.version}\n")

    results: list = []

    def on_notify(msg) -> None:
        """Collect the asynchronous scan report."""
        results.append(msg)

    # Arm the listener BEFORE issuing the request: the notify can arrive fast.
    # Zigpy-znp's API is callback_for_response(<an incoming command>, fn).
    listeners = [
        znp.callback_for_response(c.ZDO.MgmtNWKUpdateNotify.Callback(), on_notify),
        znp.callback_for_response(c.ZDO.MgmtNwkDiscRsp.Callback(), on_notify),
    ]
    print("listener armed for MgmtNWKUpdateNotify / MgmtNwkDiscRsp")

    # --- energy scan across every channel ---
    print(f"requesting energy scan over {len(ALL_CHANNELS)} channels ...")
    rsp = await znp.request(
        c.ZDO.MgmtNWKUpdateReq.Req(
            Dst=0x0000,
            DstAddrMode=t.AddrMode.NWK,
            Channels=t.Channels(0x07FFF800),  # channels 11-26
            ScanDuration=0x02,
            ScanCount=1,
            NwkManagerAddr=0x0000,
        )
    )
    print(f"  request status: {getattr(rsp, 'Status', rsp)}")

    # --- network discovery: is any Zigbee network on the air? ---
    print("\nrequesting network discovery ...")
    try:
        rsp2 = await znp.request(
            c.ZDO.MgmtNwkDiscReq.Req(
                Dst=0x0000,
                Channels=t.Channels(0x07FFF800),
                ScanDuration=0x02,
                StartIndex=0,
            )
        )
        print(f"  discovery status: {getattr(rsp2, 'Status', rsp2)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  discovery failed: {type(exc).__name__}: {exc}")

    await asyncio.sleep(8)

    print(f"\ncollected {len(results)} async report(s)")
    for msg in results:
        print(f"  {type(msg).__name__}: {msg}")

    await znp.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
