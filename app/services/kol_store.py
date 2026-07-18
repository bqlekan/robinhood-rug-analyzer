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
    ClusterInfo,
    CryptoClassification,
    CryptoIntelEvent,
    Evidence,
    ExtractedContract,
    FollowEvent,
    FollowingSnapshot,
    KolContributor,
    KolEntry,
    KolIntelEvent,
    ProfileChange,
    ProjectIntelligence,
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
    # --- Deliverable D: crypto intelligence tables ---------------------------
    # Latest classification per followed account (upserted): the account type,
    # confidence band, weighted score, and the fired signals + evidence + extracted
    # contracts as JSON. One row per account (the current verdict); the event log
    # below keeps the history. JSON payloads keep the schema forward-compatible as
    # the evidence/contract shapes evolve.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_classifications (
            platform TEXT NOT NULL,
            kol_handle TEXT NOT NULL,
            account_key TEXT NOT NULL,
            account_handle TEXT NOT NULL DEFAULT '',
            classification TEXT NOT NULL,
            confidence TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            signals TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]',
            contracts TEXT NOT NULL DEFAULT '[]',
            classified_at TEXT NOT NULL,
            PRIMARY KEY (platform, kol_handle, account_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crypto_class_kol "
        "ON crypto_classifications (platform, kol_handle, classification)"
    )
    # Engine-internal crypto-pipeline events (detected/extracted/analyzed/failed).
    # Append-only audit log — NOT user alerts. `payload` is a self-describing JSON
    # dict (e.g. the analyzed contract + risk summary) so later intelligence and the
    # eventual alerter read history rather than recomputing.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            platform TEXT NOT NULL,
            kol_handle TEXT NOT NULL,
            account_key TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crypto_events_kol "
        "ON crypto_events (platform, kol_handle, detected_at)"
    )
    # --- Deliverable F: KOL intelligence scoring & correlation ---------------
    # Current KOL Intelligence for one project account (upserted): the score, its
    # confidence band, the full structured evidence + contributors + cluster +
    # correlation of the reused analysis, all as JSON so the schema is forward-
    # compatible and the object is self-describing for the future AI stage. Keyed by
    # the PROJECT account, NOT a single KOL — this is the cross-KOL correlation unit.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kol_intel_scores (
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            project_handle TEXT,
            classification TEXT,
            crypto_confidence TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            confidence TEXT NOT NULL DEFAULT 'very_low',
            kol_count INTEGER NOT NULL DEFAULT 0,
            evidence TEXT NOT NULL DEFAULT '[]',
            contributors TEXT NOT NULL DEFAULT '[]',
            cluster TEXT,
            correlation TEXT NOT NULL DEFAULT '{}',
            fingerprint TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (platform, account_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kol_intel_scores_score "
        "ON kol_intel_scores (platform, score)"
    )
    # Append-only score history: one row each time a project's score is (re)computed
    # to a new value. Powers momentum + future historical analytics/AI timelines.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kol_intel_score_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            score INTEGER NOT NULL,
            confidence TEXT NOT NULL,
            kol_count INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kol_score_hist "
        "ON kol_intel_score_history (platform, account_key, id)"
    )
    # Append-only cluster history: a row each time a cluster is (re)detected, with the
    # typed kinds + membership snapshot as JSON, so how a cluster formed over time is
    # queryable later without recomputation.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kol_cluster_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            cluster_types TEXT NOT NULL DEFAULT '[]',
            kol_count INTEGER NOT NULL DEFAULT 0,
            cluster TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kol_cluster_hist "
        "ON kol_cluster_history (platform, account_key, id)"
    )
    # Append-only intelligence events (score updated / cluster / momentum / umbrella).
    # Engine-internal timeline — NOT user notifications (transports are Deliverable H).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kol_intel_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            project_handle TEXT,
            detected_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kol_intel_events "
        "ON kol_intel_events (platform, account_key, id)"
    )
    # --- Deliverable H: notification delivery log ----------------------------
    # One row per (event, destination) delivery attempt: status, timestamp, and the
    # error message on failure. The (event_key, destination) pair is UNIQUE so a
    # replayed/duplicate event is not delivered twice to the same destination — the
    # dedupe seam for the notification layer. Append-only audit; never updated.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            platform TEXT NOT NULL,
            account_key TEXT NOT NULL,
            destination TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            attempted_at TEXT NOT NULL,
            UNIQUE (event_key, destination)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_deliveries "
        "ON notification_deliveries (platform, account_key, id)"
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


