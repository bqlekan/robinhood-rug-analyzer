from __future__ import annotations

"""Persistent store for the Token Watchlist & Monitoring Engine (M24).

Mirrors the established store pattern (`kol_store`, `watchlist_store`): stdlib
`sqlite3` so there is no extra dependency, one module-level connection guarded by
a lock, an idempotent schema created on first connect, and `reset_for_tests` to
rebind the connection at a temp DB. Kept in its own DB file so the monitoring
domain (watchlist + history + events) stays decoupled from the wallet and KOL
stores and can grow independently.

Three tables:
  - `token_watchlist`  — the monitored tokens (CRUD + enable/disable + options).
  - `monitor_history`  — append-only before/after change records (deduped: a row
    is written only when something actually changed).
  - `monitor_events`   — append-only internal monitoring-event timeline.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.models.monitor import (
    MonitorEvent,
    MonitorHistoryEntry,
    MonitorOptions,
    MonitorSnapshot,
    TokenWatchEntry,
)

logger = logging.getLogger(__name__)

# One module-level connection guarded by a lock — matches kol_store/watchlist_store.
_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(address: str) -> str:
    return (address or "").strip().lower()


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    db_path = Path(settings.token_monitor_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_watchlist (
            contract_address TEXT PRIMARY KEY,
            label TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            options TEXT NOT NULL DEFAULT '{}',
            date_added TEXT NOT NULL,
            last_checked TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    # Append-only change history. AUTOINCREMENT id gives a stable chronological
    # order even when captured_at ties. One row is written only when a change was
    # detected (see save_history_if_changed) so untouched tokens don't churn it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_address TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            changed_fields TEXT NOT NULL DEFAULT '[]',
            previous_values TEXT NOT NULL DEFAULT '{}',
            current_values TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    # Latest tracked snapshot per token — the baseline the next cycle diffs
    # against. One row per token (the current values); history holds the deltas.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_latest (
            contract_address TEXT PRIMARY KEY,
            captured_at TEXT NOT NULL,
            values_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    # Append-only internal event timeline (never updated/deleted).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            contract_address TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.commit()
    _CONN = conn
    return conn


# --- Watchlist CRUD ----------------------------------------------------------


def _row_to_entry(r: sqlite3.Row) -> TokenWatchEntry:
    try:
        options = MonitorOptions(**json.loads(r["options"] or "{}"))
    except (ValueError, TypeError):
        options = MonitorOptions()
    return TokenWatchEntry(
        contract_address=r["contract_address"],
        label=r["label"],
        enabled=bool(r["enabled"]),
        options=options,
        date_added=r["date_added"],
        last_checked=r["last_checked"],
        status=r["status"],
    )


def upsert_entry(entry: TokenWatchEntry) -> None:
    """Insert or update a watched token. Preserves the original date_added."""
    address = _norm(entry.contract_address)
    with _LOCK:
        conn = _connect()
        existing = conn.execute(
            "SELECT date_added FROM token_watchlist WHERE contract_address = ?",
            (address,),
        ).fetchone()
        date_added = existing["date_added"] if existing else (entry.date_added or _now())
        conn.execute(
            """
            INSERT INTO token_watchlist
                (contract_address, label, enabled, options, date_added, last_checked, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contract_address) DO UPDATE SET
                label=excluded.label,
                enabled=excluded.enabled,
                options=excluded.options,
                last_checked=excluded.last_checked,
                status=excluded.status
            """,
            (
                address,
                entry.label,
                1 if entry.enabled else 0,
                json.dumps(entry.options.model_dump()),
                date_added,
                entry.last_checked,
                entry.status,
            ),
        )
        conn.commit()


def get_entry(address: str) -> TokenWatchEntry | None:
    a = _norm(address)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM token_watchlist WHERE contract_address = ?", (a,)
        ).fetchone()
        return _row_to_entry(r) if r else None


def list_entries(*, enabled_only: bool = False, limit: int = 500) -> list[TokenWatchEntry]:
    with _LOCK:
        conn = _connect()
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = conn.execute(
            f"SELECT * FROM token_watchlist {where} "
            "ORDER BY date_added ASC, contract_address ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]


def delete_entry(address: str) -> bool:
    """Remove a token and all its monitoring data. True if a row was deleted."""
    a = _norm(address)
    with _LOCK:
        conn = _connect()
        cur = conn.execute("DELETE FROM token_watchlist WHERE contract_address = ?", (a,))
        conn.execute("DELETE FROM monitor_history WHERE contract_address = ?", (a,))
        conn.execute("DELETE FROM monitor_latest WHERE contract_address = ?", (a,))
        conn.execute("DELETE FROM monitor_events WHERE contract_address = ?", (a,))
        conn.commit()
        return cur.rowcount > 0


def set_last_checked(address: str, status: str, when: str | None = None) -> None:
    """Stamp a token's last-check time and lifecycle status (after a cycle)."""
    a = _norm(address)
    with _LOCK:
        conn = _connect()
        conn.execute(
            "UPDATE token_watchlist SET last_checked = ?, status = ? WHERE contract_address = ?",
            (when or _now(), status, a),
        )
        conn.commit()


