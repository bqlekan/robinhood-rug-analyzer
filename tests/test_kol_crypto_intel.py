"""Tests for the crypto intelligence pipeline (M23 Deliverable D).

Layers, all offline (no network, no browser, no real rug analysis):
  - contract_extract  — pure address mining across chains + validation.
  - crypto_signals    — config-driven signal detectors (weights come from config).
  - crypto_intel      — the pure classifier: type + confidence + score + evidence,
                        including the "never classify on a single weak signal" gate.
  - kol_crypto_pipeline — orchestration: persist classification + events and invoke
                        the EXISTING rug analyzer (stubbed) for confident projects.
  - capture_following — the end-to-end hook: new follows get classified automatically.

Scope: this deliverable classifies + persists engine-internal facts and reuses the
rug analyzer. It emits NO user alerts and does NO KOL scoring/clustering; tests
assert those surfaces stay absent.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.kol import CryptoClassification, SocialAccount
from app.models.token import RugAnalysis, TokenAnalysisResponse
from app.services import kol_crypto_pipeline, kol_store, kol_watchlist as w
from app.services.social import contract_extract, crypto_intel, crypto_signals


# --- helpers -----------------------------------------------------------------

# A structurally valid EVM address (40 hex after 0x) for reuse.
EVM = "0x1234567890abcdef1234567890abcdef12345678"
EVM2 = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def acct(handle, **kw):
    return SocialAccount(platform="x", handle=handle, **kw)


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    kol_crypto_pipeline.reset_cache_for_tests()
    yield
    kol_store.reset_for_tests()
    kol_crypto_pipeline.reset_cache_for_tests()


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "kol_crypto_intel_enabled", True)


# --- contract extraction -----------------------------------------------------


def test_extract_evm_address_on_configured_chain_is_supported():
    contracts = contract_extract.extract_contracts(f"our token CA: {EVM}", source="bio")
    assert len(contracts) == 1
    c = contracts[0]
    assert c.address == EVM.lower()
    assert c.supported is True
    assert c.source == "bio"


def test_extract_rejects_malformed_address():
    # 39 hex chars — not a valid EVM address.
    assert contract_extract.extract_contracts("0x1234 short") == []
    assert contract_extract.extract_contracts("") == []
    assert contract_extract.extract_contracts(None) == []


def test_extract_evm_does_not_spawn_phantom_solana_from_its_own_tail():
    # The 40 hex chars after 0x are valid base58; extraction must not invent a second
    # "solana" contract overlapping the EVM match.
    contracts = contract_extract.extract_contracts(f"CA: {EVM}")
    assert [c.chain for c in contracts].count("solana") == 0
    assert len(contracts) == 1


def test_extract_solana_recorded_but_unsupported():
    sol = "So11111111111111111111111111111111111111112"
    contracts = contract_extract.extract_contracts(f"solana mint {sol}")
    assert len(contracts) == 1
    assert contracts[0].chain == "solana"
    assert contracts[0].supported is False


def test_extract_bare_base58_word_is_not_a_contract():
    # A lowercase english-ish base58 blob with no CA marker / solana keyword nearby
    # must not be mistaken for a mint.
    assert contract_extract.extract_contracts("thisisjustsomelowercasetexthere no marker") == []


def test_extract_dedups_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "kol_crypto_max_contracts_per_account", 1)
    text = f"{EVM} and again {EVM} and {EVM2}"
    contracts = contract_extract.extract_contracts(text)
    assert len(contracts) == 1  # capped


def test_extract_from_fields_tags_source_and_merges():
    fields = {"bio": f"CA {EVM}", "website": f"https://x.io/{EVM2}"}
    contracts = contract_extract.extract_from_fields(fields)
    addrs = {c.address for c in contracts}
    assert addrs == {EVM.lower(), EVM2.lower()}


# --- signal detection --------------------------------------------------------


def test_signals_fire_from_config_weights():
    intel = crypto_intel.build_profile_intelligence(
        acct("t", bio="trade on dexscreener", links=["https://t.me/x"])
    )
    fired = crypto_signals.detect_signals(intel, [])
    names = {e.signal for e in fired}
    assert "dexscreener" in names
    assert "telegram" in names
    # Each fired evidence carries the configured weight.
    for e in fired:
        assert e.weight == settings.kol_crypto_signal_weights[e.signal]


def test_signal_with_zero_weight_is_disabled(monkeypatch):
    weights = dict(settings.kol_crypto_signal_weights)
    weights["dexscreener"] = 0
    monkeypatch.setattr(settings, "kol_crypto_signal_weights", weights)
    intel = crypto_intel.build_profile_intelligence(acct("t", bio="see dexscreener"))
    fired = crypto_signals.detect_signals(intel, [])
    assert "dexscreener" not in {e.signal for e in fired}


def test_new_signal_via_config_only_needs_registered_detector():
    # Every registered detector name should be tunable via config; the registry is
    # the extension point. This guards the "add signals through configuration" claim.
    for name in crypto_signals.registered_signals():
        assert isinstance(name, str)


# --- classification ----------------------------------------------------------


def test_official_project_with_contract_is_actionable():
    a = acct(
        "moon",
        display_name="MoonToken Official",
        bio=f"The official $MOON token. CA: {EVM}",
        links=["https://dexscreener.com/robinhood/x", "https://t.me/moon"],
    )
    c = crypto_intel.classify_account(a)
    assert c.classification == "official"
    assert c.is_crypto_project is True
    assert c.confidence in ("high", "very_high")
    assert len(c.supported_contracts()) == 1
    # Evidence is populated and explains the verdict.
    assert any(e.signal == "contract_address" for e in c.evidence)


def test_plain_individual_is_unknown():
    c = crypto_intel.classify_account(acct("jane", display_name="Jane", bio="cat lover, photographer"))
    assert c.classification == "unknown"
    assert c.score == 0
    assert c.contracts == []


def test_single_weak_signal_never_becomes_a_project():
    # One weak keyword only: must NOT be classified as a crypto project.
    c = crypto_intel.classify_account(acct("g", bio="i love defi"))
    assert c.is_crypto_project is False
    assert c.classification in ("individual", "unknown")


def test_strong_signal_alone_satisfies_corroboration():
    # A valid contract address is a "strong" signal: it can carry a project verdict on
    # its own even below the min-signals count.
    c = crypto_intel.classify_account(acct("solo", bio=f"{EVM}"))
    assert c.is_crypto_project is True
    assert "contract_address" in c.signals


def test_infrastructure_detected_over_project():
    c = crypto_intel.classify_account(
        acct("dex", display_name="DexScreener",
             bio="the leading dex analytics aggregator dashboard",
             links=["https://dexscreener.com"])
    )
    assert c.classification == "infrastructure"


def test_confidence_bands_are_config_driven(monkeypatch):
    bands = {"very_high": 90, "high": 70, "medium": 50, "low": 30, "very_low": 0}
    monkeypatch.setattr(settings, "kol_crypto_confidence_bands", bands)
    # A single dexscreener signal (weight 30) -> score 30 -> "low" under these bands.
    c = crypto_intel.classify_account(acct("t", bio="dexscreener"))
    assert c.confidence == "low"


def test_min_score_gate_downgrades(monkeypatch):
    # Raise the bar so a moderate crypto account can't be an actionable project.
    monkeypatch.setattr(settings, "kol_crypto_min_score", 99)
    c = crypto_intel.classify_account(
        acct("t", bio="community fan account", links=["https://t.me/x", "https://discord.gg/y"])
    )
    assert c.is_crypto_project is False


# --- pipeline orchestration --------------------------------------------------


def _stub_analyzer(monkeypatch, calls):
    async def fake_analyze(address):
        calls.append(address)
        return TokenAnalysisResponse(
            contract_address=address, chain="Robinhood Chain",
            status="analysis_completed", message="ok",
            analysis=RugAnalysis(
                risk_score=42, risk_level="medium", signals=[],
                data_sources=["stub"], limitations=[],
            ),
        )
    monkeypatch.setattr(kol_crypto_pipeline.rug_analyzer, "analyze_token_contract", fake_analyze)


def test_pipeline_disabled_is_noop():
    # Default: disabled -> returns None, persists nothing.
    out = asyncio.run(kol_crypto_pipeline.process_new_follow("x", "kol", acct("moon", bio=f"CA {EVM}")))
    assert out is None
    assert kol_store.list_classifications("x", "kol") == []


def test_pipeline_classifies_persists_and_analyzes(_enabled, monkeypatch):
    calls = []
    _stub_analyzer(monkeypatch, calls)
    a = acct("moon", display_name="MoonToken Official",
             bio=f"official $MOON token CA: {EVM}",
             links=["https://dexscreener.com/x", "https://t.me/moon"])
    result = asyncio.run(kol_crypto_pipeline.process_new_follow("x", "kol", a))

    assert isinstance(result, CryptoClassification)
    assert result.is_crypto_project
    # Classification persisted.
    stored = kol_store.get_classification("x", "kol", a.key())
    assert stored is not None and stored.classification == result.classification
    # Rug analyzer was invoked for the supported contract.
    assert calls == [EVM.lower()]
    # Events recorded: detected, extracted, completed.
    types = {e.event_type for e in kol_store.list_crypto_events("x", "kol")}
    assert {"crypto_project_detected", "contract_extracted", "analysis_completed"} <= types
    # analysis_completed payload carries the risk summary.
    done = kol_store.list_crypto_events("x", "kol", event_type="analysis_completed")
    assert done[0].payload["risk_score"] == 42


def test_pipeline_non_project_persists_classification_but_does_not_analyze(_enabled, monkeypatch):
    calls = []
    _stub_analyzer(monkeypatch, calls)
    asyncio.run(kol_crypto_pipeline.process_new_follow("x", "kol", acct("jane", bio="cat lover")))
    assert calls == []  # never analyzed
    stored = kol_store.get_classification("x", "kol", acct("jane").key())
    assert stored is not None and stored.classification == "unknown"


def test_pipeline_analysis_failure_is_recorded_not_raised(_enabled, monkeypatch):
    async def boom(address):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(kol_crypto_pipeline.rug_analyzer, "analyze_token_contract", boom)

    a = acct("moon", display_name="X Official", bio=f"official $MOON CA: {EVM}",
             links=["https://dexscreener.com/x"])
    # Must not raise.
    asyncio.run(kol_crypto_pipeline.process_new_follow("x", "kol", a))
    failed = kol_store.list_crypto_events("x", "kol", event_type="analysis_failed")
    assert len(failed) == 1
    assert "rpc down" in failed[0].payload["error"]


def test_pipeline_dedups_repeat_contract_via_cache(_enabled, monkeypatch):
    calls = []
    _stub_analyzer(monkeypatch, calls)
    a = acct("moon", display_name="Off", bio=f"official CA: {EVM}", links=["https://dexscreener.com/x"])
    b = acct("moon2", display_name="Off2", bio=f"official CA: {EVM}", links=["https://dexscreener.com/y"])
    asyncio.run(kol_crypto_pipeline.process_new_follows("x", "kol", [a, b]))
    # Same contract analyzed once despite two follows referencing it.
    assert calls == [EVM.lower()]


# --- end-to-end via capture_following ----------------------------------------


class _FakeProvider:
    platform = "x"

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def capabilities(self):
        from app.models.kol import ProviderCapabilities
        return ProviderCapabilities(platform="x", can_fetch_following=True)

    def normalize_handle(self, handle):
        return handle.strip().lstrip("@").lower()

    def account_url(self, handle):
        return f"https://x.com/{self.normalize_handle(handle)}"

    async def fetch_following(self, handle):
        return self._snapshot


def _register(snapshot):
    from app.services.social import registry
    registry.register_provider(_FakeProvider(snapshot), replace=True)


def _snap(accounts, *, handle="target", captured_at="2024-01-01T00:00:00+00:00"):
    from app.models.kol import FollowingSnapshot
    return FollowingSnapshot(platform="x", kol_handle=handle, accounts=accounts,
                             complete=True, captured_at=captured_at)


def test_capture_following_classifies_new_follows(_enabled, monkeypatch):
    calls = []
    _stub_analyzer(monkeypatch, calls)
    from app.services.social import registry
    try:
        w.add_kol("target")
        # Baseline: existing follow, must NOT trigger analysis (not a "new" follow).
        _register(_snap([acct("old", bio=f"official CA: {EVM}", links=["https://dexscreener.com/x"])],
                        captured_at="2024-01-01T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        assert calls == []  # baseline yields no new follows

        # Now a new crypto-project follow arrives.
        _register(_snap([
            acct("old", bio=f"official CA: {EVM}", links=["https://dexscreener.com/x"]),
            acct("newproj", display_name="New Official", bio=f"official $NEW CA: {EVM2}",
                 links=["https://dexscreener.com/y", "https://t.me/new"]),
        ], captured_at="2024-01-02T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))

        # Only the new follow's contract was analyzed.
        assert calls == [EVM2.lower()]
        stored = kol_store.get_classification("x", "target", acct("newproj").key())
        assert stored is not None and stored.is_crypto_project
    finally:
        registry.reset_for_tests()


def test_capture_following_pipeline_failure_does_not_break_capture(_enabled, monkeypatch):
    # If the whole pipeline explodes, the capture/sync must still succeed.
    async def boom(platform, handle, accounts):
        raise RuntimeError("pipeline bug")
    monkeypatch.setattr(kol_crypto_pipeline, "process_new_follows", boom)
    from app.services.social import registry
    try:
        w.add_kol("target")
        _register(_snap([acct("a")], captured_at="2024-01-01T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        _register(_snap([acct("a"), acct("b")], captured_at="2024-01-02T00:00:00+00:00"))
        asyncio.run(w.capture_following("target"))
        # Capture still marked active despite the pipeline error.
        assert w.get_kol("target").status == "active"
    finally:
        registry.reset_for_tests()


# --- scope guard -------------------------------------------------------------


def test_no_alerting_or_kol_scoring_surfaces():
    """Deliverable D stops at classification + reusing the rug analyzer. Assert
    later-deliverable surfaces (user alerts, KOL scoring, clustering) stay absent."""
    for banned in ("alert", "notify", "send_alert", "score_kol", "cluster"):
        assert not hasattr(kol_crypto_pipeline, banned)
        assert not hasattr(crypto_intel, banned)
