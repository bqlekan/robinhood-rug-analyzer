from __future__ import annotations

"""Notification & delivery layer (M23 Deliverable H).

The transport layer the earlier deliverables designed for. It CONSUMES the
intelligence events already produced + persisted by `kol_intel_engine`
(`KolIntelEvent`s) and delivers the alert-worthy ones to configured destinations.
It generates NO intelligence of its own: no scoring, no analysis, no event
creation. It reads the `ProjectIntelligence` the engine already computed to decide
what clears the forwarding rules, then hands a formatted message to each provider.

Design (kept deliberately small; nothing speculative):
  - `NotificationProvider` — a tiny ABC: a `name` + a `send(notification)`. The one
    seam for adding Telegram/Discord/webhook/UI later with no producer change.
  - Two providers, exactly the ones the roadmap asks for: `LogNotificationProvider`
    (emits via the app logger) and `MemoryNotificationProvider` (in-process buffer,
    the UI-feed / test sink).
  - `dispatch_events(...)` — the engine's single call-in. Config-gated, rule-filtered,
    dedupe-guarded, and fully failure-isolated: a provider raising never propagates,
    never interrupts analysis; the failure is logged and recorded, processing continues.

Every threshold and destination is config (`settings.notify_*`). Delivery attempts
(status/timestamp/destination/error) are persisted via `kol_store` for audit + dedupe.
"""

import logging
from abc import ABC, abstractmethod

from app.core.config import settings
from app.models.kol import CONFIDENCE_LEVELS, KolIntelEvent, ProjectIntelligence
from app.services import kol_store

logger = logging.getLogger(__name__)


class Notification:
    """A formatted, ready-to-deliver notification derived from one intelligence event.

    A thin carrier — no logic. `event_key` is the stable dedupe identity of the
    underlying event; `title`/`body` are the human-readable rendering; `payload` is the
    event's own self-describing dict passed straight through for structured sinks."""

    __slots__ = ("event_key", "event_type", "platform", "account_key",
                 "project_handle", "title", "body", "payload")

    def __init__(self, *, event_key: str, event_type: str, platform: str,
                 account_key: str, project_handle: str | None, title: str,
                 body: str, payload: dict):
        self.event_key = event_key
        self.event_type = event_type
        self.platform = platform
        self.account_key = account_key
        self.project_handle = project_handle
        self.title = title
        self.body = body
        self.payload = payload


class NotificationProvider(ABC):
    """The destination abstraction. Implement `send`; raise on failure (the engine
    isolates it). Adding a transport = one subclass + a `_PROVIDER_FACTORIES` entry."""

    #: stable destination name, also the key used in `settings.notify_providers`
    name: str = ""

    @abstractmethod
    def send(self, notification: Notification) -> None:
        """Deliver one notification. Raise any exception on failure — the caller
        records it and moves on; a raise here never reaches the analysis path."""
        raise NotImplementedError


class LogNotificationProvider(NotificationProvider):
    """Default sink: emit the notification through the app logger. Always available,
    no external dependency — the honest baseline transport."""

    name = "log"

    def send(self, notification: Notification) -> None:
        logger.info(
            "[KOL ALERT] %s — %s (%s:%s)",
            notification.title, notification.body,
            notification.platform, notification.account_key,
        )


class MemoryNotificationProvider(NotificationProvider):
    """In-process buffer sink. Serves as a UI-feed source and the natural test sink.
    Bounded so a long-running process can't grow it without limit."""

    name = "memory"
    _MAX = 500

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)
        if len(self.sent) > self._MAX:
            del self.sent[: -self._MAX]


# Provider registry. Only the roadmap sinks. A new transport registers its factory
# here (and its name goes in `settings.notify_providers`) — producers never change.
_PROVIDER_FACTORIES: dict[str, type[NotificationProvider]] = {
    LogNotificationProvider.name: LogNotificationProvider,
    MemoryNotificationProvider.name: MemoryNotificationProvider,
}

# Instantiated singletons, so a stateful sink (memory) keeps its buffer across calls.
_PROVIDERS: dict[str, NotificationProvider] = {}


def register_provider(provider: NotificationProvider) -> None:
    """Register/replace a live provider instance by its `name`. Used by tests to inject
    a sink, and the seam a future transport wires itself in through."""
    _PROVIDERS[provider.name] = provider


def _get_provider(name: str) -> NotificationProvider | None:
    """Return the live provider for `name`, lazily instantiating a known factory.
    Unknown names return None (logged by the caller) — never raise on misconfig."""
    if name in _PROVIDERS:
        return _PROVIDERS[name]
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        return None
    provider = factory()
    _PROVIDERS[name] = provider
    return provider


def reset_for_tests() -> None:
    """Drop all live provider instances (clears the memory buffer between tests)."""
    _PROVIDERS.clear()


