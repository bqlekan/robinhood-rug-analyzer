"""M26: notification transport layer — WebhookProvider, TelegramProvider,
DiscordWebhookProvider, plus the shared retry/backoff in the dispatcher.

Fully offline. `httpx.Client` is stubbed with a fake that records the request and
returns a configurable status, so no test touches the network. These tests own
only the transport concerns (payload shape, headers, HMAC signature, self-skip on
missing config, raise-on-non-2xx, retry/backoff, failure isolation across
providers). The M23-H dispatch/dedupe/rule surface is covered by
`tests/test_notifications.py` and is not re-tested here.
"""

import hashlib
import hmac
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.models.kol import ClusterInfo, KolIntelEvent, ProjectIntelligence
from app.services import kol_store, notifications


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _temp_db():
    tmp = Path(tempfile.mkdtemp()) / "kol.db"
    kol_store.reset_for_tests(str(tmp))
    yield
    kol_store.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_providers():
    notifications.reset_for_tests()
    yield
    notifications.reset_for_tests()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Retry backoff must never actually sleep in tests.
    monkeypatch.setattr(notifications.time, "sleep", lambda _s: None)


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad status", request=None, response=None)


class FakeClient:
    """Stand-in for httpx.Client: records every POST and returns a scripted status.
    Instances share a class-level log so a test can inspect calls after the
    `with` block closes."""

    calls: list[dict] = []
    status_code = 200
    raise_transport = False

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, *, json=None, content=None, headers=None):
        FakeClient.calls.append({"url": url, "json": json, "content": content,
                                 "headers": headers or {}})
        if FakeClient.raise_transport:
            raise httpx.ConnectError("boom")
        return FakeResponse(FakeClient.status_code)


@pytest.fixture(autouse=True)
def _fake_httpx(monkeypatch):
    FakeClient.calls = []
    FakeClient.status_code = 200
    FakeClient.raise_transport = False
    monkeypatch.setattr(notifications.httpx, "Client", FakeClient)


# --- helpers -----------------------------------------------------------------


def _notification():
    return notifications.Notification(
        event_key="x:proj:kol_cluster_detected:2024-06-01T12:00:00+00:00",
        event_type="kol_cluster_detected", platform="x", account_key="proj",
        project_handle="proj", title="kol_cluster_detected · proj",
        body="score=80 confidence=high kols=3", payload={"score": 80},
    )


def make_event(event_type="kol_cluster_detected", detected_at="2024-06-01T12:00:00+00:00"):
    return KolIntelEvent(
        event_type=event_type, platform="x", account_key="proj",
        project_handle="proj", detected_at=detected_at, payload={"kol_count": 3},
    )


def make_intel():
    cluster = ClusterInfo(
        platform="x", account_key="proj", project_handle="proj",
        is_cluster=True, cluster_types=["tier_1"], kol_count=3,
    )
    return ProjectIntelligence(
        platform="x", account_key="proj", project_handle="proj",
        score=80, confidence="high", kol_count=3, cluster=cluster,
    )


# --- WebhookProvider ---------------------------------------------------------


def test_webhook_posts_json_payload(monkeypatch):
    monkeypatch.setattr(settings, "notify_webhook_url", "https://hook.example/x")
    monkeypatch.setattr(settings, "notify_webhook_headers", {"X-Custom": "1"})
    monkeypatch.setattr(settings, "notify_webhook_secret", "")

    notifications.WebhookProvider().send(_notification())

    assert len(FakeClient.calls) == 1
    call = FakeClient.calls[0]
    assert call["url"] == "https://hook.example/x"
    assert call["json"]["event_type"] == "kol_cluster_detected"
    assert call["json"]["title"].startswith("kol_cluster_detected")
    assert call["headers"]["X-Custom"] == "1"


