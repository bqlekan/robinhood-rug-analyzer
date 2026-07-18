from __future__ import annotations

"""Platform-neutral domain models for the KOL Intelligence Engine (M23, Deliverable A).

The intelligence engine speaks ONLY the vocabulary defined here — `KolEntry`,
`SocialAccount`, `FollowingSnapshot`, etc. It never imports X/Twitter, Farcaster,
Telegram or any concrete platform type. Each platform is reached through a
`SocialGraphProvider` (see `app/services/social/`) that maps its own wire format
into these models. That inversion is what lets new providers (Farcaster, Lens,
Reddit, ...) be added without touching the engine.

Conventions follow the existing `models/token.py`: pydantic `BaseModel`,
string-typed "enums" documented with a `# "a" | "b"` comment and enforced by a
`field_validator`, and stringly-typed ISO-8601 timestamps (matching how the
sqlite stores in this project persist time).
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

# --- Controlled vocabularies -------------------------------------------------
# Kept as module-level frozensets (not enum classes) to match the existing
# codebase style, which uses plain strings + validators rather than enum.Enum.

# Tier drives scoring weight in a later deliverable. Stored as an int 1..3 so it
# sorts naturally and future weight maps can key off it directly.
KOL_TIERS: frozenset[int] = frozenset({1, 2, 3})

# Lifecycle status of a watched KOL. This is operational health, distinct from
# the enabled/disabled toggle (a KOL can be enabled but temporarily "error").
#   active  — enabled and last sync (if any) succeeded
#   paused  — administratively disabled (enabled=False); never synced
#   error   — enabled but the last sync attempt failed
#   pending — enabled, never synced yet (no snapshot on record)
KOL_STATUSES: frozenset[str] = frozenset({"active", "paused", "error", "pending"})

# Platforms the engine can, in principle, address. A platform appearing here does
# NOT mean a provider is implemented — it means the domain model accepts it. The
# provider registry (app/services/social/registry.py) is the source of truth for
# what is actually wired. X is the first implemented provider (Deliverable B);
# the rest are declared so watchlist entries and config validate ahead of their
# providers landing, per the M23 multi-provider design.
SOCIAL_PLATFORMS: frozenset[str] = frozenset(
    {"x", "farcaster", "telegram", "discord", "reddit", "lens"}
)


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, matching the format the sqlite stores persist."""
    return datetime.now(timezone.utc).isoformat()


# --- Provider-facing value objects -------------------------------------------


