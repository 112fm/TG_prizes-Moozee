import os
import re

# 🔑 Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 👑 Админы
# Принимает:
#  - ADMIN_IDS="1,2,3"
#  - ADMIN_ID="1,2"  или "1"
_admins_raw = (os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "").strip()
ADMIN_IDS = sorted({
    int(x)
    for x in re.split(r"[,\s]+", _admins_raw)
    if x.strip().lstrip("-").isdigit()
})

# 🗃 Старый SQLite (оставлен для совместимости, не используется с PG)
DB_NAME = os.getenv("DB_NAME", "participants.db")

# 🌐 PostgreSQL (Supabase)
# В приоритете переменная окружения DATABASE_URL.
# Если её нет — используем дефолт (порт 6543 — Transaction Pooler, IPv4-compatible).
DATABASE_URL = (os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:Energizer_776GF4_SUPABASE@db.foptoqqcyjlbcwpwtecc.supabase.co:6543/postgres?sslmode=require",
) or "").strip()

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
