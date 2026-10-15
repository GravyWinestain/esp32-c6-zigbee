#!/usr/bin/env python3
"""start_then_form.py - Start the Z-Stack application, then form a PAN.

Definitive diagnosis from probe_sapi.py on this CC2652P (fw 20250321):

  SYS.Ping capabilities = SYS | AF | ZDO | UTIL | GP | APP_CNF
      -> SAPI is NOT among them. The SAPI subsystem is absent on this
         firmware; every ZB_* command is rejected with InvalidSubsystem.
  UTIL.GetDeviceInfo   -> DeviceState = InitializedNotStarted (0)
      -> the stack is sitting stopped. No network can ever form while it is
         in this state, which is why every formation attempt failed.
  AppConfig.BDBStartCommissioning -> SUCCESS, but with no state change
      -> BDB commissioning is only meaningful once the app is running.

So the missing step, from the very beginning, was starting the application:
ZDO_STARTUP_FROM_APP, then wait for ZDO_STATE_CHANGE_IND with
StartedAsCoordinator, and only then form.

This does it in that order, over the raw ZNP layer, so the sequence is explicit
rather than buried inside a library's assumptions about stack version.

Usage:
    python start_then_form.py --channel 15
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import zigpy.config as conf
import zigpy_znp.commands as c
import zigpy_znp.types as t
from zigpy_znp.api import ZNP
from zigpy_znp.config import CONFIG_SCHEMA

PORT = "/dev/ttyUSB0"
STARTUP_TIMEOUT = 30


async def main(channel: int) -> None:
    """Start the app, confirm the state change, then form a network."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("zigpy_znp").setLevel(logging.WARNING)

    config = CONFIG_SCHEMA(
        {
            conf.CONF_DEVICE: {conf.CONF_DEVICE_PATH: PORT},
            conf.CONF_NWK: {conf.CONF_NWK_CHANNELS: [channel]},
        }
    )
    znp = ZNP(config)
    await znp.connect()
    print(f"connected to {PORT}, stack {znp.version}\n")

    cap = await znp.request(c.SYS.Ping.Req())
    print(f"capabilities : {cap.Capabilities}")

    info = await znp.request(c.UTIL.GetDeviceInfo.Req())
    print(f"device state : {info.DeviceState}")

    if info.DeviceState == t.DeviceState.StartedAsCoordinator:
        print("\nalready started as coordinator")
    else:
        # --- step 1: capture the state-change notification BEFORE starting ---
        state_future = znp.wait_for_response(
            c.ZDO.StateChangeInd.Callback(State=t.DeviceState.StartedAsCoordinator)
        )

        # --- step 2: start the application ---
        print("\nstep 1: ZDO_STARTUP_FROM_APP")
        rsp = await znp.request(c.ZDO.StartupFromApp.Req(StartDelay=100))
        print(f"  StartupFromApp status: {getattr(rsp, 'Status', rsp)}")

        # --- step 3: wait for the stack to say it is up ---
        print("step 2: waiting for StartedAsCoordinator ...")
        try:
            state = await asyncio.wait_for(state_future, timeout=STARTUP_TIMEOUT)
            print(f"  {state}")
        except asyncio.TimeoutError:
            print(f"  timed out after {STARTUP_TIMEOUT}s")
            info = await znp.request(c.UTIL.GetDeviceInfo.Req())
            print(f"  device state now: {info.DeviceState}")

    # --- step 4: form the network now that the stack is running ---
    print(f"\nstep 3: form network on channel {channel}")
    try:
        rsp = await znp.start_network()
        print(f"  start_network OK: {rsp}")
    except Exception as exc:  # noqa: BLE001
        print(f"  start_network FAILED: {type(exc).__name__}: {exc}")

    await asyncio.sleep(2)

    info = await znp.request(c.UTIL.GetDeviceInfo.Req())
    print("\n=== final state ===")
    print(f"  DeviceState : {info.DeviceState}")
    print(f"  NWK         : 0x{info.NWK:04x}")
    print(f"  IEEE        : {info.IEEE}")
    print(f"\nNETWORK FORMED: {info.DeviceState == t.DeviceState.StartedAsCoordinator and info.NWK == 0x0000}")

    await znp.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=15)
    args = ap.parse_args()
    asyncio.run(main(args.channel))
