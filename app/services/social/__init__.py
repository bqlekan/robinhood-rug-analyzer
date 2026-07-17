"""Social graph providers for the KOL Intelligence Engine (M23).

This package holds the platform abstraction. The KOL Intelligence Engine depends
ONLY on `base.SocialGraphProvider` and the platform-neutral models in
`app/models/kol.py`; it never imports a concrete provider. Providers register
themselves in `registry.py`, so adding Farcaster/Telegram/Discord/Reddit/Lens is
a matter of writing one new module and registering it — no engine change.
"""

from app.services.social.base import ProviderError, SocialGraphProvider
from app.services.social.registry import (
    available_platforms,
    get_provider,
    is_supported,
    register_provider,
)

__all__ = [
    "SocialGraphProvider",
    "ProviderError",
    "get_provider",
    "register_provider",
    "available_platforms",
    "is_supported",
]
