"""Market data integrity: fields survive the pipeline from DexScreener to RankedToken."""

from app.models.token import RankedToken, VolumeSnapshot
from app.services.rug_analyzer import _build_market_data


def _pair(**overrides) -> dict:
    base = {
        "chainId": "robinhood",
        "dexId": "uniswap",
        "pairAddress": "0x" + "a" * 40,
        "baseToken": {"name": "Foo", "symbol": "FOO"},
        "quoteToken": {"symbol": "WETH"},
        "priceUsd": "0.001",
        "marketCap": 50000.0,
        "fdv": 60000.0,
        "liquidity": {"usd": 5000.0, "base": 1000.0, "quote": 2.5},
        "volume": {"h24": 1000.0, "h6": 200.0, "h1": 50.0, "m5": 5.0},
        "priceChange": {"h24": 1.5, "h6": 0.5, "h1": 0.1, "m5": 0.01},
        "txns": {"h24": {"buys": 42, "sells": 17}},
        "pairCreatedAt": 1700000000,
        "url": "https://dexscreener.com/robinhood/0xaaa",
        "info": {"websites": [], "socials": []},
    }
    base.update(overrides)
    return base


class TestVolumeSnapshotFields:
    def test_buys_sells_default_none(self):
        v = VolumeSnapshot(h24=100.0)
        assert v.buys is None
        assert v.sells is None

    def test_buys_sells_set(self):
        v = VolumeSnapshot(h24=100.0, buys=10, sells=5)
        assert v.buys == 10
        assert v.sells == 5


class TestBuildMarketData:
    def test_buys_sells_parsed(self):
        md = _build_market_data(_pair())
        assert md is not None
        assert md.volume is not None
        assert md.volume.buys == 42
        assert md.volume.sells == 17

    def test_buys_sells_missing_txns(self):
        md = _build_market_data(_pair(txns={}))
        assert md.volume.buys is None
        assert md.volume.sells is None

    def test_buys_sells_missing_h24(self):
        md = _build_market_data(_pair(txns={"h24": {}}))
        assert md.volume.buys is None
        assert md.volume.sells is None

    def test_market_cap_zero_not_overridden_by_fdv(self):
        md = _build_market_data(_pair(marketCap=0.0, fdv=60000.0))
        assert md is not None
        assert md.market_cap == 0.0

    def test_market_cap_none_falls_back_to_fdv(self):
        md = _build_market_data(_pair(marketCap=None, fdv=60000.0))
        assert md is not None
        assert md.market_cap == 60000.0

    def test_market_cap_present_used(self):
        md = _build_market_data(_pair(marketCap=50000.0, fdv=60000.0))
        assert md.market_cap == 50000.0

    def test_fdv_populated(self):
        md = _build_market_data(_pair())
        assert md.fdv == 60000.0

    def test_price_usd_populated(self):
        md = _build_market_data(_pair())
        assert md.price_usd == "0.001"

    def test_volume_h24_populated(self):
        md = _build_market_data(_pair())
        assert md.volume.h24 == 1000.0

    def test_price_change_h24_populated(self):
        md = _build_market_data(_pair())
        assert md.price_change.h24 == 1.5

    def test_none_pair_returns_none(self):
        assert _build_market_data(None) is None


class TestRankedTokenFields:
    def test_new_fields_exist_with_defaults(self):
        r = RankedToken(
            contract_address="0x" + "a" * 40,
            risk_score=10,
            risk_level="low",
        )
        assert r.fdv is None
        assert r.volume_h24 is None
        assert r.price_usd is None
        assert r.price_change_h24 is None

    def test_new_fields_set(self):
        r = RankedToken(
            contract_address="0x" + "a" * 40,
            risk_score=10,
            risk_level="low",
            fdv=60000.0,
            volume_h24=1000.0,
            price_usd="0.001",
            price_change_h24=1.5,
        )
        assert r.fdv == 60000.0
        assert r.volume_h24 == 1000.0
        assert r.price_usd == "0.001"
        assert r.price_change_h24 == 1.5
