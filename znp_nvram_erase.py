#!/usr/bin/env python3
"""znp_nvram_erase.py - Erase stale Zigbee network state from a ZNP dongle.

Writes zeroed values to the Z-Stack NVRAM items that define a network
(network key, PAN ID, extended PAN ID, channel mask) and forces a reset, so
the coordinator forms a genuinely fresh network.

The Sonoff ZBDongle-P here previously belonged to another network; Z2M
commissioning times out with "network with the same panId or extendedPanId
already exists nearby" until this stale state is cleared.

Run with the target serial port as argv[1] (default /dev/ttyUSB0).
"""

import sys
import time

import serial

DEFAULT_PORT = "/dev/ttyUSB0"
BAUD = 115200
SOF = 0xFE

MT_SYS = 0x41
SYS_RESET_REQ = 0x00
SYS_OSAL_NV_LENGTH = 0x13
SYS_OSAL_NV_READ = 0x08
SYS_OSAL_NV_WRITE = 0x09

# Z-Stack NV item IDs holding network identity.
NV_LOGICAL_TYPE = 0x87
NV_PANID = 0x83
NV_EXTENDED_PANID = 0x2D
NV_NETWORK_KEY = 0x62
NV_CHANNEL_MASK = 0x84


def build(mt: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a UNPI frame; LEN counts DATA bytes only."""
    body = bytes([mt, cmd]) + payload
    fcs = len(payload)
    for b in body:
        fcs ^= b
    return bytes([SOF, len(payload)]) + body + bytes([fcs])


def read_frames(ser: serial.Serial, timeout: float = 2.0) -> bytes:
    """Read whatever arrives within timeout."""
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf.extend(chunk)
            deadline = time.time() + 0.4
    return bytes(buf)


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    ser = serial.Serial(port, BAUD, timeout=1.0)
    time.sleep(0.3)
    ser.reset_input_buffer()

    targets = [
        ("NV_NETWORK_KEY", NV_NETWORK_KEY, 16),
        ("NV_PANID", NV_PANID, 2),
        ("NV_EXTENDED_PANID", NV_EXTENDED_PANID, 8),
        ("NV_CHANNEL_MASK", NV_CHANNEL_MASK, 4),
        ("NV_LOGICAL_TYPE", NV_LOGICAL_TYPE, 1),
    ]

    for name, nv_id, size in targets:
        # 1. Ask the firmware how long this NV item is.
        ser.reset_input_buffer()
        ser.write(build(MT_SYS, SYS_OSAL_NV_LENGTH, bytes([nv_id])))
        ser.flush()
        resp = read_frames(ser, 1.5)
        length = None
        # SRSP payload: cmd, status, length(2 LE)
        idx = resp.find(bytes([0x61, SYS_OSAL_NV_LENGTH]))
        if idx != -1 and idx + 4 < len(resp):
            length = resp[idx + 3] | (resp[idx + 4] << 8)
        n = length if length else size
        print(f"{name:20} nv_id={nv_id:#04x} current_len={length}")

        # 2. Write zeros over it.
        zeros = bytes(n)
        payload = bytes([nv_id]) + (0).to_bytes(2, "little") + n.to_bytes(2, "little") + zeros
        ser.reset_input_buffer()
        ser.write(build(MT_SYS, SYS_OSAL_NV_WRITE, payload))
        ser.flush()
        r = read_frames(ser, 1.5)
        ok = bytes([0x61, SYS_OSAL_NV_WRITE]) in r
        print(f"{'':20} write zeros({n}) -> {'SRSP ok' if ok else 'no SRSP'} {r.hex(' ')}")

    # Force a reset so the firmware reloads NVRAM.
    ser.reset_input_buffer()
    ser.write(build(MT_SYS, SYS_RESET_REQ, bytes([0x01])))
    ser.flush()
    r = read_frames(ser, 3.0)
    print(f"\nreset -> {r.hex(' ')}")
    ser.close()
    print("\nNVRAM cleared. Restart zigbee2mqtt so it forms a fresh network.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