# --- Crypto intelligence persistence (Deliverable D) -------------------------


def save_classification(kol_handle: str, classification: CryptoClassification) -> None:
    """Upsert the current crypto classification for a followed account.

    `kol_handle` is the watched KOL whose follow this classification belongs to;
    `classification.handle` is the followed account itself. One row per
    (platform, kol_handle, account_key) — the latest verdict. Signals, evidence, and
    contracts are stored as JSON so the evidence trail is complete and the schema
    survives model changes. History lives in the crypto_events log."""
    p, h = _norm(classification.platform, kol_handle)
    with _LOCK:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO crypto_classifications
                (platform, kol_handle, account_key, account_handle, classification,
                 confidence, score, signals, evidence, contracts, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, kol_handle, account_key) DO UPDATE SET
                account_handle=excluded.account_handle,
                classification=excluded.classification,
                confidence=excluded.confidence,
                score=excluded.score,
                signals=excluded.signals,
                evidence=excluded.evidence,
                contracts=excluded.contracts,
                classified_at=excluded.classified_at
            """,
            (
                p, h, classification.account_key, classification.handle,
                classification.classification,
                classification.confidence,
                classification.score,
                json.dumps(classification.signals),
                json.dumps([e.model_dump() for e in classification.evidence]),
                json.dumps([c.model_dump() for c in classification.contracts]),
                classification.classified_at,
            ),
        )
        conn.commit()


def get_classification(platform: str, handle: str, account_key: str) -> CryptoClassification | None:
    """The current classification for one followed account, or None."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM crypto_classifications "
            "WHERE platform = ? AND kol_handle = ? AND account_key = ?",
            (p, h, account_key),
        ).fetchone()
    return _row_to_classification(r) if r else None


def list_classifications(
    platform: str,
    handle: str,
    *,
    classification: str | None = None,
    limit: int = 5000,
) -> list[CryptoClassification]:
    """Stored classifications for a KOL's follows, newest first. Optionally filter by
    account type (e.g. only 'official' projects)."""
    p, h = _norm(platform, handle)
    with _LOCK:
        conn = _connect()
        clauses = ["platform = ?", "kol_handle = ?"]
        params: list = [p, h]
        if classification:
            clauses.append("classification = ?")
            params.append(classification)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM crypto_classifications WHERE {' AND '.join(clauses)} "
            "ORDER BY classified_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_row_to_classification(r) for r in rows]


def _row_to_classification(r: sqlite3.Row) -> CryptoClassification:
    try:
        signals = json.loads(r["signals"] or "[]")
        evidence = [Evidence(**e) for e in json.loads(r["evidence"] or "[]")]
        contracts = [ExtractedContract(**c) for c in json.loads(r["contracts"] or "[]")]
    except (ValueError, TypeError):
        signals, evidence, contracts = [], [], []
    return CryptoClassification(
        platform=r["platform"],
        handle=r["account_handle"] or r["account_key"],
        account_key=r["account_key"],
        classification=r["classification"],
        confidence=r["confidence"],
        score=r["score"],
        signals=signals,
        evidence=evidence,
        contracts=contracts,
        classified_at=r["classified_at"],
    )


def save_crypto_events(events: list[CryptoIntelEvent]) -> None:
    """Append crypto-pipeline events. Append-only audit log (never updates/deletes)."""
    if not events:
        return
    with _LOCK:
        conn = _connect()
        conn.executemany(
            """
            INSERT INTO crypto_events
                (event_type, platform, kol_handle, account_key, detected_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e.event_type,
                    *_norm(e.platform, e.kol_handle),
                    e.account_key,
                    e.detected_at,
                    json.dumps(e.payload),
                )
                for e in events
            ],
        )
        conn.commit()


def list_crypto_events(
    platform: str,
    handle: str,
    *,
    event_type: str | None = None,
    limit: int = 200,
) -> list[CryptoIntelEvent]:
    """Recent crypto-pipeline events for a KOL, newest first. Optional type filter."""
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
            f"SELECT * FROM crypto_events WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    out: list[CryptoIntelEvent] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append(CryptoIntelEvent(
            event_type=r["event_type"],
            platform=r["platform"],
            kol_handle=r["kol_handle"],
            account_key=r["account_key"],
            detected_at=r["detected_at"],
            payload=payload,
        ))
    return out


# --- KOL intelligence persistence (Deliverable F) ----------------------------


def best_classification_for_account(platform: str, account_key: str) -> CryptoClassification | None:
    """The strongest crypto classification recorded for a project account across ALL
    KOLs who follow it (highest score wins, newest breaks ties). Correlation read:
    the crypto verdict belongs to the PROJECT, but Deliverable D stored it per-KOL;
    this picks the most confident view without recomputing anything."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM crypto_classifications WHERE platform = ? AND account_key = ? "
            "ORDER BY score DESC, classified_at DESC LIMIT 1",
            (p, account_key),
        ).fetchone()
    return _row_to_classification(r) if r else None


