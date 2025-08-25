from __future__ import annotations

import os
import sys
import asyncio
import csv
import datetime as dt
import logging
import random
import secrets
from io import StringIO
from collections import defaultdict
import socket
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from typing import Dict, List

# Windows: нужна эта политика для psycopg async
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web
from aiogram.webhook.aiohttp_server import setup_application

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import tuple_row

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("prizes-bot")

dp = Dispatcher(storage=MemoryStorage())
# ✅ Новый способ: default=DefaultBotProperties(...)
bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True),
)

PART_LEN = config.PARTICIPANT_CODE_LEN
ALPHABET = config.PARTICIPANT_CODE_ALPHABET

# Канал, на который нужна подписка
REQ_CH_USERNAME = os.getenv("REQUIRED_CHANNEL_USERNAME", "projectglml").lstrip("@")
REQ_CH_ID = int(os.getenv("REQUIRED_CHANNEL_ID", "-1002675692681"))

POOL: AsyncConnectionPool | None = None


def make_participant_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(PART_LEN))


def is_admin(user_id: int) -> bool:
    return user_id in set(getattr(config, "ADMIN_IDS", []) or [])


INIT_SQL = """
create table if not exists public.users (
    user_id bigint primary key,
    username text,
    first_name text,
    participant_code text unique not null
);

create table if not exists public.entries (
    id bigserial primary key,
    user_id bigint not null references public.users(user_id) on delete cascade,
    username text,
    first_name text,
    code text not null,
    entry_number int not null,
    created_at timestamp not null default now()
);
create unique index if not exists idx_entries_user_code on public.entries(user_id, code);

create table if not exists public.user_prefs (
    user_id bigint primary key references public.users(user_id) on delete cascade,
    notify_results boolean not null default true,
    notify_new_video boolean not null default true,
    notify_streams boolean not null default true,
    created_at timestamp not null default now(),
    updated_at timestamp not null default now()
);
"""


def _mask_url(u: str) -> str:
    try:
        p = urlparse(u)
        if p.password:
            return u.replace(p.password, "****")
    except Exception:
        pass
    return u


def _get_dsn() -> str:
    raw = (getattr(config, "DATABASE_URL", None) or getattr(config, "DB_URL", None) or "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL/DB_URL is not set in config")

    raw = raw.replace("\n", "").replace("\r", "").strip()
    u = urlparse(raw)
    if u.scheme not in ("postgresql", "postgres"):
        raise RuntimeError("DATABASE_URL must start with postgresql:// or postgres://")

    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q["sslmode"] = "require"
    q.setdefault("connect_timeout", "8")
    q.setdefault("application_name", "tg_prizes_bot")

    host = u.hostname or ""
    port = u.port or 5432
    # Для Supabase на 5432 — используем 6543 (pooler)
    if (".supabase.co" in host or ".supabase.net" in host) and port == 5432:
        port = 6543

    hostaddr_env = os.getenv("PGHOSTADDR", "").strip()
    if hostaddr_env:
        q["hostaddr"] = hostaddr_env
    else:
        try:
            infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            if infos:
                q["hostaddr"] = infos[0][4][0]
        except Exception as e:
            logger.warning("DNS resolve failed for %s:%s (%s). Will connect by hostname only.", host, port, e)

    userinfo = ""
    if u.username:
        userinfo = u.username
        if u.password:
            userinfo += f":{u.password}"
        userinfo += "@"
    netloc = f"{userinfo}{host}:{port}"

    new_query = urlencode(q)
    final = urlunparse((u.scheme, netloc, u.path, u.params, new_query, u.fragment))
    logger.info("DB DSN prepared: %s", _mask_url(final))
    if "hostaddr" in q:
        logger.info("DB host=%s port=%s hostaddr=%s", host, port, q["hostaddr"])
    else:
        logger.info("DB host=%s port=%s (no hostaddr)", host, port)
    return final


