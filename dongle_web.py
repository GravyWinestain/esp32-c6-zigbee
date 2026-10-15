#!/usr/bin/env python3
"""dongle_web.py - Web control panel for the Sonoff Zigbee 3.0 USB Dongle Plus.

Serves a single-page dashboard at http://localhost:8001 that talks to the
CC2652P ZNP coordinator directly over its serial port. Shows live firmware
identity, IEEE address, network state, and lets the user start/stop
commissioning (permit-join) and query device information.

Backend: FastAPI + pyserial. All serial access is guarded by a lock because the
ZNP protocol is a single-request/single-response channel.

Run:  python3 dongle_web.py
"""

from __future__ import annotations

import datetime
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

import serial
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

APP_VERSION = "0.1.0"
APP_COPYRIGHT = "(c) GnomeWorx 2026"
DEFAULT_PORT = "/dev/zigbee-coordinator"
BAUD = 115200

MT_SYS = 0x41
MT_UTIL = 0x67
MT_ZDO = 0x25

SYS_RESET_REQ = 0x00
SYS_VERSION = 0x02
SYS_NV_LENGTH = 0x13
UTIL_GET_DEVICE_INFO = 0x00
ZDO_STARTUP_FROM_APP = 0x40

# ---------------------------------------------------------------- debug log

DEBUG_LOG_MAX = 400
_debug_log: Deque[str] = deque(maxlen=DEBUG_LOG_MAX)


def dbg(msg: str) -> None:
    """Append a timestamped line to the in-memory debug ring buffer."""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]
    _debug_log.append(f"[{ts}] {msg}")


# ------------------------------------------------------------- ZNP transport


class ZnpError(RuntimeError):
    """Raised when the dongle does not answer a request in time."""


