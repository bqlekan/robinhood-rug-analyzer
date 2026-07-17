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
from app.models.kol import (
    FollowEvent,
    FollowingSnapshot,
    KolEntry,
    ProfileChange,
    SocialAccount,
)

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
    # --- Deliverable C: follow-change detection tables -----------------------
    # Detected follow/unfollow events. Append-only history (one row per detection)
    # so later intelligence/alerting modules read a durable log. AUTOINCREMENT id
    # gives a stable chronological order even when detected_at ties.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS follow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            platform TEXT NOT NULL,
            kol_handle TEXT NOT NULL,
            account_key TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT '{}',
            detected_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_events_kol "
        "ON follow_events (platform, kol_handle, detected_at)"
    )
    # Detected profile-attribute changes on already-followed accounts.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            kol_handle TEXT NOT NULL,
            account_key TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            detected_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_profile_changes_kol "
        "ON profile_changes (platform, kol_handle, detected_at)"
    )
    # Current known metadata per followed account, with first/last-seen tracking.
    # The whole SocialAccount is stored as JSON (not fixed columns) so new profile
    # fields land here without a schema migration. `active` marks whether the KOL
    # currently follows this account (an unfollow flips it to 0, preserving history).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS followed_accounts (
            platform TEXT NOT NULL,
            kol_handle TEXT NOT NULL,
            account_key TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT '{}',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (platform, kol_handle, account_key)
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
        conn.execute("DELETE FROM follow_events WHERE platform = ? AND kol_handle = ?", (p, h))
        conn.execute("DELETE FROM profile_changes WHERE platform = ? AND kol_handle = ?", (p, h))
        conn.execute("DELETE FROM followed_accounts WHERE platform = ? AND kol_handle = ?", (p, h))
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
    """Persist a following snapshot, then prune old ones per the retention policy.

    Diffing only ever needs the latest *complete* snapshot; a small history is kept
    for debugging/trend work. `settings.kol_snapshot_retain` bounds the per-KOL row
    count so the table can't grow forever."""
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
    _prune_snapshots(p, h)


def _prune_snapshots(platform: str, handle: str) -> None:
    """Keep only the most recent `kol_snapshot_retain` snapshots for a KOL.

    Assumes `platform`/`handle` are already normalized. A retain value <= 0 disables
    pruning (keep everything). The most recent complete snapshot is ALWAYS preserved
    even if newer incomplete/pruned rows would otherwise push it out of the window,
    so pruning can never destroy the diff baseline and turn the next capture into a
    spurious mass-unfollow."""
    retain = settings.kol_snapshot_retain
    if retain is None or retain <= 0:
        return
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            """
            SELECT captured_at, complete FROM following_snapshots
            WHERE platform = ? AND handle = ?
            ORDER BY captured_at DESC
            """,
            (platform, handle),
        ).fetchall()
        if len(rows) <= retain:
            return
        keep: set[str] = {r["captured_at"] for r in rows[:retain]}
        # Guarantee the newest complete snapshot survives regardless of the window.
        for r in rows:
            if r["complete"]:
                keep.add(r["captured_at"])
                break
        stale = [r["captured_at"] for r in rows if r["captured_at"] not in keep]
        if not stale:
            return
        placeholders = ",".join("?" for _ in stale)
        conn.execute(
            f"DELETE FROM following_snapshots WHERE platform = ? AND handle = ? "
            f"AND captured_at IN ({placeholders})",
            (platform, handle, *stale),
        )
        conn.commit()


def _row_to_snapshot(r: sqlite3.Row) -> FollowingSnapshot | None:
    """Deserialize a snapshot row, tolerating corruption.

    A row whose `accounts` JSON is malformed or whose account objects fail model
    validation is treated as unreadable and returns None, rather than raising.
    Callers (diffing, baselining) then fall back to an older intact snapshot — a
    corrupted capture must never crash the engine or be mistaken for 'follows
    nobody'."""
    try:
        raw = json.loads(r["accounts"] or "[]")
        accounts = [SocialAccount(**a) for a in raw]
    except (ValueError, TypeError) as exc:  # JSON error or bad account shape
        logger.warning(
            "Skipping corrupted snapshot for %s:%s @ %s: %s",
            r["platform"], r["handle"], r["captured_at"], exc,
        )
        return None
    return FollowingSnapshot(
        platform=r["platform"],
        kol_handle=r["handle"],
        captured_at=r["captured_at"],
        complete=bool(r["complete"]),
        accounts=accounts,
    )


def latest_snapshot(platform: str, handle: str) -> FollowingSnapshot | None:
    """Most recent snapshot for a KOL (complete or not), or None if none is
    readable. Skips corrupted rows, returning the newest intact one."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            """
            SELECT * FROM following_snapshots
            WHERE platform = ? AND handle = ?
            ORDER BY captured_at DESC
            """,
            (p, h),
        ).fetchall()
    for r in rows:
        snap = _row_to_snapshot(r)
        if snap is not None:
            return snap
    return None


def latest_complete_snapshot(platform: str, handle: str) -> FollowingSnapshot | None:
    """Most recent *complete* and readable snapshot for a KOL, or None.

    This is the diff baseline: an incomplete (interrupted/partial) capture is never
    used as 'the previous known state', so gaps in a bad pull can't be misread as
    unfollows. Corrupted rows are skipped, so the engine recovers by comparing
    against the last snapshot that is both complete and intact."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            """
            SELECT * FROM following_snapshots
            WHERE platform = ? AND handle = ? AND complete = 1
            ORDER BY captured_at DESC
            """,
            (p, h),
        ).fetchall()
    for r in rows:
        snap = _row_to_snapshot(r)
        if snap is not None:
            return snap
    return None


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


# --- Follow-change persistence (Deliverable C) -------------------------------


def save_follow_events(events: list[FollowEvent]) -> None:
    """Append detected follow/unfollow events. Append-only: never updates or
    deletes existing rows, so the event history is a durable audit log."""
    if not events:
        return
    with _LOCK:
        conn = _connect()
        conn.executemany(
            """
            INSERT INTO follow_events
                (event_type, platform, kol_handle, account_key, account, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e.event_type,
                    *_norm(e.platform, e.kol_handle),
                    e.account_key,
                    json.dumps(e.account.model_dump()),
                    e.detected_at,
                )
                for e in events
            ],
        )
        conn.commit()


