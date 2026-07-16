"""Unit tests for lore parsing helpers (no network)."""

from app.services.lore_client import _decode_ddg_url, _heuristic_sentiment, _strip_tags


def test_decode_ddg_wrapped_url():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftoken&rut=abc"
    assert _decode_ddg_url(href) == "https://example.com/token"


def test_decode_plain_url():
    assert _decode_ddg_url("https://example.com/x") == "https://example.com/x"


def test_strip_tags():
    assert _strip_tags("<b>Cash</b> &amp; Cat") == "Cash & Cat"


def test_sentiment_negative():
    sentiment, themes = _heuristic_sentiment("This token is a rug scam, total dump, avoid")
    assert sentiment == "negative"
    assert isinstance(themes, list)


def test_sentiment_positive():
    sentiment, _ = _heuristic_sentiment("bullish community gem, safe and strong, locked liquidity")
    assert sentiment == "positive"


def test_sentiment_unknown_when_neutral():
    sentiment, _ = _heuristic_sentiment("a listing page describing the asset details")
    assert sentiment == "unknown"
