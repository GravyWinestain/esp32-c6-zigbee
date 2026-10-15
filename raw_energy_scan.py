#!/usr/bin/env python3
"""raw_energy_scan.py - Energy scan even when no network can be formed.

The stack refuses to form a network claiming "too much RF interference". This
measures that claim directly, so we get numbers instead of a refusal message.

Why this file exists rather than just calling the bundled tool: the shipped
`python -m zigpy_znp.tools.energy_scan` aborts when start_network(read_only=True)
raises NetworkNotFormed. But that call does two useful things before it fails -
it starts the Z-Stack application AND populates the device table - and the
energy scan itself works fine with no network. So we call it inside a
try/except and carry on.

Usage:
    python raw_energy_scan.py [--scans 3]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict, deque

from zigpy.exceptions import NetworkNotFormed
import zigpy.zdo.types as zdo_t
import zigpy_znp.types as t
from zigpy_znp.zigbee.application import ControllerApplication

PORT = "/dev/ttyUSB0"


async def main(num_scans: int) -> None:
    """Energy-scan all channels and rank them by quietness."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    app = ControllerApplication({"device": {"path": PORT}})
    await app.connect()
    print(f"connected to {PORT}")

    # Start the app and populate the device table. This raises NetworkNotFormed
    # because no PAN exists - but the coordinator is still running and scan-capable,
    # so swallow the error and proceed.
    try:
        await app.start_network(read_only=True)
        print("started application: a network IS formed")
    except NetworkNotFormed as exc:
        print(f"started application: no network formed ({type(exc).__name__})")
        print("  -> continuing anyway; a raw energy scan does not need a PAN")

    # start_network() adds the coordinator to the device table, but it raised
    # before getting that far. Add it by hand; get_device needs a real EUI64,
    # which we read out of the chip's own NVRAM.
    from zigpy_znp.types.nvids import OsalNvIds

    ieee = await app._znp.nvram.osal_read(OsalNvIds.EXTADDR, item_type=t.EUI64)
    coordinator = app.add_device(ieee=ieee, nwk=0x0000)
    print(f"coordinator device added: {coordinator.ieee} nwk=0x{coordinator.nwk:04x}")

    channel_energies: dict[int, deque] = defaultdict(lambda: deque([], maxlen=5))

    for i in range(1, num_scans + 1):
        rsp = await coordinator.zdo.Mgmt_NWK_Update_req(
            zdo_t.NwkUpdate(
                ScanChannels=t.Channels.ALL_CHANNELS,
                ScanDuration=0x02,
                ScanCount=1,
            )
        )
        _, scanned_channels, _, _, energy_values = rsp
        for channel, energy in zip(scanned_channels, energy_values, strict=False):
            channel_energies[channel].append(energy)
        print(f"  scan {i}/{num_scans} complete")

    print("\n=== per-channel mean energy (LOWER IS QUIETER) ===")
    print("    0-255 scale. ~60 or below is quiet; above ~120 is busy.")
    print("-" * 56)

    means = {}
    for channel in sorted(channel_energies):
        vals = channel_energies[channel]
        means[channel] = sum(vals) / len(vals)

    for channel in sorted(means):
        bar = "#" * int(means[channel] / 8)
        flag = "  BUSY" if means[channel] > 120 else ""
        print(f"  ch {channel:>2}  {means[channel]:>6.1f}  {bar}{flag}")

    if means:
        ranked = sorted(means, key=lambda ch: means[ch])
        print("\n=== quietest ===")
        for channel in ranked[:5]:
            print(f"  ch {channel:>2}  {means[channel]:.1f}")
        print(f"\nquietest: ch {ranked[0]} ({means[ranked[0]]:.1f})")
        print(f"noisiest: ch {ranked[-1]} ({means[ranked[-1]]:.1f})")

    await app.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scans", type=int, default=3)
    args = ap.parse_args()
    asyncio.run(main(args.scans))
