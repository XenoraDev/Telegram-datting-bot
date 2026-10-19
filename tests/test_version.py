"""Release identity: read from the image's VERSION.json, strictly validated, no secrets."""

import json
import re
from pathlib import Path

import pytest
import yaml

from bot import version
from bot.version import VersionInfo, load, parse

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def write(tmp_path, data):
    f = tmp_path / "VERSION.json"
    f.write_text(data if isinstance(data, str) else json.dumps(data))
    return f


def test_a_well_formed_file_is_read(tmp_path):
    info = load(write(tmp_path, {"version": "v2.0.0", "sha": SHA, "built": "2026-10-20T01:02:03Z"}))
    assert info == VersionInfo("v2.0.0", SHA, "2026-10-20T01:02:03Z")
    assert info.short_sha == "a" * 7 and info.known
    assert info.line() == f"v2.0.0 ({'a' * 7}) built 2026-10-20T01:02:03Z"


@pytest.mark.parametrize("tag", ["v2.0.0-rc1", "v2.0.1", "v2.1.0", "v2.0.0-rc0", "dev"])
def test_release_tags_of_the_approved_scheme_are_accepted(tag):
    assert parse({"version": tag}).version == tag


@pytest.mark.parametrize(
    "bad",
    [
        "v2",
        "2.0.0",
        "v2.0.0-beta",
        "v2.0.0; rm -rf /",
        "<script>",
        "v2.0.0\n",
        "",
        "V2.0.0",
        None,
        7,
    ],
)
def test_anything_else_is_unknown_never_trusted(bad):
    assert parse({"version": bad, "sha": bad, "built": bad}) == VersionInfo()


def test_a_short_or_odd_sha_or_time_is_rejected():
    for sha in ("abc123", "A" * 40, "g" * 40, "a" * 41):
        assert parse({"sha": sha}).sha == "unknown"
    assert parse({"built": "yesterday"}).built == "unknown"


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '"x"', "null", "{"])
def test_a_broken_file_gives_the_dev_identity(tmp_path, content):
    assert load(write(tmp_path, content)) == VersionInfo()


def test_a_missing_file_gives_the_dev_identity(tmp_path):
    info = load(tmp_path / "nope.json")
    assert info == VersionInfo() and not info.known and info.short_sha == "unknown"


def test_extra_fields_are_ignored_so_nothing_secret_can_leak_through(tmp_path):
    info = load(write(tmp_path, {"version": "v2.0.0", "sha": SHA, "BOT_TOKEN": "123:secret"}))
    assert "secret" not in repr(info) and "secret" not in info.line()


def test_the_process_start_time_is_set_once():
    assert version.STARTED_AT.tzinfo is not None
    assert version.current() is version.current()


def test_this_code_declares_that_it_tolerates_a_newer_additive_schema():
    assert version.SCHEMA_TOLERANT is True


# ------------------------------------------------------------------ build wiring


def test_the_dockerfile_bakes_the_identity_in_the_last_layers_of_both_images():
    text = (ROOT / "Dockerfile").read_text()
    for stage in text.split("\nFROM ")[2:]:  # web, bot
        assert stage.count("ARG GIT_SHA") == 1 and "/app/VERSION.json" in stage
        assert "org.opencontainers.image.revision" in stage
        assert 'dating-bot.schema-tolerance="1"' in stage
        # after the dependency layers (so a new release does not rebuild pip / apt) ...
        assert stage.index("pip install") < stage.index("ARG GIT_SHA")
        assert stage.index("COPY migrations") < stage.index("ARG GIT_SHA")
        # ... and before the privileges are dropped (the file is written as root)
        assert stage.index("ARG GIT_SHA") < stage.index("USER bot")


def test_the_compose_file_tags_and_labels_images_by_release_and_leaves_the_database_alone():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    svc = compose["services"]
    assert svc["bot"]["image"] == "dating-bot/bot:${RELEASE_TAG:-dev}"
    assert svc["web"]["image"] == "dating-bot/web:${RELEASE_TAG:-dev}"
    for name in ("bot", "web"):
        args = svc[name]["build"]["args"]
        assert set(args) == {"GIT_SHA", "APP_VERSION", "BUILD_TIME"}
    assert svc["bot"]["environment"]["ALLOW_NEWER_SCHEMA"] == "${ALLOW_NEWER_SCHEMA:-}"
    db = svc["db"]  # the database service and its volume are exactly what they were
    assert db["image"] == "postgres:16" and db["volumes"] == ["pgdata:/var/lib/postgresql/data"]
    assert "ports" not in db and "build" not in db
    assert compose["volumes"] == {"pgdata": {}} and compose["name"] == "dating-bot"
    assert svc["web"]["ports"] == ["127.0.0.1:${ADMIN_WEB_PORT:-8000}:8000"]  # loopback only


def test_the_load_test_worker_is_not_given_a_release_image_name():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "image" not in compose["services"]["loadtest-worker"]


def test_every_build_arg_default_is_the_dev_identity():
    text = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"GIT_SHA: \$\{GIT_SHA:-unknown\}", text)
    assert re.search(r"APP_VERSION: \$\{RELEASE_TAG:-dev\}", text)
