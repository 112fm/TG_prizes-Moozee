import os

# 🔑 Токен и ID администратора подставляются из переменных окружения (Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# 📂 Имя файла базы данных (создаётся автоматически, хранится в проекте)
DB_NAME = os.getenv("DB_NAME", "participants.db")

# ✅ Список допустимых кодовых слов
VALID_CODES = [
    "HEADSHOTKING",
    "MOOVICTORY",
    "CLUTCHGOD"
]
