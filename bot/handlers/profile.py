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


async def begin_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Reg.name)
    await message.answer(t.ASK_NAME, reply_markup=kb.remove)


def _invalid(code: str) -> str:
    return t.INVALID.get(code, t.INVALID["bio_chars"])


# ---- steps (each is text-only and never treats /commands as answers) -------------------------

_TEXT = F.text & ~F.text.startswith("/")


async def got_name(message: Message, state: FSMContext) -> None:
    try:
        name = validate_name(message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code))
    await state.update_data(name=name)
    await state.set_state(Reg.age)
    await message.answer(t.ASK_AGE)


async def got_age(message: Message, state: FSMContext, services: Services, bot: Bot) -> None:
    try:
        result = await services.onboarding.submit_age(message.from_user.id, message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code))
    if result.kind == "underage":
        await state.clear()
        await message.answer(t.BLOCKED_ADULTS_ONLY, reply_markup=kb.remove)
        if result.partner_id is not None:
            await notify_partner_left(bot, result.partner_id)
        return
    await state.update_data(age=result.age)
    await state.set_state(Reg.gender)
    await message.answer(t.ASK_GENDER, reply_markup=kb.gender_kb)


async def got_gender(message: Message, state: FSMContext) -> None:
    await state.update_data(gender=kb.GENDER_BUTTONS[message.text])
    await state.set_state(Reg.looking)
    await message.answer(t.ASK_LOOKING, reply_markup=kb.looking_kb)


async def got_looking(message: Message, state: FSMContext) -> None:
    await state.update_data(looking_for=kb.LOOKING_BUTTONS[message.text])
    await state.set_state(Reg.country)
    await message.answer(t.ASK_COUNTRY, reply_markup=kb.country_kb)


async def got_country(message: Message, state: FSMContext) -> None:
    code = kb.COUNTRY_BUTTONS[message.text]
    await state.update_data(country=code)
    await state.set_state(Reg.city)
    await message.answer(t.ASK_CITY, reply_markup=kb.india_city_kb if code == "IN" else kb.remove)


async def got_city(message: Message, state: FSMContext) -> None:
    try:
        city = validate_city(message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code))
    await state.update_data(city=city)
    await state.set_state(Reg.bio)
    await message.answer(t.ASK_BIO)


async def got_bio(message: Message, state: FSMContext, services: Services, bot: Bot) -> None:
    try:
        bio = validate_bio(message.text)
    except InvalidInput as exc:
        return await message.answer(_invalid(exc.code))
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
        await message.answer(t.PROFILE_RESTART)
        return await begin_profile(message, state)
    await state.clear()
    if result.kind == "ok":
        await message.answer(t.PROFILE_SAVED, reply_markup=kb.main_menu)
    elif result.kind == "underage":
        await message.answer(t.BLOCKED_ADULTS_ONLY, reply_markup=kb.remove)
        if result.partner_id is not None:
            await notify_partner_left(bot, result.partner_id)
    else:
        await message.answer(t.FINISH_SETUP, reply_markup=kb.remove)


async def wrong_input(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current in (Reg.gender.state, Reg.looking.state, Reg.country.state):
        await message.answer(t.USE_BUTTONS)
    else:
        await message.answer(t.SEND_TEXT)


async def cancel(message: Message, state: FSMContext, user: asyncpg.Record | None) -> None:
    await state.clear()
    await message.answer(
        t.PROFILE_CANCELLED,
        reply_markup=kb.main_menu if user and user["status"] == domain.ACTIVE else kb.remove,
    )


# ---- /editprofile, /profile --------------------------------------------------------------------


async def edit_profile(message: Message, state: FSMContext, services: Services) -> None:
    if await services.matchmaking.current_chat(message.from_user.id):
        return await message.answer(t.EDIT_NOT_IN_CHAT)
    await begin_profile(message, state)


async def show_profile(message: Message, pool: asyncpg.Pool) -> None:
    p = await repo_profiles.get(pool, message.from_user.id)
    if p is None:
        return await message.answer(t.EDIT_NEEDS_PROFILE)
    await message.answer(
        f"<b>{t.esc(p['name'])}</b>, {p['age']}\n"
        f"{domain.GENDER_LABEL[p['gender']]} · "
        f"looking for {domain.LOOKING_LABEL[p['looking_for']]}\n"
        f"{domain.flag(p['country'])} {t.esc(p['city'])}\n\n"
        f"{t.esc(p['bio'])}\n\n/editprofile to change it",
        reply_markup=kb.main_menu,
    )


def build_router() -> Router:
    r = Router(name="profile")
    r.message.register(cancel, Command("cancel"), StateFilter(Reg))
    r.message.register(got_name, StateFilter(Reg.name), _TEXT)
    r.message.register(got_age, StateFilter(Reg.age), _TEXT)
    r.message.register(got_gender, StateFilter(Reg.gender), F.text.in_(kb.GENDER_BUTTONS))
    r.message.register(got_looking, StateFilter(Reg.looking), F.text.in_(kb.LOOKING_BUTTONS))
    r.message.register(got_country, StateFilter(Reg.country), F.text.in_(kb.COUNTRY_BUTTONS))
    r.message.register(got_city, StateFilter(Reg.city), _TEXT)
    r.message.register(got_bio, StateFilter(Reg.bio), _TEXT)
    r.message.register(wrong_input, StateFilter(Reg), ~F.text.startswith("/"))
    r.message.register(edit_profile, Command("editprofile"), ActiveUser())
    r.message.register(show_profile, Command("profile"), ActiveUser())
    r.message.register(show_profile, F.text == kb.BTN_PROFILE, ActiveUser())
    return r
