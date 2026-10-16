"""Text of a partner card (presentation only; visibility was decided by PartnerCardService)."""

from __future__ import annotations

from . import domain
from .services.partner_card import FieldView, PartnerCard
from .texts import en as t
from .texts.i18n import tr


def _field(key: str, view: FieldView, lang: str, show) -> str:
    if view.state == "shown":
        value = show(view.value)
    else:
        value = tr("pc.locked" if view.state == "locked" else "pc.hidden", lang)
    return tr(key, lang, value=value)


def render(card: PartnerCard, lang: str, *, anonymous: bool) -> str:
    """The same fields for everyone; `anonymous` (random chat) leaves out name / looking-for / bio.

    A locked field shows "🔒 Premium", an owner-hidden one "🙈 Hidden by the user"; the real value
    is simply not present in `card` for either."""
    country = card.country if card.country in domain.COUNTRIES else "ZZ"
    lines = [tr("pc.rule", lang), tr("pc.title", lang), tr("pc.rule", lang), ""]
    if not anonymous:
        lines.append(tr("pc.name", lang, value=t.esc(card.name)))
    lines += [
        _field("pc.age", card.age, lang, str),
        _field(
            "pc.gender",
            card.gender,
            lang,
            lambda g: f"{domain.GENDER_EMOJI[g]} {tr(f'gender.{g}', lang)}",
        ),
        _field("pc.city", card.city, lang, lambda c: t.esc(c)),
        tr("pc.country", lang, value=f"{domain.flag(country)} {tr(f'country.{country}', lang)}"),
    ]
    if not anonymous:
        lines.append(tr("pc.looking", lang, value=tr(f"looking.{card.looking_for}", lang)))
        lines.append(tr("pc.about", lang, value=t.esc(card.bio)))
    lines.append("")
    if card.badge:
        lines.append(tr(f"pc.badge.{min(card.badge, 3)}", lang))
    lines.append(tr("pc.rule", lang))
    if anonymous:
        lines.append(tr("pc.anon", lang))
    if card.cta:
        lines.append(tr("pc.cta", lang))
    return "\n".join(lines)


def chat_started(card: PartnerCard, lang: str) -> str:
    return f"{tr('pc.matched', lang)}\n\n{render(card, lang, anonymous=True)}"
