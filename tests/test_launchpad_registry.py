"""M8: registry-driven launchpad + locker detection (example addresses only).

The PRODUCTION registry is empty by design (no fabricated/scraped addresses), so
these tests seed example entries via monkeypatch to exercise the match logic.
"""

from app.services import launchpad_registry as reg

# Example addresses — TEST ONLY. Never added to the production registry.
FACTORY = "0x" + "1" * 40
TEAM = "0x" + "2" * 40
LOCKER = "0x" + "3" * 40


def _seed_launchpad(monkeypatch):
    monkeypatch.setattr(
        reg,
        "LAUNCHPADS",
        [
            {
                "name": "Example Launch",
                "factory_address": FACTORY,
                "team_addresses": [TEAM],
                "event_signatures": [],
                "source": "test",
                "verified_date": "2026-01-01",
                "enabled": True,
            }
        ],
    )


# --- launchpad detection ---


def test_factory_match_is_high(monkeypatch):
    _seed_launchpad(monkeypatch)
    name, confidence, _ = reg.detect_launchpad(FACTORY.upper(), None, None)
    assert name == "Example Launch"
    assert confidence == "high"


def test_team_match_is_low(monkeypatch):
    _seed_launchpad(monkeypatch)
    name, confidence, _ = reg.detect_launchpad(TEAM, None, None)
    assert name == "Example Launch"
    assert confidence == "low"


def test_disabled_entry_is_ignored(monkeypatch):
    monkeypatch.setattr(
        reg,
        "LAUNCHPADS",
        [{"name": "Off", "factory_address": FACTORY, "enabled": False}],
    )
    name, confidence, _ = reg.detect_launchpad(FACTORY, None, None)
    assert name == "Unknown"


def test_empty_registry_degrades_to_unknown(monkeypatch):
    monkeypatch.setattr(reg, "LAUNCHPADS", [])
    name, confidence, _ = reg.detect_launchpad(FACTORY, None, None)
    assert name == "Unknown"
    assert confidence == "low"


# --- locker detection ---


def test_verified_locker_match(monkeypatch):
    monkeypatch.setattr(
        reg,
        "LP_LOCKERS",
        [{"address": LOCKER, "label": "Example Locker", "source": "test", "verified_date": "2026-01-01", "enabled": True}],
    )
    assert reg.locker_label(LOCKER.upper()) == "Example Locker"


def test_burn_address_always_recognized():
    # Chain-agnostic burn addresses need no registry entry.
    assert reg.locker_label("0x000000000000000000000000000000000000dEaD") == "Burn address"


def test_unknown_locker_returns_none(monkeypatch):
    monkeypatch.setattr(reg, "LP_LOCKERS", [])
    assert reg.locker_label(LOCKER) is None


# --- M9: on-chain creation evidence ---

EVENT_SIG = "0x" + "a" * 64  # example factory event topic — TEST ONLY


def _seed_with_event(monkeypatch):
    monkeypatch.setattr(
        reg,
        "LAUNCHPADS",
        [
            {
                "name": "Example Launch",
                "factory_address": FACTORY,
                "team_addresses": [TEAM],
                "event_signatures": [EVENT_SIG],
                "source": "test",
                "verified_date": "2026-01-01",
                "enabled": True,
            }
        ],
    )


def test_has_enabled_launchpads_reflects_registry(monkeypatch):
    monkeypatch.setattr(reg, "LAUNCHPADS", [])
    assert reg.has_enabled_launchpads() is False
    _seed_launchpad(monkeypatch)
    assert reg.has_enabled_launchpads() is True


def test_creation_factory_match_is_high(monkeypatch):
    _seed_with_event(monkeypatch)
    result = reg.match_creation_evidence(FACTORY.upper(), None)
    assert result is not None
    name, confidence, _ = result
    assert name == "Example Launch"
    assert confidence == "high"


def test_creation_event_match_is_medium(monkeypatch):
    _seed_with_event(monkeypatch)
    # No factory match, but a creation log carries a verified event signature.
    result = reg.match_creation_evidence(None, [EVENT_SIG.upper()])
    assert result is not None
    name, confidence, _ = result
    assert name == "Example Launch"
    assert confidence == "medium"


def test_factory_beats_event_when_both_present(monkeypatch):
    _seed_with_event(monkeypatch)
    _, confidence, _ = reg.match_creation_evidence(FACTORY, [EVENT_SIG])
    assert confidence == "high"


def test_no_creation_evidence_returns_none(monkeypatch):
    _seed_with_event(monkeypatch)
    assert reg.match_creation_evidence("0x" + "9" * 40, ["0x" + "b" * 64]) is None


def test_empty_registry_yields_no_evidence(monkeypatch):
    monkeypatch.setattr(reg, "LAUNCHPADS", [])
    assert reg.match_creation_evidence(FACTORY, [EVENT_SIG]) is None
