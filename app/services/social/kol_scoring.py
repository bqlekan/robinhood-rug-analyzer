from __future__ import annotations

"""Pure KOL Intelligence scoring + cluster detection (M23 Deliverable F).

Provider-neutral and completely offline — no I/O, no persistence, no analyzer
calls. Given the already-correlated inputs (the KOLs converging on a project, the
project's crypto classification, and a compact summary of the REUSED rug/risk
analysis), it produces:

  1. `detect_cluster(...)  -> ClusterInfo`  — who converged, how many, over what
     span, and which typed cluster kinds apply (tier_1 / mixed_tier / rapid /
     high_conviction). Timing and thresholds are ALL config; nothing is hardcoded.
  2. `score_project(...)   -> (score, confidence, evidence)` — a 0..100 KOL
     Intelligence Score built as a capped, additive sum of configured components,
     where EVERY component that fires emits one structured `Evidence` item. No
     opaque scoring: the evidence list reconstructs the score exactly.

Design discipline (mirrors M10 honeypot/unknown handling): this sub-score is kept
entirely separate from the core rug/confidence math — it augments intelligence, it
never mutates or distorts the existing risk score. And it reuses the existing
analysis summary rather than recomputing anything.

Extensibility: components, tier weights, confidence multipliers/bands, and every
cluster window/threshold live in `settings.kol_*`. Adding a Tier 4, retuning a
component, or widening the rapid window is a config edit — never a code change and
never a hardcoded KOL name. An external `alpha` score is accepted as an OPTIONAL
input so the engine is ready for a future alpha scorer without inventing one now.
"""

from datetime import datetime, timezone

from app.core.config import settings
from app.models.kol import (
    CONFIDENCE_LEVELS,
    ClusterInfo,
    Evidence,
    KolContributor,
)


# --- helpers -----------------------------------------------------------------


def tier_weight(tier: int) -> int:
    """Configured influence weight for a KOL tier. An unlisted/unknown tier falls
    back to the (low) default so a misconfigured tier never inflates a score."""
    weights = settings.kol_tier_weights or {}
    return int(weights.get(str(tier), settings.kol_tier_default_weight))


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime; None on anything odd.

    Tolerant by design: a missing/garbled timestamp must degrade timing signals to
    'unknown', never raise. Naive timestamps are assumed UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _span_hours(contributors: list[KolContributor]) -> tuple[str | None, str | None, float | None]:
    """(first_follow_at, latest_follow_at, span_hours) across contributors' follow
    times. Returns (None, None, None) when no parseable timestamps exist."""
    stamped = [(c, _parse_ts(c.followed_at)) for c in contributors]
    parsed = [(c, dt) for c, dt in stamped if dt is not None]
    if not parsed:
        return None, None, None
    first_c, first_dt = min(parsed, key=lambda p: p[1])
    last_c, last_dt = max(parsed, key=lambda p: p[1])
    span = (last_dt - first_dt).total_seconds() / 3600.0
    return first_c.followed_at, last_c.followed_at, span


def _confidence_multiplier(confidence: str | None) -> float:
    """0..1 multiplier for a crypto-classification confidence band (config-driven)."""
    if not confidence:
        return 0.0
    mults = settings.kol_confidence_multipliers or {}
    return float(mults.get(confidence, 0.0))


def score_confidence_band(score: int) -> str:
    """Map a 0..100 KOL Intelligence Score onto its confidence band, high->low, using
    the configured thresholds (the strongest band the score qualifies for wins)."""
    bands = settings.kol_score_confidence_bands or {}
    for level in CONFIDENCE_LEVELS:  # already ordered very_high -> very_low
        if level in bands and score >= int(bands[level]):
            return level
    return "very_low"


# --- cluster detection -------------------------------------------------------


