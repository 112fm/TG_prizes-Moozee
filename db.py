# db.py
from __future__ import annotations
import os
import csv
import random
from io import StringIO
from typing import Optional, List, Tuple, Dict

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL")  # уже добавили в Render

_pool: Optional[asyncpg.Pool] = None


# ---------- ИНИЦИАЛИЗАЦИЯ/МИГРАЦИИ ----------
async def init() -> None:
    """
    Создаёт пул подключений и гарантирует наличие таблиц/индексов.
    Вызывать при старте приложения и перед обработкой апдейтов вебхука.
    """
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=60)

    async with _pool.acquire() as con:
        # users
        await con.execute("""
        create table if not exists public.users (
            user_id bigint primary key,
            username text,
            first_name text,
            participant_code text unique not null
        );
        """)

        # entries
        await con.execute("""
        create table if not exists public.entries (
            id bigserial primary key,
            user_id bigint not null references public.users(user_id) on delete cascade,
            username text,
            first_name text,
            code text not null,
            entry_number int not null,
            created_at timestamp not null default now()
        );
        """)
        await con.execute("""
        create unique index if not exists idx_entries_user_code on public.entries(user_id, code);
        """)

        # предпочтения пользователя (подписки на рассылки)
        await con.execute("""
        create table if not exists public.user_prefs (
            user_id bigint primary key references public.users(user_id) on delete cascade,
            notify_results boolean not null default true,
            notify_new_video boolean not null default true,
            notify_streams boolean not null default true,
            created_at timestamp not null default now(),
            updated_at timestamp not null default now()
        );
        """)


# ---------- УТИЛИТЫ ----------
def _make_participant_code(length: int, alphabet: str) -> str:
    import secrets
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------- БИЗНЕС-ЛОГИКА (совместимо с прежними сигнатурами) ----------
async def ensure_user(user_id: int, username: Optional[str], first_name: Optional[str],
                      part_len: int, alphabet: str) -> str:
    """
    Убедиться, что пользователь есть в users и имеет постоянный participant_code.
    Вернёт participant_code.
    """
    assert _pool is not None
    async with _pool.acquire() as con:
        row = await con.fetchrow("select participant_code from public.users where user_id=$1", user_id)
        if row:
            await con.execute("update public.users set username=$1, first_name=$2 where user_id=$3",
                              username or "", first_name or "", user_id)
            return row["participant_code"]

        # генерируем уникальный participant_code
        while True:
            pc = _make_participant_code(part_len, alphabet)
            exists = await con.fetchrow("select 1 from public.users where participant_code=$1", pc)
            if not exists:
                break

        await con.execute(
            "insert into public.users(user_id, username, first_name, participant_code) values ($1,$2,$3,$4)",
            user_id, username or "", first_name or "", pc
        )
        # сразу создадим prefs со значениями по умолч.
        await con.execute(
            """insert into public.user_prefs(user_id) values ($1)
               on conflict (user_id) do nothing""",
            user_id
        )
        return pc


async def register_entry(user_id: int, username: Optional[str], first_name: Optional[str], code: str,
                         part_len: int, alphabet: str) -> Tuple[int, bool, str]:
    """
    Зарегистрировать код для пользователя.
    Возвращает (entry_number, is_new, participant_code).
    """
    assert _pool is not None
    async with _pool.acquire() as con:
        # ensure user
        pcode = await ensure_user(user_id, username, first_name, part_len, alphabet)

        # уже есть такой код?
        row = await con.fetchrow(
            "select entry_number from public.entries where user_id=$1 and code=$2",
            user_id, code
        )
        if row:
            return int(row["entry_number"]), False, pcode

        # новый номер = max(entry_number)+1
        row = await con.fetchrow("select coalesce(max(entry_number),0) as m from public.entries")
        new_number = int(row["m"]) + 1

        await con.execute(
            """insert into public.entries(user_id, username, first_name, code, entry_number)
               values ($1,$2,$3,$4,$5)""",
            user_id, username or "", first_name or "", code, new_number
        )
        return new_number, True, pcode


