"""Web admin test harness: the real FastAPI app on a fresh migrated database, driven in-process."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from bot.config import Settings
from tests.handlers.harness import FakeClock
from web.app import csrf
from web.app.auth import telegram_hash
from web.app.config import WebSettings, load_web_settings
from web.app.dependencies import COOKIE
from web.app.main import create_app

ADMIN = 9000001
OTHER_ADMIN = 9000002
DOMAIN = "admin.example.com"
TOKEN = "123456:TEST-TOKEN-not-real"
SECRET = "s" * 48


def make_settings(**extra: str) -> WebSettings:
    return load_web_settings(
        {
            "BOT_TOKEN": TOKEN,
            "ADMIN_IDS": f"{ADMIN},{OTHER_ADMIN}",
            "DATABASE_URL": "postgresql://u:p@h/d",
            "ADMIN_DOMAIN": DOMAIN,
            "ADMIN_BOT_USERNAME": "my_test_bot",
            "ADMIN_SESSION_SECRET": SECRET,
            **extra,
        }
    )


class Web:
    def __init__(self, app, client: AsyncClient, clock: FakeClock, pool) -> None:
        self.app, self.client, self.clock, self.pool = app, client, clock, pool
        self.state = app.state.web

    def signed(self, uid: int = ADMIN, *, token: str = TOKEN, **override) -> dict[str, str]:
        params = {
            "id": str(uid),
            "first_name": "Ada",
            "username": "ada",
            "auth_date": str(int(self.clock())),
        }
        params |= {k: str(v) for k, v in override.items()}
        params["hash"] = telegram_hash(params, token)
        return params

    async def login(self, uid: int = ADMIN):
        r = await self.client.get("/login/callback", params=self.signed(uid))
        assert r.status_code == 303, r.text
        return r

    @property
    def sid(self) -> str | None:
        return self.client.cookies.get(COOKIE)

    def token(self) -> str:
        return csrf.make_token(SECRET, self.sid)

    async def post(self, path: str, data: dict | None = None, *, with_csrf: bool = True, **kw):
        body = dict(data or {})
        if with_csrf:
            body.setdefault("csrf_token", self.token())
        return await self.client.post(path, data=body, **kw)

    async def audit_rows(self):
        return await self.pool.fetch("SELECT * FROM admin_actions ORDER BY id")


@pytest.fixture
async def web(app_pool):
    clock = FakeClock()
    app = create_app(make_settings(), pool=app_pool, wall_clock=clock, mono_clock=clock)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"https://{DOMAIN}", follow_redirects=False
    ) as client:
        yield Web(app, client, clock, app_pool)


@pytest.fixture
async def admin(web: Web) -> Web:
    """A web harness that is already logged in as ADMIN."""
    await web.login()
    return web


async def file_report(pool, reporter, reported, *, reason="spam", status="open", context=None):
    return await pool.fetchval(
        "INSERT INTO reports (reporter_id, reported_id, source, reason, status, context, "
        "context_expires_at) VALUES ($1, $2, 'chat', $3, $4, $5::jsonb, now() + interval '1 day') "
        "RETURNING id",
        reporter,
        reported,
        reason,
        status,
        json.dumps(context) if context is not None else None,
    )


__all__ = ["ADMIN", "DOMAIN", "OTHER_ADMIN", "Settings", "Web", "file_report", "make_settings"]
