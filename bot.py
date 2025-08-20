"""
Основной файл Telegram‑бота для проведения розыгрыша по кодовым словам.

Режимы запуска:
- WEBHOOK (Render Web Service): если задана переменная окружения WEBHOOK_URL.
- POLLING (локально): если WEBHOOK_URL не задан.

Команды:
  /start — приветствие и инструкция
  /my    — показать свои коды
  /export (админ), /draw (админ), /stats (админ)

Зависимости: см. requirements.txt
"""

import os
import asyncio
import csv
import datetime
import logging
import random
from io import StringIO

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import aiosqlite
import config

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# -------------------- БОТ/DP --------------------
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# -------------------- КОНФИГ/БД --------------------
DB_NAME = getattr(config, "DB_NAME", os.getenv("DB_NAME", "participants.db"))

# -------------------- БАЗА ДАННЫХ --------------------
async def init_db() -> None:
    """Создаёт таблицу entries, если она ещё не существует."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                code TEXT NOT NULL,
                entry_number INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        await db.commit()
    logger.info("База данных и таблица готовы к работе.")

# -------------------- КОМАНДЫ В МЕНЮ --------------------
async def set_bot_commands() -> None:
    commands = [
        BotCommand(command="start", description="Начать и получить инструкцию"),
        BotCommand(command="my", description="Показать свои коды"),
        # Админские (их нельзя скрыть из меню для других пользователей)
        BotCommand(command="export", description="Выгрузить CSV (админ)"),
        BotCommand(command="draw", description="Розыгрыш (админ)"),
        BotCommand(command="stats", description="Статистика (админ)"),
    ]
    await bot.set_my_commands(commands)

# -------------------- ЛОГИКА РОЗЫГРЫША --------------------
async def register_entry(user_id: int, username: str | None, first_name: str | None, code: str) -> tuple[int, bool]:
    """Сохранить код. Вернёт (номер, is_new)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT entry_number FROM entries WHERE user_id = ? AND code = ?",
            (user_id, code),
        )
        row = await cur.fetchone()
        if row:
            return row[0], False

        cur = await db.execute("SELECT MAX(entry_number) FROM entries")
        result = await cur.fetchone()
        max_number = result[0] or 0
        new_number = max_number + 1

        created_at = datetime.datetime.now().isoformat()
        await db.execute(
            "INSERT INTO entries (user_id, username, first_name, code, entry_number, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username or "", first_name or "", code, new_number, created_at),
        )
        await db.commit()
        return new_number, True

async def get_user_entries(user_id: int) -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT code, entry_number FROM entries WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
        rows = await cur.fetchall()
    return [(row[0], row[1]) for row in rows]

async def export_csv() -> bytes:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, username, code, entry_number FROM entries ORDER BY id"
        )
        rows = await cur.fetchall()
    buff = StringIO()
    writer = csv.writer(buff)
    writer.writerow(["user_id", "username", "code", "entry_number"])
    for r in rows:
        writer.writerow(r)
    return buff.getvalue().encode("utf-8")

async def draw_winner() -> dict[str, str] | None:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, username, first_name, code, entry_number FROM entries"
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    winner = random.choice(rows)
    return {
        "user_id": str(winner[0]),
        "username": winner[1] or "",
        "first_name": winner[2] or "",
        "code": winner[3],
        "entry_number": str(winner[4]),
    }

async def get_stats() -> dict[str, int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM entries")
        total_entries = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM entries")
        unique_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT code) FROM entries")
        unique_codes = (await cur.fetchone())[0]
    return {"total_entries": total_entries, "unique_users": unique_users, "unique_codes": unique_codes}

# -------------------- ХЕНДЛЕРЫ --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    first_name = message.from_user.first_name or "друг"
    text = (
        f"Привет, {first_name} 👋!\n\n"
        "Добро пожаловать в розыгрыш. Чтобы принять участие, отправь кодовое слово.\n"
        "Если код верный, ты получишь уникальный номер участника.\n"
        "Посмотреть свои коды и номера можно командой /my."
    )
    await message.answer(text)

