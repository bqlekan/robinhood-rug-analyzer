"""Unit tests for M14 funder-graph depth & bundler detection (pure, no network)."""

from app.models.token import BundleAnalysis, ClusterAnalysis, HolderCluster
from app.services import analyzers
from app.services.scoring import score_token
from tests.test_scoring import _clean_kwargs


# --- multi-hop shared-funder unification (analyze_clusters with funder_chains) ---


def test_multihop_shared_funder_unifies_wallets():
    # A and B share no immediate funder, but both chains meet at 0xroot two hops up.
    chains = {
        "0xA": ["0xmid1", "0xroot"],
        "0xB": ["0xmid2", "0xroot"],
        "0xC": ["0xother"],
    }
    pcts = {"0xA": 10.0, "0xB": 15.0, "0xC": 5.0}
    result = analyzers.analyze_clusters({}, pcts, funder_chains=chains)
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert set(cluster.member_addresses) == {"0xa", "0xb"}
    assert cluster.combined_percentage == 25.0


def test_single_hop_map_still_works_unchanged():
    # No funder_chains -> legacy single-hop behaviour (backward compatible).
    funders = {"0xA": "0xF1", "0xB": "0xF1", "0xC": "0xF2"}
    pcts = {"0xA": 10.0, "0xB": 15.0, "0xC": 5.0}
    result = analyzers.analyze_clusters(funders, pcts)
    assert len(result.clusters) == 1
    assert set(result.clusters[0].member_addresses) == {"0xa", "0xb"}


# --- analyze_bundle (pure) ---


def _cluster(members, pct, funder="0xfunder", link="shared_funder"):
    return HolderCluster(funder_address=funder, member_addresses=members,
                         combined_percentage=pct, link_type=link)


def test_bundle_flagged_above_threshold():
    clusters = ClusterAnalysis(
        clusters=[_cluster(["0xa", "0xb", "0xc", "0xd"], 40.0)],
        clustered_percentage=40.0,
    )
    b = analyzers.analyze_bundle(clusters, min_wallets=3)
    assert b.bundled_wallets == 4
    assert b.bundled_percentage == 40.0
    assert b.top_funder == "0xfunder"
    assert b.classification in ("Heavy", "Extreme")
    assert b.score >= 50


def test_bundle_below_threshold_is_normal():
    clusters = ClusterAnalysis(clusters=[_cluster(["0xa", "0xb"], 8.0)], clustered_percentage=8.0)
    b = analyzers.analyze_bundle(clusters, min_wallets=3)
    assert b.classification == "Normal"
    assert b.score == 0
    assert b.bundled_wallets == 2


def test_bundle_none_without_funder_cluster():
    # Mutual-transfer-only clusters are not a funding bundle.
    clusters = ClusterAnalysis(
        clusters=[_cluster(["0xa", "0xb", "0xc"], 30.0, funder=None, link="mutual_transfer")],
        clustered_percentage=30.0,
    )
    b = analyzers.analyze_bundle(clusters, min_wallets=3)
    assert b.classification == "Normal"
    assert b.bundled_wallets == 0


def test_creator_funded_bundle_raises_score():
    members = ["0xa", "0xb", "0xc"]
    clusters = ClusterAnalysis(clusters=[_cluster(members, 15.0)], clustered_percentage=15.0)
    chains = {"0xa": ["0xmid", "0xcreator"], "0xb": ["0xcreator"], "0xc": ["0xmid"]}
    b_no = analyzers.analyze_bundle(clusters, creator=None, funder_chains=chains, min_wallets=3)
    b_yes = analyzers.analyze_bundle(clusters, creator="0xCreator", funder_chains=chains, min_wallets=3)
    assert b_yes.creator_funded_bundle is True
    assert b_no.creator_funded_bundle is False
    assert b_yes.score > b_no.score


def test_bundle_empty_clusters():
    b = analyzers.analyze_bundle(ClusterAnalysis(clusters=[], clustered_percentage=0.0))
    assert b.classification == "Normal"
    assert b.score == 0


# --- scoring: only a positive bundle classification scores ---


def test_heavy_bundle_scores_signal():
    kwargs = _clean_kwargs()
    kwargs["bundle"] = BundleAnalysis(score=60, classification="Heavy", bundled_wallets=5,
                                      bundled_percentage=30.0, detail="Heavy bundling.")
    result = score_token(**kwargs)
    assert any(s.name == "Bundled / sybil launch" for s in result.signals)


def test_normal_bundle_adds_no_signal():
    kwargs = _clean_kwargs()
    kwargs["bundle"] = BundleAnalysis(score=10, classification="Normal", bundled_wallets=2)
    result = score_token(**kwargs)
    assert not any(s.name == "Bundled / sybil launch" for s in result.signals)


def test_no_bundle_is_backward_compatible():
    # score_token with no bundle arg behaves exactly as before.
    base = score_token(**_clean_kwargs())
    assert not any(s.name == "Bundled / sybil launch" for s in base.signals)