def latest_analysis_summary(platform: str, account_key: str) -> dict | None:
    """The most recent reused rug-analysis summary for a project account, taken from
    the Deliverable-D `analysis_completed` event log (across any KOL). Returns the
    stored payload dict (risk_score/risk_level/etc.) or None if never analyzed. This
    is how the correlation engine REUSES existing analysis instead of recomputing."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT payload FROM crypto_events "
            "WHERE platform = ? AND account_key = ? AND event_type = 'analysis_completed' "
            "ORDER BY id DESC LIMIT 1",
            (p, account_key),
        ).fetchone()
    if r is None:
        return None
    try:
        return json.loads(r["payload"] or "{}")
    except (ValueError, TypeError):
        return None


def list_kols_following(platform: str, account_key: str, *, active_only: bool = True) -> list[dict]:
    """Every watched KOL that follows a given project account, with follow timing.

    This is the cross-KOL correlation read at the heart of Deliverable F: the crypto
    pipeline stores follows keyed by (kol_handle, account_key); this inverts it to
    'who follows THIS project'. Each dict carries the KOL handle, their tier (joined
    from `kols`), and `first_seen`/`last_seen` (the follow timing). Rows for KOLs no
    longer in the watchlist still return (tier falls back to the stored value / a
    default) so history stays intact if a KOL is later removed."""
    p = platform.strip().lower()
    ak = account_key
    with _LOCK:
        conn = _connect()
        clauses = ["fa.platform = ?", "fa.account_key = ?"]
        params: list = [p, ak]
        if active_only:
            clauses.append("fa.active = 1")
        rows = conn.execute(
            f"""
            SELECT fa.platform, fa.kol_handle, fa.account_key, fa.account,
                   fa.first_seen, fa.last_seen, fa.active, k.tier AS kol_tier
            FROM followed_accounts fa
            LEFT JOIN kols k
                   ON k.platform = fa.platform AND k.handle = fa.kol_handle
            WHERE {' AND '.join(clauses)}
            ORDER BY fa.first_seen ASC
            """,
            tuple(params),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            account = SocialAccount(**json.loads(r["account"] or "{}"))
        except (ValueError, TypeError):
            account = SocialAccount(platform=r["platform"], handle=r["account_key"])
        out.append({
            "platform": r["platform"],
            "kol_handle": r["kol_handle"],
            "account_key": r["account_key"],
            "account": account,
            "tier": r["kol_tier"],  # may be None if the KOL was removed from the watchlist
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "active": bool(r["active"]),
        })
    return out


def save_project_intelligence(intel: ProjectIntelligence) -> None:
    """Upsert the current KOL Intelligence for a project account (one row per project).

    History (score/cluster) is appended separately by the callers below; this row is
    always the LATEST correlation object. Everything structured is stored as JSON so
    the object round-trips intact for the future AI stage."""
    p = intel.platform.strip().lower()
    with _LOCK:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO kol_intel_scores
                (platform, account_key, project_handle, classification, crypto_confidence,
                 score, confidence, kol_count, evidence, contributors, cluster,
                 correlation, fingerprint, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, account_key) DO UPDATE SET
                project_handle=excluded.project_handle,
                classification=excluded.classification,
                crypto_confidence=excluded.crypto_confidence,
                score=excluded.score,
                confidence=excluded.confidence,
                kol_count=excluded.kol_count,
                evidence=excluded.evidence,
                contributors=excluded.contributors,
                cluster=excluded.cluster,
                correlation=excluded.correlation,
                fingerprint=excluded.fingerprint,
                updated_at=excluded.updated_at
            """,
            (
                p, intel.account_key, intel.project_handle, intel.classification,
                intel.crypto_confidence, intel.score, intel.confidence, intel.kol_count,
                json.dumps([e.model_dump() for e in intel.evidence]),
                json.dumps([c.model_dump() for c in intel.contributors]),
                json.dumps(intel.cluster.model_dump()) if intel.cluster else None,
                json.dumps(intel.correlation),
                intel.fingerprint,
                intel.updated_at,
            ),
        )
        conn.commit()


