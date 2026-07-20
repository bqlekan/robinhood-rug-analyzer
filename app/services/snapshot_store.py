from __future__ import annotations

"""Historical analysis snapshots for trend detection (M19).

Persists one lightweight row per analyze — the key metrics + risk score + a
timestamp — so a later analyze can diff against the prior snapshot and surface a
*slow rug* (liquidity bleeding out, holder concentration rising) that no single
point-in-time score can see.

Same discipline as `watchlist_store`/`token_monitor_store`: stdlib sqlite3 (no new
dependency), one lock-guarded module connection, every read tolerates an empty/missing
DB, and history is pruned to a configurable per-token retention so the DB stays bounded.
This store holds NO analysis logic — it only reads/writes rows; the trend math is a pure
function in `analyzers.analyze_trend`.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    db_path = Path(settings.snapshot_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_address TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            risk_score INTEGER,
            liquidity_usd REAL,
            top10_percentage REAL,
            holder_count INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_addr ON snapshots (contract_address, id)"
    )
    conn.commit()
    _CONN = conn
    return conn


def latest_snapshot(address: str) -> dict | None:
    """The most recent stored snapshot for a token, or None if it has never been analyzed.

    Defensive: an empty/missing DB or any error yields None (caller treats it as
    "no prior" and computes no trend), never raises.
    """
    if not address:
        return None
    address = address.lower()
    try:
        with _LOCK:
            conn = _connect()
            r = conn.execute(
                "SELECT * FROM snapshots WHERE contract_address = ? ORDER BY id DESC LIMIT 1",
                (address,),
            ).fetchone()
        if not r:
            return None
        return {
            "captured_at": r["captured_at"],
            "risk_score": r["risk_score"],
            "liquidity_usd": r["liquidity_usd"],
            "top10_percentage": r["top10_percentage"],
            "holder_count": r["holder_count"],
        }
    except Exception as exc:  # store is a cache, never break analysis
        logger.warning("latest_snapshot failed for %s: %s", address, exc)
        return None


def record_snapshot(
    address: str,
    *,
    risk_score: int | None = None,
    liquidity_usd: float | None = None,
    top10_percentage: float | None = None,
    holder_count: int | None = None,
) -> None:
    """Append one snapshot row for a token and prune to the retention cap. Best-effort."""
    if not address:
        return
    address = address.lower()
    try:
        with _LOCK:
            conn = _connect()
            conn.execute(
                """
                INSERT INTO snapshots
                    (contract_address, captured_at, risk_score, liquidity_usd, top10_percentage, holder_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (address, _now(), risk_score, liquidity_usd, top10_percentage, holder_count),
            )
            _prune(conn, address)
            conn.commit()
    except Exception as exc:  # snapshot write must never break analysis
        logger.warning("record_snapshot failed for %s: %s", address, exc)


def _prune(conn: sqlite3.Connection, address: str) -> None:
    """Keep only the most recent `snapshot_retain` rows per token (bounds DB growth)."""
    retain = int(settings.snapshot_history_retain)
    if retain <= 0:
        return
    conn.execute(
        """
        DELETE FROM snapshots
        WHERE contract_address = ? AND id NOT IN (
            SELECT id FROM snapshots
            WHERE contract_address = ?
            ORDER BY id DESC LIMIT ?
        )
        """,
        (address, address, retain),
    )


def list_snapshots(address: str, *, limit: int = 200) -> list[dict]:
    """Recent snapshots for a token, newest first (history endpoint / back-testing)."""
    if not address:
        return []
    address = address.lower()
    try:
        with _LOCK:
            conn = _connect()
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE contract_address = ? ORDER BY id DESC LIMIT ?",
                (address, int(limit)),
            ).fetchall()
        return [
            {
                "captured_at": r["captured_at"],
                "risk_score": r["risk_score"],
                "liquidity_usd": r["liquidity_usd"],
                "top10_percentage": r["top10_percentage"],
                "holder_count": r["holder_count"],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("list_snapshots failed for %s: %s", address, exc)
        return []


def reset_for_tests(db_path: str | None = None) -> None:
    """Close and rebind the connection (used by tests with a temp DB)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        if db_path:
            settings.snapshot_db_path = db_path
