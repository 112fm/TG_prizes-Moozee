import os
import re

# 🔑 Токен бота (ENV: BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# 👑 Админы (ENV: ADMIN_IDS="111,222,333")
_admins_raw = (os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "").strip()
ADMIN_IDS = sorted({
    int(x)
    for x in re.split(r"[,\s]+", _admins_raw)
    if x.strip().lstrip("-").isdigit()
})

# 🌐 PostgreSQL (Supabase) — обязательно в ENV: DATABASE_URL
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_URL = DATABASE_URL  # алиас

# ✅ Допустимые кодовые слова
VALID_CODES = [
    "HEADSHOTKING",
    "MOOVICTORY",
    "CLUTCHGOD",
]

# 🧩 Постоянный буквенно‑цифровой ID участника
PARTICIPANT_CODE_LEN = int(os.getenv("PARTICIPANT_CODE_LEN", "6"))
PARTICIPANT_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"  # без 0/O/1/l