@dp.message(Command("my"))
async def cmd_my(message: types.Message) -> None:
    entries = await get_user_entries(message.from_user.id)
    if not entries:
        await message.answer("Вы ещё не вводили кодовых слов.")
        return
    lines = ["Ваши коды:"]
    for code, number in entries:
        lines.append(f"№{number} — {code}")
    await message.answer("\n".join(lines))

@dp.message(Command("export"))
async def cmd_export(message: types.Message) -> None:
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    csv_bytes = await export_csv()
    file = BufferedInputFile(csv_bytes, filename="participants.csv")
    await message.answer_document(file, caption="CSV‑файл со списком участников")

@dp.message(Command("draw"))
async def cmd_draw(message: types.Message) -> None:
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    winner = await draw_winner()
    if not winner:
        await message.answer("Пока нет участников для розыгрыша.")
        return
    username_part = f"@{winner['username']}" if winner['username'] else f"user_id={winner['user_id']}"
    response = (
        f"🎉 Победитель: {winner['first_name']} {username_part}\n"
        f"Код: {winner['code']}\n"
        f"Номер участника: №{winner['entry_number']}\n"
        "Поздравляем!"
    )
    await message.answer(response)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    stats = await get_stats()
    response = (
        "Статистика:\n"
        f"Всего заявок: {stats['total_entries']}\n"
        f"Уникальных пользователей: {stats['unique_users']}\n"
        f"Уникальных кодов: {stats['unique_codes']}"
    )
    await message.answer(response)

@dp.message()
async def handle_code(message: types.Message) -> None:
    if not message.text:
        return
    code_text = message.text.strip()
    if not code_text or code_text.startswith("/"):
        return
    # Проверяем код (регистр игнорируем)
    code = code_text.lower()
    valid_codes_lower = [c.lower() for c in config.VALID_CODES]
    if code not in valid_codes_lower:
        await message.answer("Кодовое слово неверно. Попробуйте ещё раз.")
        return
    entry_number, is_new = await register_entry(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        code=code,
    )
    if is_new:
        await message.answer(f"Ты участник №{entry_number} в розыгрыше")
    else:
        await message.answer(f"Этот код уже зарегистрирован за тобой как №{entry_number}")

# -------------------- ЗАПУСК: WEBHOOK или POLLING --------------------
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # напр.: https://<app>.onrender.com/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
PORT = int(os.getenv("PORT", "10000"))  # Render отдаёт порт в переменной PORT

async def _on_startup(app: web.Application):
    """Инициализация перед приёмом апдейтов (webhook)."""
    await init_db()
    await set_bot_commands()
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL не задан. Укажите полный https‑URL в переменных окружения.")
    await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    logger.info("Webhook установлен: %s", WEBHOOK_URL)

async def _on_shutdown(app: web.Application):
    """Аккуратное снятие вебхука при остановке."""
    try:
        await bot.delete_webhook()
        logger.info("Webhook снят.")
    except Exception as e:
        logger.warning("Ошибка снятия вебхука: %s", e)

def create_app() -> web.Application:
    """Создаёт aiohttp‑приложение для приёма webhook от Telegram."""
    app = web.Application()

    # Health‑чек для Render
    async def health(_):
        return web.Response(text="ok")
    app.router.add_get("/health", health)

    # Регистрируем обработчик апдейтов Telegram
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, on_startup=[_on_startup], on_shutdown=[_on_shutdown])
    return app

async def _run_polling():
    """Локальный запуск в режиме long polling (без вебхука)."""
    await init_db()
    await set_bot_commands()
    logger.info("Бот запущен (polling). Ожидание сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if WEBHOOK_URL:
        # WEBHOOK‑режим (Render Web Service)
        logger.info("Старт в режиме WEBHOOK на порту %s", PORT)
        web.run_app(create_app(), host="0.0.0.0", port=PORT)
    else:
        # POLLING‑режим (локально)
        try:
            asyncio.run(_run_polling())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Бот остановлен")
