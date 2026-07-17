from __future__ import annotations

"""Persistent store for the KOL watchlist and (future) following snapshots.

Deliberately mirrors `watchlist_store.py`: stdlib sqlite3 (no new dependency), one
module-level connection guarded by a lock, defensive reads that tolerate an
empty/missing DB, and a `reset_for_tests` hook. It is intentionally low-level —
raw CRUD over rows — with all validation and business rules living one layer up in
`kol_watchlist.py`. That keeps the store swappable if the project later moves to a
different backend for scale.

Schema (three tables, designed so later deliverables extend rather than migrate):
  - kols               : the watchlist (one row per platform+handle)
  - following_snapshots: point-in-time captures of who a KOL follows (Deliverable
                         B/C populate/diff these; the schema exists now so the
                         persistence model is production-ready)
  - sync_meta          : last-successful-sync bookkeeping per KOL, kept separate
                         from `kols` so sync accounting doesn't churn the entry row

Snapshots store their account list as JSON in one row; this is fine at watchlist
scale (tens of KOLs, one current + one previous snapshot each) and avoids a wide
join. If snapshot volume grows, this table can be normalized without touching the
`kols` table or the public API.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.models.kol import FollowingSnapshot, KolEntry, SocialAccount

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    db_path = Path(settings.kol_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kols (
            platform TEXT NOT NULL,
            handle TEXT NOT NULL,
            display_name TEXT,
            tier INTEGER NOT NULL DEFAULT 2,
            enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            date_added TEXT NOT NULL,
            last_checked TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (platform, handle)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS following_snapshots (
            platform TEXT NOT NULL,
            handle TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 1,
            accounts TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (platform, handle, captured_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_meta (
            platform TEXT NOT NULL,
            handle TEXT NOT NULL,
            last_success TEXT,
            last_attempt TEXT,
            last_error TEXT,
            PRIMARY KEY (platform, handle)
        )
        """
    )
    conn.commit()
    _CONN = conn
    return conn


# The store keys on (platform, lowercased handle). Handles are normalized here as a
# defensive backstop; the model/service already normalize, but the store must never
# create two rows that differ only by case.
def _norm(platform: str, handle: str) -> tuple[str, str]:
    return (platform.strip().lower(), handle.strip().lstrip("@").strip().lower())


def _row_to_entry(r: sqlite3.Row) -> KolEntry:
    return KolEntry(
        platform=r["platform"],
        handle=r["handle"],
        display_name=r["display_name"],
        tier=r["tier"],
        enabled=bool(r["enabled"]),
        notes=r["notes"],
        date_added=r["date_added"],
        last_checked=r["last_checked"],
        status=r["status"],
    )


# --- KOL CRUD ----------------------------------------------------------------


def upsert_kol(entry: KolEntry) -> None:
    """Insert or update a KOL. Preserves the original date_added on update."""
    platform, handle = _norm(entry.platform, entry.handle)
    with _LOCK:
        conn = _connect()
        existing = conn.execute(
            "SELECT date_added FROM kols WHERE platform = ? AND handle = ?",
            (platform, handle),
        ).fetchone()
        date_added = existing["date_added"] if existing else (entry.date_added or _now())
        conn.execute(
            """
            INSERT INTO kols
                (platform, handle, display_name, tier, enabled, notes, date_added, last_checked, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, handle) DO UPDATE SET
                display_name=excluded.display_name,
                tier=excluded.tier,
                enabled=excluded.enabled,
                notes=excluded.notes,
                last_checked=excluded.last_checked,
                status=excluded.status
            """,
            (
                platform,
                handle,
                entry.display_name,
                entry.tier,
                1 if entry.enabled else 0,
                entry.notes,
                date_added,
                entry.last_checked,
                entry.status,
            ),
        )
        conn.commit()


