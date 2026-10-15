#!/usr/bin/env python3
"""hub/db.py - SQLite device registry and event log for the Zigbee hub.

Owns all persistence: the devices table (one row per known node) and the
events table (an append-only journal of joins, leaves and state changes).
WAL mode is enabled so the FastAPI request threads and the background poll
loop can read and write concurrently.

Nothing in this module knows about Zigbee - it stores what the drivers report.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "hub.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    ieee          TEXT PRIMARY KEY,
    nwk           TEXT,
    driver        TEXT NOT NULL,
    model         TEXT,
    vendor        TEXT,
    node_type     TEXT,
    power_source  TEXT,
    friendly_name TEXT,
    capabilities  TEXT,
    state         TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    online        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    ieee    TEXT,
    kind    TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_ieee ON events (ieee);
"""


def _now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Device:
    """A node known to the hub, in transport-agnostic form."""

    ieee: str
    driver: str
    nwk: Optional[str] = None
    model: Optional[str] = None
    vendor: Optional[str] = None
    node_type: Optional[str] = None
    power_source: Optional[str] = None
    friendly_name: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    online: bool = False

    def to_row(self) -> Dict[str, Any]:
        """Flatten for SQLite; JSON-encode the list/dict fields."""
        row = asdict(self)
        row["capabilities"] = json.dumps(self.capabilities)
        row["state"] = json.dumps(self.state)
        row["online"] = 1 if self.online else 0
        return row

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Device":
        """Rebuild from a SQLite row, tolerating absent JSON columns."""
        return cls(
            ieee=row["ieee"],
            driver=row["driver"],
            nwk=row["nwk"],
            model=row["model"],
            vendor=row["vendor"],
            node_type=row["node_type"],
            power_source=row["power_source"],
            friendly_name=row["friendly_name"],
            capabilities=json.loads(row["capabilities"] or "[]"),
            state=json.loads(row["state"] or "{}"),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            online=bool(row["online"]),
        )


class Registry:
    """Thread-safe device registry and event log."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- devices ------------------------------------------------------------

    def upsert(self, device: Device) -> Device:
        """Insert or update a device, preserving first_seen and friendly_name."""
        row = device.to_row()
        with self._lock:
            existing = self._conn.execute(
                "SELECT first_seen, friendly_name FROM devices WHERE ieee = ?",
                (device.ieee,),
            ).fetchone()
            if existing:
                row["first_seen"] = existing["first_seen"]
                if not row.get("friendly_name"):
                    row["friendly_name"] = existing["friendly_name"]
            else:
                row["first_seen"] = row.get("first_seen") or _now()
            row["last_seen"] = _now()
            cols = ", ".join(row)
            marks = ", ".join(f":{c}" for c in row)
            self._conn.execute(
                f"INSERT OR REPLACE INTO devices ({cols}) VALUES ({marks})", row
            )
            self._conn.commit()
        return self.get(device.ieee)  # type: ignore[return-value]

    def get(self, ieee: str) -> Optional[Device]:
        """Fetch one device by IEEE address, case-insensitive."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE ieee = ?", (ieee.lower(),)
            ).fetchone()
        return Device.from_row(row) if row else None

    def list(self) -> List[Device]:
        """All devices, coordinators first then by friendly name."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY "
                "CASE node_type WHEN 'coordinator' THEN 0 ELSE 1 END, "
                "COALESCE(friendly_name, ieee)"
            ).fetchall()
        return [Device.from_row(r) for r in rows]

    def rename(self, ieee: str, friendly_name: str) -> Optional[Device]:
        """Set a device's friendly name."""
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET friendly_name = ? WHERE ieee = ?",
                (friendly_name, ieee.lower()),
            )
            self._conn.commit()
        return self.get(ieee)

    def delete(self, ieee: str) -> bool:
        """Remove a device. Returns True if a row was deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM devices WHERE ieee = ?", (ieee.lower(),)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def update_state(self, ieee: str, state: Dict[str, Any]) -> None:
        """Merge new state into a device's stored state."""
        dev = self.get(ieee)
        if not dev:
            return
        merged = {**dev.state, **state}
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET state = ?, last_seen = ? WHERE ieee = ?",
                (json.dumps(merged), _now(), ieee.lower()),
            )
            self._conn.commit()

    def set_online(self, ieee: str, online: bool) -> None:
        """Mark a device online or offline."""
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET online = ?, last_seen = ? WHERE ieee = ?",
                (1 if online else 0, _now(), ieee.lower()),
            )
            self._conn.commit()

    # -- events -------------------------------------------------------------

    def log_event(
        self,
        kind: str,
        ieee: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append an event to the journal."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, ieee, kind, payload) VALUES (?,?,?,?)",
                (_now(), ieee, kind, json.dumps(payload or {})),
            )
            self._conn.commit()

    def events(self, limit: int = 100, ieee: Optional[str] = None) -> List[Dict]:
        """Most recent events, newest first."""
        sql = "SELECT * FROM events"
        args: List[Any] = []
        if ieee:
            sql += " WHERE ieee = ?"
            args.append(ieee.lower())
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "ieee": r["ieee"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"] or "{}"),
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