class SocialAccount(BaseModel):
    """A single account on some social platform, as returned by a provider.

    Platform-neutral: `handle` is the platform's stable username/identifier and
    `platform_id` (when a provider can supply it) is the immutable numeric/hash id
    that survives handle renames — the preferred key for diffing. Bio/links are
    optional enrichment that later deliverables mine for crypto references; they
    are modeled here so the shape is stable, not populated in Deliverable A.
    """

    platform: str
    handle: str
    platform_id: str | None = None
    display_name: str | None = None
    bio: str | None = None
    profile_url: str | None = None
    # Free-form links surfaced on the profile (website, linktree, etc.). Mined by
    # the crypto-account detector in a later deliverable; empty for now.
    links: list[str] = Field(default_factory=list)

    # --- Rich profile metadata (Deliverable C) -------------------------------
    # All optional: a provider populates what it can scrape, and absence ("None")
    # is treated as "unknown", never as a change. Adding future fields here is
    # non-breaking because everything defaults to None and the store serializes
    # the whole model to JSON (see kol_store) rather than fixed columns.
    verified: bool | None = None
    followers_count: int | None = None
    following_count: int | None = None
    profile_image_url: str | None = None

    @field_validator("platform")
    @classmethod
    def _validate_platform(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in SOCIAL_PLATFORMS:
            raise ValueError(f"unknown platform {v!r}; expected one of {sorted(SOCIAL_PLATFORMS)}")
        return v

    @field_validator("handle")
    @classmethod
    def _normalize_handle(cls, v: str) -> str:
        # Strip a leading @ and surrounding whitespace so "@foo", "foo", and
        # " foo " compare equal. Case is left to the provider (some platforms are
        # case-sensitive); providers lowercase where appropriate.
        return v.strip().lstrip("@").strip()

    def key(self) -> str:
        """Stable identity for diffing: prefer the immutable id, else the handle."""
        return self.platform_id or self.handle.lower()


class FollowingSnapshot(BaseModel):
    """A point-in-time capture of who one KOL follows on one platform.

    Deliverable A defines and persists this shape (so the schema and the reader
    are production-ready), but does NOT populate it from the network or diff two
    snapshots — that is Deliverable B/C. `accounts` is therefore typically empty
    until the provider fetch lands.
    """

    platform: str
    kol_handle: str
    captured_at: str = Field(default_factory=utc_now_iso)
    accounts: list[SocialAccount] = Field(default_factory=list)
    # True when the capture completed cleanly; False marks a partial/failed pull
    # so downstream diffing (later) can refuse to treat gaps as "unfollows".
    complete: bool = True

    def keys(self) -> set[str]:
        """Set of account identities, for snapshot diffing."""
        return {a.key() for a in self.accounts}

    def by_key(self) -> dict[str, "SocialAccount"]:
        """Accounts indexed by their diff key. Later duplicates (same key) collapse
        onto the first occurrence, so a snapshot that accidentally lists an account
        twice still counts it once — diffing must never treat a duplicate as noise."""
        indexed: dict[str, SocialAccount] = {}
        for a in self.accounts:
            indexed.setdefault(a.key(), a)
        return indexed


# --- Follow-change detection (Deliverable C) ---------------------------------

# Structured internal event kinds. These are engine-internal facts, NOT user
# alerts (alerting is a later deliverable). Stored so later intelligence modules
# and the eventual alerting layer read a durable history rather than recomputing.
FOLLOW_EVENT_TYPES: frozenset[str] = frozenset({"new_follow", "unfollow"})

# What kind of profile attribute changed between two snapshots of the same account.
PROFILE_CHANGE_FIELDS: frozenset[str] = frozenset(
    {"handle", "display_name", "bio", "verified"}
)


class FollowEvent(BaseModel):
    """A detected change in *who* a KOL follows: a new follow or an unfollow.

    Engine-internal and platform-neutral. `account` carries the full metadata of
    the counterparty at detection time so downstream modules (crypto detection,
    scoring — later deliverables) don't have to re-fetch. No alert is implied.
    """

    event_type: str          # "new_follow" | "unfollow"
    platform: str
    kol_handle: str          # the watched KOL this event is about
    account_key: str         # stable identity of the followed/unfollowed account
    account: SocialAccount   # snapshot of that account's metadata at detection
    detected_at: str = Field(default_factory=utc_now_iso)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if v not in FOLLOW_EVENT_TYPES:
            raise ValueError(
                f"unknown follow event type {v!r}; expected one of {sorted(FOLLOW_EVENT_TYPES)}"
            )
        return v


class ProfileChange(BaseModel):
    """A detected change to an *already-followed* account's profile attributes
    (handle rename, display-name/bio edit, verification gained/lost).

    Recorded for future intelligence modules; not an alert. `account_key` is the
    stable identity (platform_id where available) so a handle rename is still
    tracked as the same account rather than an unfollow+new-follow pair."""

    platform: str
    kol_handle: str          # the watched KOL whose following list this was seen in
    account_key: str
    field: str               # one of PROFILE_CHANGE_FIELDS
    old_value: str | None = None
    new_value: str | None = None
    detected_at: str = Field(default_factory=utc_now_iso)

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        if v not in PROFILE_CHANGE_FIELDS:
            raise ValueError(
                f"unknown profile-change field {v!r}; expected one of {sorted(PROFILE_CHANGE_FIELDS)}"
            )
        return v


class SnapshotDiff(BaseModel):
    """The full result of comparing a previous snapshot to a current one.

    Platform-neutral and pure data — the diff engine (`services/social/diff.py`)
    produces it, and callers decide what to persist. `is_baseline` is True when
    there was no previous snapshot: everything is "unchanged from unknown", so we
    emit NO new-follow events (a first observation is not a follow *event*).
    """

    platform: str
    kol_handle: str
    new_follows: list[SocialAccount] = Field(default_factory=list)
    unfollows: list[SocialAccount] = Field(default_factory=list)
    unchanged: list[SocialAccount] = Field(default_factory=list)
    profile_changes: list[ProfileChange] = Field(default_factory=list)
    is_baseline: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.new_follows or self.unfollows or self.profile_changes)

    def events(self) -> list[FollowEvent]:
        """Materialize the follow/unfollow events implied by this diff. A baseline
        diff yields none, so establishing the first snapshot never floods the log
        with 'new follow' events for the KOL's entire existing following list."""
        out: list[FollowEvent] = []
        if self.is_baseline:
            return out
        for acct in self.new_follows:
            out.append(FollowEvent(
                event_type="new_follow", platform=self.platform,
                kol_handle=self.kol_handle, account_key=acct.key(), account=acct,
            ))
        for acct in self.unfollows:
            out.append(FollowEvent(
                event_type="unfollow", platform=self.platform,
                kol_handle=self.kol_handle, account_key=acct.key(), account=acct,
            ))
        return out