async def init_db() -> None:
    global POOL
    if POOL is None:
        POOL = AsyncConnectionPool(
            conninfo=_get_dsn(),
            max_size=8,
            kwargs={"autocommit": True},
            timeout=30,
        )
        await POOL.open(wait=True)

    async with POOL.connection() as conn:
        await conn.execute(INIT_SQL)
    logger.info("Postgres готов: таблицы проверены/созданы.")


async def set_bot_commands() -> None:
    base_cmds = [
        BotCommand(command="start", description="Начать"),
        BotCommand(command="my", description="Мои коды"),
        BotCommand(command="prefs", description="Настройки уведомлений"),
    ]
    await bot.set_my_commands(base_cmds, scope=BotCommandScopeAllPrivateChats())

    admin_cmds = base_cmds + [
        BotCommand(command="admin", description="Админ‑панель"),
        BotCommand(command="export", description="Выгрузить CSV"),
        BotCommand(command="draw", description="Розыгрыш"),
        BotCommand(command="stats", description="Статистика"),
    ]
    for admin_id in getattr(config, "ADMIN_IDS", []):
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning("Не удалось назначить команды для админа %s: %s", admin_id, e)


def channel_url() -> str:
    # используем внутрителеграмную ссылку без web‑превью
    return f"tg://resolve?domain={REQ_CH_USERNAME}" if REQ_CH_USERNAME else "tg://resolve"