def test_webhook_signs_body_with_hmac(monkeypatch):
    monkeypatch.setattr(settings, "notify_webhook_url", "https://hook.example/x")
    monkeypatch.setattr(settings, "notify_webhook_headers", {})
    monkeypatch.setattr(settings, "notify_webhook_secret", "s3cr3t")
    monkeypatch.setattr(settings, "notify_webhook_signature_header", "X-Signature-256")

    notifications.WebhookProvider().send(_notification())

    call = FakeClient.calls[0]
    # Signed path sends raw bytes via `content`, not `json`.
    assert call["content"] is not None and call["json"] is None
    expected = hmac.new(b"s3cr3t", call["content"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Signature-256"] == expected
    # The signed body is valid JSON with the notification fields.
    body = json.loads(call["content"])
    assert body["account_key"] == "proj"


def test_webhook_self_skips_when_url_missing(monkeypatch):
    monkeypatch.setattr(settings, "notify_webhook_url", "")
    with pytest.raises(ValueError):
        notifications.WebhookProvider().send(_notification())
    assert FakeClient.calls == []  # never hit the network


def test_webhook_raises_on_non_2xx(monkeypatch):
    monkeypatch.setattr(settings, "notify_webhook_url", "https://hook.example/x")
    monkeypatch.setattr(settings, "notify_webhook_secret", "")
    FakeClient.status_code = 500
    with pytest.raises(httpx.HTTPStatusError):
        notifications.WebhookProvider().send(_notification())


# --- TelegramProvider --------------------------------------------------------


def test_telegram_posts_markdown_message(monkeypatch):
    monkeypatch.setattr(settings, "notify_telegram_bot_token", "123:ABC")
    monkeypatch.setattr(settings, "notify_telegram_chat_id", "42")

    notifications.TelegramProvider().send(_notification())

    call = FakeClient.calls[0]
    assert call["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert call["json"]["chat_id"] == "42"
    assert call["json"]["parse_mode"] == "Markdown"
    assert "kol_cluster_detected" in call["json"]["text"]


def test_telegram_self_skips_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "notify_telegram_bot_token", "")
    monkeypatch.setattr(settings, "notify_telegram_chat_id", "")
    with pytest.raises(ValueError):
        notifications.TelegramProvider().send(_notification())
    assert FakeClient.calls == []


# --- DiscordWebhookProvider --------------------------------------------------


def test_discord_posts_embed(monkeypatch):
    monkeypatch.setattr(settings, "notify_discord_webhook_url", "https://discord/wh")

    notifications.DiscordWebhookProvider().send(_notification())

    call = FakeClient.calls[0]
    assert call["url"] == "https://discord/wh"
    embed = call["json"]["embeds"][0]
    assert embed["title"].startswith("kol_cluster_detected")
    assert embed["description"] == "score=80 confidence=high kols=3"
    field_names = {f["name"] for f in embed["fields"]}
    assert {"event", "account"} <= field_names


def test_discord_self_skips_when_url_missing(monkeypatch):
    monkeypatch.setattr(settings, "notify_discord_webhook_url", "")
    with pytest.raises(ValueError):
        notifications.DiscordWebhookProvider().send(_notification())
    assert FakeClient.calls == []


# --- retry / backoff (via the dispatcher) ------------------------------------


@pytest.fixture
def _enabled_webhook(monkeypatch):
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "notify_providers", ["webhook"])
    monkeypatch.setattr(settings, "notify_min_score", 0)
    monkeypatch.setattr(settings, "notify_min_confidence", "very_low")
    monkeypatch.setattr(settings, "notify_min_cluster_size", 0)
    monkeypatch.setattr(settings, "notify_event_types", ["kol_cluster_detected"])
    monkeypatch.setattr(settings, "notify_webhook_url", "https://hook.example/x")
    monkeypatch.setattr(settings, "notify_webhook_secret", "")


def test_retry_exhausts_then_records_failed(monkeypatch, _enabled_webhook):
    monkeypatch.setattr(settings, "notify_retry_count", 3)
    monkeypatch.setattr(settings, "notify_retry_delay_seconds", 0.0)
    FakeClient.status_code = 500  # always fails

    notifications.dispatch_events([make_event()], make_intel())

    assert len(FakeClient.calls) == 3  # tried the full budget
    rows = kol_store.list_deliveries(destination="webhook")
    assert len(rows) == 1 and rows[0]["status"] == "failed"


def test_retry_then_success_records_single_sent(monkeypatch, _enabled_webhook):
    monkeypatch.setattr(settings, "notify_retry_count", 3)
    monkeypatch.setattr(settings, "notify_retry_delay_seconds", 0.0)

    # Fail twice, then succeed on the third attempt.
    seq = [500, 500, 200]

    def post(self, url, *, json=None, content=None, headers=None):
        FakeClient.calls.append({"url": url})
        return FakeResponse(seq[len(FakeClient.calls) - 1])

    monkeypatch.setattr(FakeClient, "post", post)

    notifications.dispatch_events([make_event()], make_intel())

    assert len(FakeClient.calls) == 3
    rows = kol_store.list_deliveries(destination="webhook")
    assert len(rows) == 1 and rows[0]["status"] == "sent"


def test_no_retry_when_count_is_one(monkeypatch, _enabled_webhook):
    monkeypatch.setattr(settings, "notify_retry_count", 1)
    FakeClient.status_code = 500

    notifications.dispatch_events([make_event()], make_intel())
    assert len(FakeClient.calls) == 1  # single attempt, no retry


# --- failure isolation across providers --------------------------------------


def test_one_transport_failure_does_not_block_others(monkeypatch):
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "notify_providers", ["webhook", "memory"])
    monkeypatch.setattr(settings, "notify_min_score", 0)
    monkeypatch.setattr(settings, "notify_min_confidence", "very_low")
    monkeypatch.setattr(settings, "notify_min_cluster_size", 0)
    monkeypatch.setattr(settings, "notify_event_types", ["kol_cluster_detected"])
    monkeypatch.setattr(settings, "notify_webhook_url", "https://hook.example/x")
    monkeypatch.setattr(settings, "notify_webhook_secret", "")
    monkeypatch.setattr(settings, "notify_retry_count", 1)
    FakeClient.raise_transport = True  # webhook always errors

    notifications.dispatch_events([make_event()], make_intel())

    statuses = {r["destination"]: r["status"] for r in kol_store.list_deliveries()}
    assert statuses == {"webhook": "failed", "memory": "sent"}


def test_disabled_notifications_do_no_http(monkeypatch):
    monkeypatch.setattr(settings, "notify_enabled", False)
    monkeypatch.setattr(settings, "notify_providers", ["webhook"])
    monkeypatch.setattr(settings, "notify_webhook_url", "https://hook.example/x")

    notifications.dispatch_events([make_event()], make_intel())
    assert FakeClient.calls == []  # zero overhead when disabled
