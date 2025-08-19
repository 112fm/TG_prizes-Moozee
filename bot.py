"""
Основной файл Telegram‑бота для проведения розыгрыша по кодовым словам.

Этот скрипт реализует бот на базе библиотеки aiogram (версия 3.22.0).
Пользователи могут отправлять кодовые слова. Если введённый код
присутствует в списке допустимых кодов (см. config.VALID_CODES), бот
создаёт новую запись в базе данных (SQLite) и присваивает этому
участнику уникальный номер. При повторном вводе уже учтённого кода
пользователь получает тот же номер. Администратор может выгрузить
список участников в CSV, провести случайный розыгрыш и посмотреть
статистику.

Перед запуском убедитесь, что вы указали свои данные в файле
config.py (BOT_TOKEN, ADMIN_ID и список VALID_CODES).

Для запуска бота используйте команду:

    python bot.py

Файл requirements.txt содержит необходимые зависимости.
"""

import asyncio
import csv
import datetime
import logging
import random
from io import StringIO

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile

import aiosqlite

import config

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Создаём экземпляр бота и диспетчера
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# База данных (имя файла берём из config.DB_NAME)
DB_NAME = config.DB_NAME


async def init_db() -> None:
    """Создаёт таблицу entries, если она ещё не существует.

    Поля таблицы:
      - id: первичный ключ (autoincrement)
      - user_id: идентификатор пользователя в Telegram
      - username: имя пользователя (@username) или пустая строка
      - first_name: имя, указанное в профиле
      - code: введённое кодовое слово
      - entry_number: порядковый номер участника
      - created_at: время создания записи (ISO‑формат)
    """
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


async def set_bot_commands() -> None:
    """Регистрирует команды бота в Telegram для удобства пользователей."""
    commands = [
        BotCommand(command="start", description="Начать и получить инструкцию"),
        BotCommand(command="my", description="Показать свои коды"),
    ]
    # Добавляем админские команды, но они будут отображаться для всех
    # (Telegram не позволяет скрывать команды по ID)
    admin_commands = [
        BotCommand(command="export", description="Выгрузить CSV (админ)"),
        BotCommand(command="draw", description="Розыгрыш (админ)"),
        BotCommand(command="stats", description="Статистика (админ)"),
    ]
    await bot.set_my_commands(commands + admin_commands)


async def register_entry(user_id: int, username: str | None, first_name: str | None, code: str) -> tuple[int, bool]:
    """Регистрирует кодовое слово для пользователя.

    Возвращает кортеж (entry_number, is_new), где is_new=True,
    если запись создаётся впервые, и False, если код уже был учтён.

    Все операции выполняются в одной транзакции.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # Проверяем, есть ли запись с таким user_id и кодом
        cursor = await db.execute(
            "SELECT entry_number FROM entries WHERE user_id = ? AND code = ?",
            (user_id, code),
        )
        row = await cursor.fetchone()
        if row:
            return row[0], False

        # Получаем максимальный номер участника
        cursor = await db.execute("SELECT MAX(entry_number) FROM entries")
        result = await cursor.fetchone()
        max_number = result[0] or 0
        new_number = max_number + 1

        # Сохраняем новую запись
        created_at = datetime.datetime.now().isoformat()
        await db.execute(
            "INSERT INTO entries (user_id, username, first_name, code, entry_number, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username or "", first_name or "", code, new_number, created_at),
        )
        await db.commit()
        return new_number, True


async def get_user_entries(user_id: int) -> list[tuple[str, int]]:
    """Возвращает список (code, entry_number) для заданного пользователя."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT code, entry_number FROM entries WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [(row[0], row[1]) for row in rows]


async def export_csv() -> bytes:
    """Формирует CSV‑файл с данными участников и возвращает его в виде байтов."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT user_id, username, code, entry_number FROM entries ORDER BY id"
        )
        rows = await cursor.fetchall()
    # Записываем в буфер
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "username", "code", "entry_number"])
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


async def draw_winner() -> dict[str, str] | None:
    """Выбирает случайного победителя из всех записей.

    Возвращает словарь с полями first_name, username, user_id, code,
    entry_number или None, если участников нет.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT user_id, username, first_name, code, entry_number FROM entries"
        )
        rows = await cursor.fetchall()
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
    """Возвращает статистику: количество записей, уникальных пользователей и кодов."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Общее количество записей (участий)
        cursor = await db.execute("SELECT COUNT(*) FROM entries")
        total_entries = (await cursor.fetchone())[0]

        # Количество уникальных пользователей
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM entries")
        unique_users = (await cursor.fetchone())[0]

        # Количество уникальных кодов
        cursor = await db.execute("SELECT COUNT(DISTINCT code) FROM entries")
        unique_codes = (await cursor.fetchone())[0]

    return {
        "total_entries": total_entries,
        "unique_users": unique_users,
        "unique_codes": unique_codes,
    }


@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    """Обрабатывает команду /start, приветствует пользователя и даёт инструкцию."""
    first_name = message.from_user.first_name or "друг"
    text = (
    f"Привет, {message.from_user.first_name} 👋!\n\n"
    "Добро пожаловать в розыгрыш. Чтобы принять участие, отправь кодовое слово.\n"
    "Если код верный, ты получишь уникальный номер участника.\n"
    "Посмотреть свои коды и номера можно командой /my."
)
    await message.answer(text)


@dp.message(Command("my"))
async def cmd_my(message: types.Message) -> None:
    """Показывает пользователю список введённых им кодов и назначенных номеров."""
    entries = await get_user_entries(message.from_user.id)
    if not entries:
        await message.answer("Вы ещё не вводили кодовых слов.")
        return
    # Формируем список кодов и номеров
    lines = ["Ваши коды:"]
    for code, number in entries:
        lines.append(f"№{number} — {code}")
    await message.answer("\n".join(lines))


@dp.message(Command("export"))
async def cmd_export(message: types.Message) -> None:
    """Выгружает CSV с участниками. Доступно только админу."""
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    csv_bytes = await export_csv()
    file = BufferedInputFile(csv_bytes, filename="participants.csv")
    await message.answer_document(file, caption="CSV‑файл со списком участников")


@dp.message(Command("draw"))
async def cmd_draw(message: types.Message) -> None:
    """Выбирает случайного победителя. Доступно только админу."""
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
    """Показывает статистику записей. Доступно только админу."""
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
    """Обрабатывает любые сообщения, которые не являются командами.

    Если пользователь отправляет кодовое слово, оно проверяется на
    наличие в списке допустимых. В случае успеха сохраняется в базе
    данных и пользователю возвращается его номер участника. Если код
    неправильный, отправляется соответствующее сообщение.
    """
    # Игнорируем сообщения без текста
    if not message.text:
        return
    # Убираем пробелы и приводим к нижнему регистру для проверки
    code = message.text.strip().lower()
    if not code:
        return
    # Проверяем, является ли сообщение командой (начинается с /)
    if code.startswith("/"):
        # Команды будут обработаны отдельными хендлерами
        return
    # Проверяем кодовое слово
    valid_codes_lower = [c.lower() for c in config.VALID_CODES]
    if code not in valid_codes_lower:
        await message.answer("Кодовое слово неверно. Попробуйте ещё раз.")
        return
    # Регистрируем код для пользователя
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


async def main() -> None:
    """Точка входа в программу. Инициализирует БД, команды и запускает polling."""
    await init_db()
    await set_bot_commands()
    logger.info("Бот запущен. Ожидание сообщений...")
    # Запускаем длинный опрос
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")