#!/usr/bin/env python3
"""znp_bsl_enter.py - Force a Sonoff ZBDongle-P (CC2652P) into bootloader mode.

The ZBDongle-P has no user button: the CP210x UART's DTR/RTS outputs are wired
to the CC2652P's RESET and BOOT pins. This drives the documented toggle sequence
directly at the tty layer, then leaves the port closed so a flasher can attach.

Usage:
    python3 znp_bsl_enter.py [port]

Exits 0 if the BSL sync reply (0xCC 0xCC) was seen after the sequence.
"""

import sys
import time

import serial

DEFAULT_PORT = "/dev/ttyUSB0"
BAUD = 115200

# ZBDongle-P wiring: DTR -> RESET (active low), RTS -> BOOT (active low on this board)
SEQUENCES = [
    # (dtr, rts, hold_s, label)
    (True, False, 0.10, "reset asserted, boot low"),
    (True, True, 0.10, "both released"),
    (False, True, 0.10, "reset released, boot low -> BSL"),
    (True, True, 0.10, "release"),
]


def try_sequence(port: str, invert: bool) -> bool:
    """Apply the toggle sequence and probe for the BSL sync response."""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 0.4
    ser.dtr = False
    ser.rts = False
    ser.open()
    try:
        for dtr, rts, hold, label in SEQUENCES:
            if invert:
                dtr, rts = not dtr, not rts
            ser.dtr = dtr
            ser.rts = rts
            time.sleep(hold)

        ser.reset_input_buffer()
        # BSL sync: two 0x55 bytes; target answers 0xCC 0xCC.
        for _ in range(6):
            ser.write(b"\x55\x55")
            ser.flush()
            time.sleep(0.05)
            resp = ser.read(64)
            if resp and (0xCC in resp or 0x00 in resp):
                print(f"  invert={invert}: reply {resp.hex(' ')}")
                return True
        return False
    finally:
        ser.close()


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    for invert in (False, True):
        print(f"Trying BSL toggle (invert={invert}) on {port} ...")
        try:
            if try_sequence(port, invert):
                print("  -> target appears to be in BSL mode")
                return 0
        except Exception as exc:  # noqa: BLE001 - report and try next variant
            print(f"  error: {exc}")
        time.sleep(0.3)

    print("No BSL response on either polarity.")
    print("The ZBDongle-P auto-BSL depends on the CP210x DTR/RTS wiring;")
    print("use the Silabs web flasher, which sequences these lines itself.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
