#!/usr/bin/env python3
"""znp_query.py - Read live coordinator state from a ZStack ZNP dongle.

Speaks directly to the Sonoff ZBDongle-P over serial using the ZNP/UNPI
framing to fetch version, device info and network state. Read-only queries
only: no network parameters are written.
"""

import sys
import time

import serial

PORT = "/dev/ttyUSB0"
BAUD = 115200
SOF = 0xFE

# (MT, CMD) constants
SYS = 0x41
SYS_VERSION = 0x02
SYS_RESET_REQ = 0x00
SYS_RANDOM = 0x0C
UTIL = 0x67
UTIL_GET_DEVICE_INFO = 0x00
ZDO = 0x65
ZDO_STATE_CHANGE = 0x00


def build_frame(mt: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a UNPI/MT frame: SOF | LEN | MT | CMD | DATA | FCS.

    LEN counts the DATA payload only (MT and CMD are not counted), which is
    what the CC2652P ZNP firmware expects.
    """
    body = bytes([mt, cmd]) + payload
    length = len(payload)
    fcs = length
    for b in body:
        fcs ^= b
    return bytes([SOF, length]) + body + bytes([fcs])


def parse_frames(buf: bytes) -> list[tuple[int, int, bytes, bool]]:
    """Parse all complete UNPI frames as (mt, cmd, data, fcs_ok).

    LEN in the wire format counts DATA bytes only, so the frame is
    SOF | LEN | MT | CMD | DATA(LEN) | FCS.
    """
    frames = []
    i = 0
    while i + 3 < len(buf):
        if buf[i] != SOF:
            i += 1
            continue
        length = buf[i + 1]
        if length == 0:
            body = buf[i + 2 : i + 4]
            fcs_wire = buf[i + 4]
            calc = 0
            for b in body:
                calc ^= b
            frames.append((body[0], body[1], b"", fcs_wire == calc))
            i += 5
            continue
        body = buf[i + 2 : i + 2 + length]
        if len(body) < 2:
            i += 1
            continue
        fcs_wire = buf[i + 2 + length]
        calc = length
        for b in body:
            calc ^= b
        frames.append((body[0], body[1], body[2:], fcs_wire == calc))
        i += 2 + length + 1
    return frames


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print(f"Opening {PORT} @ {BAUD}")
    ser = serial.Serial(PORT, BAUD, timeout=1.5)
    time.sleep(0.3)
    ser.reset_input_buffer()

    probes = [
        ("SYS_VERSION      ", SYS, SYS_VERSION, b""),
        ("UTIL_DEVICE_INFO ", UTIL, UTIL_GET_DEVICE_INFO, b""),
        ("SYS_RANDOM       ", SYS, SYS_RANDOM, b""),
        ("ZDO_STATE_CHANGE ", ZDO, ZDO_STATE_CHANGE, b""),
    ]

    for label, mt, cmd, pl in probes:
        frame = build_frame(mt, cmd, pl)
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        time.sleep(1.0)
        raw = ser.read(512)
        print(f"\n--- {label} ---")
        print(f"  TX : {frame.hex(' ')}")
        if not raw:
            print("  RX : <silent>")
            continue
        print(f"  RX : {raw.hex(' ')}")
        for f_mt, f_cmd, f_data, ok in parse_frames(raw):
            print(
                f"  frame MT={f_mt:#04x} CMD={f_cmd:#04x} "
                f"fcs_ok={ok} data={f_data.hex(' ')}"
            )

    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
