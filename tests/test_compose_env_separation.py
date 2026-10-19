"""The production stack and the load-test stack must never share an env file.

`--env-file` only feeds Compose's own ${...} interpolation; the containers' environment comes from
each service's `env_file:`. It therefore has to follow ENV_FILE (default `.env`), otherwise a
load-test stack silently receives production credentials. Dummy values only; nothing real.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from bot.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
PROD_ADMINS = "ADMIN_IDS=111\n"
PROD = (
    "POSTGRES_USER=dating_bot\nPOSTGRES_PASSWORD=dummy-prod\nPOSTGRES_DB=dating_bot\n"
    "BOT_TOKEN=1:dummy\n" + PROD_ADMINS
)
LOADTEST = (
    "POSTGRES_USER=dating_loadtest\nPOSTGRES_PASSWORD=dummy-lt\nPOSTGRES_DB=dating_loadtest\n"
    "BOT_TOKEN=2:dummy\nADMIN_WEB_PORT=8001\nADMIN_IDS=6655948687\n"
)


def test_every_service_env_file_follows_ENV_FILE_and_defaults_to_dot_env():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, svc in services.items():
        if "env_file" in svc:
            assert svc["env_file"] == "${ENV_FILE:-.env}", name


@pytest.fixture
def project(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)  # noqa: S607
    if probe.returncode != 0:
        pytest.skip("docker compose plugin not installed")
    (tmp_path / "docker-compose.yml").write_text((ROOT / "docker-compose.yml").read_text())
    (tmp_path / ".env").write_text(PROD)
    (tmp_path / ".env.loadtest").write_text(LOADTEST)
    return tmp_path


def resolve(project, *args, env=None):
    out = subprocess.run(  # noqa: S603
        ["docker", "compose", *args, "config", "--format", "json"],  # noqa: S607
        cwd=project, capture_output=True, text=True, check=True, env=env,
    )  # fmt: skip
    return json.loads(out.stdout)["services"]


def clean_env():
    import os

    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("POSTGRES_", "ENV_FILE", "ADMIN_WEB"))
    }


def test_production_is_unchanged_it_reads_dot_env_and_port_8000(project):
    web = resolve(project, "--profile", "web", env=clean_env())["web"]
    assert web["environment"]["POSTGRES_USER"] == "dating_bot"
    assert web["environment"]["POSTGRES_DB"] == "dating_bot"
    assert web["environment"]["POSTGRES_HOST"] == "db"
    assert web["environment"]["ADMIN_IDS"] == "111"
    port = web["ports"][0]
    assert (port["host_ip"], port["published"], port["target"]) == ("127.0.0.1", "8000", 8000)
    assert "loadtest-worker" not in resolve(project, "--profile", "web", env=clean_env())


@pytest.mark.parametrize("how", ["inside_file", "shell_prefix"])
def test_the_load_test_stack_reads_its_own_env_file_and_port_8001(project, how):
    env = clean_env()
    if how == "inside_file":
        (project / ".env.loadtest").write_text(LOADTEST + "ENV_FILE=.env.loadtest\n")
    else:
        env["ENV_FILE"] = ".env.loadtest"
    args = [
        "-p",
        "dating-bot-loadtest",
        "--env-file",
        ".env.loadtest",
        "--profile",
        "web",
        "--profile",
        "loadtest",
    ]
    services = resolve(project, *args, env=env)
    for name in ("web", "bot", "loadtest-worker"):
        e = services[name]["environment"]
        assert (e["POSTGRES_USER"], e["POSTGRES_DB"]) == ("dating_loadtest", "dating_loadtest"), (
            name
        )
        assert e["POSTGRES_HOST"] == "db" and e["BOT_TOKEN"] == "2:dummy"
        assert e["ADMIN_IDS"] == "6655948687", name  # not overridden by the compose file
        assert "dummy-prod" not in json.dumps(services[name]), name
    port = services["web"]["ports"][0]
    assert (port["host_ip"], port["published"]) == ("127.0.0.1", "8001")
    # the URL the app builds from these variables targets the testing database on the db service
    e = services["web"]["environment"]
    url = load_settings({k: v for k, v in e.items() if isinstance(v, str)}).database_url
    assert re.fullmatch(r"postgresql://dating_loadtest:dummy-lt@db:5432/dating_loadtest", url)


def test_no_service_overrides_a_variable_the_env_file_provides():
    """`environment:` beats env_file, so it may only hold values compose itself must dictate."""
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, svc in services.items():
        if "env_file" in svc:
            # ALLOW_NEWER_SCHEMA is driven by the release script's shell, never by an env file
            # (it is only for an application rollback); see test_the_env_examples_do_not_set_it.
            assert set(svc.get("environment", {})) <= {"POSTGRES_HOST", "ALLOW_NEWER_SCHEMA"}, name


def test_the_env_examples_do_not_set_the_rollback_switch():
    for name in (".env.example", ".env.loadtest.example"):
        assert "ALLOW_NEWER_SCHEMA" not in (ROOT / name).read_text(), name


@pytest.mark.parametrize("stack", ["production", "loadtest"])
def test_database_url_and_admin_ids_from_the_env_file_reach_bot_web_and_worker(project, stack):
    url = "postgresql://u:p@db:5432/some_db"
    env = clean_env()
    if stack == "production":
        (project / ".env").write_text(PROD + f"DATABASE_URL={url}\n")
        args = ["--profile", "web", "--profile", "loadtest"]
        admins = "111"
    else:
        (project / ".env.loadtest").write_text(
            LOADTEST + f"DATABASE_URL={url}\nENV_FILE=.env.loadtest\n"
        )
        args = [
            "-p",
            "dating-bot-loadtest",
            "--env-file",
            ".env.loadtest",
            "--profile",
            "web",
            "--profile",
            "loadtest",
        ]
        admins = "6655948687"
    services = resolve(project, *args, env=env)
    for name in ("web", "bot", "loadtest-worker"):
        e = services[name]["environment"]
        assert e["DATABASE_URL"] == url, name  # never blanked by the compose file
        assert e["ADMIN_IDS"] == admins, name


def test_without_a_database_url_in_the_env_file_none_is_injected(project):
    services = resolve(project, "--profile", "web", env=clean_env())
    assert all("DATABASE_URL" not in services[n]["environment"] for n in ("web", "bot"))