class ProviderCapabilities(BaseModel):
    """What a concrete `SocialGraphProvider` can actually do.

    The engine reads this instead of special-casing platforms. A provider whose
    `fetch_following` is not yet implemented advertises `can_fetch_following=False`
    so the engine can skip or degrade gracefully rather than crash.
    """

    platform: str
    can_fetch_following: bool = False
    # Whether the provider exposes a stable immutable account id (preferred diff
    # key). If False, diffing falls back to the handle.
    provides_stable_ids: bool = False
    # Whether this provider needs an out-of-band authenticated session (e.g. X
    # scraping needs cookies). Informational; used by ops/docs and health checks.
    requires_auth_session: bool = False


# --- Crypto intelligence (Deliverable D) -------------------------------------

# What kind of account we think this is. Deliberately coarse and platform-neutral;
# scoring/clustering (later deliverables) refine on top, they do not replace this.
#   official      — the token/project's own account (name/CA/official links align)
#   team          — a founder/dev/core-team account tied to a project
#   community      — a fan/community/announcements account for a project
#   infrastructure — tooling/exchange/aggregator (DexScreener, a wallet, a launchpad)
#   individual     — a person with no strong project affiliation
#   unknown        — not enough signal to say (the safe default)
ACCOUNT_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"official", "team", "community", "infrastructure", "individual", "unknown"}
)

# Which classifications are "a crypto project worth analyzing". Only these, when
# confident, hand contracts to the rug analyzer. `individual`/`unknown` never do.
CRYPTO_PROJECT_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"official", "team", "community"}
)

# Confidence bands, high->low. Strings (not enum) to match the codebase style; the
# numeric thresholds that map a score onto a band live in config so they're tunable.
CONFIDENCE_LEVELS: tuple[str, ...] = ("very_high", "high", "medium", "low", "very_low")

# Internal event kinds for the crypto pipeline. Engine-internal facts, NOT user
# alerts (alerting is a later deliverable) — persisted so later modules read history.
CRYPTO_EVENT_TYPES: frozenset[str] = frozenset(
    {"crypto_project_detected", "contract_extracted", "analysis_completed", "analysis_failed"}
)


class Evidence(BaseModel):
    """One piece of supporting evidence for a classification.

    Structured (not a free string) so later AI/heuristic reasoning can weigh it:
    `signal` is the machine name (a key in `kol_crypto_signal_weights`), `detail` is
    the human-readable specifics, `weight` is the contribution actually applied, and
    `source` says where on the profile it was found (bio/website/links/name/handle).
    """

    signal: str
    detail: str
    weight: int = 0
    source: str | None = None  # "bio" | "website" | "links" | "display_name" | "handle" | ...


