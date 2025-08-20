"""
Бот розыгрыша с кодовыми словами.
"""

import os
import asyncio
import csv
import datetime
import logging
import random
import secrets
from io import StringIO
from collections import defaultdict

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile

from aiohttp import web
# ВАЖНО: SimpleRequestHandler больше не используем
from aiogram.webhook.aiohttp_server import setup_application

import aiosqlite
import config

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- БОТ/DP ----------
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# ---------- КОНФИГ/БД ----------
DB_NAME = getattr(config, "DB_NAME", os.getenv("DB_NAME", "participants.db"))
PART_LEN = config.PARTICIPANT_CODE_LEN
ALPHABET = config.PARTICIPANT_CODE_ALPHABET

# ---------- УТИЛЫ ----------
def make_participant_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(PART_LEN))

def is_admin(user_id: int) -> bool:
    return user_id in set(getattr(config, "ADMIN_IDS", []) or [])

# ---------- ИНИТ БД + МИГРАЦИИ ----------
async def init_db() -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                participant_code TEXT UNIQUE NOT NULL
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                code TEXT NOT NULL,
                entry_number INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            """
        )
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_user_code ON entries(user_id, code);")
        await db.commit()
    logger.info("База данных и таблицы готовы к работе.")

# ---------- КОМАНДЫ МЕНЮ ----------
async def set_bot_commands() -> None:
    commands = [
        BotCommand(command="start", description="Начать и получить инструкцию"),
        BotCommand(command="my", description="Показать свои коды"),
        BotCommand(command="export", description="Выгрузить CSV (админ)"),
        BotCommand(command="draw", description="Розыгрыш (админ)"),
        BotCommand(command="stats", description="Статистика (админ)"),
    ]
    await bot.set_my_commands(commands)

# ---------- ЛОГИКА ----------
async def ensure_user(db, user_id: int, username: str | None, first_name: str | None) -> str:
    cur = await db.execute("SELECT participant_code FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    if row:
        await db.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (username or "", first_name or "", user_id),
        )
        return row[0]
    while True:
        pc = make_participant_code()
        cur2 = await db.execute("SELECT 1 FROM users WHERE participant_code = ?", (pc,))
        if not await cur2.fetchone():
            break
    await db.execute(
        "INSERT INTO users (user_id, username, first_name, participant_code) VALUES (?, ?, ?, ?)",
        (user_id, username or "", first_name or "", pc),
    )
    return pc

async def register_entry(user_id: int, username: str | None, first_name: str | None, code: str) -> tuple[int, bool, str]:
    async with aiosqlite.connect(DB_NAME) as db:
        participant_code = await ensure_user(db, user_id, username, first_name)

        cur = await db.execute("SELECT entry_number FROM entries WHERE user_id = ? AND code = ?", (user_id, code))
        row = await cur.fetchone()
        if row:
            await db.commit()
            return row[0], False, participant_code

        cur = await db.execute("SELECT MAX(entry_number) FROM entries")
        max_number = (await cur.fetchone())[0] or 0
        new_number = max_number + 1

        created_at = datetime.datetime.now().isoformat()
        await db.execute(
            "INSERT INTO entries (user_id, username, first_name, code, entry_number, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username or "", first_name or "", code, new_number, created_at),
        )
        await db.commit()
        return new_number, True, participant_code

async def get_user_entries(user_id: int) -> tuple[str, list[tuple[str,int]]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT participant_code FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        participant_code = row[0] if row else "—"
        cur = await db.execute(
            "SELECT code, entry_number FROM entries WHERE user_id = ? ORDER BY created_at", (user_id,)
        )
        rows = await cur.fetchall()
    return participant_code, [(r[0], r[1]) for r in rows]

async def export_csv() -> bytes:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT e.user_id, e.username, e.code, e.entry_number FROM entries e ORDER BY e.id"
        )
        rows = await cur.fetchall()
    buff = StringIO()
    writer = csv.writer(buff)
    writer.writerow(["user_id", "username", "code", "entry_number"])
    for r in rows:
        writer.writerow(r)
    return buff.getvalue().encode("utf-8")

async def draw_weighted_winner() -> dict | None:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT u.user_id, u.username, u.first_name, u.participant_code, COUNT(DISTINCT e.code) AS codes_count "
            "FROM users u LEFT JOIN entries e ON e.user_id = u.user_id "
            "GROUP BY u.user_id, u.username, u.first_name, u.participant_code"
        )
        users = await cur.fetchall()
        cur = await db.execute("SELECT user_id, code FROM entries")
        code_rows = await cur.fetchall()

    if not users:
        return None

    codes_by_user = defaultdict(list)
    for uid, code in code_rows:
        codes_by_user[uid].append(code)

    pool = []
    for uid, username, first_name, pcode, ccount in users:
        tickets = int(ccount or 0)
        if tickets <= 0:
            continue
        pool.append({
            "user_id": uid,
            "username": username or "",
            "first_name": first_name or "",
            "participant_code": pcode,
            "codes_count": tickets,
            "codes": codes_by_user.get(uid, []),
        })

    if not pool:
        return None

    weights = [p["codes_count"] for p in pool]
    total = sum(weights)
    r = random.uniform(0, total)
    upto = 0
    for p, w in zip(pool, weights):
        if upto + w >= r:
            p["tickets"] = w
            return p
        upto += w
    choice = random.choice(pool)
    choice["tickets"] = choice["codes_count"]
    return choice

# ---------- ХЕНДЛЕРЫ ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        pcode = await ensure_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)
        await db.commit()
    text = (
        f"Привет, {message.from_user.first_name or 'друг'} 👋\n\n"
        "Отправь кодовое слово, чтобы участвовать в розыгрыше.\n"
        "Чем больше найденных кодов (до 3), тем выше шанс при розыгрыше.\n\n"
        f"Твой постоянный ID участника: `{pcode}`\n"
        "Посмотреть свои коды: /my"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("my"))
async def cmd_my(message: types.Message) -> None:
    pcode, entries = await get_user_entries(message.from_user.id)
    if not entries:
        await message.answer(f"Твой ID: `{pcode}`\nТы ещё не вводил кодовые слова.", parse_mode="Markdown")
        return
    lines = [f"Твой ID: `{pcode}`", "Твои коды:"]
    for code, number in entries:
        lines.append(f"№{number} — {code}")
    await message.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("export"))
async def cmd_export(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    csv_bytes = await export_csv()
    file = BufferedInputFile(csv_bytes, filename="participants.csv")
    await message.answer_document(file, caption="CSV со списком участников")

@dp.message(Command("draw"))
async def cmd_draw(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    winner = await draw_weighted_winner()
    if not winner:
        await message.answer("Пока нет участников для розыгрыша.")
        return

    uname = f"@{winner['username']}" if winner["username"] else f"user_id={winner['user_id']}"
    codes_list = ", ".join(winner["codes"]) if winner["codes"] else "—"
    text = (
        "🎉 *Победитель розыгрыша!*\n"
        f"Игрок: *{winner['first_name']}* ({uname})\n"
        f"ID участника: `{winner['participant_code']}`\n"
        f"Найдено кодов: *{winner['codes_count']}* (вес в жеребьёвке)\n"
        f"Коды: {codes_list}"
    )
    await message.answer(text, parse_mode="Markdown")

    if getattr(config, "GROUP_CHAT_ID", None):
        try:
            await bot.send_message(config.GROUP_CHAT_ID, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Не удалось отправить анонс в группу: %s", e)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM entries")
        total_entries = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM entries")
        unique_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT code) FROM entries")
        unique_codes = (await cur.fetchone())[0]
    text = (
        "Статистика:\n"
        f"Всего заявок: {total_entries}\n"
        f"Уникальных пользователей: {unique_users}\n"
        f"Уникальных кодов: {unique_codes}"
    )
    await message.answer(text)

@dp.message()
async def handle_code(message: types.Message) -> None:
    if not message.text:
        return
    txt = message.text.strip()
    if not txt or txt.startswith("/"):
        return
    code = txt.lower()
    valid_codes = [c.lower() for c in config.VALID_CODES]
    if code not in valid_codes:
        await message.answer("Кодовое слово неверно. Попробуйте ещё раз.")
        return
    entry_number, is_new, pcode = await register_entry(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        code=code,
    )
    if is_new:
        await message.answer(
            f"Принято! Твой постоянный ID: `{pcode}`\nТы участник №{entry_number} в розыгрыше.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"Этот код уже зарегистрирован за тобой как №{entry_number}.\nТвой ID: `{pcode}`",
            parse_mode="Markdown"
        )

# ---------- ЗАПУСК: WEBHOOK или POLLING ----------
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://<app>.onrender.com/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
PORT = int(os.getenv("PORT", "10000"))

async def _on_startup(app: web.Application):
    await init_db()
    await set_bot_commands()
    if WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        logger.info("Webhook установлен: %s", WEBHOOK_URL)
    else:
        logger.info("WEBHOOK_URL не задан — будет POLLING при локальном запуске.")

async def _on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook()
        logger.info("Webhook снят.")
    except Exception:
        pass

def create_app() -> web.Application:
    app = web.Application()

    # healthcheck
    async def health(_):
        return web.Response(text="ok")
    app.router.add_get("/health", health)

    # ЯВНЫЙ обработчик Telegram webhook + проверка секрета
    async def telegram_webhook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        try:
            update = types.Update.model_validate(data)
            await dp.feed_update(bot, update)
        except Exception as e:
            logger.exception("Ошибка обработки webhook: %s", e)
            return web.Response(text="ok")

        return web.Response(text="ok")

    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    # регистрация хуков старта/остановки
    setup_application(app, dp, bot=bot, on_startup=[_on_startup], on_shutdown=[_on_shutdown])
    return app

async def _run_polling():
    await init_db()
    await set_bot_commands()
    logger.info("Бот запущен (polling). Ожидание сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if WEBHOOK_URL:
        logger.info("Старт WEBHOOK на порту %s", PORT)
        web.run_app(create_app(), host="0.0.0.0", port=PORT)
    else:
        try:
            asyncio.run(_run_polling())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Бот остановлен")
