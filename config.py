import os

# 🔑 Берём секреты из переменных окружения Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# 📂 Имя файла базы (по умолчанию participants.db)
DB_NAME = os.getenv("DB_NAME", "participants.db")

# ✅ Ваши кодовые слова
VALID_CODES = [
    "HEADSHOTKING",
    "MOOVICTORY",
    "CLUTCHGOD"
]
