import asyncio
import json
import os
import random
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from datetime import datetime, timedelta
import aiofiles
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
TZ = ZoneInfo("Europe/Moscow")
DATA_FILE = "users_data.json"

ALLOWED_CHAT_IDS = {
    -1002376734638,
    -5201564768,
}

router = Router()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler()                            # Дублирует в консоль
    ]
)
logger = logging.getLogger(__name__)

# Загрузка игровых данных
with open("actions.json", "r", encoding="utf-8") as f:
    GAME_DATA = json.load(f)


# --- Функции работы с JSON-файлом пользователей ---

async def load_users() -> dict:
    """Асинхронно загружает базу пользователей из JSON-файла."""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
            return json.loads(content) if content.strip() else {}
    except Exception as e:
        logger.error(f"Ошибка чтения файла данных: {e}")
        return {}


async def save_users(users: dict):
    """Асинхронно сохраняет базу пользователей в JSON-файл."""
    try:
        async with aiofiles.open(DATA_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(users, ensure_ascii=False, indent=4))
    except Exception as e:
        logger.error(f"Ошибка записи файла данных: {e}")


async def get_user(tg_id: int, username: str | None, full_name: str) -> dict:
    """Получает пользователя из памяти/JSON или создает нового с дефолтными значениями."""
    users = await load_users()
    str_id = str(tg_id)  # В JSON ключи всегда строки
    
    if str_id not in users:
        users[str_id] = {
            "tg_id": tg_id,
            "username": username,
            "full_name": full_name,
            "aura_balance": 0,
            "sessions_played": 0,
            "current_date": None,
            "current_game": None,
            "play_used": False,
            "action_message_chat_id": None,
            "action_message_id": None,
            "level": 1
        }
    else:
        users[str_id]["username"] = username
        users[str_id]["full_name"] = full_name
        
    await save_users(users)
    return users[str_id]


async def save_user_data(user_data: dict):
    """Сохраняет измененные данные конкретного пользователя обратно в JSON."""
    users = await load_users()
    users[str(user_data["tg_id"])] = user_data
    await save_users(users)


# --- Вспомогательные утилиты ---

