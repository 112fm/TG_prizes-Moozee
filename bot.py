"""
Бот розыгрыша с кодовыми словами. (Supabase/Postgres)
"""
from __future__ import annotations

import os
import asyncio
import datetime
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiohttp import web
from aiogram.webhook.aiohttp_server import setup_application  # без SimpleRequestHandler

import config
import db  # наш новый слой БД

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- БОТ/DP ----------
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# ---------- КОНФИГ ----------
PART_LEN = config.PARTICIPANT_CODE_LEN
ALPHABET = config.PARTICIPANT_CODE_ALPHABET

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://<app>.onrender.com/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
PORT = int(os.getenv("PORT", "10000"))

# ---------- КОМАНДЫ МЕНЮ ----------
async def set_bot_commands() -> None:
    commands = [
        BotCommand(command="start", description="Начать и получить инструкцию"),
        BotCommand(command="my", description="Показать свои коды"),
        BotCommand(command="settings", description="Мои уведомления"),
        BotCommand(command="export", description="Выгрузить CSV (админ)"),
        BotCommand(command="draw", description="Розыгрыш (админ)"),
        BotCommand(command="stats", description="Статистика (админ)"),
        BotCommand(command="broadcast_video", description="Разослать про новый выпуск (админ)"),
    ]
    await bot.set_my_commands(commands)

# ---------- ХЕЛПЕРЫ ----------
def is_admin(user_id: int) -> bool:
    return user_id in set(getattr(config, "ADMIN_IDS", []) or [])

def build_settings_kb(prefs: dict) -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    def row(label: str, field: str, val: bool):
        icon = "✅" if val else "❌"
        b.button(text=f"{label}: {icon}", callback_data=f"toggle:{field}")
    row("Результаты розыгрыша", "notify_results", prefs.get("notify_results", True))
    row("Новые выпуски", "notify_new_video", prefs.get("notify_new_video", True))
    row("Стримы", "notify_streams", prefs.get("notify_streams", True))
    b.adjust(1)
    return b.as_markup()

async def broadcast_to(kind: str, text: str) -> int:
    """
    Массовая отправка c ограничением скорости.
    kind in {'video','results','streams'}
    """
    user_ids = await db.list_subscribers_for(kind)
    sent = 0
    batch = 20
    for i in range(0, len(user_ids), batch):
        chunk = user_ids[i:i+batch]
        tasks = []
        for uid in chunk:
            tasks.append(bot.send_message(uid, text))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sent += sum(1 for r in results if not isinstance(r, Exception))
        await asyncio.sleep(1)  # не агрим Телеграм
    return sent

# ---------- ХЕНДЛЕРЫ ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    pcode = await db.ensure_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name,
        PART_LEN, ALPHABET
    )
    text = (
        f"Привет, {message.from_user.first_name or 'друг'} 👋\n\n"
        "Отправь кодовое слово, чтобы участвовать в розыгрыше.\n"
        "Чем больше найденных кодов (до 3), тем выше шанс при розыгрыше.\n\n"
        f"Твой постоянный ID участника: `{pcode}`\n"
        "Посмотреть свои коды: /my\n"
        "Настроить уведомления: /settings"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("my"))
async def cmd_my(message: types.Message) -> None:
    pcode, entries = await db.get_user_entries(message.from_user.id)
    if not entries:
        await message.answer(f"Твой ID: `{pcode}`\nТы ещё не вводил кодовые слова.", parse_mode="Markdown")
        return
    lines = [f"Твой ID: `{pcode}`", "Твои коды:"]
    for code, number in entries:
        lines.append(f"№{number} — {code}")
    await message.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("settings"))
async def cmd_settings(message: types.Message) -> None:
    # убедимся, что пользователь создан
    await db.ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name,
                         PART_LEN, ALPHABET)
    prefs = await db.get_prefs(message.from_user.id)
    await message.answer("Что присылать?", reply_markup=build_settings_kb(prefs))

@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle_pref(cq: types.CallbackQuery):
    field = cq.data.split(":", 1)[1]
    prefs = await db.toggle_pref(cq.from_user.id, field)
    await cq.message.edit_reply_markup(reply_markup=build_settings_kb(prefs))
    await cq.answer("Сохранено")

@dp.message(Command("export"))
async def cmd_export(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    csv_bytes = await db.export_csv()
    file = BufferedInputFile(csv_bytes, filename="participants.csv")
    await message.answer_document(file, caption="CSV со списком участников")

@dp.message(Command("draw"))
async def cmd_draw(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    winner = await db.draw_weighted_winner()
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
    # простая статистика через CSV мы уже можем получить; детальную статистику перенесём позже
    await message.answer("Статистика через /export (CSV). Отдельный отчёт добавим позже.")

@dp.message(Command("broadcast_video"))
async def cmd_broadcast_video(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору.")
        return
    # Формат: /broadcast_video <ссылка> <текст...>
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Формат:\n/broadcast_video <ссылка> <текст>\n\nНапример:\n"
            "/broadcast_video https://youtu.be/abcde Новый выпуск на канале!"
        )
        return
    url = parts[1]
    text = parts[2]
    full = f"🎬 Новый выпуск!\n{text}\n{url}"
    sent = await broadcast_to("video", full)
    await message.answer(f"Отправлено {sent} пользователям (подписка «Новые выпуски»).")

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

    entry_number, is_new, pcode = await db.register_entry(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        code=code,
        part_len=PART_LEN,
        alphabet=ALPHABET,
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
async def _on_startup(app: web.Application):
    await db.init()           # <— ВАЖНО: инициализация Postgres
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

    async def health(_): return web.Response(text="ok")
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
            await db.init()  # на всякий случай
            update = types.Update.model_validate(data)
            await dp.feed_update(bot, update)
        except Exception as e:
            logger.exception("Ошибка обработки webhook: %s", e)
            return web.Response(text="ok")

        return web.Response(text="ok")

    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    setup_application(app, dp, bot=bot, on_startup=[_on_startup], on_shutdown=[_on_shutdown])
    return app

async def _run_polling():
    await db.init()
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
