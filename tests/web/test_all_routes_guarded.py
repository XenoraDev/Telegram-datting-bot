"""Every route the app registers is guarded: found by enumeration, so a new route can not be
forgotten. POST needs a session AND a CSRF token AND our own origin; admin GETs need a session."""

import re

import pytest

PUBLIC = {"/login", "/login/callback", "/healthz", "/static/{path:path}"}
SAMPLE = {"pid": "1", "plan_id": "1", "tier": "premium", "uid": "1", "rid": "1", "report_id": "1"}


def _fill(path: str) -> str:
    return re.sub(r"\{(\w+)(:[^}]*)?\}", lambda m: SAMPLE.get(m.group(1), "1"), path)


def routes(app):
    out = []
    flat = []
    for top in app.routes:
        flat.extend(top.original_router.routes if hasattr(top, "original_router") else [top])
    for r in flat:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if path in PUBLIC or path.startswith(("/docs", "/redoc", "/openapi")):
            continue
        out.append((path, sorted(m for m in methods if m in ("GET", "POST"))))
    return out


async def test_the_enumeration_finds_the_new_routes(web):
    paths = {p for p, _ in routes(web.app)}
    assert {"/admin/tiers", "/admin/tiers/{tier}", "/admin/payments/{pid}/approve"} <= paths
    assert {
        "/admin/loadtest",
        "/admin/loadtest/review",
        "/admin/loadtest/confirmation",
        "/admin/loadtest/{rid}/stop",
        "/admin/loadtest/{rid}/export.{kind}",
        "/admin/loadtest/compare",
        "/admin/loadtest/thresholds",
    } <= paths


async def test_every_post_route_rejects_a_request_without_csrf_and_without_session(admin, web):
    for path, methods in routes(web.app):
        if "POST" not in methods:
            continue
        url = _fill(path)
        r = await admin.client.post(url, data={})  # logged in, but no token
        assert r.status_code in (403, 404, 405, 422), (path, r.status_code)
        assert r.status_code != 303 or "msg=" not in r.headers.get("location", ""), path
    for path, methods in routes(web.app):
        if "POST" not in methods:
            continue
        r = await web.client.post(_fill(path), data={})  # not even logged in
        assert r.status_code in (303, 307, 401, 403, 404, 405, 422), (path, r.status_code)
        assert not (200 <= r.status_code < 300), path


async def test_every_post_route_rejects_a_foreign_origin_even_with_a_valid_token(admin, web):
    for path, methods in routes(web.app):
        if "POST" not in methods or path == "/logout":
            continue
        r = await admin.post(_fill(path), {}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, (path, r.status_code)


@pytest.mark.parametrize("method", ["get"])
async def test_every_admin_page_needs_a_session(web, method):
    for path, methods in routes(web.app):
        if "GET" not in methods or not path.startswith("/admin"):
            continue
        r = await web.client.get(_fill(path))
        assert r.status_code in (303, 307, 401, 403, 404), (path, r.status_code)
        assert "Bot admin" not in r.text or "Log in" in r.text, path
