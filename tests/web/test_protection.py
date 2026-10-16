"""CSRF, Host validation, security headers, login rate limiting."""

import pytest
from httpx import ASGITransport, AsyncClient

from tests.factories import make_user
from tests.web.conftest import DOMAIN, make_settings
from web.app.config import ConfigError
from web.app.main import create_app


# ---------------------------------------------------------------- CSRF
async def test_post_without_csrf_token_is_rejected_and_changes_nothing(admin):
    await make_user(admin.pool, 1)
    r = await admin.post("/admin/users/1/ban", with_csrf=False)
    assert r.status_code == 403
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "active"


@pytest.mark.parametrize("bad", ["", "nope", "0" * 64, "A" * 500])
async def test_invalid_csrf_token_is_rejected(admin, bad):
    await make_user(admin.pool, 1)
    r = await admin.client.post("/admin/users/1/ban", data={"csrf_token": bad})
    assert r.status_code == 403
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "active"


async def test_csrf_token_of_another_session_is_rejected(admin):
    stolen = admin.token()
    await admin.post("/logout")
    admin.clock.advance(1)  # a new auth_date, otherwise the login payload would be a replay
    await admin.login()
    r = await admin.client.post("/admin/users/1/ban", data={"csrf_token": stolen})
    assert r.status_code == 403


async def test_valid_csrf_token_is_accepted(admin):
    await make_user(admin.pool, 1)
    r = await admin.post("/admin/users/1/ban")
    assert r.status_code == 303
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "banned"


async def test_csrf_token_in_query_string_does_not_count(admin):
    await make_user(admin.pool, 1)
    r = await admin.client.post(f"/admin/users/1/ban?csrf_token={admin.token()}")
    assert r.status_code in (403, 415)


async def test_foreign_origin_is_rejected_even_with_a_valid_token(admin):
    await make_user(admin.pool, 1)
    r = await admin.post("/admin/users/1/ban", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await admin.post("/admin/users/1/ban", headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403


async def test_own_origin_is_accepted(admin):
    await make_user(admin.pool, 1)
    r = await admin.post("/admin/users/1/ban", headers={"Origin": f"https://{DOMAIN}"})
    assert r.status_code == 303


async def test_non_form_content_type_is_rejected(admin):
    r = await admin.client.post(
        "/admin/users/1/ban",
        content=b'{"csrf_token":"x"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 415


async def test_oversized_form_is_rejected(admin):
    r = await admin.client.post(
        "/admin/users/1/ban", data={"csrf_token": admin.token(), "note": "x" * 20000}
    )
    assert r.status_code == 413


async def test_get_requests_never_change_state(admin):
    await make_user(admin.pool, 1)
    await make_user(admin.pool, 2)
    rid = await admin.pool.fetchval(
        "INSERT INTO reports (reporter_id, reported_id, source, reason) "
        "VALUES (2, 1, 'chat', 'spam') RETURNING id"
    )
    for path in (
        "/admin/users/1",
        "/admin/users/1/ban",
        f"/admin/reports/{rid}",
        f"/admin/reports/{rid}/ban",
        "/admin/users/1/unban",
        f"/admin/reports/{rid}/resolve",
    ):
        await admin.client.get(path)
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "active"
    assert await admin.pool.fetchval("SELECT status FROM reports") == "open"
    assert await admin.audit_rows() == []


# ---------------------------------------------------------------- Host
async def test_configured_domain_is_served(web):
    assert (await web.client.get("/login")).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "admin.example.com.evil.example",
        "xadmin.example.com",
        "1.2.3.4",
        "localhost",
    ],
)
async def test_other_hosts_are_rejected(web, host):
    for path in ("/login", "/admin/", "/healthz", "/static/style.css"):
        r = await web.client.get(path, headers={"Host": host})
        assert r.status_code == 400 and r.text == "Invalid host"


async def test_forwarded_host_headers_cannot_override_the_check(web):
    r = await web.client.get("/login", headers={"Host": "evil.example", "X-Forwarded-Host": DOMAIN})
    assert r.status_code == 400


async def test_domain_match_is_case_insensitive_and_port_tolerant(web):
    assert (await web.client.get("/login", headers={"Host": DOMAIN.upper()})).status_code == 200
    assert (await web.client.get("/login", headers={"Host": f"{DOMAIN}:443"})).status_code == 200
    assert (await web.client.get("/login", headers={"Host": f"{DOMAIN}:x"})).status_code == 400


async def test_domain_comes_from_configuration(app_pool):
    app = create_app(make_settings(ADMIN_DOMAIN="panel.other.org"), pool=app_pool)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://panel.other.org"
    ) as c:
        assert (await c.get("/login")).status_code == 200
        assert (await c.get("/login", headers={"Host": DOMAIN})).status_code == 400