def detect_cluster(
    platform: str,
    account_key: str,
    contributors: list[KolContributor],
    *,
    project_handle: str | None = None,
    score: int | None = None,
) -> ClusterInfo:
    """Classify the convergence of `contributors` on one project into a `ClusterInfo`.

    Cluster-ness and its typed kinds are entirely config-driven:
      - `is_cluster` once >= `kol_cluster_min_kols` distinct KOLs are within the main
        `kol_cluster_window_hours` window (measured as the span of their follows).
      - `tier_1`          when >= `kol_cluster_tier1_min` Tier-1 KOLs contributed.
      - `mixed_tier`      when contributors span more than one distinct tier.
      - `rapid`           when >= `kol_cluster_rapid_min_kols` followed within the
                          tighter `kol_cluster_rapid_window_hours`.
      - `high_conviction` when the provided `score` reaches
                          `kol_cluster_high_conviction_score`.

    Distinct KOLs are de-duplicated by (platform, kol_handle): one KOL following twice
    is still one contributor. `score` is optional so cluster shape can be computed
    before or after scoring; only the high_conviction tag needs it.
    """
    # De-dup to distinct KOLs, keeping the EARLIEST follow per KOL (first conviction).
    by_kol: dict[str, KolContributor] = {}
    for c in contributors:
        key = f"{c.platform}:{c.kol_handle.lower()}"
        prev = by_kol.get(key)
        if prev is None or (_parse_ts(c.followed_at) or _now_dt()) < (_parse_ts(prev.followed_at) or _now_dt()):
            by_kol[key] = c
    distinct = list(by_kol.values())
    kol_count = len(distinct)

    tier_counts: dict[str, int] = {}
    for c in distinct:
        tier_counts[str(c.tier)] = tier_counts.get(str(c.tier), 0) + 1

    first_at, latest_at, span = _span_hours(distinct)

    info = ClusterInfo(
        platform=platform,
        account_key=account_key,
        project_handle=project_handle,
        kol_count=kol_count,
        tier_counts=tier_counts,
        contributors=distinct,
        first_follow_at=first_at,
        latest_follow_at=latest_at,
        window_hours=span,
    )

    min_kols = int(settings.kol_cluster_min_kols)
    window = float(settings.kol_cluster_window_hours)
    # A cluster requires enough distinct KOLs AND (when we can measure it) a span that
    # fits the window. Unknown span (missing timestamps) doesn't block cluster-ness —
    # convergence by count still counts; timing tags simply won't fire.
    within_window = span is None or span <= window
    info.is_cluster = kol_count >= min_kols and within_window
    if not info.is_cluster:
        return info

    types: list[str] = []
    # tier_1: enough Tier-1 KOLs.
    if tier_counts.get("1", 0) >= int(settings.kol_cluster_tier1_min):
        types.append("tier_1")
    # mixed_tier: contributors span more than one tier.
    if len({c.tier for c in distinct}) > 1:
        types.append("mixed_tier")
    # rapid: enough KOLs inside the tighter rapid window.
    if span is not None and span <= float(settings.kol_cluster_rapid_window_hours) \
            and kol_count >= int(settings.kol_cluster_rapid_min_kols):
        types.append("rapid")
    # high_conviction: the score cleared the bar (only when a score was supplied).
    if score is not None and score >= int(settings.kol_cluster_high_conviction_score):
        types.append("high_conviction")

    info.cluster_types = types
    return info


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


# --- scoring -----------------------------------------------------------------