async def get_user_entries(user_id: int) -> Tuple[str, List[Tuple[str, int]]]:
    assert _pool is not None
    async with _pool.acquire() as con:
        row = await con.fetchrow("select participant_code from public.users where user_id=$1", user_id)
        pcode = row["participant_code"] if row else "—"
        rows = await con.fetch("""select code, entry_number
                                  from public.entries
                                  where user_id=$1
                                  order by created_at""", user_id)
        return pcode, [(r["code"], int(r["entry_number"])) for r in rows]


async def export_csv() -> bytes:
    assert _pool is not None
    async with _pool.acquire() as con:
        rows = await con.fetch("""select e.user_id, e.username, e.code, e.entry_number
                                  from public.entries e order by e.id""")
    buff = StringIO()
    w = csv.writer(buff)
    w.writerow(["user_id", "username", "code", "entry_number"])
    for r in rows:
        w.writerow([r["user_id"], r["username"], r["code"], r["entry_number"]])
    return buff.getvalue().encode("utf-8")


async def draw_weighted_winner() -> Optional[Dict]:
    """
    Взвешенный победитель: вес = кол-ву уникальных кодов у пользователя.
    """
    assert _pool is not None
    async with _pool.acquire() as con:
        users = await con.fetch("""
            select u.user_id, u.username, u.first_name, u.participant_code,
                   count(distinct e.code) as codes_count
            from public.users u
            left join public.entries e on e.user_id = u.user_id
            group by u.user_id, u.username, u.first_name, u.participant_code
        """)
        if not users:
            return None

        code_rows = await con.fetch("select user_id, code from public.entries")
        codes_by_user: Dict[int, List[str]] = {}
        for r in code_rows:
            codes_by_user.setdefault(int(r["user_id"]), []).append(r["code"])

    pool = []
    for u in users:
        tickets = int(u["codes_count"] or 0)
        if tickets <= 0:
            continue
        uid = int(u["user_id"])
        pool.append({
            "user_id": uid,
            "username": u["username"] or "",
            "first_name": u["first_name"] or "",
            "participant_code": u["participant_code"],
            "codes_count": tickets,
            "codes": codes_by_user.get(uid, []),
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


# ---------- ПРЕДПОЧТЕНИЯ / ПОДПИСКИ ----------
async def get_prefs(user_id: int) -> Dict[str, bool]:
    assert _pool is not None
    async with _pool.acquire() as con:
        row = await con.fetchrow("select notify_results, notify_new_video, notify_streams from public.user_prefs where user_id=$1",
                                 user_id)
        if not row:
            # создать по умолчанию
            await con.execute("insert into public.user_prefs(user_id) values ($1) on conflict (user_id) do nothing", user_id)
            return {"notify_results": True, "notify_new_video": True, "notify_streams": True}
        return {k: bool(row[k]) for k in ["notify_results", "notify_new_video", "notify_streams"]}


async def toggle_pref(user_id: int, field: str) -> Dict[str, bool]:
    assert field in ("notify_results", "notify_new_video", "notify_streams")
    assert _pool is not None
    async with _pool.acquire() as con:
        await con.execute("""
            insert into public.user_prefs(user_id) values ($1)
            on conflict (user_id) do nothing
        """, user_id)
        await con.execute(f"""
            update public.user_prefs
               set {field} = not {field},
                   updated_at = now()
             where user_id = $1
        """, user_id)
    return await get_prefs(user_id)


async def list_subscribers_for(kind: str) -> List[int]:
    """
    kind in {'video','results','streams'}
    Возвращает user_id подписчиков соответствующей рассылки.
    """
    field_map = {
        "video": "notify_new_video",
        "results": "notify_results",
        "streams": "notify_streams",
    }
    field = field_map[kind]
    assert _pool is not None
    async with _pool.acquire() as con:
        rows = await con.fetch(f"""
            select u.user_id
            from public.user_prefs p
            join public.users u on u.user_id = p.user_id
            where p.{field} = true
        """)
    return [int(r["user_id"]) for r in rows]