def get_kol(platform: str, handle: str) -> KolEntry | None:
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM kols WHERE platform = ? AND handle = ?", (p, h)
        ).fetchone()
        return _row_to_entry(r) if r else None


def list_kols(
    platform: str | None = None,
    *,
    enabled_only: bool = False,
    limit: int = 500,
) -> list[KolEntry]:
    with _LOCK:
        conn = _connect()
        clauses = []
        params: list = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform.strip().lower())
        if enabled_only:
            clauses.append("enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM kols {where} ORDER BY tier ASC, handle ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]


def delete_kol(platform: str, handle: str) -> bool:
    """Remove a KOL and its snapshots/sync rows. Returns True if a row was deleted."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        cur = conn.execute("DELETE FROM kols WHERE platform = ? AND handle = ?", (p, h))
        conn.execute("DELETE FROM following_snapshots WHERE platform = ? AND handle = ?", (p, h))
        conn.execute("DELETE FROM sync_meta WHERE platform = ? AND handle = ?", (p, h))
        conn.commit()
        return cur.rowcount > 0


def set_last_checked(platform: str, handle: str, status: str, when: str | None = None) -> None:
    """Stamp a KOL's last-check time and lifecycle status (called after a sync)."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        conn.execute(
            "UPDATE kols SET last_checked = ?, status = ? WHERE platform = ? AND handle = ?",
            (when or _now(), status, p, h),
        )
        conn.commit()


# --- Snapshot persistence (schema + reader ready; producers land in later deliverables) ---


def save_snapshot(snapshot: FollowingSnapshot) -> None:
    """Persist a following snapshot. Later deliverables call this; provided now so
    the persistence model is complete and testable."""
    p, h = _norm(snapshot.platform, snapshot.kol_handle)
    payload = json.dumps([a.model_dump() for a in snapshot.accounts])
    with _LOCK:
        conn = _connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO following_snapshots
                (platform, handle, captured_at, complete, accounts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (p, h, snapshot.captured_at, 1 if snapshot.complete else 0, payload),
        )
        conn.commit()


def latest_snapshot(platform: str, handle: str) -> FollowingSnapshot | None:
    """Most recent snapshot for a KOL, or None. Diffing (Deliverable C) will pull
    the two most recent; the schema/reader support that already."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            """
            SELECT * FROM following_snapshots
            WHERE platform = ? AND handle = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (p, h),
        ).fetchone()
        if r is None:
            return None
        raw = json.loads(r["accounts"] or "[]")
        return FollowingSnapshot(
            platform=r["platform"],
            kol_handle=r["handle"],
            captured_at=r["captured_at"],
            complete=bool(r["complete"]),
            accounts=[SocialAccount(**a) for a in raw],
        )


def record_sync(platform: str, handle: str, *, success: bool, error: str | None = None) -> None:
    """Book a sync attempt outcome. Kept separate from the `kols` row so sync
    accounting doesn't churn watchlist data."""
    p, h = _norm(platform, handle)
    now = _now()
    with _LOCK:
        conn = _connect()
        existing = conn.execute(
            "SELECT last_success FROM sync_meta WHERE platform = ? AND handle = ?", (p, h)
        ).fetchone()
        last_success = now if success else (existing["last_success"] if existing else None)
        conn.execute(
            """
            INSERT INTO sync_meta (platform, handle, last_success, last_attempt, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform, handle) DO UPDATE SET
                last_success=excluded.last_success,
                last_attempt=excluded.last_attempt,
                last_error=excluded.last_error
            """,
            (p, h, last_success, now, None if success else error),
        )
        conn.commit()


def get_sync_meta(platform: str, handle: str) -> dict | None:
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM sync_meta WHERE platform = ? AND handle = ?", (p, h)
        ).fetchone()
        return dict(r) if r else None


def reset_for_tests(db_path: str | None = None) -> None:
    """Close and rebind the connection (used by tests with a temp DB)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        if db_path:
            settings.kol_db_path = db_path