class ExtractedContract(BaseModel):
    """A contract address mined from an account's profile, with provenance.

    `chain` is a best-effort label ("robinhood"/"ethereum"/"base"/"solana"/...);
    `supported` marks whether this project's single-chain analyzer can actually
    analyze it (only EVM addresses on the configured chain today). Unsupported
    chains are still recorded — extraction never silently drops a discovery."""

    address: str
    chain: str | None = None
    supported: bool = False
    source: str | None = None       # where it was found (bio/links/website/...)
    evidence: str | None = None     # human note, e.g. "CA: prefix in bio"


class ProfileIntelligence(BaseModel):
    """Structured, normalized read of a social profile — the analyzer's input view.

    Platform-neutral and provider-agnostic: built from a `SocialAccount` alone, it
    flattens display name / username / bio / website / links / metadata / verified
    into one object the detector reasons over. Kept separate from `SocialAccount` so
    enrichment logic never mutates the raw provider capture."""

    platform: str
    handle: str
    display_name: str | None = None
    bio: str | None = None
    website: str | None = None
    links: list[str] = Field(default_factory=list)
    verified: bool | None = None
    followers_count: int | None = None
    following_count: int | None = None
    # Lowercased concatenation of the text fields, for cheap keyword scanning.
    text_blob: str = ""


