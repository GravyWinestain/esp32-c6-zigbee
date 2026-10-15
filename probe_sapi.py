#!/usr/bin/env python3
"""probe_sapi.py - Test whether this firmware's SAPI/AppConfig layer works.

The hypothesis this tests: this CC2652P (fw 20250321) reports Z-Stack 3.3 over
SYS_VERSION, which makes zigpy-znp take the BDB commissioning path. But its
SAPI/AppConfig command handlers are stubs - they accept a command and return
SUCCESS without doing anything - so BDB network formation silently does nothing
and then times out.

Evidence already gathered pointing this way:
  * ZB_WRITE_CONFIGURATION returned 0x00 (SUCCESS) but the config items
    (CHANLIST, PRECFGKEY, NWKKEY, STARTUP_OPTION) all read back empty/short.
  * ZB_READ_CONFIGURATION returned nothing at all.
  * BDBStartCommissioning produced no BDBCommissioningNotification within the
    commissioning timeout.

This probes the SAPI layer directly with the most harmless read possible, so a
definitive answer does not depend on inference.

Usage:
    python probe_sapi.py
"""

from __future__ import annotations

import asyncio
import logging

import zigpy.config as conf
import zigpy_znp.commands as c
from zigpy_znp.api import ZNP
from zigpy_znp.config import CONFIG_SCHEMA

PORT = "/dev/ttyUSB0"


async def try_request(znp: ZNP, label: str, req) -> object:
    """Issue one request and report exactly what comes back."""
    try:
        rsp = await znp.request(req, timeout=5)
        print(f"  {label:34s} -> {rsp}")
        return rsp
    except Exception as exc:  # noqa: BLE001 - any failure is the answer
        print(f"  {label:34s} -> {type(exc).__name__}: {exc}")
        return None


async def main() -> None:
    """Probe the SAPI, AppConfig and SYS subsystems for signs of life."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    config = CONFIG_SCHEMA({conf.CONF_DEVICE: {conf.CONF_DEVICE_PATH: PORT}})
    znp = ZNP(config)
    await znp.connect()
    print(f"connected to {PORT}, stack {znp.version}\n")

    print("=== SYS subsystem (known good: this drives the whole device) ===")
    await try_request(znp, "SYS.Version", c.SYS.Version.Req())
    await try_request(znp, "SYS.Ping", c.SYS.Ping.Req())

    print("\n=== UTIL subsystem (known good: we read NV through it) ===")
    await try_request(znp, "UTIL.GetDeviceInfo", c.UTIL.GetDeviceInfo.Req())

    print("\n=== SAPI subsystem (the suspected stub) ===")
    await try_request(znp, "SAPI.ZBReadConfiguration(chanlist)", c.SAPI.ZBReadConfiguration.Req(ConfigId=0x83))
    await try_request(znp, "SAPI.ZBReadConfiguration(precfgkey)", c.SAPI.ZBReadConfiguration.Req(ConfigId=0x62))

    print("\n=== AppConfig / BDB subsystem (used by Z-Stack 3 formation) ===")
    await try_request(
        znp,
        "AppConfig.BDBStartCommissioning",
        c.AppConfig.BDBStartCommissioning.Req(
            Mode=c.app_config.BDBCommissioningMode.NwkFormation
        ),
    )

    print("\n=== interpretation ===")
    print("SYS/UTIL answering while SAPI/AppConfig stay silent => the 3.x")
    print("command surface is advertised but unimplemented. Formation cannot")
    print("work through it, no matter what ZNP calls are issued.")

    await znp.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
