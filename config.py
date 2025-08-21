import os

# 🔑 Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 👑 Админы: один через ADMIN_ID или список через ADMIN_IDS="1,2,3"
_admin_ids_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in _admin_ids_env.split(",") if x.strip().lstrip("-").isdigit()]

_admin_id_single = os.getenv("ADMIN_ID")
if _admin_id_single and _admin_id_single.strip().lstrip("-").isdigit():
    ADMIN_IDS.append(int(_admin_id_single))

# 🗃 Старый SQLite (оставлен для совместимости, не используется с PG)
DB_NAME = os.getenv("DB_NAME", "participants.db")

# 🌐 PostgreSQL (Supabase)
# В приоритете переменная окружения DATABASE_URL.
# Если её нет — берём значение ниже. Важно: порт 6543 (Transaction Pooler, IPv4-compatible).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:Energizer_776GF4_SUPABASE@db.foptoqqcyjlbcwpwtecc.supabase.co:6543/postgres?sslmode=require",
).strip()

# Для совместимости с кодом
DB_URL = DATABASE_URL

# 📢 Необязательная группа для анонса победителя
_group_id = os.getenv("GROUP_CHAT_ID")
GROUP_CHAT_ID = int(_group_id) if _group_id and _group_id.strip().lstrip("-").isdigit() else None

# ✅ Допустимые кодовые слова
VALID_CODES = [
    "HEADSHOTKING",
    "MOOVICTORY",
    "CLUTCHGOD",
]

# 🧩 Постоянный буквенно‑цифровой ID участника
PARTICIPANT_CODE_LEN = int(os.getenv("PARTICIPANT_CODE_LEN", "6"))
PARTICIPANT_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без 0/O/1/l