def _unpi(mt: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a UNPI frame. LEN counts DATA bytes only (excludes MT, CMD, FCS)."""
    body = bytes([mt, cmd]) + payload
    fcs = len(payload)
    for byte in body:
        fcs ^= byte
    return bytes([0xFE, len(payload)]) + body + bytes([fcs])


class Znp:
    """Synchronous ZNP client for the coordinator dongle."""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = BAUD) -> None:
        self.port = port
        self.baud = baud
        self._lock = threading.Lock()
        self._ser: Optional[serial.Serial] = None

    def _ensure(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(self.port, self.baud, timeout=1.0)
            time.sleep(0.3)
            self._ser.reset_input_buffer()
            dbg(f"opened {self.port} @ {self.baud}")
        return self._ser

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
            dbg("closed serial port")

    def request(
        self, mt: int, cmd: int, payload: bytes = b"", timeout: float = 1.5
    ) -> bytes:
        """Send a request and return the matching SRSP payload (after status)."""
        frame = _unpi(mt, cmd, payload)
        with self._lock:
            ser = self._ensure()
            ser.reset_input_buffer()
            ser.write(frame)
            ser.flush()
            dbg(f"TX {frame.hex(' ')}")
            deadline = time.time() + timeout
            buf = bytearray()
            while time.time() < deadline:
                chunk = ser.read(256)
                if chunk:
                    buf.extend(chunk)
                    # Look for SRSP: FE len mt cmd data... fcs
                    idx = buf.find(bytes([0xFE]))
                    while idx != -1 and idx + 4 <= len(buf):
                        length = buf[idx + 1]
                        # Frame = FE + len + mt + cmd + len data bytes + fcs
                        if idx + 4 + length + 1 > len(buf):
                            break
                        mt_r = buf[idx + 2]
                        cmd_r = buf[idx + 3]
                        data = bytes(buf[idx + 4 : idx + 4 + length])
                        if mt_r == (mt | 0x20) and cmd_r == cmd:
                            dbg(f"RX {bytes(buf[idx:idx+4+length+1]).hex(' ')}")
                            return data
                        idx = buf.find(bytes([0xFE]), idx + 1)
                    deadline = time.time() + 0.3
            dbg(f"TIMEOUT waiting for mt={mt:#04x} cmd={cmd:#04x}")
            raise ZnpError(
                f"no response for mt={mt:#04x} cmd={cmd:#04x} within {timeout}s"
            )

    # -- high level queries -------------------------------------------------

    def version(self) -> Dict[str, Any]:
        """SYS_VERSION -> transportrev, product, Z-Stack version, revision.

        SYS command payloads carry no separate status byte: byte 0 is the
        first field. Verified against firmware 20250321:
            FE 0A 61 02 02 01 02 07 01 D1 FE 34 01 00 74
            payload = 02 01 02 07 01 d1 fe 34
        """
        data = self.request(MT_SYS, SYS_VERSION)
        if len(data) < 8:
            raise ZnpError(f"short SYS_VERSION reply: {data.hex(' ')}")
        revision = int.from_bytes(data[5:9], "little")
        return {
            "transportrev": data[0],
            "product": data[1],
            "majorrel": data[2],
            "minorrel": data[3],
            "maintrel": data[4],
            "revision": revision,
            "revision_text": (
                f"{revision // 10000:04d}-{revision // 100 % 100:02d}-"
                f"{revision % 100:02d}"
                if revision > 9999999
                else str(revision)
            ),
            "zstack_version": f"{data[2]}.{data[3]}.{data[4]}",
            "product_name": {1: "Coordinator", 2: "Router", 0: "End device"}.get(
                data[1], f"unknown ({data[1]})"
            ),
        }

    def device_info(self) -> Dict[str, Any]:
        """UTIL_GET_DEVICE_INFO -> IEEE address, short address, state.

        Verified payload (12 bytes) from firmware 20250321:
            00 | c6 7e 29 39 00 4b 12 00 | c6 7e | 07
            status | ieee(8, little-endian) | nwk(LE) | state

        The IEEE field reversed matches the address the flasher reports
        (00:12:4B:00:39:29:7E:C6), which confirms the byte order.
        """
        data = self.request(MT_UTIL, UTIL_GET_DEVICE_INFO)
        if len(data) < 12:
            raise ZnpError(f"short device info reply: {data.hex(' ')}")
        status = data[0]
        ieee = bytes(data[1:9])[::-1].hex(":")
        nwk = int.from_bytes(data[9:11], "little")
        state = data[11]
        return {
            "status": status,
            "state_code": state,
            "state": {
                0: "Initialised - not started",
                1: "Connecting to network",
                2: "Connected",
                3: "Authenticating",
                4: "Authentication complete",
                5: "Network joined",
                6: "Rejoining network",
                7: "Coordinator (no network)",
                8: "Coordinator running",
                9: "Router running",
            }.get(state, f"unknown ({state})"),
            "ieee_address": ieee,
            "short_address": f"0x{nwk:04X}",
        }

    def nv_length(self, nv_id: int) -> Optional[int]:
        """SYS_NV_LENGTH -> declared length, or None if the id is rejected.

        Verified frame: FE 02 61 13 00 00 70
        LEN=2 with payload 00 00 => success, length 0 (item not set).
        """
        try:
            data = self.request(MT_SYS, SYS_NV_LENGTH, bytes([nv_id]))
        except ZnpError:
            return None
        if len(data) < 2:
            return None
        if data[0] != 0:
            return None
        return int.from_bytes(data[1:3], "little") if len(data) >= 3 else 0

    def reset(self, mode: int = 1) -> None:
        """SYS_RESET_REQ. mode 1 = soft reset, 0 = hard."""
        data = self.request(MT_SYS, SYS_RESET_REQ, bytes([mode]), timeout=3.0)
        dbg(f"reset issued (mode={mode}), reply {data.hex(' ')}")


ZNP = Znp()

# ------------------------------------------------------------------ FastAPI


class PermitBody(BaseModel):
    """Request body for the commissioning endpoint."""

    enable: bool
    seconds: int = 180


app = FastAPI(title="Sonoff Dongle Panel", version=APP_VERSION)


@app.get("/api/status")
def api_status() -> JSONResponse:
    """Return the live hardware state of the dongle."""
    dbg("GET /api/status")
    out: Dict[str, Any] = {"ok": False}
    try:
        out["version"] = ZNP.version()
        out["device"] = ZNP.device_info()
        out["ok"] = True
        out["error"] = None
    except (ZnpError, serial.SerialException, OSError) as exc:
        out["error"] = str(exc)
        dbg(f"status error: {exc}")
    return JSONResponse(out)


@app.get("/api/nv")
def api_nv() -> JSONResponse:
    """Report the length of each network-identity NV item."""
    dbg("GET /api/nv")
    items = {
        "Network key": 0x62,
        "PAN ID": 0x83,
        "Extended PAN ID": 0x2D,
        "Channel list": 0x84,
        "Logical type": 0x87,
        "Extended address": 0x01,
    }
    out = {}
    for name, nv_id in items.items():
        out[name] = ZNP.nv_length(nv_id)
    return JSONResponse({"items": out})


@app.post("/api/reset")
def api_reset() -> JSONResponse:
    """Issue a soft reset to the coordinator."""
    dbg("POST /api/reset")
    try:
        ZNP.reset(1)
        return JSONResponse({"ok": True})
    except (ZnpError, serial.SerialException) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/debug/logs")
def api_debug_logs() -> JSONResponse:
    """Return the in-memory debug ring buffer."""
    return JSONResponse({"logs": list(_debug_log)})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the single-page panel."""
    return HTMLResponse(
        PAGE.replace("__VERSION__", APP_VERSION).replace("__COPYRIGHT__", APP_COPYRIGHT)
    )


# --------------------------------------------------------------------- HTML

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sonoff Dongle Panel</title>
<style>
  :root {
    --bg: #14161a; --panel: #1c1f26; --line: #2a2e38;
    --fg: #dfe3ea; --dim: #8b93a3; --accent: #e8a33d;
    --ok: #4caf7d; --bad: #d9534f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    padding: 18px 24px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 14px;
  }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--dim); flex: none;
  }
  .dot.ok { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .dot.bad { background: var(--bad); }
  main { padding: 22px 24px; max-width: 1000px; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px; margin-bottom: 20px;
  }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 16px 18px;
  }
  .card h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .09em;
    color: var(--dim); margin: 0 0 12px; font-weight: 600;
  }
  .row {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,.04);
    align-items: baseline;
  }
  .row:last-child { border-bottom: none; }
  .row .k { color: var(--dim); flex: none; }
  .row .v {
    font-family: ui-monospace, Menlo, monospace; text-align: right;
    word-break: break-all;
  }
  .v.hi { color: var(--accent); }
  button {
    background: #262a34; color: var(--fg); border: 1px solid var(--line);
    border-radius: 6px; padding: 9px 15px; font-size: 13px; cursor: pointer;
  }
  button:hover { background: #2f343f; border-color: #3d434f; }
  button.primary { background: var(--accent); color: #17181c; border-color: var(--accent); font-weight: 600; }
  button:disabled { opacity: .45; cursor: default; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  #err {
    margin-top: 14px; padding: 10px 14px; border-radius: 6px;
    background: rgba(217,83,79,.12); border: 1px solid rgba(217,83,79,.35);
    color: #f0a5a2; font-size: 13px; display: none;
  }
  #err.show { display: block; }
  pre#log {
    background: #0f1114; border: 1px solid var(--line); border-radius: 6px;
    padding: 12px; font-size: 11.5px; line-height: 1.45; max-height: 220px;
    overflow: auto; margin: 0; color: #9aa3b2; display: none;
  }
  pre#log.show { display: block; }
  footer {
    text-align: center; padding: 14px 0 22px; font-size: 11px;
    color: #5a6070; border-top: 1px solid var(--line); margin-top: 24px;
  }
</style>
</head>
<body>
<header>
  <span class="dot" id="statusDot"></span>
  <h1>Sonoff Zigbee 3.0 USB Dongle Plus</h1>
</header>

<main>
  <div class="grid">
    <div class="card">
      <h2>Firmware</h2>
      <div class="row"><span class="k">Transport rev</span><span class="v" id="fTransport">-</span></div>
      <div class="row"><span class="k">Product</span><span class="v" id="fProduct">-</span></div>
      <div class="row"><span class="k">Z-Stack</span><span class="v" id="fStack">-</span></div>
      <div class="row"><span class="k">Revision</span><span class="v hi" id="fRevision">-</span></div>
    </div>

    <div class="card">
      <h2>Device</h2>
      <div class="row"><span class="k">IEEE address</span><span class="v" id="dIeee">-</span></div>
      <div class="row"><span class="k">Short address</span><span class="v" id="dShort">-</span></div>
      <div class="row"><span class="k">Network state</span><span class="v" id="dState">-</span></div>
    </div>

    <div class="card">
      <h2>Network identity (NVRAM)</h2>
      <div class="row"><span class="k">Network key</span><span class="v" id="nKey">-</span></div>
      <div class="row"><span class="k">PAN ID</span><span class="v" id="nPan">-</span></div>
      <div class="row"><span class="k">Extended PAN ID</span><span class="v" id="nExt">-</span></div>
      <div class="row"><span class="k">Channel list</span><span class="v" id="nChan">-</span></div>
      <div class="row"><span class="k">Logical type</span><span class="v" id="nType">-</span></div>
    </div>
  </div>

  <div class="card">
    <h2>Actions</h2>
    <div class="controls">
      <button class="primary" id="btnRefresh">Refresh</button>
      <button id="btnReset">Soft reset</button>
      <button id="btnLog">Toggle log</button>
    </div>
    <div id="err"></div>
  </div>

  <pre id="log"></pre>
</main>

<footer>__COPYRIGHT__ &nbsp; Version __VERSION__</footer>

<script>
const state = { version: null, device: null, nv: null, online: false };

const el = {
  statusDot: document.getElementById('statusDot'),
  fTransport: document.getElementById('fTransport'),
  fProduct: document.getElementById('fProduct'),
  fStack: document.getElementById('fStack'),
  fRevision: document.getElementById('fRevision'),
  dIeee: document.getElementById('dIeee'),
  dShort: document.getElementById('dShort'),
  dState: document.getElementById('dState'),
  nKey: document.getElementById('nKey'),
  nPan: document.getElementById('nPan'),
  nExt: document.getElementById('nExt'),
  nChan: document.getElementById('nChan'),
  nType: document.getElementById('nType'),
  err: document.getElementById('err'),
  log: document.getElementById('log'),
};

function setText(node, value) { node.textContent = value; }

function updateUI() {
  el.statusDot.className = 'dot ' + (state.online ? 'ok' : 'bad');
  const v = state.version, d = state.device, n = state.nv;
  setText(el.fTransport, v ? v.transportrev : '-');
  setText(el.fProduct, v ? v.product_name : '-');
  setText(el.fStack, v ? v.zstack_version : '-');
  setText(el.fRevision, v ? (v.revision_text || v.revision) : '-');
  setText(el.dIeee, d ? d.ieee_address : '-');
  setText(el.dShort, d ? d.short_address : '-');
  setText(el.dState, d ? d.state : '-');
  setText(el.nKey, n ? fmtLen(n['Network key']) : '-');
  setText(el.nPan, n ? fmtLen(n['PAN ID']) : '-');
  setText(el.nExt, n ? fmtLen(n['Extended PAN ID']) : '-');
  setText(el.nChan, n ? fmtLen(n['Channel list']) : '-');
  setText(el.nType, n ? fmtLen(n['Logical type']) : '-');
}

function fmtLen(v) {
  if (v === null || v === undefined) return 'absent';
  if (v === 0) return 'not set';
  return v + ' bytes';
}

function showError(msg) {
  if (!msg) { el.err.classList.remove('show'); return; }
  el.err.textContent = msg;
  el.err.classList.add('show');
}

async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    state.online = !!s.ok;
    state.version = s.version || null;
    state.device = s.device || null;
    updateUI();
    showError(s.ok ? null : ('Dongle not responding: ' + (s.error || 'unknown')));
    const nv = await (await fetch('/api/nv')).json();
    state.nv = nv.items;
    updateUI();
  } catch (e) {
    state.online = false;
    updateUI();
    showError('Request failed: ' + e.message);
  }
}

async function doReset() {
  showError(null);
  try {
    const r = await fetch('/api/reset', { method: 'POST' });
    if (!r.ok) { const t = await r.json(); throw new Error(t.detail || r.statusText); }
    await new Promise(res => setTimeout(res, 1200));
    await refresh();
  } catch (e) { showError('Reset failed: ' + e.message); }
}

async function toggleLog() {
  el.log.classList.toggle('show');
  if (!el.log.classList.contains('show')) return;
  try {
    const d = await (await fetch('/api/debug/logs')).json();
    el.log.textContent = d.logs.join('\\n');
    el.log.scrollTop = el.log.scrollHeight;
  } catch (e) { el.log.textContent = 'log fetch failed: ' + e.message; }
}

document.getElementById('btnRefresh').addEventListener('click', refresh);
document.getElementById('btnReset').addEventListener('click', doReset);
document.getElementById('btnLog').addEventListener('click', toggleLog);

refresh();
</script>
</body>
</html>
"""


def main() -> None:
    """Run the panel with uvicorn."""
    import uvicorn

    dbg("starting dongle panel on :8001")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")


if __name__ == "__main__":
    main()
