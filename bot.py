from __future__ import annotations

"""
Бот розыгрыша с кодовыми словами (Postgres/Supabase).
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
from aiogram.webhook.aiohttp_server import setup_application

import psycopg
from psycopg_pool import AsyncConnectionPool

import config

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- БОТ/DP ----------
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# ---------- КОНФИГ ----------
PART_LEN = config.PARTICIPANT_CODE_LEN
ALPHABET = config.PARTICIPANT_CODE_ALPHABET
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # postgresql://.../postgres?sslmode=require

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан — укажите строку подключения Supabase в переменных окружения Render.")

# Пул соединений с Postgres
pg_pool: AsyncConnectionPool | None = None


# ---------- УТИЛЫ ----------
def make_participant_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(PART_LEN))


def is_admin(user_id: int) -> bool:
    return user_id in set(getattr(config, "ADMIN_IDS", []) or [])


# ---------- БАЗА: ИНИТ/ХЕЛПЕРЫ ----------
async def init_pool() -> None:
    """Создать пул соединений, если ещё не создан."""
    global pg_pool
    if pg_pool is None:
        # Пул лениво открывается, а дальше переиспользуется
        pg_pool = AsyncConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            timeout=30,
        )
        await pg_pool.open()
        logger.info("Подключение к Postgres (Supabase) установлено.")


async def init_db() -> None:
    """Миграции (создание таблиц/индексов)."""
    await init_pool()
    assert pg_pool is not None

    async with pg_pool.connection() as conn:
        async with conn.cursor() as cur:
            # Таблица пользователей со стабильным participant_code
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    participant_code TEXT UNIQUE NOT NULL
                );
                """
            )

            # Таблица заявок
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id),
                    username TEXT,
                    first_name TEXT,
                    code TEXT NOT NULL,
                    entry_number INT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            # Уникальная пара (user_id, code)
            await cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_user_code ON entries(user_id, code);"
            )

        await conn.commit()

    logger.info("База данных и таблицы готовы к работе.")


# Небольшие хелперы для запросов
async def pg_fetchone(sql: str, params: tuple = ()) -> tuple | None:
    assert pg_pool is not None
    async with pg_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def pg_fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    assert pg_pool is not None
    async with pg_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def pg_execute(sql: str, params: tuple = ()) -> None:
    assert pg_pool is not None
    async with pg_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
        await conn.commit()


# ---------- ЛОГИКА ----------
async def ensure_user(user_id: int, username: str | None, first_name: str | None) -> str:
    """Убедиться, что пользователь есть в users и имеет постоянный participant_code. Вернёт participant_code."""
    row = await pg_fetchone("SELECT participant_code FROM users WHERE user_id = %s", (user_id,))
    if row:
        await pg_execute(
            "UPDATE users SET username = %s, first_name = %s WHERE user_id = %s",
            (username or "", first_name or "", user_id),
        )
        return row[0]

    # Сгенерировать уникальный participant_code
    while True:
        pc = make_participant_code()
        exists = await pg_fetchone("SELECT 1 FROM users WHERE participant_code = %s", (pc,))
        if not exists:
            break

    await pg_execute(
        "INSERT INTO users (user_id, username, first_name, participant_code) VALUES (%s, %s, %s, %s)",
        (user_id, username or "", first_name or "", pc),
    )
    return pc


async def register_entry(user_id: int, username: str | None, first_name: str | None, code: str) -> tuple[int, bool, str]:
    """
    Зарегистрировать код для пользователя.
    Возвращает (entry_number, is_new, participant_code)
    is_new=False — если этот код уже был у пользователя.
    """
    participant_code = await ensure_user(user_id, username, first_name)

    row = await pg_fetchone(
        "SELECT entry_number FROM entries WHERE user_id = %s AND code = %s",
        (user_id, code),
    )
    if row:
        return row[0], False, participant_code

    row = await pg_fetchone("SELECT COALESCE(MAX(entry_number), 0) FROM entries")
    new_number = (row[0] if row else 0) + 1

    await pg_execute(
        """
        INSERT INTO entries (user_id, username, first_name, code, entry_number)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, username or "", first_name or "", code, new_number),
    )
    return new_number, True, participant_code


async def get_user_entries(user_id: int) -> tuple[str, list[tuple[str, int]]]:
    row = await pg_fetchone("SELECT participant_code FROM users WHERE user_id = %s", (user_id,))
    participant_code = row[0] if row else "—"
    rows = await pg_fetchall(
        "SELECT code, entry_number FROM entries WHERE user_id = %s ORDER BY created_at",
        (user_id,),
    )
    return participant_code, [(r[0], r[1]) for r in rows]


async def export_csv() -> bytes:
    rows = await pg_fetchall(
        "SELECT e.user_id, e.username, e.code, e.entry_number FROM entries e ORDER BY e.id"
    )
    buff = StringIO()
    writer = csv.writer(buff)
    writer.writerow(["user_id", "username", "code", "entry_number"])
    for r in rows:
        writer.writerow(r)
    return buff.getvalue().encode("utf-8")


async def draw_weighted_winner() -> dict | None:
    users = await pg_fetchall(
        """
        SELECT u.user_id, u.username, u.first_name, u.participant_code, COUNT(DISTINCT e.code) AS codes_count
        FROM users u
        LEFT JOIN entries e ON e.user_id = u.user_id
        GROUP BY u.user_id, u.username, u.first_name, u.participant_code
        """
    )
    code_rows = await pg_fetchall("SELECT user_id, code FROM entries")

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
        pool.append(
            {
                "user_id": uid,
                "username": username or "",
                "first_name": first_name or "",
                "participant_code": pcode,
                "codes_count": tickets,
                "codes": codes_by_user.get(uid, []),
            }
        )

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
    pcode = await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
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
    row = await pg_fetchone("SELECT COUNT(*) FROM entries")
    total_entries = row[0] if row else 0
    row = await pg_fetchone("SELECT COUNT(DISTINCT user_id) FROM entries")
    unique_users = row[0] if row else 0
    row = await pg_fetchone("SELECT COUNT(DISTINCT code) FROM entries")
    unique_codes = row[0] if row else 0
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
            parse_mode="Markdown",
        )
    else:
        await message.answer(
            f"Этот код уже зарегистрирован за тобой как №{entry_number}.\nТвой ID: `{pcode}`",
            parse_mode="Markdown",
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
    if pg_pool is not None:
        await pg_pool.close()
        logger.info("Пул соединений с Postgres закрыт.")


def create_app() -> web.Application:
    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/health", health)

    async def telegram_webhook(request: web.Request) -> web.Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        try:
            # На всякий случай гарантируем инициализацию
            await init_db()
            update = types.Update.model_validate(data)
            await dp.feed_update(bot, update)
        except Exception as e:
            logger.exception("Ошибка обработки webhook: %s", e)
            # Telegram нужно вернуть 200, иначе он будет ретраить
            return web.Response(text="ok")

        return web.Response(text="ok")

    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

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
