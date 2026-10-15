from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from . import config, db, keyboards as kb

router = Router()


class Reg(StatesGroup):
    name = State()
    age = State()
    gender = State()
    looking = State()
    city = State()
    bio = State()
    photo = State()


def profile_text(u) -> str:
    return (
        f"<b>{u['name']}, {u['age']}</b>\n"
        f"{kb.GENDER_LABEL.get(u['gender'], '')} · 📍 {u['city']}\n\n{u['bio']}"
    )


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if await db.has_profile(m.from_user.id):
        await db.set_username(m.from_user.id, m.from_user.username)
        await m.answer("Welcome back! 👋 Choose an option below.", reply_markup=kb.main_menu)
        return
    await m.answer(
        "👋 Welcome! Let's create your profile (18+ only).\n\nWhat's your <b>name</b>?",
        reply_markup=kb.remove,
    )
    await state.set_state(Reg.name)


@router.message(Command("editprofile"))
async def edit(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Let's redo your profile. What's your <b>name</b>?", reply_markup=kb.remove)
    await state.set_state(Reg.name)


@router.message(Reg.name, F.text)
async def r_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 1 <= len(name) <= 30:
        return await m.answer("Name must be 1-30 characters.")
    await state.update_data(name=name)
    await m.answer("How old are you?")
    await state.set_state(Reg.age)


@router.message(Reg.age, F.text)
async def r_age(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("Please send your age as a number.")
    age = int(m.text)
    if not config.MIN_AGE <= age <= config.MAX_AGE:
        return await m.answer(f"You must be {config.MIN_AGE}-{config.MAX_AGE} to use this bot.")
    await state.update_data(age=age)
    await m.answer("You are…", reply_markup=kb.gender_kb)
    await state.set_state(Reg.gender)


@router.message(Reg.gender, F.text.in_(kb.GENDERS))
async def r_gender(m: Message, state: FSMContext):
    await state.update_data(gender=kb.GENDERS[m.text])
    await m.answer("Who do you want to meet?", reply_markup=kb.looking_kb)
    await state.set_state(Reg.looking)


@router.message(Reg.looking, F.text.in_(kb.LOOKING))
async def r_looking(m: Message, state: FSMContext):
    await state.update_data(looking_for=kb.LOOKING[m.text])
    await m.answer("Which city are you in?", reply_markup=kb.remove)
    await state.set_state(Reg.city)


@router.message(Reg.city, F.text)
async def r_city(m: Message, state: FSMContext):
    city = m.text.strip()
    if not 1 <= len(city) <= 40:
        return await m.answer("City must be 1-40 characters.")
    await state.update_data(city=city)
    await m.answer("Write a short <b>bio</b> about yourself (max 300 chars).")
    await state.set_state(Reg.bio)


@router.message(Reg.bio, F.text)
async def r_bio(m: Message, state: FSMContext):
    if len(m.text) > 300:
        return await m.answer("Too long, max 300 characters.")
    await state.update_data(bio=m.text.strip())
    await m.answer("Last step: send your <b>photo</b> 📸")
    await state.set_state(Reg.photo)


@router.message(Reg.photo, F.photo)
async def r_photo(m: Message, state: FSMContext):
    d = await state.get_data()
    await db.save_profile(
        m.from_user.id, m.from_user.username, d["name"], d["age"], d["gender"],
        d["looking_for"], d["city"], d["bio"], m.photo[-1].file_id,
    )
    await state.clear()
    await m.answer("✅ Profile saved!", reply_markup=kb.main_menu)


@router.message(Reg.photo)
async def r_photo_bad(m: Message):
    await m.answer("Please send a photo.")


@router.message(Command("profile"))
@router.message(F.text == kb.BTN_PROFILE)
async def show_profile(m: Message):
    u = await db.get_user(m.from_user.id)
    if not u or not u["photo"]:
        return await m.answer("You have no profile yet. Send /start")
    await m.answer_photo(
        u["photo"], caption=profile_text(u) + "\n\n/editprofile to change it", reply_markup=kb.main_menu
    )
