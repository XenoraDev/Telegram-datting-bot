"""The bot image carries the OCR engine; the web image does not need it."""

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()


def stage(name: str) -> str:
    parts = re.split(r"(?m)^FROM ", DOCKERFILE)
    return next(p for p in parts if p.split()[2:3] == [name] or f"AS {name}" in p.splitlines()[0])


def test_the_bot_stage_installs_tesseract_before_dropping_privileges():
    bot = stage("bot")
    assert "tesseract-ocr" in bot and "--no-install-recommends" in bot
    assert bot.index("tesseract-ocr") < bot.index("USER bot")
    assert "rm -rf /var/lib/apt/lists" in bot


def test_the_web_stage_does_not_read_images():
    assert "tesseract" not in stage("web")
