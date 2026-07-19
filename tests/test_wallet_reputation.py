"""Unit tests for M17 persistent wallet reputation (cross-token memory).

Two layers:
- watchlist_store.prior_token_counts — the persisted cross-token count (defensive, bounded).
- scoring.score_token — the reputation risk signal (fires only on non-trivial history).
"""

import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.token import WalletActivity, WatchlistHit
from app.services import scoring, watchlist_store


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "watchlist.db"
    watchlist_store.reset_for_tests(str(tmp))
    yield
    watchlist_store.reset_for_tests()


def _seed(wallet: str, tokens: list[str], kind: str = "insider") -> None:
    watchlist_store.upsert_wallet(wallet, kind, proxy_score=80)
    watchlist_store.record_activity(
        wallet,
        [WalletActivity(token_address=t, symbol="X", timestamp=f"2024-01-0{i+1}T00:00:00Z")
         for i, t in enumerate(tokens)],
    )


# --- store: prior_token_counts ---


def test_wallet_recognized_across_tokens():
    # Flagged on tokens A and B; when it appears on C, prior history is 2.
    _seed("0xWALLET", ["0xA", "0xB"])
    counts = watchlist_store.prior_token_counts(["0xwallet"], exclude_token="0xC")
    assert counts == {"0xwallet": 2}


def test_current_token_excluded_from_prior_count():
    # Seen on A and B; analyzing B -> only A counts as "prior".
    _seed("0xWALLET", ["0xA", "0xB"])
    counts = watchlist_store.prior_token_counts(["0xwallet"], exclude_token="0xB")
    assert counts == {"0xwallet": 1}


def test_distinct_tokens_only():
    # Repeated activity on the same token counts once.
    _seed("0xWALLET", ["0xA", "0xA", "0xA"])
    counts = watchlist_store.prior_token_counts(["0xwallet"], exclude_token="0xZ")
    assert counts == {"0xwallet": 1}


def test_empty_db_returns_no_reputation_and_no_raise():
    # Fresh DB, unknown wallet -> empty mapping, never raises.
    assert watchlist_store.prior_token_counts(["0xnobody"], exclude_token="0xC") == {}


def test_empty_address_list_returns_empty():
    _seed("0xWALLET", ["0xA"])
    assert watchlist_store.prior_token_counts([]) == {}


# --- scoring: reputation signal ---


def _hit(kind, prior, addr="0xw"):
    return WatchlistHit(address=addr, kind=kind, proxy_score=80, prior_tokens=prior)


def _reputation_signals(analysis):
    return [s for s in analysis.signals
            if s.name in ("Repeat insider wallets present", "Recurring smart wallets present")]


def _score(hits):
    return scoring.score_token(
        age=None, market=None, holders=None, clusters=None, dev=None,
        liquidity_lock=None, launchpad=None, lore=None, data_sources=["test"],
        watchlist_hits=hits,
    )


def test_no_signal_below_min_prior_tokens(monkeypatch):
    monkeypatch.setattr(settings, "wallet_reputation_min_prior_tokens", 2)
    # A first sighting (prior_tokens == 1) must not score.
    analysis = _score([_hit("insider", 1)])
    assert _reputation_signals(analysis) == []


def test_no_signal_when_no_hits():
    analysis = _score([])
    assert _reputation_signals(analysis) == []


def test_insider_history_scores(monkeypatch):
    monkeypatch.setattr(settings, "wallet_reputation_min_prior_tokens", 2)
    analysis = _score([_hit("insider", 3)])
    sigs = [s for s in analysis.signals if s.name == "Repeat insider wallets present"]
    assert len(sigs) == 1
    assert sigs[0].points > 0
    assert sigs[0].category == "clusters"


def test_multiple_insiders_escalate_severity(monkeypatch):
    monkeypatch.setattr(settings, "wallet_reputation_min_prior_tokens", 2)
    one = _score([_hit("insider", 2, "0x1")])
    two = _score([_hit("insider", 2, "0x1"), _hit("insider", 2, "0x2")])
    p_one = next(s.points for s in one.signals if s.name == "Repeat insider wallets present")
    p_two = next(s.points for s in two.signals if s.name == "Repeat insider wallets present")
    assert p_two > p_one


def test_smart_wallet_history_is_lighter_than_insider(monkeypatch):
    monkeypatch.setattr(settings, "wallet_reputation_min_prior_tokens", 2)
    ins = _score([_hit("insider", 3)])
    smt = _score([_hit("smart", 3)])
    p_ins = next(s.points for s in ins.signals if s.name == "Repeat insider wallets present")
    p_smt = next(s.points for s in smt.signals if s.name == "Recurring smart wallets present")
    assert p_smt < p_ins