def _confidence_rank(level: str) -> int:
    """Rank a confidence band so bands are comparable. CONFIDENCE_LEVELS runs
    strongest->weakest, so a LOWER index means stronger. Unknown => weakest."""
    try:
        return CONFIDENCE_LEVELS.index(level)
    except ValueError:
        return len(CONFIDENCE_LEVELS)


def _passes_rules(event: KolIntelEvent, intel: ProjectIntelligence) -> bool:
    """Whether an event clears the configured forwarding rules, judged against the
    project's already-computed intelligence (never recomputed here).

    All rules are AND-ed: event type in the forward list, score >= min, confidence
    band at least the minimum, and distinct-KOL (cluster) size >= min."""
    if event.event_type not in set(settings.notify_event_types):
        return False
    if intel.score < int(settings.notify_min_score):
        return False
    if _confidence_rank(intel.confidence) > _confidence_rank(settings.notify_min_confidence):
        return False
    if intel.kol_count < int(settings.notify_min_cluster_size):
        return False
    return True


def _format(event: KolIntelEvent, intel: ProjectIntelligence) -> tuple[str, str]:
    """Render a (title, body) for an event. Reuses the intelligence the engine already
    computed — no new analysis. Kept plain; richer per-transport formatting is a
    future adapter's concern, not this layer's."""
    who = event.project_handle or event.account_key
    title = f"{event.event_type} · {who}"
    body = (
        f"score={intel.score} confidence={intel.confidence} "
        f"kols={intel.kol_count}"
    )
    if intel.cluster is not None and intel.cluster.cluster_types:
        body += f" cluster={','.join(intel.cluster.cluster_types)}"
    return title, body


def dispatch_events(
    events: list[KolIntelEvent], intel: ProjectIntelligence,
) -> None:
    """Deliver the alert-worthy `events` for one project to every configured provider.

    The engine's single call-in, invoked right after it persists these events. It:
      1. no-ops when `notify_enabled` is off (events are still produced + persisted);
      2. filters by the configured rules against the already-computed `intel`;
      3. skips any (event, destination) already delivered (dedupe);
      4. delivers, recording each attempt's status/timestamp/destination/error.

    FULLY failure-isolated: a bad provider or store write is logged + recorded and the
    loop continues, so a delivery failure can NEVER interrupt the capture/analysis that
    triggered it. Never raises."""
    if not settings.notify_enabled or not events:
        return

    provider_names = list(settings.notify_providers)
    if not provider_names:
        return

    for event in events:
        try:
            if not _passes_rules(event, intel):
                continue
            title, body = _format(event, intel)
            event_key = _event_key(event)
            for name in provider_names:
                _deliver_one(name, event, event_key, title, body)
        except Exception:  # noqa: BLE001 — delivery is additive; never sink the caller
            logger.exception(
                "notification dispatch errored for %s:%s (analysis unaffected)",
                event.platform, event.account_key,
            )


def _event_key(event: KolIntelEvent) -> str:
    """Stable dedupe identity for an event: type + project + detection time. Two
    distinct updates of the same type differ by `detected_at`; a replay of the exact
    same event collides and is skipped."""
    return f"{event.platform}:{event.account_key}:{event.event_type}:{event.detected_at}"


def _deliver_one(
    name: str, event: KolIntelEvent, event_key: str, title: str, body: str,
) -> None:
    """Deliver one event to one destination, with dedupe + per-destination isolation.
    A failure is recorded and swallowed here so one bad destination never blocks the
    others (or the caller)."""
    provider = _get_provider(name)
    if provider is None:
        logger.warning("unknown notification provider %r — skipping", name)
        return

    if kol_store.was_delivered(event_key, name):
        return

    notification = Notification(
        event_key=event_key, event_type=event.event_type, platform=event.platform,
        account_key=event.account_key, project_handle=event.project_handle,
        title=title, body=body, payload=event.payload,
    )
    try:
        provider.send(notification)
    except Exception as exc:  # noqa: BLE001 — isolate a failing destination
        logger.warning(
            "notification delivery failed via %s for %s:%s: %s",
            name, event.platform, event.account_key, exc,
        )
        _record(event, event_key, name, "failed", str(exc))
        return
    _record(event, event_key, name, "sent", None)


def _record(
    event: KolIntelEvent, event_key: str, destination: str,
    status: str, error: str | None,
) -> None:
    """Persist one delivery attempt. A store failure here is itself isolated — the
    audit log must never be the thing that breaks delivery of a real alert."""
    try:
        kol_store.record_delivery(
            event_key=event_key, event_type=event.event_type, platform=event.platform,
            account_key=event.account_key, destination=destination,
            status=status, error=error, when=event.detected_at,
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to record notification delivery (%s)", destination)
