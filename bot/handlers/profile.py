"""Profile onboarding / editing (FSM) and /profile. Thin: validation lives in the service."""

from __future__ import annotations

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .. import domain
from .. import keyboards as kb
from ..db import repo_profiles
from ..services import Services
from ..services.onboarding import InvalidInput, validate_bio, validate_city, validate_name
from ..texts import en as t
from ..texts.i18n import tr
from .common import notify_partner_left
from .filters import ActiveUser


class Reg(StatesGroup):
    name = State()
    age = State()
    gender = State()
    looking = State()
    country = State()
    city = State()
    bio = State()


async def begin_profile(message: Message, state: FSMContext, lang: str) -> None:
    await state.clear()
    await state.set_state(Reg.name)
    await message.answer(tr("ask_name", lang), reply_markup=kb.remove)


def _invalid(code: str, lang: str) -> str:
    try:
        return tr(f"invalid.{code}", lang)
    except KeyError:  # unknown validation code: generic text rather than a crash
        return tr("invalid.bio_chars", lang)


# ---- steps (each is text-only and never treats /commands as answers) -------------------------

_TEXT = F.text & ~F.text.startswith("/")


async def got_name(message: Message, state: FSMContext, lang: str) -> None:
    try:
        name = validate_name(message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code, lang))
    await state.update_data(name=name)
    await state.set_state(Reg.age)
    await message.answer(tr("ask_age", lang))


async def got_age(
    message: Message, state: FSMContext, services: Services, bot: Bot, lang: str
) -> None:
    try:
        result = await services.onboarding.submit_age(message.from_user.id, message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code, lang))
    if result.kind == "underage":
        await state.clear()
        await message.answer(tr("blocked_adults_only", lang), reply_markup=kb.remove)
        if result.partner_id is not None:
            await notify_partner_left(bot, result.partner_id, services)
        return
    await state.update_data(age=result.age)
    await state.set_state(Reg.gender)
    await message.answer(tr("ask_gender", lang), reply_markup=kb.gender_kb(lang))


async def got_gender(message: Message, state: FSMContext, lang: str) -> None:
    await state.update_data(gender=kb.GENDER_LABELS[message.text])
    await state.set_state(Reg.looking)
    await message.answer(tr("ask_looking", lang), reply_markup=kb.looking_kb(lang))


async def got_looking(message: Message, state: FSMContext, lang: str) -> None:
    await state.update_data(looking_for=kb.LOOKING_LABELS[message.text])
    await state.set_state(Reg.country)
    await message.answer(tr("ask_country", lang), reply_markup=kb.country_kb(lang))


async def got_country(message: Message, state: FSMContext, lang: str) -> None:
    code = kb.COUNTRY_LABELS[message.text]
    await state.update_data(country=code)
    await state.set_state(Reg.city)
    await message.answer(
        tr("ask_city", lang), reply_markup=kb.india_city_kb if code == "IN" else kb.remove
    )


async def got_city(message: Message, state: FSMContext, lang: str) -> None:
    try:
        city = validate_city(message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code, lang))
    await state.update_data(city=city)
    await state.set_state(Reg.bio)
    await message.answer(tr("ask_bio", lang))


async def got_bio(
    message: Message, state: FSMContext, services: Services, bot: Bot, lang: str
) -> None:
    try:
        bio = validate_bio(message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code, lang))
    data = await state.get_data()
    try:
        result = await services.onboarding.complete_profile(
            message.from_user.id,
            name=data.get("name", ""),
            age=int(data.get("age", 0)),
            gender=data.get("gender", ""),
            looking_for=data.get("looking_for", ""),
            city=data.get("city", ""),
            country=data.get("country", ""),
            bio=bio,
        )
    except InvalidInput:
        await message.answer(tr("profile_restart", lang))
        return await begin_profile(message, state, lang)
    await state.clear()
    if result.kind == "ok":
        await message.answer(tr("profile_saved", lang), reply_markup=kb.main_menu(lang))
    elif result.kind == "underage":
        await message.answer(tr("blocked_adults_only", lang), reply_markup=kb.remove)
        if result.partner_id is not None:
            await notify_partner_left(bot, result.partner_id, services)
    else:
        await message.answer(tr("finish_setup", lang), reply_markup=kb.remove)


async def wrong_input(message: Message, state: FSMContext, lang: str) -> None:
    current = await state.get_state()
    if current in (Reg.gender.state, Reg.looking.state, Reg.country.state):
        await message.answer(tr("use_buttons", lang))
    else:
        await message.answer(tr("send_text", lang))


async def cancel(
    message: Message, state: FSMContext, lang: str, user: asyncpg.Record | None = None
) -> None:
    await state.clear()
    await message.answer(
        tr("profile_cancelled", lang),
        reply_markup=kb.main_menu(lang) if user and user["status"] == domain.ACTIVE else kb.remove,
    )


# ---- /editprofile, /profile --------------------------------------------------------------------


async def edit_profile(message: Message, state: FSMContext, services: Services, lang: str) -> None:
    if await services.matchmaking.current_chat(message.from_user.id):
        return await message.answer(tr("edit_not_in_chat", lang))
    await begin_profile(message, state, lang)


async def show_profile(message: Message, pool: asyncpg.Pool, lang: str) -> None:
    p = await repo_profiles.get(pool, message.from_user.id)
    if p is None:
        return await message.answer(tr("edit_needs_profile", lang))
    await message.answer(
        tr(
            "profile.view",
            lang,
            name=t.esc(p["name"]),
            age=p["age"],
            gender=tr(f"gender.{p['gender']}", lang),
            looking=tr(f"looking.{p['looking_for']}", lang),
            flag=domain.flag(p["country"]),
            city=t.esc(p["city"]),
            bio=t.esc(p["bio"]),
        ),
        reply_markup=kb.main_menu(lang),
    )


def build_router() -> Router:
    r = Router(name="profile")
    r.message.register(cancel, Command("cancel"), StateFilter(Reg))
    r.message.register(got_name, StateFilter(Reg.name), _TEXT)
    r.message.register(got_age, StateFilter(Reg.age), _TEXT)
    r.message.register(got_gender, StateFilter(Reg.gender), F.text.in_(kb.GENDER_LABELS))
    r.message.register(got_looking, StateFilter(Reg.looking), F.text.in_(kb.LOOKING_LABELS))
    r.message.register(got_country, StateFilter(Reg.country), F.text.in_(kb.COUNTRY_LABELS))
    r.message.register(got_city, StateFilter(Reg.city), _TEXT)
    r.message.register(got_bio, StateFilter(Reg.bio), _TEXT)
    r.message.register(wrong_input, StateFilter(Reg), ~F.text.startswith("/"))
    r.message.register(edit_profile, Command("editprofile"), ActiveUser())
    r.message.register(show_profile, Command("profile"), ActiveUser())
    r.message.register(show_profile, F.text.in_(kb.labels("btn.profile")), ActiveUser())
    return r
