"""Unit tests for M15 same-block / coordinated-buy timing detection (pure, no network)."""

from app.models.token import BuyTimingAnalysis
from app.services import analyzers, wallet_intel
from app.services.scoring import score_token
from tests.test_scoring import _clean_kwargs

ZERO = analyzers.ZERO_ADDRESS


def _t(to, block=None, ts=None, frm=ZERO):
    return {"from": frm, "to": to, "value": 10, "ts": ts, "method": "transfer", "block": block}


# --- normalize_transfers captures block (M15 groundwork) ---


def test_normalize_transfers_captures_block():
    raw = [{"from": {"hash": "0xAA"}, "to": {"hash": "0xBB"},
            "total": {"value": "5", "decimals": "0"}, "timestamp": "t", "block_number": 42}]
    recs = wallet_intel.normalize_transfers(raw)
    assert recs[0]["block"] == 42


# --- same-block cohort ---


def test_same_block_cohort_is_coordinated():
    transfers = [
        _t("0xa", block=100),
        _t("0xb", block=100),
        _t("0xc", block=100),
        _t("0xd", block=105),
    ]
    r = analyzers.analyze_buy_timing(transfers, min_cohort=3)
    assert r.coordinated is True
    assert r.same_block_wallets == 3
    assert r.same_block_number == 100


def test_within_window_cohort_when_no_blocks():
    # Block numbers absent; timestamps within 2s of launch form the cohort.
    transfers = [
        _t("0xa", ts="2026-01-01T00:00:00Z"),
        _t("0xb", ts="2026-01-01T00:00:01Z"),
        _t("0xc", ts="2026-01-01T00:00:02Z"),
    ]
    r = analyzers.analyze_buy_timing(transfers, min_cohort=3, window_seconds=2)
    assert r.coordinated is True
    assert r.first_window_wallets == 3


# --- single-buyer / spread-out tokens unaffected ---


def test_single_buyer_not_coordinated():
    r = analyzers.analyze_buy_timing([_t("0xa", block=1)], min_cohort=3)
    assert r.coordinated is False
    assert r.same_block_wallets == 0


def test_spread_out_buys_not_coordinated():
    transfers = [
        _t("0xa", block=1, ts="2026-01-01T00:00:00Z"),
        _t("0xb", block=50, ts="2026-01-01T00:05:00Z"),
        _t("0xc", block=900, ts="2026-01-01T01:00:00Z"),
    ]
    r = analyzers.analyze_buy_timing(transfers, min_cohort=3, window_seconds=2)
    assert r.coordinated is False


def test_repeated_buys_by_one_wallet_not_a_cohort():
    # One wallet buying repeatedly in a block is NOT a cohort (distinct wallets count).
    transfers = [_t("0xa", block=7), _t("0xa", block=7), _t("0xa", block=7)]
    r = analyzers.analyze_buy_timing(transfers, min_cohort=3)
    assert r.coordinated is False


def test_creator_and_contracts_excluded():
    # mint (zero), creator, and LP contract must not count toward a cohort.
    transfers = [
        _t("0xcreator", block=1),
        _t("0xlp", block=1),
        _t("0xa", block=1),
    ]
    r = analyzers.analyze_buy_timing(
        transfers, creator="0xCreator", known_contracts={"0xlp"}, min_cohort=3
    )
    # Only 0xa remains as a real buyer -> no cohort.
    assert r.coordinated is False


# --- scoring: only a positive cohort scores ---


def test_coordinated_timing_scores_signal():
    kwargs = _clean_kwargs()
    kwargs["buy_timing"] = BuyTimingAnalysis(same_block_wallets=4, same_block_number=100,
                                             coordinated=True, detail="cohort")
    result = score_token(**kwargs)
    assert any(s.name == "Coordinated buy timing" for s in result.signals)


def test_normal_timing_adds_no_signal():
    kwargs = _clean_kwargs()
    kwargs["buy_timing"] = BuyTimingAnalysis(coordinated=False)
    result = score_token(**kwargs)
    assert not any(s.name == "Coordinated buy timing" for s in result.signals)


def test_no_buy_timing_is_backward_compatible():
    base = score_token(**_clean_kwargs())
    assert not any(s.name == "Coordinated buy timing" for s in base.signals)
