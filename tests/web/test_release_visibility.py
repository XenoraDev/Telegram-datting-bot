"""The admin can see which release is running; nothing secret is shown."""

import json

import pytest

from bot import version
from bot.version import VersionInfo
from tests.web.conftest import DOMAIN

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def release(monkeypatch):
    info = VersionInfo("v2.0.0-rc1", SHA, "2026-10-20T01:02:03Z")
    monkeypatch.setattr(version, "current", lambda: info)
    return info


async def test_the_dashboard_shows_the_release_commit_build_time_start_time_and_environment(
    admin, release
):
    page = (await admin.client.get("/admin/")).text
    assert "<h2>Release</h2>" in page
    assert "<code>v2.0.0-rc1</code>" in page and f"<code>{SHA}</code>" in page
    assert "2026-10-20T01:02:03Z" in page
    assert "Running since" in page and "Environment" in page and DOMAIN in page
    assert "not started from a release image" not in page


async def test_a_development_process_says_it_has_no_identity(admin):
    page = (await admin.client.get("/admin/")).text
    assert "<code>dev</code>" in page and "not started from a release image" in page


async def test_every_admin_page_has_the_short_release_in_its_footer(admin, release):
    for path in ("/admin/", "/admin/users", "/admin/payments", "/admin/support", "/admin/traffic"):
        page = (await admin.client.get(path)).text
        assert 'class="release muted small"' in page, path
        assert "<code>v2.0.0-rc1</code>" in page and f'title="{SHA}">{SHA[:7]}</code>' in page


async def test_the_login_page_and_the_health_check_do_not_reveal_the_release(web, release):
    login = (await web.client.get("/login")).text
    assert SHA not in login and "v2.0.0-rc1" not in login
    health = await web.client.get("/healthz")
    assert health.text == "ok" and SHA not in health.text


async def test_anonymous_requests_never_see_it(web, release):
    for path in ("/admin/", "/admin/users"):
        r = await web.client.get(path)
        assert r.status_code == 303 and SHA not in r.text


async def test_no_secret_can_appear_in_the_release_card(admin, monkeypatch, tmp_path):
    f = tmp_path / "VERSION.json"
    f.write_text(
        json.dumps(
            {
                "version": "v2.0.0",
                "sha": SHA,
                "built": "2026-10-20T01:02:03Z",
                "BOT_TOKEN": "123:SECRET",
            }
        )
    )
    monkeypatch.setattr(version, "current", lambda: version.load(f))
    page = (await admin.client.get("/admin/")).text
    assert "SECRET" not in page and "BOT_TOKEN" not in page


async def test_a_malicious_version_file_cannot_inject_markup(admin, monkeypatch, tmp_path):
    f = tmp_path / "VERSION.json"
    f.write_text(
        json.dumps({"version": "<script>alert(1)</script>", "sha": "<img src=x>", "built": "x"})
    )
    monkeypatch.setattr(version, "current", lambda: version.load(f))
    page = (await admin.client.get("/admin/")).text
    assert "<script>alert(1)</script>" not in page and "<img src=x>" not in page
    assert "<code>dev</code>" in page
