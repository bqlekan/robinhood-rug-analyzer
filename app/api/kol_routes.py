"""Read-only KOL intelligence routes (F5).

Pure reads of existing kol_store state — no scoring, no capture, no new
computation. Every endpoint returns an empty payload when the KOL engine is
disabled (kol_intel_enabled=False) so the frontend degrades gracefully.
"""
from fastapi import APIRouter

from app.core.config import settings
from app.services import kol_store

router = APIRouter(prefix="/api/v1/kol", tags=["kol"])


def _enabled() -> bool:
    return bool(settings.kol_intel_enabled)


@router.get("/kols")
async def list_kols(platform: str | None = None, limit: int = 200) -> dict:
    """Watched KOL roster (tier, enabled, last-checked status)."""
    if not _enabled():
        return {"enabled": False, "kols": []}
    kols = kol_store.list_kols(platform or settings.kol_default_platform, limit=limit)
    return {
        "enabled": True,
        "kols": [
            {
                "platform": k.platform,
                "handle": k.handle,
                "display_name": k.display_name,
                "tier": k.tier,
                "enabled": k.enabled,
                "status": k.status,
                "last_checked": k.last_checked,
            }
            for k in kols
        ],
    }


@router.get("/projects")
async def list_projects(
    platform: str | None = None, min_score: int = 0, limit: int = 100
) -> dict:
    """Ranked project intelligence (highest score first)."""
    if not _enabled():
        return {"enabled": False, "projects": []}
    p = platform or settings.kol_default_platform
    projects = kol_store.list_project_intelligence(p, min_score=min_score, limit=limit)
    return {
        "enabled": True,
        "projects": [proj.model_dump() for proj in projects],
    }


@router.get("/projects/{platform}/{account_key}")
async def get_project(platform: str, account_key: str) -> dict:
    """Single project intelligence record."""
    if not _enabled():
        return {"enabled": False, "project": None}
    proj = kol_store.get_project_intelligence(platform, account_key)
    return {"enabled": True, "project": proj.model_dump() if proj else None}


@router.get("/events")
async def list_events(
    platform: str | None = None,
    handle: str | None = None,
    limit: int = 100,
) -> dict:
    """Recent follow + crypto-intel events for a KOL (newest first)."""
    if not _enabled() or not handle:
        return {"enabled": _enabled(), "events": []}
    p = platform or settings.kol_default_platform
    follow = kol_store.list_follow_events(p, handle, limit=limit)
    crypto = kol_store.list_crypto_events(p, handle, limit=limit)
    events = sorted(
        [{"kind": "follow", **e.model_dump()} for e in follow]
        + [{"kind": "crypto", **e.model_dump()} for e in crypto],
        key=lambda e: e.get("detected_at") or "",
        reverse=True,
    )[:limit]
    return {"enabled": True, "events": events}


@router.get("/clusters")
async def list_clusters(
    platform: str | None = None, account_key: str | None = None, limit: int = 50
) -> dict:
    """Cluster history for a project account."""
    if not _enabled() or not account_key:
        return {"enabled": _enabled(), "clusters": []}
    p = platform or settings.kol_default_platform
    clusters = kol_store.list_cluster_history(p, account_key, limit=limit)
    return {"enabled": True, "clusters": [c.model_dump() for c in clusters]}