def not_subscribed_kb(code_lc: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="→ Открыть канал", url=channel_url())],
        [InlineKeyboardButton(text="✅ Подписался, проверить", callback_data=f"subchk:{code_lc}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def is_subscribed(user_id: int) -> bool:
    ok_status = {"member", "administrator", "creator"}
    if REQ_CH_USERNAME:
        try:
            m = await bot.get_chat_member(chat_id=f"@{REQ_CH_USERNAME}", user_id=user_id)
            if m.status in ok_status:
                return True
        except Exception as e:
            logger.info("get_chat_member by username failed: %s", e)
    try:
        m = await bot.get_chat_member(chat_id=REQ_CH_ID, user_id=user_id)
        return m.status in ok_status
    except Exception as e:
        logger.info("get_chat_member by id failed: %s", e)
        return False


async def ensure_user(user_id: int, username: str | None, first_name: str | None) -> str:
    await init_db()
    async with POOL.connection() as conn:  # type: ignore[union-attr]
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute("select participant_code from public.users where user_id = %s", (user_id,))
            row = await cur.fetchone()
            if row:
                await conn.execute(
                    "update public.users set username = %s, first_name = %s where user_id = %s",
                    (username or "", first_name or "", user_id),
                )
                await conn.execute("""insert into public.user_prefs(user_id)
                                      values (%s) on conflict (user_id) do nothing""", (user_id,))
                return row[0]

        # генерируем уникальный participant_code
        while True:
            pc = make_participant_code()
            async with conn.cursor(row_factory=tuple_row) as cur:
                await cur.execute("select 1 from public.users where participant_code = %s", (pc,))
                if await cur.fetchone() is None:
                    break

        await conn.execute(
            "insert into public.users(user_id, username, first_name, participant_code) values (%s, %s, %s, %s)",
            (user_id, username or "", first_name or "", pc),
        )
        await conn.execute("""insert into public.user_prefs(user_id) values (%s)
                              on conflict (user_id) do nothing""", (user_id,))
        return pc


async def register_entry(user_id: int, username: str | None, first_name: str | None, code: str) -> tuple[int, bool, str]:
    await init_db()
    participant_code = await ensure_user(user_id, username, first_name)
    async with POOL.connection() as conn:  # type: ignore[union-attr]
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute("select entry_number from public.entries where user_id = %s and code = %s", (user_id, code))
            row = await cur.fetchone()
            if row:
                return row[0], False, participant_code

        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute("select coalesce(max(entry_number), 0) from public.entries")
            max_number = (await cur.fetchone())[0] or 0
        new_number = int(max_number) + 1

        created_at = dt.datetime.now()
        await conn.execute(
            "insert into public.entries(user_id, username, first_name, code, entry_number, created_at) "
            "values (%s, %s, %s, %s, %s, %s)",
            (user_id, username or "", first_name or "", code, new_number, created_at),
        )
        return new_number, True, participant_code


async def get_user_entries(user_id: int) -> tuple[str, list[tuple[str, int]]]:
    await init_db()
    async with POOL.connection() as conn:  # type: ignore[union-attr]
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute("select participant_code from public.users where user_id = %s", (user_id,))
            row = await cur.fetchone()
            participant_code = row[0] if row else "—"

        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(
                "select code, entry_number from public.entries where user_id = %s order by created_at",
                (user_id,),
            )
            rows = await cur.fetchall()

    return participant_code, [(r[0], r[1]) for r in rows]


async def export_csv() -> bytes:
    await init_db()
    async with POOL.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:  # type: ignore[union-attr]
        await cur.execute(
            "select e.user_id, e.username, e.code, e.entry_number "
            "from public.entries e order by e.id"
        )
        rows = await cur.fetchall()

    buff = StringIO()
    writer = csv.writer(buff)
    writer.writerow(["user_id", "username", "code", "entry_number"])
    for r in rows:
        writer.writerow(r)
    return buff.getvalue().encode("utf-8")


async def draw_weighted_winner() -> dict | None:
    await init_db()
    async with POOL.connection() as conn:  # type: ignore[union-attr]
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(
                "select u.user_id, u.username, u.first_name, u.participant_code, "
                "count(distinct e.code) as codes_count "
                "from public.users u left join public.entries e on e.user_id = u.user_id "
                "group by u.user_id, u.username, u.first_name, u.participant_code"
            )
            users = await cur.fetchall()

        async with POOL.connection() as conn2, conn2.cursor(row_factory=tuple_row) as cur2:
            await cur2.execute("select user_id, code from public.entries")
            code_rows = await cur2.fetchall()

    if not users:
        return None

    codes_by_user: Dict[int, List[str]] = defaultdict(list)
    for uid, code in code_rows:
        codes_by_user[int(uid)].append(code)

    pool = []
    for uid, username, first_name, pcode, ccount in users:
        tickets = int(ccount or 0)
        if tickets <= 0:
            continue
        pool.append({
            "user_id": int(uid),
            "username": username or "",
            "first_name": first_name or "",
            "participant_code": pcode,
            "codes_count": tickets,
            "codes": codes_by_user.get(int(uid), []),
        })

    if not pool:
        return None

    weights = [p["codes_count"] for p in pool]
    total = sum(weights)
    r = random.uniform(0, total)
    upto = 0.0
    for p, w in zip(pool, weights):
        if upto + w >= r:
            p["tickets"] = w
            return p
        upto += w
    choice = random.choice(pool)
    choice["tickets"] = choice["codes_count"]
    return choice


async def get_prefs(user_id: int) -> Dict[str, bool]:
    await init_db()
    async with POOL.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:  # type: ignore[union-attr]
        await cur.execute(
            "select notify_results, notify_new_video, notify_streams from public.user_prefs where user_id = %s",
            (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            await conn.execute(
                "insert into public.user_prefs(user_id) values (%s) on conflict (user_id) do nothing",
                (user_id,)
            )
            return {"notify_results": True, "notify_new_video": True, "notify_streams": True}
    return {
        "notify_results": bool(row[0]),
        "notify_new_video": bool(row[1]),
        "notify_streams": bool(row[2]),
    }


async def toggle_pref(user_id: int, field: str) -> Dict[str, bool]:
    assert field in ("notify_results", "notify_new_video", "notify_streams")
    await init_db()
    async with POOL.connection() as conn:  # type: ignore[union-attr]
        await conn.execute(
            "insert into public.user_prefs(user_id) values (%s) on conflict (user_id) do nothing",
            (user_id,)
        )
        await conn.execute(
            f"update public.user_prefs set {field} = not {field}, updated_at = now() where user_id = %s",
            (user_id,)
        )
    return await get_prefs(user_id)


async def list_subscribers_for(kind: str) -> List[int]:
    field_map = {"video": "notify_new_video", "results": "notify_results", "streams": "notify_streams"}
    field = field_map[kind]
    await init_db()
    async with POOL.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:  # type: ignore[union-attr]
        await cur.execute(
            f"select u.user_id from public.user_prefs p join public.users u on u.user_id = p.user_id where p.{field} = true"
        )
        rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


class BroadcastState(StatesGroup):
    btype = State()
    text = State()


def admin_keyboard() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Экспорт CSV", callback_data="admin:export")
    kb.button(text="🎯 Розыгрыш", callback_data="admin:draw")
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="📢 Рассылка: видео", callback_data="admin:broadcast:video")
    kb.button(text="🔴 Рассылка: стрим", callback_data="admin:broadcast:streams")
    kb.button(text="🏆 Рассылка: результаты", callback_data="admin:broadcast:results")
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def prefs_keyboard(prefs: Dict[str, bool]) -> types.InlineKeyboardMarkup:
    def mark(v: bool) -> str:
        return "✅" if v else "❌"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{mark(prefs['notify_new_video'])} Новые видео", callback_data="prefs:toggle:notify_new_video")
    kb.button(text=f"{mark(prefs['notify_streams'])} Стримы", callback_data="prefs:toggle:notify_streams")
    kb.button(text=f"{mark(prefs['notify_results'])} Результаты розыгрышей", callback_data="prefs:toggle:notify_results")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    logger.info("/start from user_id=%s", message.from_user.id)
    pcode = await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = (
        "👋 Йо, ты в Moozee_Movie Prizes — тут скины не падают, тут их вырывают.\n"
        "Хочешь шанс? Всё просто:\n"
        "1️⃣ Находишь кодовое слово в видосе.\n"
        "2️⃣ Вводишь его сюда.\n"
        "3️⃣ Бот даёт тебе номер, и ты попадаешь в список розыгрыша.\n\n"
        "⚠️ Но номер получают только те, кто подписан на наш Telegram‑канал 👉 "
        f"<a href=\"tg://resolve?domain={REQ_CH_USERNAME}\">@{REQ_CH_USERNAME}</a>\n"
        "Игра честная: без подписки — без шанса.\n\n"
        "Ну что, готов проверить удачу?\n\n"
        f"Твой постоянный ID участника: <code>{pcode}</code>\n"
        "Команды: /my — твои коды, /prefs — уведомления."
    )
    await message.answer(text)


@dp.message(Command("my"))
async def cmd_my(message: types.Message) -> None:
    logger.info("/my from user_id=%s", message.from_user.id)
    pcode, entries = await get_user_entries(message.from_user.id)
    if not entries:
        await message.answer(f"Твой ID: <code>{pcode}</code>\nТы ещё не вводил кодовые слова.")
        return
    lines = [f"Твой ID: <code>{pcode}</code>", "Твои коды:"]
    for code, number in entries:
        lines.append(f"№{number} — {code}")
    await message.answer("\n".join(lines))


@dp.message(Command("prefs"))
async def cmd_prefs(message: types.Message) -> None:
    logger.info("/prefs from user_id=%s", message.from_user.id)
    await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    prefs = await get_prefs(message.from_user.id)
    await message.answer("Выбери, какие уведомления получать:", reply_markup=prefs_keyboard(prefs))


@dp.callback_query(F.data.startswith("prefs:toggle:"))
async def cb_prefs_toggle(cb: CallbackQuery):
    logger.info("prefs toggle %s by user_id=%s", cb.data, cb.from_user.id)
    field = cb.data.split(":", 2)[2]
    prefs = await toggle_pref(cb.from_user.id, field)
    await cb.message.edit_text("Выбери, какие уведомления получать:", reply_markup=prefs_keyboard(prefs))
    await cb.answer("Обновлено")


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        logger.info("non-admin tried /admin user_id=%s", message.from_user.id)
        return
    logger.info("open admin panel user_id=%s", message.from_user.id)
    await message.answer("Админ‑панель:", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin:stats")
async def cb_admin_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Недоступно", show_alert=True)
    logger.info("admin:stats by %s", cb.from_user.id)
    await cb.answer("Считаю…")
    await init_db()
    async with POOL.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:  # type: ignore[union-attr]
        await cur.execute("select count(*) from public.entries")
        total_entries = (await cur.fetchone())[0]
        await cur.execute("select count(distinct user_id) from public.entries")
        unique_users = (await cur.fetchone())[0]
        await cur.execute("select count(distinct code) from public.entries")
        unique_codes = (await cur.fetchone())[0]
        # Статистика подписок
        await cur.execute("select count(*) from public.user_prefs where notify_new_video = true")
        subs_video = (await cur.fetchone())[0]
        await cur.execute("select count(*) from public.user_prefs where notify_streams = true")
        subs_streams = (await cur.fetchone())[0]
        await cur.execute("select count(*) from public.user_prefs where notify_results = true")
        subs_results = (await cur.fetchone())[0]
    text = (
        "Статистика:\n"
        f"Всего заявок: {total_entries}\n"
        f"Уникальных пользователей: {unique_users}\n"
        f"Уникальных кодов: {unique_codes}\n\n"
        f"Уведомления — новые видео: {subs_video}\n"
        f"Уведомления — стримы: {subs_streams}\n"
        f"Уведомления — результаты: {subs_results}"
    )
    await cb.message.answer(text)


@dp.message(Command("export"))
async def cmd_export(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        return
    logger.info("admin export by %s", message.from_user.id)
    await message.answer("Готовлю CSV…")
    csv_bytes = await export_csv()
    file = BufferedInputFile(csv_bytes, filename="participants.csv")
    await message.answer_document(file, caption="CSV со списком участников")


@dp.callback_query(F.data == "admin:export")
async def cb_admin_export(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Недоступно", show_alert=True)
    logger.info("admin:export by %s", cb.from_user.id)
    await cb.answer("Готовлю CSV…")
    try:
        csv_bytes = await export_csv()
        file = BufferedInputFile(csv_bytes, filename="participants.csv")
        await cb.message.answer_document(file, caption="CSV со списком участников")
    except Exception as e:
        logger.exception("Ошибка экспорта: %s", e)
        await cb.message.answer(f"Ошибка экспорта: {e}")


@dp.message(Command("draw"))
async def cmd_draw(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        return
    logger.info("admin draw by %s (message)", message.from_user.id)
    await message.answer("Запускаю розыгрыш…")
    winner = await draw_weighted_winner()
    if not winner:
        await message.answer("Пока нет участников для розыгрыша.")
        return

    uname = f"@{winner['username']}" if winner["username"] else f"user_id={winner['user_id']}"
    codes_list = ", ".join(winner["codes"]) if winner["codes"] else "—"
    text = (
        "🎉 <b>Победитель розыгрыша!</b>\n"
        f"Игрок: <b>{winner['first_name']}</b> ({uname})\n"
        f"ID участника: <code>{winner['participant_code']}</code>\n"
        f"Найдено кодов: <b>{winner['codes_count']}</b> (вес)\n"
        f"Коды: {codes_list}"
    )
    await message.answer(text)


@dp.callback_query(F.data == "admin:draw")
async def cb_admin_draw(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Недоступно", show_alert=True)
    logger.info("admin draw by %s (callback)", cb.from_user.id)
    await cb.answer("Делаю розыгрыш…")
    try:
        winner = await draw_weighted_winner()
        if not winner:
            return await cb.message.answer("Пока нет участников для розыгрыша.")
        uname = f"@{winner['username']}" if winner["username"] else f"user_id={winner['user_id']}"
        codes_list = ", ".join(winner["codes"]) if winner["codes"]else "—"
        text = (
            "🎉 <b>Победитель розыгрыша!</b>\n"
            f"Игрок: <b>{winner['first_name']}</b> ({uname})\n"
            f"ID участника: <code>{winner['participant_code']}</code>\n"
            f"Найдено кодов: <b>{winner['codes_count']}</b>\n"
            f"Коды: {codes_list}"
        )
        await cb.message.answer(text)
    except Exception as e:
        logger.exception("Ошибка розыгрыша: %s", e)
        await cb.message.answer(f"Ошибка розыгрыша: {e}")


@dp.callback_query(F.data.startswith("admin:broadcast:"))
async def cb_admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Недоступно", show_alert=True)
    _, _, btype = cb.data.split(":")
    logger.info("open broadcast type=%s by %s", btype, cb.from_user.id)
    await state.set_state(BroadcastState.btype)
    await state.update_data(btype=btype)
    await state.set_state(BroadcastState.text)
    await cb.answer("Ок")
    await cb.message.answer(
        "Пришли текст сообщения для рассылки (можно с ссылками). "
        "После этого я покажу, сколько получателей, и попрошу подтверждение.\n\n"
        "Чтобы отменить — /cancel"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    logger.info("cancel broadcast by %s", message.from_user.id)
    await state.clear()
    await message.answer("Ок, отменил.")


@dp.message(BroadcastState.text)
async def broadcast_collect_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer("Недоступно.")
    data = await state.get_data()
    btype = data.get("btype", "video")
    text = message.html_text or message.text or ""
    await state.update_data(text=text)
    logger.info("broadcast preview type=%s by %s", btype, message.from_user.id)

    kind_label = {"video": "Новое видео", "streams": "Стрим", "results": "Результаты"}[btype]
    subs = await list_subscribers_for(btype)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Разослать ({len(subs)})", callback_data="broadcast:confirm")
    kb.button(text="❌ Отмена", callback_data="broadcast:cancel")
    kb.adjust(2)
    await message.answer(
        f"Тип: <b>{kind_label}</b>\nПолучателей: <b>{len(subs)}</b>\n\nПредпросмотр:\n{text}",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "broadcast:cancel")
async def cb_broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    logger.info("broadcast cancelled by %s", cb.from_user.id)
    await cb.answer("Отменено")
    await cb.message.answer("Рассылка отменена.")


async def _send_broadcast(btype: str, text: str, admin_chat_id: int):
    subs = await list_subscribers_for(btype)
    if not subs:
        await bot.send_message(admin_chat_id, "Получателей нет. Рассылка не отправлена.")
        return
    sent = 0
    failed = 0
    logger.info("broadcast start type=%s recipients=%s", btype, len(subs))
    for uid in subs:
        try:
            # Превью ссылок глобально отключены через DefaultBotProperties
            await bot.send_message(uid, text)
            sent += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                logger.warning("Не доставлено %s: %s", uid, e)
        await asyncio.sleep(0.05)
    logger.info("broadcast done type=%s sent=%s failed=%s", btype, sent, failed)
    await bot.send_message(
        admin_chat_id,
        f"Готово.\nТип: {btype}\nВсего получателей: {len(subs)}\nДоставлено: {sent}\nОшибок: {failed}"
    )


@dp.callback_query(F.data == "broadcast:confirm")
async def cb_broadcast_confirm(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Недоступно", show_alert=True)
    data = await state.get_data()
    btype = data.get("btype", "video")
    text = data.get("text", "")
    await state.clear()
    logger.info("broadcast confirmed type=%s by %s", btype, cb.from_user.id)
    await cb.answer("Отправляю…")
    await cb.message.answer("Стартую рассылку… Отчёт пришлю сюда.")
    asyncio.create_task(_send_broadcast(btype=btype, text=text, admin_chat_id=cb.from_user.id))


UNSUB_TEXT = (
    "Эй, халявы не будет. Только свои забирают скины.\n"
    f"Подпишись на 👉 <a href=\"tg://resolve?domain={REQ_CH_USERNAME}\">@{REQ_CH_USERNAME}</a>\n"
    "и жми «✅ Подписался, проверить»."
)


@dp.message()
async def handle_code(message: types.Message) -> None:
    if not message.text:
        return
    txt = message.text.strip()
    if not txt or txt.startswith("/"):
        return

    code_lc = txt.lower()
    valid_codes = [c.lower() for c in config.VALID_CODES]
    if code_lc not in valid_codes:
        logger.info("invalid code from user_id=%s text=%s", message.from_user.id, txt)
        await message.answer("Кодовое слово неверно. Попробуй ещё раз.")
        return

    if not await is_subscribed(message.from_user.id):
        logger.info("not subscribed user_id=%s", message.from_user.id)
        await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        await message.answer(UNSUB_TEXT, reply_markup=not_subscribed_kb(code_lc))
        return

    entry_number, is_new, pcode = await register_entry(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        code=code_lc,
    )
    if is_new:
        logger.info("entry added user_id=%s code=%s number=%s", message.from_user.id, code_lc, entry_number)
        await message.answer(
            f"Принято! Твой постоянный ID: <code>{pcode}</code>\nТы участник №{entry_number} в розыгрыше."
        )
    else:
        logger.info("entry duplicate user_id=%s code=%s number=%s", message.from_user.id, code_lc, entry_number)
        await message.answer(
            f"Этот код уже зарегистрирован за тобой как №{entry_number}.\nТвой ID: <code>{pcode}</code>"
        )


@dp.callback_query(F.data.startswith("subchk:"))
async def cb_check_sub(cb: CallbackQuery):
    await cb.answer("Проверяю…")
    code_lc = cb.data.split(":", 1)[1]
    valid_codes = [c.lower() for c in config.VALID_CODES]
    if code_lc not in valid_codes:
        return await cb.message.answer("Кодовое слово устарело или неверно.")

    if not await is_subscribed(cb.from_user.id):
        logger.info("sub check: still not subscribed user_id=%s", cb.from_user.id)
        await cb.message.answer(
            "Ты ещё не подписан. Подпишись и жми «✅ Подписался, проверить».",
            reply_markup=not_subscribed_kb(code_lc),
        )
        return

    entry_number, is_new, pcode = await register_entry(
        user_id=cb.from_user.id,
        username=cb.from_user.username,
        first_name=cb.from_user.first_name,
        code=code_lc,
    )
    if is_new:
        logger.info("sub check: added after subscribe user_id=%s code=%s", cb.from_user.id, code_lc)
        await cb.message.answer(
            f"Отлично! Подписка есть ✅\nТвой ID: <code>{pcode}</code>\nТы участник №{entry_number} в розыгрыше."
        )
    else:
        await cb.message.answer(
            f"Подписка подтверждена ✅\nЭтот код уже был за тобой как №{entry_number}.\nТвой ID: <code>{pcode}</code>"
        )


# WEBHOOK / POLLING
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
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
    global POOL
    if POOL:
        await POOL.close()
        POOL = None


async def _process_update_async(data: dict) -> None:
    try:
        update = types.Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.exception("Ошибка обработки апдейта: %s", e)


def create_app() -> web.Application:
    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/health", health)

    async def telegram_webhook(request: web.Request) -> web.Response:
        if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        # мгновенный ответ + обработка в фоне
        asyncio.create_task(_process_update_async(data))
        return web.Response(text="ok")

    app.router.add_post(WEBHOOK_PATH, telegram_webhook)
    setup_application(app, dp, bot=bot, on_startup=[_on_startup], on_shutdown=[_on_shutdown])
    return app


async def _run_polling():
    await init_db()
    await set_bot_commands()
    logger.info("Бот запущен (polling). Ожидание сообщений...")
    try:
        await dp.start_polling(bot)
    finally:
        global POOL
        if POOL:
            await POOL.close()
            POOL = None


if __name__ == "__main__":
    if WEBHOOK_URL:
        logger.info("Старт WEBHOOK на порту %s", PORT)
        web.run_app(create_app(), host="0.0.0.0", port=PORT)
    else:
        try:
            asyncio.run(_run_polling())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Бот остановлен")