# --- Latest snapshot (diff baseline) -----------------------------------------


def get_latest_values(address: str) -> dict | None:
    """The last tracked value map for a token, or None if never monitored."""
    a = _norm(address)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT values_json FROM monitor_latest WHERE contract_address = ?", (a,)
        ).fetchone()
    if r is None:
        return None
    try:
        return json.loads(r["values_json"] or "{}")
    except (ValueError, TypeError):
        return None


def _save_latest(conn: sqlite3.Connection, snapshot: MonitorSnapshot) -> None:
    conn.execute(
        """
        INSERT INTO monitor_latest (contract_address, captured_at, values_json)
        VALUES (?, ?, ?)
        ON CONFLICT(contract_address) DO UPDATE SET
            captured_at=excluded.captured_at,
            values_json=excluded.values_json
        """,
        (
            _norm(snapshot.contract_address),
            snapshot.captured_at,
            json.dumps(snapshot.tracked_values()),
        ),
    )


def save_history_if_changed(
    snapshot: MonitorSnapshot,
    previous_values: dict | None,
    changed_fields: list[str],
) -> bool:
    """Persist the new snapshot as the diff baseline, and — only when something
    changed — append a history row recording the before/after.

    Returns True if a history row was written (i.e. a change was recorded). The
    latest-snapshot baseline is ALWAYS updated so `last_checked`-style freshness
    is accurate; the history table stays free of duplicate no-change rows."""
    address = _norm(snapshot.contract_address)
    wrote_history = bool(changed_fields)
    with _LOCK:
        conn = _connect()
        _save_latest(conn, snapshot)
        if wrote_history:
            current = snapshot.tracked_values()
            conn.execute(
                """
                INSERT INTO monitor_history
                    (contract_address, captured_at, changed_fields, previous_values, current_values)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    address,
                    snapshot.captured_at,
                    json.dumps(list(changed_fields)),
                    json.dumps(previous_values or {}),
                    json.dumps({k: current.get(k) for k in changed_fields}),
                ),
            )
            _prune_history(conn, address)
        conn.commit()
    return wrote_history


def _prune_history(conn: sqlite3.Connection, address: str) -> None:
    """Keep only the most recent `token_monitor_history_retain` rows per token."""
    retain = int(settings.token_monitor_history_retain)
    if retain <= 0:
        return
    conn.execute(
        """
        DELETE FROM monitor_history
        WHERE contract_address = ? AND id NOT IN (
            SELECT id FROM monitor_history
            WHERE contract_address = ?
            ORDER BY id DESC LIMIT ?
        )
        """,
        (address, address, retain),
    )


def list_history(address: str, *, limit: int = 200) -> list[MonitorHistoryEntry]:
    """Recent monitoring-history rows for a token, newest first."""
    a = _norm(address)
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM monitor_history WHERE contract_address = ? "
            "ORDER BY id DESC LIMIT ?",
            (a, int(limit)),
        ).fetchall()
    out: list[MonitorHistoryEntry] = []
    for r in rows:
        try:
            changed = json.loads(r["changed_fields"] or "[]")
            prev = json.loads(r["previous_values"] or "{}")
            curr = json.loads(r["current_values"] or "{}")
        except (ValueError, TypeError):
            changed, prev, curr = [], {}, {}
        out.append(
            MonitorHistoryEntry(
                contract_address=r["contract_address"],
                captured_at=r["captured_at"],
                changed_fields=changed,
                previous_values=prev,
                current_values=curr,
            )
        )
    return out


# --- Events ------------------------------------------------------------------


def save_events(events: list[MonitorEvent]) -> None:
    """Append monitoring events. Append-only timeline (never updates/deletes)."""
    if not events:
        return
    with _LOCK:
        conn = _connect()
        conn.executemany(
            "INSERT INTO monitor_events (event_type, contract_address, detected_at, payload) "
            "VALUES (?, ?, ?, ?)",
            [
                (e.event_type, _norm(e.contract_address), e.detected_at, json.dumps(e.payload))
                for e in events
            ],
        )
        conn.commit()


def list_events(
    address: str | None = None, *, event_type: str | None = None, limit: int = 200
) -> list[MonitorEvent]:
    """Recent monitoring events, newest first. Optional address/type filters."""
    with _LOCK:
        conn = _connect()
        clauses: list[str] = []
        params: list = []
        if address is not None:
            clauses.append("contract_address = ?")
            params.append(_norm(address))
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT * FROM monitor_events {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    out: list[MonitorEvent] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append(
            MonitorEvent(
                event_type=r["event_type"],
                contract_address=r["contract_address"],
                detected_at=r["detected_at"],
                payload=payload,
            )
        )
    return out


def reset_for_tests(db_path: str | None = None) -> None:
    """Close and rebind the connection (used by tests with a temp DB)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        if db_path:
            settings.token_monitor_db_path = db_path