def list_follow_events(
    platform: str,
    handle: str,
    *,
    event_type: str | None = None,
    limit: int = 200,
) -> list[FollowEvent]:
    """Recent follow events for a KOL, newest first. Optionally filter by type."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        clauses = ["platform = ?", "kol_handle = ?"]
        params: list = [p, h]
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM follow_events WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    out: list[FollowEvent] = []
    for r in rows:
        try:
            account = SocialAccount(**json.loads(r["account"] or "{}"))
        except (ValueError, TypeError):
            # Fall back to a minimal account rather than dropping the event.
            account = SocialAccount(platform=r["platform"], handle=r["account_key"])
        out.append(FollowEvent(
            event_type=r["event_type"],
            platform=r["platform"],
            kol_handle=r["kol_handle"],
            account_key=r["account_key"],
            account=account,
            detected_at=r["detected_at"],
        ))
    return out


def save_profile_changes(changes: list[ProfileChange]) -> None:
    """Append detected profile-attribute changes. Append-only history."""
    if not changes:
        return
    with _LOCK:
        conn = _connect()
        conn.executemany(
            """
            INSERT INTO profile_changes
                (platform, kol_handle, account_key, field, old_value, new_value, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    *_norm(c.platform, c.kol_handle),
                    c.account_key,
                    c.field,
                    c.old_value,
                    c.new_value,
                    c.detected_at,
                )
                for c in changes
            ],
        )
        conn.commit()


def list_profile_changes(
    platform: str,
    handle: str,
    *,
    field: str | None = None,
    limit: int = 200,
) -> list[ProfileChange]:
    """Recent profile changes for a KOL, newest first. Optionally filter by field."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        clauses = ["platform = ?", "kol_handle = ?"]
        params: list = [p, h]
        if field:
            clauses.append("field = ?")
            params.append(field)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM profile_changes WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [
        ProfileChange(
            platform=r["platform"],
            kol_handle=r["kol_handle"],
            account_key=r["account_key"],
            field=r["field"],
            old_value=r["old_value"],
            new_value=r["new_value"],
            detected_at=r["detected_at"],
        )
        for r in rows
    ]


def upsert_followed_account(
    platform: str,
    handle: str,
    account: SocialAccount,
    *,
    active: bool,
    seen_at: str | None = None,
) -> None:
    """Record/refresh the current known metadata for a followed account.

    First insert stamps `first_seen`; subsequent upserts refresh the stored
    metadata and `last_seen` and the `active` flag (an unfollow sets active=0),
    while preserving the original `first_seen`."""
    p, h = _norm(platform, handle)
    now = seen_at or _now()
    payload = json.dumps(account.model_dump())
    with _LOCK:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO followed_accounts
                (platform, kol_handle, account_key, account, first_seen, last_seen, active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, kol_handle, account_key) DO UPDATE SET
                account=excluded.account,
                last_seen=excluded.last_seen,
                active=excluded.active
            """,
            (p, h, account.key(), payload, now, now, 1 if active else 0),
        )
        conn.commit()


def deactivate_followed_account(platform: str, handle: str, account_key: str) -> None:
    """Mark a followed account as no longer active (an unfollow) without touching
    its metadata or `last_seen` — `last_seen` stays the last time it was actually
    observed in the following list, preserving accurate history."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        conn.execute(
            "UPDATE followed_accounts SET active = 0 "
            "WHERE platform = ? AND kol_handle = ? AND account_key = ?",
            (p, h, account_key),
        )
        conn.commit()


def get_followed_account(platform: str, handle: str, account_key: str) -> dict | None:
    """Stored metadata row for one followed account (dict with account + seen
    timestamps + active), or None."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM followed_accounts "
            "WHERE platform = ? AND kol_handle = ? AND account_key = ?",
            (p, h, account_key),
        ).fetchone()
    if r is None:
        return None
    return _followed_row_to_dict(r)


def list_followed_accounts(
    platform: str,
    handle: str,
    *,
    active_only: bool = True,
    limit: int = 5000,
) -> list[dict]:
    """Stored followed-account metadata for a KOL. Defaults to currently-active
    follows; pass active_only=False to include historical (unfollowed) ones."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        clauses = ["platform = ?", "kol_handle = ?"]
        params: list = [p, h]
        if active_only:
            clauses.append("active = 1")
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM followed_accounts WHERE {' AND '.join(clauses)} "
            "ORDER BY last_seen DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_followed_row_to_dict(r) for r in rows]


def _followed_row_to_dict(r: sqlite3.Row) -> dict:
    try:
        account = SocialAccount(**json.loads(r["account"] or "{}"))
    except (ValueError, TypeError):
        account = SocialAccount(platform=r["platform"], handle=r["account_key"])
    return {
        "platform": r["platform"],
        "kol_handle": r["kol_handle"],
        "account_key": r["account_key"],
        "account": account,
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "active": bool(r["active"]),
    }


def reset_for_tests(db_path: str | None = None) -> None:
    """Close and rebind the connection (used by tests with a temp DB)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        if db_path:
            settings.kol_db_path = db_path
