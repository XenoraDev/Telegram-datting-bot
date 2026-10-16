"""Authentication: Telegram payload verification, sessions, authorization of every route."""

import pytest

from tests.web.conftest import ADMIN, OTHER_ADMIN, TOKEN
from web.app.dependencies import COOKIE

GET_ROUTES = [
    "/admin/",
    "/admin/users",
    "/admin/users/1",
    "/admin/users/1/ban",
    "/admin/reports",
    "/admin/reports/1",
    "/admin/reports/1/ban",
    "/admin/audit",
    "/admin/statistics",
]
POST_ROUTES = [
    "/logout",
    "/admin/users/1/ban",
    "/admin/users/1/unban",
    "/admin/reports/1/resolve",
    "/admin/reports/1/dismiss",
    "/admin/reports/1/ban",
]


async def test_login_page_shows_the_widget_without_secrets(web):
    r = await web.client.get("/login")
    assert r.status_code == 200
    assert 'data-telegram-login="my_test_bot"' in r.text
    assert TOKEN not in r.text and "ADMIN_SESSION" not in r.text


async def test_authorised_admin_is_logged_in_with_a_hardened_cookie(web):
    r = await web.client.get("/login/callback", params=web.signed(ADMIN))
    assert r.status_code == 303 and r.headers["location"] == "/admin/"
    cookie = r.headers["set-cookie"].lower()
    assert COOKIE.lower() in cookie
    for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age="):
        assert flag in cookie
    assert "domain=" not in cookie  # __Host- prefix: bound to this exact host
    assert (await web.client.get("/admin/")).status_code == 200


async def test_the_cookie_is_an_opaque_random_handle(web):
    await web.login()
    assert web.sid is not None and len(web.sid) >= 40
    assert str(ADMIN) not in web.sid


async def test_second_admin_is_accepted_too(web):
    await web.login(OTHER_ADMIN)
    assert (await web.client.get("/admin/")).status_code == 200


@pytest.mark.parametrize("uid", [111, 0, 9000003])
async def test_non_admin_telegram_user_is_rejected(web, uid):
    r = await web.client.get("/login/callback", params=web.signed(uid))
    assert r.status_code == 403 and web.sid is None
    assert (await web.client.get("/admin/")).status_code == 303


async def test_invalid_hash_is_rejected(web):
    p = web.signed()
    p["hash"] = "0" * 64
    assert (await web.client.get("/login/callback", params=p)).status_code == 403
    assert web.sid is None


async def test_payload_signed_with_another_token_is_rejected(web):
    p = web.signed(token="999:other-token")
    assert (await web.client.get("/login/callback", params=p)).status_code == 403


@pytest.mark.parametrize(
    "tamper",
    [{"id": str(OTHER_ADMIN)}, {"auth_date": "1000001"}, {"username": "evil"}],
)
async def test_tampered_payload_is_rejected(web, tamper):
    p = web.signed(ADMIN) | tamper  # hash no longer matches
    assert (await web.client.get("/login/callback", params=p)).status_code == 403


async def test_extra_unsigned_field_is_rejected(web):
    p = web.signed() | {"is_admin": "1"}
    assert (await web.client.get("/login/callback", params=p)).status_code == 403


@pytest.mark.parametrize("params", [{}, {"id": str(ADMIN)}, {"hash": "a" * 64}])
async def test_missing_fields_are_rejected(web, params):
    assert (await web.client.get("/login/callback", params=params)).status_code == 403


async def test_expired_login_is_rejected(web):
    p = web.signed()
    web.clock.advance(301)
    assert (await web.client.get("/login/callback", params=p)).status_code == 403


async def test_login_just_inside_the_window_works(web):
    p = web.signed()
    web.clock.advance(299)
    assert (await web.client.get("/login/callback", params=p)).status_code == 303


async def test_future_dated_login_is_rejected(web):
    p = web.signed(auth_date=int(web.clock()) + 3600)
    assert (await web.client.get("/login/callback", params=p)).status_code == 403


async def test_a_login_payload_cannot_be_replayed(web):
    p = web.signed()
    assert (await web.client.get("/login/callback", params=p)).status_code == 303
    web.client.cookies.clear()
    assert (await web.client.get("/login/callback", params=p)).status_code == 403
    assert web.sid is None


async def test_failures_do_not_say_why(web):
    a = await web.client.get("/login/callback", params=web.signed(111))
    b = await web.client.get("/login/callback", params=web.signed(ADMIN, token="1:x"))
    assert a.text == b.text and a.status_code == b.status_code == 403


@pytest.mark.parametrize("path", GET_ROUTES)
async def test_every_protected_page_requires_a_session(web, path):
    r = await web.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", POST_ROUTES)
async def test_every_mutation_requires_a_session(web, path):
    r = await web.client.post(path, data={"csrf_token": "x"})
    assert r.status_code == 403


async def test_forged_or_unknown_cookie_is_not_a_session(web):
    web.client.cookies.set(COOKIE, "A" * 43)
    assert (await web.client.get("/admin/")).status_code == 303
    assert (await web.post("/logout", with_csrf=False)).status_code == 403


async def test_client_supplied_admin_hints_are_ignored(web):
    r = await web.client.get("/admin/?admin=1&is_admin=true", headers={"X-Admin": "1"})
    assert r.status_code == 303


async def test_logout_ends_the_session_server_side(admin):
    old = admin.sid
    r = await admin.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    admin.client.cookies.set(COOKIE, old)  # replay the old cookie
    assert (await admin.client.get("/admin/")).status_code == 303


async def test_logout_by_get_does_nothing(admin):
    assert (await admin.client.get("/logout")).status_code == 405
    assert (await admin.client.get("/admin/")).status_code == 200


async def test_session_expires(admin):
    admin.clock.advance(8 * 3600 + 1)
    assert (await admin.client.get("/admin/")).status_code == 303


async def test_idle_session_expires_but_activity_extends_it(admin):
    for _ in range(3):
        admin.clock.advance(3000)  # < 1h idle each time
        assert (await admin.client.get("/admin/")).status_code == 200
    admin.clock.advance(3601)
    assert (await admin.client.get("/admin/")).status_code == 303


async def test_removing_an_admin_from_admin_ids_kills_the_session(admin):
    admin.state.sessions._admin_ids = frozenset({OTHER_ADMIN})
    assert (await admin.client.get("/admin/")).status_code == 303


async def test_login_issues_a_fresh_session_each_time(web):
    await web.login(ADMIN)
    first = web.sid
    r = await web.client.get(
        "/login/callback", params=web.signed(ADMIN, auth_date=int(web.clock()) + 1)
    )
    assert r.status_code == 303 and web.sid != first
    web.client.cookies.set(COOKIE, first)
    assert (await web.client.get("/admin/")).status_code == 303  # the old one is gone
