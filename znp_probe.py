#!/usr/bin/env python3
"""znp_probe.py - Probe a Sonoff ZBDongle-P (CC2652P, ZStack ZNP) over serial.

Sends a SYS_RESET (0x4100) and decodes the async reset-indication reply to
identify the adapter, firmware revision and transport protocol. Read-only:
no network state on the dongle is modified.
"""

import sys
import time

import serial

PORT = "/dev/ttyUSB0"
BAUD = 115200

MT_SYS = 0x41
SYS_RESET_REQ = 0x00
SYS_RESET_IND = 0x80
CMD0 = 0xFE


def frame(mt: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a ZNP (MT) frame with a correct FCS."""
    body = bytes([len(payload), mt, cmd]) + payload
    fcs = 0
    for b in body:
        fcs ^= b
    return bytes([CMD0]) + body + bytes([fcs])


def decode(buf: bytes) -> list[dict]:
    """Decode all complete MT frames in buf."""
    out = []
    i = 0
    while i < len(buf) - 3:
        if buf[i] != CMD0:
            i += 1
            continue
        length = buf[i + 1]
        if length < 2 or i + 2 + length + 1 > len(buf):
            i += 1
            continue
        body = buf[i + 1 : i + 2 + length]
        fcs = buf[i + 2 + length]
        calc = 0
        for b in body:
            calc ^= b
        out.append(
            {
                "mt": buf[i + 2],
                "cmd": buf[i + 3],
                "payload": buf[i + 4 : i + 2 + length],
                "fcs_ok": fcs == calc,
            }
        )
        i += 2 + length + 1
    return out


def main() -> int:
    print(f"Opening {PORT} @ {BAUD} ...")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1.5)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to open port: {exc}")
        return 1

    # Assert reset via DTR/RTS like the ZNP bootloader expects.
    ser.setDTR(False)
    ser.setRTS(True)
    time.sleep(0.15)
    ser.setDTR(False)
    ser.setRTS(False)
    time.sleep(0.3)
    ser.reset_input_buffer()

    probe = frame(MT_SYS, SYS_RESET_REQ, bytes([0x01]))  # 1 = hard reset
    print(f"TX SYS_RESET  : {probe.hex(' ')}")
    ser.write(probe)
    ser.flush()

    time.sleep(2.0)
    data = ser.read(512)
    ser.close()

    if not data:
        print("RX            : <nothing> - dongle did not answer SYS_RESET")
        print("Interpretation: radio silent. Wrong/failed firmware, or the CC2652")
        print("                needs reflashing (ZStack coordinator .hex).")
        return 2

    print(f"RX raw ({len(data)} bytes): {data.hex(' ')}")
    frames = decode(data)
    if not frames:
        print("No valid MT frame parsed - garbage on the line.")
        return 3

    for f in frames:
        name = {
            (0x41, 0x80): "SYS_RESET_IND (async reset indication)",
            (0x61, 0x02): "ZDO_STATE_CHANGE_IND",
            (0x41, 0x02): "SYS_PING_RSP",
        }.get((f["mt"], f["cmd"]), f"MT {f['mt']:#04x} / CMD {f['cmd']:#04x}")
        print(f"\n  frame: {name}  (fcs_ok={f['fcs_ok']})")
        p = f["payload"]
        if (f["mt"], f["cmd"]) == (0x41, 0x80) and len(p) >= 6:
            fw = ".".join(str(x) for x in p[1:4])
            print(f"    firmware rev        : {fw}")
            print(f"    transport protocol  : {p[4]:#04x} ({'ZNP' if p[4] == 0x02 else '?'})")
            print(f"    product ID          : {p[5]:#04x}")
            print(f"    major/minor/patch   : {p[1]}.{p[2]}.{p[3]}")
        elif p:
            print(f"    payload: {p.hex(' ')}")

    print("\nRESULT: dongle replied - firmware is alive and speaking ZNP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
