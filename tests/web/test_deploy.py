"""Docker / Caddy wiring (static checks) and the real uvicorn process against a real database."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from tests.web.conftest import DOMAIN, SECRET, TOKEN

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]


def test_project_name_and_existing_services_are_unchanged():
    assert COMPOSE["name"] == "dating-bot"  # `-p dating-bot-v2` on the VPS still overrides it
    assert {"db", "bot"} <= SERVICES.keys()
    assert "profiles" not in SERVICES["bot"] and "profiles" not in SERVICES["db"]
    assert SERVICES["bot"]["build"]["target"] == "bot"
    assert COMPOSE["volumes"].keys() >= {"pgdata"}  # the database volume is untouched


def test_exactly_one_postgres_and_it_is_not_published():
    postgres = [n for n, s in SERVICES.items() if str(s.get("image", "")).startswith("postgres")]
    assert postgres == ["db"]
    assert "ports" not in SERVICES["db"] and "expose" not in SERVICES["db"]


def test_only_caddy_publishes_ports_and_only_80_443():
    published = {n: s["ports"] for n, s in SERVICES.items() if "ports" in s}
    assert list(published) == ["caddy"]
    targets = sorted(p.split(":")[-1] for p in published["caddy"])
    assert targets == ["443", "80"]


def test_web_and_caddy_are_opt_in_and_share_the_internal_network_with_the_database():
    for name in ("web", "caddy"):
        assert SERVICES[name]["profiles"] == ["web"]
    for name in ("db", "bot", "web", "caddy"):
        assert "internal" in SERVICES[name]["networks"]
    assert SERVICES["web"]["build"]["target"] == "web"
    assert SERVICES["web"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert SERVICES["caddy"]["depends_on"]["web"]["condition"] == "service_healthy"


def test_no_secret_values_are_committed_in_compose_or_caddyfile():
    text = (ROOT / "docker-compose.yml").read_text() + (ROOT / "deploy/Caddyfile").read_text()
    for needle in ("BOT_TOKEN=", "password:", "SESSION_SECRET=", "nxt-server.cloud"):
        assert needle not in text
    example = (ROOT / ".env.example").read_text()
    assert "ADMIN_SESSION_SECRET=\n" in example and "ADMIN_BOT_USERNAME=\n" in example


def test_caddyfile_uses_the_configured_domain_and_proxies_to_the_internal_service():
    caddyfile = (ROOT / "deploy/Caddyfile").read_text()
    assert "{$ADMIN_DOMAIN} {" in caddyfile
    assert "reverse_proxy web:8000" in caddyfile
    assert "header_up X-Forwarded-For {remote_host}" in caddyfile  # replaced, not appended
    code = [ln for ln in caddyfile.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("http://" in ln or "auto_https" in ln for ln in code)  # auto HTTPS stays on
    assert SERVICES["caddy"]["environment"]["ADMIN_DOMAIN"].startswith("${ADMIN_DOMAIN")


def test_dockerfile_builds_both_images_as_an_unprivileged_user():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "AS web" in dockerfile and "AS bot" in dockerfile
    assert dockerfile.rindex("AS bot") > dockerfile.rindex("AS web")  # bot stays the default
    assert dockerfile.count("USER bot") == 2
    assert "--no-access-log" in dockerfile and "--workers" not in dockerfile
    assert "COPY web ./web" in dockerfile


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
@pytest.mark.parametrize("profile", [[], ["--profile", "web"]])
def test_compose_config_is_valid(tmp_path, profile):
    (tmp_path / "docker-compose.yml").write_text((ROOT / "docker-compose.yml").read_text())
    (tmp_path / ".env").write_text(
        "POSTGRES_USER=u\nPOSTGRES_PASSWORD=p\nPOSTGRES_DB=d\nBOT_TOKEN=1:x\nADMIN_DOMAIN=a.example.com\n"
    )
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", *profile, "config", "-q"],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_default_compose_needs_no_web_variables(tmp_path):
    """Deploying the bot alone must not demand ADMIN_* settings."""
    (tmp_path / "docker-compose.yml").write_text((ROOT / "docker-compose.yml").read_text())
    (tmp_path / ".env").write_text(
        "POSTGRES_USER=u\nPOSTGRES_PASSWORD=p\nPOSTGRES_DB=d\nBOT_TOKEN=1:x\n"
    )
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "config", "--services"],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0 and result.stdout.split() == ["db", "bot"], result.stderr


# ------------------------------------------------------- the real process
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str, host: str) -> tuple[int, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=3) as r:  # noqa: S310
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


async def test_uvicorn_process_serves_health_with_the_database_and_enforces_host(app_pool, dsn):
    await asyncio.to_thread(_exercise_real_process, dsn)


def _exercise_real_process(dsn: str) -> None:
    port = _free_port()
    env = {
        **os.environ,
        "BOT_TOKEN": TOKEN,
        "ADMIN_IDS": "9000001",
        "DATABASE_URL": dsn,
        "ADMIN_DOMAIN": DOMAIN,
        "ADMIN_BOT_USERNAME": "my_test_bot",
        "ADMIN_SESSION_SECRET": SECRET,
        "WEB_PORT": str(port),
    }
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "web.app.main:create_app_from_env",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                status, body = _get(port, "/healthz", DOMAIN)
                break
            except OSError:
                assert time.monotonic() < deadline and proc.poll() is None, "web did not start"
                time.sleep(0.3)
        assert (status, body) == (200, "ok")
        assert _get(port, "/healthz", "evil.example")[0] == 400
        assert _get(port, "/login", DOMAIN)[0] == 200
        health = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "web.health"], cwd=ROOT, env=env, timeout=20, check=False
        )
        assert health.returncode == 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)
