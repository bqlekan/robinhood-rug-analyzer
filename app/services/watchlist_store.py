from __future__ import annotations

"""Persistent store for flagged wallets (smart / insider) and their recent buys.

Uses stdlib sqlite3 so there is no extra dependency. The store is deliberately
small and defensive: it is a cache of heuristic flags, not a source of truth, so
every read tolerates an empty/missing DB and every write is an upsert.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.models.token import WalletActivity, WatchlistEntry

logger = logging.getLogger(__name__)

# One module-level connection guarded by a lock. SQLite handles our low write
# volume fine, and this keeps the background loop and request handlers consistent.
_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    db_path = Path(settings.watchlist_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallets (
            address TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            proxy_score INTEGER,
            label TEXT,
            evidence TEXT,
            first_seen TEXT,
            last_refreshed TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_activity (
            wallet TEXT NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT,
            direction TEXT NOT NULL DEFAULT 'buy',
            amount TEXT,
            timestamp TEXT,
            UNIQUE(wallet, token_address, timestamp) ON CONFLICT REPLACE
        )
        """
    )
    conn.commit()
    _CONN = conn
    return conn


def upsert_wallet(
    address: str,
    kind: str,
    *,
    proxy_score: int | None = None,
    label: str | None = None,
    evidence: list[str] | None = None,
) -> None:
    """Insert or update a flagged wallet. `kind` is 'smart' or 'insider'."""
    if not address:
        return
    address = address.lower()
    now = _now()
    with _LOCK:
        conn = _connect()
        existing = conn.execute("SELECT first_seen FROM wallets WHERE address = ?", (address,)).fetchone()
        first_seen = existing["first_seen"] if existing else now
        conn.execute(
            """
            INSERT INTO wallets (address, kind, proxy_score, label, evidence, first_seen, last_refreshed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                kind=excluded.kind,
                proxy_score=COALESCE(excluded.proxy_score, wallets.proxy_score),
                label=COALESCE(excluded.label, wallets.label),
                evidence=COALESCE(excluded.evidence, wallets.evidence),
                last_refreshed=excluded.last_refreshed
            """,
            (address, kind, proxy_score, label, json.dumps(evidence or []), first_seen, now),
        )
        conn.commit()


def record_activity(wallet: str, activities: list[WalletActivity]) -> None:
    if not wallet or not activities:
        return
    wallet = wallet.lower()
    with _LOCK:
        conn = _connect()
        conn.executemany(
            """
            INSERT OR REPLACE INTO wallet_activity (wallet, token_address, symbol, direction, amount, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (wallet, a.token_address.lower(), a.symbol, a.direction, a.amount, a.timestamp)
                for a in activities
                if a.token_address
            ],
        )
        conn.execute("UPDATE wallets SET last_refreshed = ? WHERE address = ?", (_now(), wallet))
        conn.commit()


def _recent_buys(conn: sqlite3.Connection, wallet: str, limit: int = 10) -> list[WalletActivity]:
    rows = conn.execute(
        """
        SELECT token_address, symbol, direction, amount, timestamp
        FROM wallet_activity WHERE wallet = ?
        ORDER BY timestamp DESC LIMIT ?
        """,
        (wallet, limit),
    ).fetchall()
    return [
        WalletActivity(
            token_address=r["token_address"],
            symbol=r["symbol"],
            direction=r["direction"],
            amount=r["amount"],
            timestamp=r["timestamp"],
        )
        for r in rows
    ]


def get_watchlist(kind: str | None = None, limit: int = 100) -> list[WatchlistEntry]:
    with _LOCK:
        conn = _connect()
        if kind:
            rows = conn.execute(
                "SELECT * FROM wallets WHERE kind = ? ORDER BY proxy_score DESC NULLS LAST, last_refreshed DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM wallets ORDER BY proxy_score DESC NULLS LAST, last_refreshed DESC LIMIT ?",
                (limit,),
            ).fetchall()
        entries = []
        for r in rows:
            entries.append(
                WatchlistEntry(
                    address=r["address"],
                    kind=r["kind"],
                    proxy_score=r["proxy_score"],
                    label=r["label"],
                    first_seen=r["first_seen"],
                    last_refreshed=r["last_refreshed"],
                    recent_buys=_recent_buys(conn, r["address"]),
                )
            )
        return entries


def get_wallet(address: str) -> WatchlistEntry | None:
    if not address:
        return None
    address = address.lower()
    with _LOCK:
        conn = _connect()
        r = conn.execute("SELECT * FROM wallets WHERE address = ?", (address,)).fetchone()
        if not r:
            return None
        return WatchlistEntry(
            address=r["address"],
            kind=r["kind"],
            proxy_score=r["proxy_score"],
            label=r["label"],
            first_seen=r["first_seen"],
            last_refreshed=r["last_refreshed"],
            recent_buys=_recent_buys(conn, r["address"], limit=25),
        )


def known_addresses() -> dict[str, dict]:
    """Return {address: {kind, proxy_score}} for fast watchlist-hit lookups."""
    with _LOCK:
        conn = _connect()
        rows = conn.execute("SELECT address, kind, proxy_score FROM wallets").fetchall()
        return {r["address"]: {"kind": r["kind"], "proxy_score": r["proxy_score"]} for r in rows}


def refresh_addresses(limit: int) -> list[str]:
    """Addresses due for a background refresh, oldest-refreshed first."""
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT address FROM wallets ORDER BY last_refreshed ASC LIMIT ?", (limit,)
        ).fetchall()
        return [r["address"] for r in rows]


def reset_for_tests(db_path: str | None = None) -> None:
    """Close and rebind the connection (used by tests with a temp DB)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        if db_path:
            settings.watchlist_db_path = db_path
