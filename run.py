import asyncio
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from sqlalchemy import BigInteger, Date, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
TZ = ZoneInfo("Europe/Moscow")

engine = create_async_engine("sqlite+aiosqlite:///game_bot.db", echo=False)
Session = async_sessionmaker(engine, expire_on_commit=False)
router = Router()

with open("actions.json", "r", encoding="utf-8") as f:
    GAME_DATA = json.load(f)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    aura_balance: Mapped[int] = mapped_column(Integer, default=0)
    sessions_played: Mapped[int] = mapped_column(Integer, default=0)
    current_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    current_game: Mapped[str | None] = mapped_column(String(32), nullable=True)
    play_used: Mapped[bool] = mapped_column(Integer, default=0)
    action_message_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class GameSession(StatesGroup):
    choosing_game = State()
    choosing_action = State()


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def level_from_sessions(sessions: int) -> int:
    return sessions


def aura_label(balance: int) -> str:
    return f"+{balance} ауры" if balance >= 0 else f"{balance} ауры"


def user_title(user: User) -> str:
    name = user.full_name or user.username or str(user.tg_id)
    return f"{name} [{level_from_sessions(user.sessions_played)}] [{aura_label(user.aura_balance)}]"


async def get_user(tg_id: int, username: str | None, full_name: str) -> User:
    async with Session() as session:
        res = await session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        if not user:
            user = User(tg_id=tg_id, username=username, full_name=full_name)
            session.add(user)
        else:
            user.username = username
            user.full_name = full_name
        await session.commit()
        await session.refresh(user)
        return user


async def save_user(user: User):
    async with Session() as session:
        await session.merge(user)
        await session.commit()


async def refresh_user(tg_id: int) -> User | None:
    async with Session() as session:
        res = await session.execute(select(User).where(User.tg_id == tg_id))
        return res.scalar_one_or_none()


def games_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, value in GAME_DATA.items():
        kb.button(text=value["title"], callback_data=f"game:{key}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def actions_kb(game: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for action in GAME_DATA[game]["actions"]:
        kb.button(text=action["title"], callback_data=f"act:{game}:{action['id']}")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def pick_template(action: dict, is_win: bool | None, delta: int) -> str:
    if action["type"] == "random":
        templates = action["templates_win"] if is_win else action["templates_lose"]
    else:
        templates = action["templates"]
    return random.choice(templates).format(delta=abs(delta))


async def reset_daily_state(user: User):
    if user.current_date != today():
        user.current_date = today()
        user.play_used = False
        user.current_game = None
        user.action_message_chat_id = None
        user.action_message_id = None


@router.message(Command("play"))
async def play_cmd(message: Message):
    user = await get_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await reset_daily_state(user)
    if user.play_used:
        await save_user(user)
        await message.answer("Сегодня ты уже играл. Жди нового дня.")
        return
    await save_user(user)
    text = f"{user_title(user)}, в какую игру ты хочешь поиграть сегодня?"
    sent = await message.answer(text, reply_markup=games_kb())
    user.action_message_chat_id = sent.chat.id
    user.action_message_id = sent.message_id
    await save_user(user)


@router.callback_query(F.data.startswith("game:"))
async def game_pick(callback: CallbackQuery):
    user = await get_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    await reset_daily_state(user)
    game = callback.data.split(":", 1)[1]
    if user.play_used:
        await callback.answer()
        return
    user.play_used = True
    user.current_game = game
    await save_user(user)
    text = f"{user_title(user)}, выбрана игра: {GAME_DATA[game]['title']}\n\nВыбирай действие:"
    await callback.message.edit_text(text, reply_markup=actions_kb(game))
    await callback.answer()


@router.callback_query(F.data.startswith("act:"))
async def action_pick(callback: CallbackQuery, bot: Bot):
    user = await get_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    await reset_daily_state(user)
    _, game, action_id = callback.data.split(":")
    if user.current_game != game:
        await callback.answer()
        return
    if callback.from_user.id != user.tg_id:
        await callback.answer()
        return

    action = next(a for a in GAME_DATA[game]["actions"] if a["id"] == action_id)

    if action["type"] == "random":
        win = random.random() < action.get("win_chance", 0.5)
        if win:
            delta = random.randint(max(1, action["max_delta"] // 2), action["max_delta"])
            user.aura_balance += delta
            text_part = pick_template(action, True, delta)
        else:
            delta = random.randint(max(1, abs(action["min_delta"]) // 2), abs(action["min_delta"]))
            user.aura_balance -= delta
            text_part = pick_template(action, False, delta)
    else:
        delta = random.randint(action["min_delta"], action["max_delta"])
        user.aura_balance += delta
        text_part = pick_template(action, None, delta)

    user.sessions_played += 1
    await save_user(user)

    final_text = f"{user.full_name} отлично поиграл в игру «{GAME_DATA[game]['title']}».\n\n{text_part}"
    try:
        await bot.delete_message(callback.message.chat.id, callback.message.message_id)
    except Exception:
        pass
    await bot.send_message(callback.message.chat.id, final_text)
    if GROUP_CHAT_ID:
        await bot.send_message(GROUP_CHAT_ID, final_text)
    await callback.answer("Готово!")


@router.message(Command("top"))
async def top_cmd(message: Message):
    async with Session() as session:
        res = await session.execute(select(User).order_by(User.sessions_played.desc(), User.aura_balance.desc()))
        users = res.scalars().all()
    if not users:
        await message.answer("Пока никто не играл.")
        return
    lines = ["Топ игроков:"]
    for i, u in enumerate(users, 1):
        lines.append(f"{i}. {u.full_name} [{u.sessions_played}] [{aura_label(u.aura_balance)}]")
    await message.answer("\n".join(lines))


async def midnight_job():
    async with Session() as session:
        res = await session.execute(select(User))
        users = res.scalars().all()
        for u in users:
            if u.current_date == today():
                u.play_used = False
                u.current_game = None
                u.action_message_chat_id = None
                u.action_message_id = None
        await session.commit()


async def on_startup(bot: Bot):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(midnight_job, "cron", hour=0, minute=0, second=0)
    scheduler.start()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
