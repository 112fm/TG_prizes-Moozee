import os

# 🔑 Секреты из переменных окружения (удобно для Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ✅ Админы: можно указать одного через ADMIN_ID или список через ADMIN_IDS
#   ADMIN_IDS формат: "12345,67890"
_admin_ids_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in _admin_ids_env.split(",") if x.strip().lstrip("-").isdigit()]

_admin_id_single = os.getenv("ADMIN_ID")
if _admin_id_single and _admin_id_single.strip().lstrip("-").isdigit():
    ADMIN_IDS.append(int(_admin_id_single))

# 📂 База данных (SQLite-файл рядом с кодом)
DB_NAME = os.getenv("DB_NAME", "participants.db")

# 📢 Опционально: ID группы для анонсов победителя (например, -1001234567890)
_group_id = os.getenv("GROUP_CHAT_ID")
GROUP_CHAT_ID = int(_group_id) if _group_id and _group_id.strip().lstrip("-").isdigit() else None

# ✅ Допустимые кодовые слова (можешь поменять на свои)
VALID_CODES = [
    "HEADSHOTKING",
    "MOOVICTORY",
    "CLUTCHGOD",
]

# 🧩 Настройки постоянного буквенно-цифрового ID участника
PARTICIPANT_CODE_LEN = int(os.getenv("PARTICIPANT_CODE_LEN", "6"))
PARTICIPANT_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без 0/O/1/l
