from __future__ import annotations

"""Provider registry — maps a platform key to its `SocialGraphProvider` instance.

This is the indirection that lets the KOL Intelligence Engine stay ignorant of
concrete platforms. The engine asks the registry for a provider by platform key;
it never imports `x_provider` (or any future provider) directly. Adding a platform
is: write the provider module, call `register_provider(...)`. Nothing in the engine
changes.

Providers are registered at import time (see `_install_default_providers`). The
registry is process-global and populated once; tests can register fakes too.
"""

import logging
import threading

from app.services.social.base import SocialGraphProvider

logger = logging.getLogger(__name__)

# Reentrant: `_ensure_defaults` holds the lock while `_install_default_providers`
# calls back into `register_provider`, which locks again. A plain Lock deadlocks.
_LOCK = threading.RLock()
_PROVIDERS: dict[str, SocialGraphProvider] = {}
_DEFAULTS_INSTALLED = False


def register_provider(provider: SocialGraphProvider, *, replace: bool = False) -> None:
    """Register a provider under its `platform` key. Idempotent unless `replace`."""
    platform = (provider.platform or "").strip().lower()
    if not platform:
        raise ValueError("provider.platform must be set to a non-empty platform key")
    with _LOCK:
        if platform in _PROVIDERS and not replace:
            # Already registered (e.g. defaults installed twice) — keep the first.
            return
        _PROVIDERS[platform] = provider
        logger.debug("Registered social provider for platform %r", platform)


def get_provider(platform: str) -> SocialGraphProvider | None:
    """Return the provider for `platform`, or None if no provider is wired.

    Returning None (rather than raising) lets the engine treat an
    unimplemented-but-declared platform as 'provider unavailable' and degrade
    gracefully, exactly as it will for a provider whose fetch is not yet built.
    """
    _ensure_defaults()
    return _PROVIDERS.get((platform or "").strip().lower())


def is_supported(platform: str) -> bool:
    """True when a provider is actually wired for `platform`."""
    return get_provider(platform) is not None


def available_platforms() -> list[str]:
    """Platform keys with a registered provider, sorted for stable output."""
    _ensure_defaults()
    with _LOCK:
        return sorted(_PROVIDERS)


def _ensure_defaults() -> None:
    global _DEFAULTS_INSTALLED
    if _DEFAULTS_INSTALLED:
        return
    with _LOCK:
        if _DEFAULTS_INSTALLED:
            return
        _install_default_providers()
        _DEFAULTS_INSTALLED = True


def _install_default_providers() -> None:
    """Wire the built-in providers. Import is local to avoid a circular import
    (providers import the registry's base types). X is the only provider today;
    future providers get one line each here."""
    from app.services.social.x_provider import XProvider

    register_provider(XProvider())
    # Future: register_provider(FarcasterProvider()), etc. — engine unaffected.


def reset_for_tests() -> None:
    """Clear the registry and force default re-install on next access."""
    global _DEFAULTS_INSTALLED
    with _LOCK:
        _PROVIDERS.clear()
        _DEFAULTS_INSTALLED = False
