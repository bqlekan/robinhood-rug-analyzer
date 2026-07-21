"""Deployment readiness: the /health liveness probe.

Added for the deployment phase (Render `healthCheckPath: /health`, load balancers,
uptime monitors). Verifies the probe is dependency-free and, critically, that it is
NOT shadowed by the catch-all `/` StaticFiles mount — the one ordering bug that would
make it silently serve index.html instead of the JSON body.
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_health_returns_ok_json():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == settings.app_name
    assert body["version"] == settings.app_version


def test_health_is_not_shadowed_by_static_mount():
    # The static frontend is mounted at "/". A regression that registered it before
    # the health route would return HTML here; assert we still get JSON.
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.headers["content-type"].startswith("application/json")
