#!/usr/bin/env python3
"""start_then_form.py - bring a blank CC2652P coordinator up and open the network.

Usage:
    python start_then_form.py --channel 15
    python start_then_form.py --channel 15 --permit-join 0     # skip permit join

Why this file exists
--------------------
Formation on a blank (or mass-erased) CC2652P fails with a misleading error:

    FormationFailure: Network formation refused: there is too much RF
    interference. Make sure your coordinator is on a USB 2.0 extension cable...

That message blames the RF environment, and it cost a long detour chasing USB
topology. The real cause is that the stack boots to `InitializedNotStarted` and
cannot form anything in that state. Moving the dongle to an isolated USB
controller did NOT fix it.

A second, quieter bug: this script used to call `znp.start_network()`, but
`ZNP` is the low-level transport object and has no such method. That call could
never work, which masked the real problem.

The sequence that does work, verified on fw 20250321 / Z-Stack 3.3:

    1. build a complete NIB in NVRAM
       - the SAPI subsystem is absent on this firmware, so every ZB_* call
         (including ZB_WRITE_CONFIGURATION) is rejected with InvalidSubsystem.
         Use osal_write.
    2. ZDO_STARTUP_FROM_APP
       - state goes InitializedNotStarted -> StartedAsCoordinator, nwk 0x0000
    3. MgmtPermitJoinReq, AddrMode=0x02 (NWK), Dst=0x0000 (the coordinator)
       - targeting itself is required. A broadcast (0x0F/0xFFFF) returns
         ZG_NWK_INVALID_REQUEST (0xC2) and never opens the network.

The NIB write and the startup must happen in the same session; the startup has
to happen after the NIB exists.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import zigpy.config as conf
import zigpy_znp.commands as c
import zigpy_znp.types as t
from zigpy_znp.api import ZNP
from zigpy_znp.config import CONFIG_SCHEMA
from zigpy_znp.types.nvids import OsalNvIds

# The device node moves (ttyUSB0/ttyUSB1/...) depending on which USB controller
# the dongle is plugged into. The udev rule at
# /etc/udev/rules.d/99-zigbee-coordinator.rules pins a stable symlink.
PORT = os.environ.get("ZIGBEE_PORT", "/dev/zigbee-coordinator")
STARTUP_TIMEOUT = 30

# A fixed PAN ID and channel make the network reproducible. The key is
# regenerated per run and lives only in the coordinator's NVRAM.
PAN_ID = 0x1A2B
SECURITY_LEVEL = 5


async def write_nib(znp: ZNP, channel: int) -> bytes:
    """Build a complete, valid network record in NVRAM.

    Returns the freshly generated network key. `load_network_info()` raises
    NetworkNotFormed on a blank chip before it ever reads the IEEE, so the IEEE
    is read directly from NVRAM here.
    """
    ieee = await znp.nvram.osal_read(OsalNvIds.EXTADDR, item_type=t.EUI64)
    nwk_key = os.urandom(16)

    await znp.nvram.osal_write(OsalNvIds.PANID, t.uint16_t(PAN_ID), create=True)
    await znp.nvram.osal_write(
        OsalNvIds.SECURITY_LEVEL, t.uint8_t(SECURITY_LEVEL), create=True
    )
    await znp.nvram.osal_write(
        OsalNvIds.CHANLIST, t.uint32_t(1 << channel), create=True
    )
    await znp.nvram.osal_write(
        OsalNvIds.LOGICAL_TYPE, t.DeviceLogicalType.Coordinator, create=True
    )
    await znp.nvram.osal_write(OsalNvIds.EXTENDED_PAN_ID, ieee, create=True)
    await znp.nvram.osal_write(OsalNvIds.PRECFGKEY, nwk_key, create=True)
    await znp.nvram.osal_write(OsalNvIds.PRECFGKEYS_ENABLE, t.uint8_t(1), create=True)
    await znp.nvram.osal_write(OsalNvIds.NWKKEY, nwk_key, create=True)

    print(f"  ieee     : {ieee}")
    print(f"  pan_id   : 0x{PAN_ID:04x}")
    print(f"  channel  : {channel}")
    print(f"  security : {SECURITY_LEVEL}")
    return nwk_key


async def start_app(znp: ZNP) -> t.DeviceState:
    """Issue ZDO_STARTUP_FROM_APP and wait for the stack to come up."""
    state_future = znp.wait_for_response(
        c.ZDO.StateChangeInd.Callback(State=t.DeviceState.StartedAsCoordinator)
    )
    rsp = await znp.request(c.ZDO.StartupFromApp.Req(StartDelay=0))
    print(f"  startup rsp : {rsp}")

    try:
        await asyncio.wait_for(state_future, timeout=STARTUP_TIMEOUT)
    except asyncio.TimeoutError:
        # Not fatal: the state often settles without the notification arriving.
        print(f"  (no StateChangeInd within {STARTUP_TIMEOUT}s; polling instead)")

    await asyncio.sleep(3)
    info = await znp.request(c.UTIL.GetDeviceInfo.Req())
    return info.DeviceState


async def open_network(znp: ZNP, seconds: int) -> bool:
    """Permit joins. Returns True if the coordinator accepted the request."""
    rsp = await znp.request(
        c.ZDO.MgmtPermitJoinReq.Req(
            AddrMode=t.AddrMode.NWK,
            Dst=0x0000,
            Duration=seconds,
            TCSignificance=0,
        )
    )
    print(f"  permit join : {rsp.Status}")
    return "SUCCESS" in str(rsp.Status)


async def main(channel: int, permit_seconds: int) -> int:
    """Run the bring-up. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    if not os.path.exists(PORT):
        print(f"ERROR: {PORT} does not exist.")
        print("Is the dongle plugged in, and is dongle-web.service stopped?")
        return 2

    config = CONFIG_SCHEMA(
        {
            conf.CONF_DEVICE: {conf.CONF_DEVICE_PATH: PORT},
            conf.CONF_NWK: {conf.CONF_NWK_CHANNELS: [channel]},
        }
    )
    znp = ZNP(config)
    await znp.connect()

    print(f"connected to {PORT}, stack {znp.version}\n")
    ping = await znp.request(c.SYS.Ping.Req())
    print(f"capabilities : {ping.Capabilities}")
    if "SAPI" not in str(ping.Capabilities):
        print("  (no SAPI subsystem - every ZB_* call will be rejected)")

    info = await znp.request(c.UTIL.GetDeviceInfo.Req())
    print(f"device state : {info.DeviceState}\n")

    print("step 1: write the network record (NVRAM)")
    await write_nib(znp, channel)

    print("\nstep 2: ZDO_STARTUP_FROM_APP")
    state = await start_app(znp)
    info = await znp.request(c.UTIL.GetDeviceInfo.Req())
    print(f"  state : {state}")
    print(f"  nwk   : 0x{info.NWK:04x}")

    started = state == t.DeviceState.StartedAsCoordinator

    permit_ok = False
    if started and permit_seconds > 0:
        print(f"\nstep 3: permit join for {permit_seconds}s")
        permit_ok = await open_network(znp, permit_seconds)
    elif not started:
        print("\nstep 3: SKIPPED - the stack is not running as coordinator")

    print("\n=== final state ===")
    print(f"  DeviceState : {state}")
    print(f"  NWK         : 0x{info.NWK:04x}")
    print(f"  IEEE        : {info.IEEE}")
    print(f"\nSTARTED AS COORDINATOR : {started}")
    if permit_seconds > 0:
        print(f"PERMIT JOIN OPEN       : {permit_ok}")

    znp.close()
    return 0 if started else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", type=int, default=15, help="Zigbee channel (11-26)")
    ap.add_argument(
        "--permit-join",
        type=int,
        default=180,
        metavar="SECONDS",
        help="open the network for this many seconds (0 to skip)",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.channel, args.permit_join)))