def get_project_intelligence(platform: str, account_key: str) -> ProjectIntelligence | None:
    """The current KOL Intelligence object for a project account, or None."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        r = conn.execute(
            "SELECT * FROM kol_intel_scores WHERE platform = ? AND account_key = ?",
            (p, account_key),
        ).fetchone()
    if r is None:
        return None
    try:
        evidence = [Evidence(**e) for e in json.loads(r["evidence"] or "[]")]
        contributors = [KolContributor(**c) for c in json.loads(r["contributors"] or "[]")]
        cluster = ClusterInfo(**json.loads(r["cluster"])) if r["cluster"] else None
        correlation = json.loads(r["correlation"] or "{}")
    except (ValueError, TypeError):
        evidence, contributors, cluster, correlation = [], [], None, {}
    return ProjectIntelligence(
        platform=r["platform"],
        account_key=r["account_key"],
        project_handle=r["project_handle"],
        classification=r["classification"],
        crypto_confidence=r["crypto_confidence"],
        score=r["score"],
        confidence=r["confidence"],
        kol_count=r["kol_count"],
        evidence=evidence,
        contributors=contributors,
        cluster=cluster,
        correlation=correlation,
        timeline=list_score_history(r["platform"], r["account_key"], limit=20),
        fingerprint=r["fingerprint"],
        updated_at=r["updated_at"],
    )


def list_project_intelligence(
    platform: str, *, min_score: int = 0, limit: int = 200
) -> list[ProjectIntelligence]:
    """Ranked project intelligence (highest score first) for dashboards/analytics."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT account_key FROM kol_intel_scores "
            "WHERE platform = ? AND score >= ? ORDER BY score DESC, updated_at DESC LIMIT ?",
            (p, int(min_score), int(limit)),
        ).fetchall()
    out: list[ProjectIntelligence] = []
    for r in rows:
        intel = get_project_intelligence(p, r["account_key"])
        if intel is not None:
            out.append(intel)
    return out


def append_score_history(
    platform: str, account_key: str, score: int, confidence: str, kol_count: int,
    when: str | None = None,
) -> None:
    """Record one score observation and prune to the configured retention."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO kol_intel_score_history "
            "(platform, account_key, score, confidence, kol_count, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (p, account_key, int(score), confidence, int(kol_count), when or _now()),
        )
        conn.commit()
    _prune_history("kol_intel_score_history", p, account_key)


def list_score_history(platform: str, account_key: str, *, limit: int = 200) -> list[dict]:
    """Score history for a project, OLDEST first (a timeline ready for AI narration)."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT score, confidence, kol_count, recorded_at "
            "FROM kol_intel_score_history WHERE platform = ? AND account_key = ? "
            "ORDER BY id DESC LIMIT ?",
            (p, account_key, int(limit)),
        ).fetchall()
    # Fetched newest-first for the LIMIT, returned oldest-first for a natural timeline.
    return [
        {"score": r["score"], "confidence": r["confidence"],
         "kol_count": r["kol_count"], "recorded_at": r["recorded_at"]}
        for r in reversed(rows)
    ]


def append_cluster_history(platform: str, cluster: ClusterInfo, when: str | None = None) -> None:
    """Record one cluster observation and prune to the configured retention."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO kol_cluster_history "
            "(platform, account_key, cluster_types, kol_count, cluster, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (p, cluster.account_key, json.dumps(cluster.cluster_types),
             cluster.kol_count, json.dumps(cluster.model_dump()), when or _now()),
        )
        conn.commit()
    _prune_history("kol_cluster_history", p, cluster.account_key)


def list_cluster_history(platform: str, account_key: str, *, limit: int = 200) -> list[ClusterInfo]:
    """Cluster history for a project, oldest first."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT cluster FROM kol_cluster_history "
            "WHERE platform = ? AND account_key = ? ORDER BY id DESC LIMIT ?",
            (p, account_key, int(limit)),
        ).fetchall()
    out: list[ClusterInfo] = []
    for r in reversed(rows):
        try:
            out.append(ClusterInfo(**json.loads(r["cluster"] or "{}")))
        except (ValueError, TypeError):
            continue
    return out