def is_allowed_chat(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


def today() -> str:
    # Берем чистое время по Гринвичу (UTC) и жестко плюсуем 3 часа для Москвы
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")


def level_from_sessions(sessions: int) -> int:
    return sessions


def aura_label(balance: int) -> str:
    return f"+{balance} ауры" if balance >= 0 else f"{balance} ауры"


def user_title(user: dict) -> str:
    name = user["full_name"] or user["username"] or str(user["tg_id"])
    return f"{name} [{level_from_sessions(user['sessions_played'])}] [{aura_label(user['aura_balance'])}]"


def games_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, value in GAME_DATA.items():
        kb.button(text=value["title"], callback_data=f"game:{key}")
    kb.adjust(2)
    return kb.as_markup()


def actions_kb(game: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for action in GAME_DATA[game]["actions"]:
        kb.button(text=action["title"], callback_data=f"act:{game}:{action['id']}")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def pick_template(action: dict, is_win: bool | None, delta: int) -> str:
    if action["type"] == "random":
        if is_win:
            templates = action.get("templates_win", [])
        else:
            templates = action.get("templates_lose", [])
    else:
        templates = action.get("templates", [])
        
    if not templates:
        return f"Результат: {aura_label(delta)}"
        
    template = random.choice(templates)
    return template.format(delta=abs(delta))


async def reset_daily_state(user: dict):
    if user["current_date"] != today():
        user["current_date"] = today()
        user["play_used"] = False
        user["current_game"] = None
        user["action_message_chat_id"] = None
        user["action_message_id"] = None


# --- Хэндлеры бота ---

@router.message(Command("play"))
async def play_cmd(message: Message):
    if not is_allowed_chat(message.chat.id):
        return
    user = await get_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await reset_daily_state(user)
    
    if user["play_used"]:
        await save_user_data(user)
        await message.answer("Сегодня время игр закончилось! Иди работать, игры ждут тебя завтра 🎮")
        return
        
    await save_user_data(user)
    text = f"🎮 {user_title(user)}, в какую игру ты хочешь поиграть сегодня?"
    sent = await message.answer(text, reply_markup=games_kb())
    
    user["action_message_chat_id"] = sent.chat.id
    user["action_message_id"] = sent.message_id
    await save_user_data(user)


@router.callback_query(F.data.startswith("game:"))
async def game_pick(callback: CallbackQuery):
    if not callback.message or not is_allowed_chat(callback.message.chat.id):
        await callback.answer()
        return
    user = await get_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    await reset_daily_state(user)
    
    game = callback.data.split(":", 1)[1]
    if user["play_used"]:
        await callback.answer()
        return
        
    user["play_used"] = True
    user["current_game"] = game
    await save_user_data(user)
    
    text = f"🎯 {user_title(user)}, выбрана игра: {GAME_DATA[game]['title']}\n\nЧем в ней займешься? Выбрать можно только одно действие!"
    await callback.message.edit_text(text, reply_markup=actions_kb(game))
    await callback.answer()


@router.callback_query(F.data.startswith("act:"))
async def action_pick(callback: CallbackQuery, bot: Bot):
    if not callback.message or not is_allowed_chat(callback.message.chat.id):
        await callback.answer()
        return
    user = await get_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    await reset_daily_state(user)
    
    _, game, action_id = callback.data.split(":")
    if user["current_game"] != game:
        await callback.answer()
        return
    if callback.from_user.id != user["tg_id"]:
        await callback.answer()
        return

    action = next(a for a in GAME_DATA[game]["actions"] if a["id"] == action_id)

    if action["type"] == "random":
        win = random.random() < action.get("win_chance", 0.5)
        if win:
            delta = action["max_delta"]
            user["aura_balance"] += delta
            text_part = pick_template(action, True, delta)
        else:
            delta = action["min_delta"]  # Возьмет отрицательное число, например -35
            user["aura_balance"] += delta  # Плюс на минус даст вычитание, баланс упадет
            text_part = pick_template(action, False, delta)
    else:
        delta = random.randint(action["min_delta"], action["max_delta"])
        user["aura_balance"] += delta
        text_part = pick_template(action, None, delta)

    user["sessions_played"] += 1
    await save_user_data(user)

# Собираем финальный текст с дублированием уровня и ауры внизу
    final_text = (
        f"🦾 {user['full_name']}, сессия в игре «{GAME_DATA[game]['title']}» закончилась!\nВозвращайся завтра 🎳\n\n"
        f"{text_part}\n\n"
        f"🔮 Уровень [{user['level']}] / ✨ Аура [{aura_label(user['aura_balance'])}]"
    )
    
    try:
        await bot.delete_message(callback.message.chat.id, callback.message.message_id)
    except Exception:
        pass
        
    await bot.send_message(callback.message.chat.id, final_text)
    if GROUP_CHAT_ID:
        await bot.send_message(GROUP_CHAT_ID, final_text)
    await callback.answer("Готово!")


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

@router.message(Command("top"))
async def top_cmd(message: Message):
    if not is_allowed_chat(message.chat.id):
        return
        
    users = await load_users()
    if not users:
        await message.answer("Пока никто не играл.")
        return

    users_list = list(users.values())

    # Сортируем по количеству сессий (уровню) и ауре
    users_lvl = sorted(users_list, key=lambda x: (x["sessions_played"], x["aura_balance"]), reverse=True)[:10]
    
    # Сортируем по ауре и сессиям
    users_aura = sorted(users_list, key=lambda x: (x["aura_balance"], x["sessions_played"]), reverse=True)[:10]

    lvl_lines = ["🏆 <b>Топ игроков по УРОВНЮ:</b>"]
    for i, u in enumerate(users_lvl, 1):
        medal = MEDALS.get(i, "")
        lvl_lines.append(f"{i}. {u['full_name']} [Уровень: {level_from_sessions(u['sessions_played'])}] [{aura_label(u['aura_balance'])}] {medal}")

    aura_lines = ["✨ <b>Топ игроков по АУРЕ:</b>"]
    for i, u in enumerate(users_aura, 1):
        medal = MEDALS.get(i, "")
        aura_lines.append(f"{i}. {u['full_name']} [{aura_label(u['aura_balance'])}] [Игр: {u['sessions_played']}] {medal}")

    final_message = "\n\n".join(["\n".join(lvl_lines), "\n".join(aura_lines)])
    await message.answer(final_message, parse_mode="HTML")


# --- Планировщик задач (APScheduler) ---

async def midnight_job():
    """Сбрасывает дневные лимиты у всех пользователей ровно в полночь."""
    users = await load_users()
    current_today = today()
    for str_id, u in users.items():
        if u["current_date"] == current_today:
            u["play_used"] = False
            u["current_game"] = None
            u["action_message_chat_id"] = None
            u["action_message_id"] = None
    await save_users(users)
    logger.info("Полночный сброс лимитов выполнен успешно.")


async def on_startup(bot: Bot):
    logger.info("Бот успешно запущен и готов к работе.")
    
    # Создаем пустой файл данных, если его нет
    if not os.path.exists(DATA_FILE):
        async with aiofiles.open(DATA_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps({}))

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(midnight_job, "cron", hour=0, minute=0, second=0)
    scheduler.start()


# --- Главная функция инициализации ---

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    bot = Bot(
        token=BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
