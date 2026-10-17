"""FastAPI application factory and process entry point.

    uvicorn web.app.main:create_app_from_env --factory --host 0.0.0.0 --port 8000 --no-access-log

(`--no-access-log`: the login callback carries Telegram's signed payload in its query string.)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import WebSettings, load_web_settings
from .db import open_pool, wait_for_schema
from .dependencies import AuthRequired
from .routes import (
    audit,
    auth,
    dashboard,
    insights,
    loadtest,
    payment_settings,
    payments,
    reports,
    statistics,
    support,
    tiers,
    users,
)
from .security import HostValidationMiddleware, SecurityHeadersMiddleware
from .state import APP_DIR, WebState, make_state
from .ui import render

log = logging.getLogger(__name__)

_TITLES = {
    400: "Bad request",
    403: "Forbidden",
    404: "Not found",
    413: "Too large",
    415: "Unsupported",
    429: "Too many requests",
}


def create_app(
    settings: WebSettings, *, pool: asyncpg.Pool | None = None, bot: Bot | None = None, **clocks
) -> FastAPI:
    """Build the app. Tests pass `pool` (and clocks); production lets the lifespan create it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_pool, owned_bot = None, None
        if getattr(app.state, "web", None) is None:
            owned_pool = await open_pool(settings.bot.database_url)
            await wait_for_schema(owned_pool)
            owned_bot = Bot(
                settings.bot.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
            )
            app.state.web = make_state(settings, owned_pool, owned_bot)
        try:
            yield
        finally:
            if owned_bot is not None:
                await owned_bot.session.close()
            if owned_pool is not None:
                await owned_pool.close()

    app = FastAPI(title="Admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = make_state(settings, pool, bot, **clocks) if pool is not None else None

    # Added last = outermost: the Host check runs before anything else sees the request.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(HostValidationMiddleware, domain=settings.admin_domain)

    app.mount("/static", StaticFiles(directory=Path(APP_DIR) / "static"), name="static")
    for module in (
        auth,
        dashboard,
        users,
        reports,
        payments,
        support,
        payment_settings,
        tiers,
        loadtest,
        audit,
        statistics,
        insights,
    ):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> PlainTextResponse:
        state: WebState = request.app.state.web
        await state.pool.fetchval("SELECT 1")
        return PlainTextResponse("ok")

    @app.exception_handler(AuthRequired)
    async def _auth_required(request: Request, exc: AuthRequired):
        if request.method in ("GET", "HEAD"):
            return RedirectResponse("/login", status_code=303)
        return PlainTextResponse("Forbidden", status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return _error_page(request, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad_input(request: Request, exc: RequestValidationError):
        return _error_page(request, 404)

    return app


def _error_page(request: Request, status: int):
    from .dependencies import COOKIE

    state: WebState | None = request.app.state.web
    session = state.sessions.get(request.cookies.get(COOKIE)) if state else None
    return render(
        request,
        "error.html",
        session,
        status_code=status,
        title=_TITLES.get(status, "Error"),
        text="The request could not be completed.",
    )


def create_app_from_env() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return create_app(load_web_settings(os.environ))