def score_project(
    contributors: list[KolContributor],
    *,
    crypto_confidence: str | None = None,
    classification: str | None = None,
    risk_score: int | None = None,
    analyzed: bool = False,
    cluster: ClusterInfo | None = None,
    alpha_score: int | None = None,
) -> tuple[int, str, list[Evidence]]:
    """Compute a project's KOL Intelligence Score with full, reconstructable evidence.

    The score is a capped, additive sum of configured components. Each component that
    contributes emits exactly one `Evidence` (signal = component name, weight = points
    actually added, detail = the human explanation). Summing the evidence weights and
    capping at 100 reproduces the score — nothing is hidden.

    Inputs are the already-correlated facts:
      - `contributors`      distinct KOLs converging on the project (with tier + timing)
      - `crypto_confidence` the project's crypto-classification band (reused, not recomputed)
      - `risk_score`/`analyzed`  the REUSED rug-analysis result (never recomputed here)
      - `cluster`           the detected cluster view (for the cluster bonus)
      - `alpha_score`       OPTIONAL external alpha in [0,100] (future extensible; when
                            None the component simply doesn't fire)

    Returns `(score, confidence_band, evidence)`.
    """
    weights = settings.kol_score_weights or {}
    evidence: list[Evidence] = []

    def add(signal: str, points: int, detail: str) -> None:
        pts = int(round(points))
        if pts <= 0:
            return
        evidence.append(Evidence(signal=signal, detail=detail, weight=pts, source="kol_intel"))

    # Distinct KOLs (by platform:handle) — the unit everything keys off.
    distinct: dict[str, KolContributor] = {}
    for c in contributors:
        distinct[f"{c.platform}:{c.kol_handle.lower()}"] = c
    kols = list(distinct.values())
    kol_count = len(kols)

    # 1. Convergence: reward each ADDITIONAL distinct KOL beyond the first. The whole
    #    reason to build a follow graph — multiple smart-money KOLs on one project.
    if kol_count > 1:
        per = int(weights.get("kol_convergence", 0))
        pts = per * (kol_count - 1)
        add("kol_convergence", pts,
            f"{kol_count} distinct KOLs converged on this project (+{per} each beyond the first)")

    # 2. Tier quality: the summed tier weights of contributors, scaled onto the
    #    component range so a couple of Tier-1s outweigh a crowd of Tier-3s.
    summed_tier = sum(tier_weight(c.tier) for c in kols)
    divisor = max(1, int(settings.kol_tier_quality_divisor))
    tq_weight = float(weights.get("tier_quality", 0))
    tier_points = (summed_tier / divisor) * tq_weight
    if tier_points > 0:
        tiers_desc = ", ".join(
            f"Tier {c.tier} @{c.kol_handle}" for c in sorted(kols, key=lambda x: x.tier)
        )
        add("tier_quality", tier_points, f"tier-weighted quality of contributors: {tiers_desc}")

    # 3. Crypto confidence: how sure we are this is a real crypto project (reused band).
    conf_mult = _confidence_multiplier(crypto_confidence)
    if conf_mult > 0:
        pts = float(weights.get("crypto_confidence", 0)) * conf_mult
        label = classification or "crypto project"
        add("crypto_confidence", pts,
            f"crypto classification '{label}' at {crypto_confidence} confidence")

    # 4. Analysis safety: reuse the rug analyzer's verdict. A LOW risk score raises the
    #    intel score (a clean, analyzable project is more actionable); a high risk score
    #    contributes little. Never recomputed — purely a correlation of existing output.
    if analyzed and risk_score is not None:
        safety = max(0.0, min(1.0, (100 - int(risk_score)) / 100.0))
        pts = float(weights.get("analysis_safety", 0)) * safety
        add("analysis_safety", pts,
            f"reused rug analysis: risk_score {risk_score} (safety factor {safety:.2f})")

    # 5. Cluster bonus: flat reward when any cluster kind was detected.
    if cluster is not None and cluster.is_cluster:
        pts = int(weights.get("cluster_bonus", 0))
        kinds = ", ".join(cluster.cluster_types) if cluster.cluster_types else "cluster"
        add("cluster_bonus", pts, f"cluster detected ({kinds})")

    # 6. Recency/timing: tight, recent convergence is a stronger signal than follows
    #    spread over weeks. Scale by how far inside the main window the span sits.
    if cluster is not None and cluster.window_hours is not None and kol_count > 1:
        window = float(settings.kol_cluster_window_hours) or 1.0
        tightness = max(0.0, min(1.0, 1.0 - (cluster.window_hours / window)))
        pts = float(weights.get("recency", 0)) * tightness
        add("recency", pts,
            f"convergence span {cluster.window_hours:.1f}h within {window:.0f}h window "
            f"(tightness {tightness:.2f})")

    # 7. Alpha (OPTIONAL / future extensible): an external alpha score, when one exists.
    #    No alpha scorer exists today, so this fires only if a caller supplies one — the
    #    engine is ready without inventing analysis.
    if alpha_score is not None:
        alpha = max(0.0, min(1.0, int(alpha_score) / 100.0))
        pts = float(weights.get("alpha", 0)) * alpha
        add("alpha", pts, f"external alpha score {alpha_score}")

    score = min(100, sum(e.weight for e in evidence))
    confidence = score_confidence_band(score)
    return score, confidence, evidence
