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
    # M18: persisted deployer reputation. `launched_tokens` is the serialized list of
    # classified LaunchedTokens (the expensive scan output); reputation/counts are cached
    # alongside so a known serial rugger is retrievable without a live re-scan. `refreshed_at`
    # drives TTL expiry so stale outcomes are re-scanned and a status can worsen over time.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deployers (
            address TEXT PRIMARY KEY,
            reputation TEXT,
            tokens_launched INTEGER,
            tokens_rugged INTEGER,
            tokens_alive INTEGER,
            launched_tokens TEXT,
            first_seen TEXT,
            last_refreshed TEXT
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


def get_watchlist(kind: str | None = None, limit: int = 100, sort: str = "score") -> list[WatchlistEntry]:
    """Flagged wallets, optionally filtered by kind and sorted (M21).

    `sort` is a whitelisted key (never raw SQL): "score" (proxy_score desc, then
    recency) or "recency" (most recently refreshed first). Each entry is enriched
    with `prior_tokens` — its distinct cross-token activity count (M17 memory).
    """
    order = {
        "score": "proxy_score DESC NULLS LAST, last_refreshed DESC",
        "recency": "last_refreshed DESC, proxy_score DESC NULLS LAST",
    }.get(sort, "proxy_score DESC NULLS LAST, last_refreshed DESC")
    with _LOCK:
        conn = _connect()
        if kind:
            rows = conn.execute(
                f"SELECT * FROM wallets WHERE kind = ? ORDER BY {order} LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM wallets ORDER BY {order} LIMIT ?",
                (limit,),
            ).fetchall()
        prior = _prior_token_counts_locked(conn, [r["address"] for r in rows])
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
                    prior_tokens=prior.get(r["address"], 0),
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
        prior = _prior_token_counts_locked(conn, [address])
        return WatchlistEntry(
            address=r["address"],
            kind=r["kind"],
            proxy_score=r["proxy_score"],
            label=r["label"],
            first_seen=r["first_seen"],
            last_refreshed=r["last_refreshed"],
            prior_tokens=prior.get(address, 0),
            recent_buys=_recent_buys(conn, r["address"], limit=25),
        )


def known_addresses() -> dict[str, dict]:
    """Return {address: {kind, proxy_score}} for fast watchlist-hit lookups."""
    with _LOCK:
        conn = _connect()
        rows = conn.execute("SELECT address, kind, proxy_score FROM wallets").fetchall()
        return {r["address"]: {"kind": r["kind"], "proxy_score": r["proxy_score"]} for r in rows}


def prior_token_counts(addresses: list[str], exclude_token: str | None = None) -> dict[str, int]:
    """For each address, how many DISTINCT tokens it has been recorded active on (M17).

    This is the persisted cross-token memory: a wallet flagged on prior tokens carries a
    reputation into the next one. Counts distinct `token_address` from `wallet_activity`,
    excluding the token under analysis so "prior" means "other tokens", not this one.
    Defensive: an empty/missing DB or unknown address yields 0, never raises.
    """
    if not addresses:
        return {}
    addrs = [a.lower() for a in addresses if a]
    if not addrs:
        return {}
    exclude = (exclude_token or "").lower()
    try:
        with _LOCK:
            conn = _connect()
            return _prior_token_counts_locked(conn, addrs, exclude_token=exclude)
    except Exception as exc:  # store is a cache, never break analysis
        logger.warning("prior_token_counts failed: %s", exc)
        return {}


def _prior_token_counts_locked(
    conn: sqlite3.Connection, addresses: list[str], exclude_token: str | None = None
) -> dict[str, int]:
    """Distinct cross-token activity count per address. Caller must hold `_LOCK`.

    Shared by `prior_token_counts` (M17) and the M21 watchlist/detail reads so the
    count is computed with one grouped query, never per-row. Addresses are assumed
    already lowercased. Returns {} for an empty list.
    """
    addrs = [a for a in (addresses or []) if a]
    if not addrs:
        return {}
    exclude = (exclude_token or "").lower()
    placeholders = ",".join("?" for _ in addrs)
    rows = conn.execute(
        f"""
        SELECT wallet, COUNT(DISTINCT token_address) AS n
        FROM wallet_activity
        WHERE wallet IN ({placeholders}) AND token_address != ?
        GROUP BY wallet
        """,
        (*addrs, exclude),
    ).fetchall()
    return {r["wallet"]: r["n"] for r in rows}


# --- Deployer reputation (M18) ---


def get_deployer(address: str, max_age_seconds: float | None = None) -> dict | None:
    """Return a cached deployer record, or None if absent/stale (M18).

    Record: {reputation, tokens_launched, tokens_rugged, tokens_alive, launched_tokens,
    last_refreshed}. `launched_tokens` is the deserialized list of LaunchedToken dicts, so
    a cache hit rebuilds the full DevProfile launch history without a live creator scan.
    A record older than `max_age_seconds` is treated as a miss so outcomes can worsen over
    time. Defensive: empty/missing DB or any error -> None (caller falls back to live scan).
    """
    if not address:
        return None
    address = address.lower()
    try:
        with _LOCK:
            conn = _connect()
            r = conn.execute("SELECT * FROM deployers WHERE address = ?", (address,)).fetchone()
        if not r:
            return None
        if max_age_seconds is not None and r["last_refreshed"]:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(r["last_refreshed"])).total_seconds()
            if age > max_age_seconds:
                return None  # stale -> force a refresh
        return {
            "reputation": r["reputation"],
            "tokens_launched": r["tokens_launched"],
            "tokens_rugged": r["tokens_rugged"],
            "tokens_alive": r["tokens_alive"],
            "launched_tokens": json.loads(r["launched_tokens"] or "[]"),
            "last_refreshed": r["last_refreshed"],
        }
    except Exception as exc:  # cache, never break analysis
        logger.warning("get_deployer failed for %s: %s", address, exc)
        return None


def upsert_deployer(
    address: str,
    *,
    reputation: str,
    tokens_launched: int | None,
    tokens_rugged: int | None,
    tokens_alive: int | None,
    launched_tokens: list[dict],
) -> None:
    """Persist a deployer's launch history + classification (M18). Best-effort."""
    if not address:
        return
    address = address.lower()
    now = _now()
    try:
        with _LOCK:
            conn = _connect()
            existing = conn.execute("SELECT first_seen FROM deployers WHERE address = ?", (address,)).fetchone()
            first_seen = existing["first_seen"] if existing else now
            conn.execute(
                """
                INSERT INTO deployers (address, reputation, tokens_launched, tokens_rugged,
                    tokens_alive, launched_tokens, first_seen, last_refreshed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    reputation=excluded.reputation,
                    tokens_launched=excluded.tokens_launched,
                    tokens_rugged=excluded.tokens_rugged,
                    tokens_alive=excluded.tokens_alive,
                    launched_tokens=excluded.launched_tokens,
                    last_refreshed=excluded.last_refreshed
                """,
                (address, reputation, tokens_launched, tokens_rugged, tokens_alive,
                 json.dumps(launched_tokens or []), first_seen, now),
            )
            conn.commit()
    except Exception as exc:  # cache write must never break analysis
        logger.warning("upsert_deployer failed for %s: %s", address, exc)


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