def _prune_history(table: str, platform: str, account_key: str) -> None:
    """Keep only the newest `kol_intel_history_retain` rows for one project in a
    history table. <= 0 disables pruning. Only called with internal table names."""
    retain = int(settings.kol_intel_history_retain)
    if retain <= 0:
        return
    with _LOCK:
        conn = _connect()
        conn.execute(
            f"DELETE FROM {table} WHERE platform = ? AND account_key = ? AND id NOT IN "
            f"(SELECT id FROM {table} WHERE platform = ? AND account_key = ? "
            f"ORDER BY id DESC LIMIT ?)",
            (platform, account_key, platform, account_key, retain),
        )
        conn.commit()


def save_intel_events(events: list[KolIntelEvent]) -> None:
    """Append intelligence events. Append-only timeline (never updates/deletes)."""
    if not events:
        return
    with _LOCK:
        conn = _connect()
        conn.executemany(
            "INSERT INTO kol_intel_events "
            "(event_type, platform, account_key, project_handle, detected_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.event_type, e.platform.strip().lower(), e.account_key,
                 e.project_handle, e.detected_at, json.dumps(e.payload))
                for e in events
            ],
        )
        conn.commit()


def list_intel_events(
    platform: str, account_key: str | None = None, *,
    event_type: str | None = None, limit: int = 200,
) -> list[KolIntelEvent]:
    """Recent intelligence events, newest first. Optional account/type filters."""
    p = platform.strip().lower()
    with _LOCK:
        conn = _connect()
        clauses = ["platform = ?"]
        params: list = [p]
        if account_key is not None:
            clauses.append("account_key = ?")
            params.append(account_key)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT * FROM kol_intel_events WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    out: list[KolIntelEvent] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        out.append(KolIntelEvent(
            event_type=r["event_type"], platform=r["platform"],
            account_key=r["account_key"], project_handle=r["project_handle"],
            detected_at=r["detected_at"], payload=payload,
        ))
    return out


# --- Deliverable H: notification delivery log --------------------------------


def was_delivered(event_key: str, destination: str) -> bool:
    """Whether this (event, destination) pair was already delivered successfully.

    The notification layer's dedupe check: a replayed event never delivers twice to
    the same destination. Only a `sent` row counts — a prior `failed` attempt may be
    retried."""
    with _LOCK:
        conn = _connect()
        row = conn.execute(
            "SELECT 1 FROM notification_deliveries "
            "WHERE event_key = ? AND destination = ? AND status = 'sent' LIMIT 1",
            (event_key, destination),
        ).fetchone()
    return row is not None


def record_delivery(
    *, event_key: str, event_type: str, platform: str, account_key: str,
    destination: str, status: str, error: str | None = None, when: str | None = None,
) -> None:
    """Record one delivery attempt (sent/failed) with its timestamp + any error.

    Append-only audit. UNIQUE(event_key, destination) means a retried attempt after a
    failure REPLACEs the failed row (so a later success supersedes it) rather than
    piling up duplicates."""
    with _LOCK:
        conn = _connect()
        conn.execute(
            "INSERT INTO notification_deliveries "
            "(event_key, event_type, platform, account_key, destination, status, error, attempted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(event_key, destination) DO UPDATE SET "
            "status=excluded.status, error=excluded.error, attempted_at=excluded.attempted_at, "
            "event_type=excluded.event_type, platform=excluded.platform, account_key=excluded.account_key",
            (event_key, event_type, platform.strip().lower(), account_key,
             destination, status, error, when or _now()),
        )
        conn.commit()


def list_deliveries(
    platform: str | None = None, account_key: str | None = None, *,
    destination: str | None = None, status: str | None = None, limit: int = 200,
) -> list[dict]:
    """Recent delivery attempts, newest first. Optional filters. Returns plain dicts
    (the delivery log is an audit surface, not a domain model)."""
    with _LOCK:
        conn = _connect()
        clauses: list[str] = []
        params: list = []
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform.strip().lower())
        if account_key is not None:
            clauses.append("account_key = ?")
            params.append(account_key)
        if destination is not None:
            clauses.append("destination = ?")
            params.append(destination)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT * FROM notification_deliveries {where}ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_for_tests(db_path: str | None = None) -> None:
    """Close and rebind the connection (used by tests with a temp DB)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        if db_path:
            settings.kol_db_path = db_path