class CryptoClassification(BaseModel):
    """The full, explainable result of classifying one account.

    Every classification carries its `evidence` and the `signals` that fired, so no
    verdict is a black box: a reader (or a later AI stage) can see exactly why. A
    verdict is only actionable (contracts get analyzed) when `is_actionable` — a
    crypto-project classification, confident enough, with enough corroboration."""

    platform: str
    handle: str
    account_key: str
    classification: str            # one of ACCOUNT_CLASSIFICATIONS
    confidence: str                # one of CONFIDENCE_LEVELS
    score: int = 0                 # 0..100 weighted signal score
    signals: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    contracts: list[ExtractedContract] = Field(default_factory=list)
    classified_at: str = Field(default_factory=utc_now_iso)

    @field_validator("classification")
    @classmethod
    def _validate_classification(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ACCOUNT_CLASSIFICATIONS:
            raise ValueError(
                f"unknown classification {v!r}; expected one of {sorted(ACCOUNT_CLASSIFICATIONS)}"
            )
        return v

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CONFIDENCE_LEVELS:
            raise ValueError(
                f"unknown confidence {v!r}; expected one of {list(CONFIDENCE_LEVELS)}"
            )
        return v

    @property
    def is_crypto_project(self) -> bool:
        return self.classification in CRYPTO_PROJECT_CLASSIFICATIONS

    def supported_contracts(self) -> list[ExtractedContract]:
        """Extracted contracts the local analyzer can actually process."""
        return [c for c in self.contracts if c.supported]


class CryptoIntelEvent(BaseModel):
    """An engine-internal crypto-pipeline fact (detection/extraction/analysis).

    NOT a user alert (alerting is a later deliverable). `payload` carries an
    event-specific JSON-able dict (e.g. the analyzed contract + risk summary) so the
    durable log is self-describing for later intelligence and the eventual alerter."""

    event_type: str                # one of CRYPTO_EVENT_TYPES
    platform: str
    kol_handle: str                # the watched KOL whose follow triggered this
    account_key: str               # the followed account being analyzed
    detected_at: str = Field(default_factory=utc_now_iso)
    payload: dict = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if v not in CRYPTO_EVENT_TYPES:
            raise ValueError(
                f"unknown crypto event type {v!r}; expected one of {sorted(CRYPTO_EVENT_TYPES)}"
            )
        return v


# --- KOL Intelligence scoring & correlation (Deliverable F) ------------------

# Typed cluster kinds. A project can satisfy several at once; the engine records all
# that apply so downstream reasoning (and a future AI stage) sees every angle. These
# are engine-internal descriptors, NOT user alerts.
#   tier_1          — enough Tier-1 KOLs converged (the strongest signal quality)
#   mixed_tier      — KOLs of differing tiers converged (breadth across the roster)
#   rapid           — the convergence happened inside the tight rapid window (timing)
#   high_conviction — the computed KOL Intelligence Score cleared the conviction bar
CLUSTER_TYPES: tuple[str, ...] = ("tier_1", "mixed_tier", "rapid", "high_conviction")

# Internal intelligence event kinds. Engine-internal facts persisted as a durable
# timeline — NOT user notifications (transports are Deliverable H). A future AI stage
# reads these plus the ProjectIntelligence object without recomputing anything.
KOL_INTEL_EVENT_TYPES: frozenset[str] = frozenset({
    "kol_score_updated",          # a project's KOL Intelligence Score changed
    "kol_cluster_detected",       # >= min KOLs converged (any cluster type)
    "high_conviction_cluster",    # a cluster whose score cleared the conviction bar
    "project_momentum_detected",  # distinct-KOL count grew since the last score
    "intelligence_updated",       # umbrella: the correlation object was refreshed
})


class KolContributor(BaseModel):
    """One KOL's contribution to a project's convergence, with the timing and quality
    needed to score and explain it. Platform-neutral; `tier` and `tier_weight` make
    the KOL's influence explicit so evidence can cite it without a second lookup."""

    platform: str
    kol_handle: str
    tier: int
    tier_weight: int = 0
    followed_at: str                 # when this KOL first followed the project account
    account_key: str                 # the followed project account's stable key


class ClusterInfo(BaseModel):
    """The convergence/cluster view of a project: who converged, how many, over what
    span, and which typed cluster kinds apply. Persisted to cluster history so the
    timeline of how a cluster formed is queryable later (and AI-explainable)."""

    platform: str
    account_key: str
    project_handle: str | None = None
    is_cluster: bool = False
    cluster_types: list[str] = Field(default_factory=list)  # subset of CLUSTER_TYPES
    kol_count: int = 0
    tier_counts: dict[str, int] = Field(default_factory=dict)  # tier(str) -> #KOLs
    contributors: list[KolContributor] = Field(default_factory=list)
    first_follow_at: str | None = None
    latest_follow_at: str | None = None
    window_hours: float | None = None   # span between first and latest follow

    @field_validator("cluster_types")
    @classmethod
    def _validate_cluster_types(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in CLUSTER_TYPES]
        if bad:
            raise ValueError(
                f"unknown cluster type(s) {bad}; expected from {list(CLUSTER_TYPES)}"
            )
        return v


class ProjectIntelligence(BaseModel):
    """The single structured intelligence object for one project account.

    This is the correlation engine's output and the durable unit later stages (and the
    future AI Trading Intelligence Engine) consume. It is deliberately self-contained:
    it carries the score, its confidence, the full structured `evidence`, the
    `contributors` (who + tier + timing), the `cluster` view, a compact `correlation`
    of the REUSED analysis (rug/risk, never recomputed here), and a `timeline` of prior
    scores — everything needed to generate an explanation WITHOUT rescanning or
    recomputing history. Nothing here is a user alert.
    """

    platform: str
    account_key: str
    project_handle: str | None = None
    # The strongest crypto classification seen among contributors (the project's own
    # or team account carries the best signal). Drives the crypto_confidence component.
    classification: str | None = None
    crypto_confidence: str | None = None    # one of CONFIDENCE_LEVELS
    score: int = 0                          # 0..100 KOL Intelligence Score
    confidence: str = "very_low"            # band for the score (CONFIDENCE_LEVELS)
    evidence: list[Evidence] = Field(default_factory=list)
    contributors: list[KolContributor] = Field(default_factory=list)
    cluster: ClusterInfo | None = None
    # Compact, JSON-able snapshot of the REUSED analysis for correlation/explanation —
    # e.g. {"analyzed": true, "risk_score": 42, "risk_level": "medium",
    #        "honeypot": "passed", "alpha_score": null}. Never recomputed here.
    correlation: dict = Field(default_factory=dict)
    # Recent prior scores (newest last), for momentum + AI timelines without a re-read.
    timeline: list[dict] = Field(default_factory=list)
    kol_count: int = 0
    updated_at: str = Field(default_factory=utc_now_iso)
    # Stable hash of the scoring inputs at compute time. When unchanged, the engine
    # skips rescoring an untouched project (see Deliverable F "Performance").
    fingerprint: str | None = None

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CONFIDENCE_LEVELS:
            raise ValueError(
                f"unknown confidence {v!r}; expected one of {list(CONFIDENCE_LEVELS)}"
            )
        return v

    @property
    def is_actionable(self) -> bool:
        """Whether this intelligence clears the configured actionable-score bar. Read
        by callers; the threshold itself lives in config, not here."""
        from app.core.config import settings
        return self.score >= int(settings.kol_intel_min_actionable_score)


class KolIntelEvent(BaseModel):
    """An engine-internal intelligence fact (score updated / cluster / momentum).

    NOT a user notification — transports (Telegram/Discord/webhook/UI) are Deliverable
    H. `payload` is a self-describing JSON-able dict so the durable timeline explains
    itself to later intelligence and the eventual alerter without a re-read."""

    event_type: str                 # one of KOL_INTEL_EVENT_TYPES
    platform: str
    account_key: str                # the project account this intelligence is about
    project_handle: str | None = None
    detected_at: str = Field(default_factory=utc_now_iso)
    payload: dict = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if v not in KOL_INTEL_EVENT_TYPES:
            raise ValueError(
                f"unknown intel event type {v!r}; expected one of {sorted(KOL_INTEL_EVENT_TYPES)}"
            )
        return v


# --- Watchlist domain model --------------------------------------------------


class KolEntry(BaseModel):
    """One watched KOL. The core record the watchlist stores and exposes.

    `platform` + `handle` form the identity (a person may be tracked on several
    platforms as separate entries). All the operator-facing fields from the
    Deliverable A spec live here: display name, handle, tier, enabled, notes,
    date added, last checked, status.
    """

    platform: str = "x"
    handle: str
    display_name: str | None = None
    tier: int = 2  # 1 = highest signal weight
    enabled: bool = True
    notes: str | None = None
    date_added: str = Field(default_factory=utc_now_iso)
    last_checked: str | None = None  # last sync attempt; None = never checked
    status: str = "pending"

    @field_validator("platform")
    @classmethod
    def _validate_platform(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in SOCIAL_PLATFORMS:
            raise ValueError(f"unknown platform {v!r}; expected one of {sorted(SOCIAL_PLATFORMS)}")
        return v

    @field_validator("handle")
    @classmethod
    def _normalize_handle(cls, v: str) -> str:
        v = v.strip().lstrip("@").strip()
        if not v:
            raise ValueError("handle must be a non-empty username")
        return v

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, v: int) -> int:
        if v not in KOL_TIERS:
            raise ValueError(f"tier must be one of {sorted(KOL_TIERS)}")
        return v

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KOL_STATUSES:
            raise ValueError(f"status must be one of {sorted(KOL_STATUSES)}")
        return v

    def identity(self) -> tuple[str, str]:
        """(platform, lowercased handle) — the unique key used for storage."""
        return (self.platform, self.handle.lower())


class WatchStatus(BaseModel):
    """Read-only operational view of a KOL, without exposing storage details.

    Returned by the public `get_watch_status` interface so callers can render
    health (enabled, status, when last checked, whether a baseline snapshot
    exists) without reaching into the store or the provider.
    """

    platform: str
    handle: str
    display_name: str | None = None
    tier: int
    enabled: bool
    status: str
    date_added: str
    last_checked: str | None = None
    has_snapshot: bool = False
    last_snapshot_at: str | None = None
    provider_available: bool = False


class KolSeed(BaseModel):
    """Config-shaped seed for a KOL, loaded from settings into the watchlist.

    Deliberately smaller than `KolEntry`: config declares intent (who to watch,
    at what tier, on/off), while the store owns lifecycle fields (status, dates,
    last_checked). This is what makes the watchlist editable without code changes.
    """

    platform: str = "x"
    handle: str
    display_name: str | None = None
    tier: int = 2
    enabled: bool = True
    notes: str | None = None
