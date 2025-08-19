import os

# Токен и ID теперь будем брать из переменных окружения Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Список допустимых кодов
VALID_CODES = [
    "HEADSHOTKING",
    "MOOVICTORY",
    "CLUTCHGOD"
]