# ---------------------------------------------------------------- config
@pytest.mark.parametrize(
    "extra",
    [
        {"ADMIN_DOMAIN": ""},
        {"ADMIN_DOMAIN": "https://admin.example.com"},
        {"ADMIN_DOMAIN": "admin.example.com/path"},
        {"ADMIN_DOMAIN": "nodots"},
        {"ADMIN_SESSION_SECRET": "short"},
        {"ADMIN_BOT_USERNAME": "x"},
        {"ADMIN_IDS": ""},
    ],
)
def test_bad_configuration_is_refused(extra):
    with pytest.raises(ConfigError):
        make_settings(**extra)


def test_settings_repr_hides_secrets():
    text = repr(make_settings())
    assert "TEST-TOKEN" not in text and "sssss" not in text


# ---------------------------------------------------------------- headers
async def test_security_headers_on_pages_and_errors(admin):
    for path in ("/admin/", "/admin/nope", "/static/style.css"):
        r = await admin.client.get(path)
        h = r.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "strict-transport-security" in h


async def test_admin_pages_have_no_script_and_are_not_cached(admin):
    r = await admin.client.get("/admin/users")
    csp = r.headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    assert "<script" not in r.text


async def test_only_the_login_page_may_load_the_telegram_widget(web):
    csp = (await web.client.get("/login")).headers["content-security-policy"]
    assert (
        "script-src https://telegram.org" in csp and "frame-src https://oauth.telegram.org" in csp
    )


async def test_api_docs_are_not_exposed(admin):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await admin.client.get(path)).status_code == 404


# ---------------------------------------------------------------- login rate limit
async def test_repeated_login_attempts_are_throttled(web):
    codes = [
        (await web.client.get("/login/callback", params={"id": "1"})).status_code for _ in range(14)
    ]
    assert codes[:10] == [403] * 10
    assert set(codes[10:]) == {429}


async def test_a_throttled_client_cannot_log_in_even_with_a_valid_payload(web):
    for _ in range(10):
        await web.client.get("/login/callback", params={"id": "1"})
    r = await web.client.get("/login/callback", params=web.signed())
    assert r.status_code == 429 and web.sid is None


async def test_throttle_window_expires(web):
    for _ in range(11):
        await web.client.get("/login/callback", params={"id": "1"})
    web.clock.advance(301)
    assert (await web.client.get("/login/callback", params=web.signed())).status_code == 303


async def test_throttle_is_per_client_but_has_a_global_cap(web):
    def from_ip(ip):
        return web.client.get(
            "/login/callback", params={"id": "1"}, headers={"X-Forwarded-For": ip}
        )

    for _ in range(10):
        await from_ip("203.0.113.5")
    assert (await from_ip("203.0.113.5")).status_code == 429
    assert (await from_ip("203.0.113.6")).status_code == 403  # another client is unaffected
    for n in range(7, 7 + 60):
        await from_ip(f"203.0.113.{n}")
    assert (await from_ip("198.51.100.1")).status_code == 429  # global cap


async def test_spoofed_forwarded_for_prefix_does_not_reset_the_limit(web):
    # The proxy appends the real address last; a client-sent prefix must not matter.
    for i in range(10):
        await web.client.get(
            "/login/callback",
            params={"id": "1"},
            headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"},
        )
    r = await web.client.get(
        "/login/callback", params={"id": "1"}, headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.9"}
    )
    assert r.status_code == 429


async def test_forwarded_for_is_ignored_when_the_direct_peer_is_public(app_pool):
    """If the port were ever reachable directly, a public peer must not choose its own address."""
    from web.app.dependencies import client_ip

    class Req:
        def __init__(self, peer, xff):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": xff}

    assert client_ip(Req("172.18.0.1", "203.0.113.9"), True) == "203.0.113.9"
    assert client_ip(Req("127.0.0.1", "203.0.113.9"), True) == "203.0.113.9"
    assert client_ip(Req("8.8.8.8", "203.0.113.9"), True) == "8.8.8.8"
    assert client_ip(Req("172.18.0.1", "not-an-ip"), True) == "172.18.0.1"
    assert client_ip(Req("172.18.0.1", "203.0.113.9"), False) == "172.18.0.1"
