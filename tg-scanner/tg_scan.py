#!/usr/bin/env python3
"""Перебор юзернеймов Telegram: word, word1, word2 ... wordN.

Для каждого имени показывает: канал/группа/пользователь (с названием и числом
подписчиков) или СВОБОДНО.

Запуск:
    export TG_API_ID=123456
    export TG_API_HASH=abcdef...
    python tg_scan.py channel --max 900
"""
import argparse
import asyncio
import csv
import os
import re
import sys

from telethon import TelegramClient, functions
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import Channel, User

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def candidates(word: str, start: int, end: int, with_base: bool):
    if with_base:
        yield word
    for i in range(start, end + 1):
        yield f"{word}{i}"


async def check(client, name):
    """Возвращает (статус, название, подписчики)."""
    if not USERNAME_RE.match(name):
        return "invalid", "", ""  # Telegram: 5-32 символа, начинается с буквы
    while True:
        try:
            res = await client(functions.contacts.ResolveUsernameRequest(name))
        except UsernameNotOccupiedError:
            return "free", "", ""
        except UsernameInvalidError:
            return "invalid", "", ""
        except FloodWaitError as e:
            print(f"  FloodWait: жду {e.seconds + 1} c...", file=sys.stderr)
            await asyncio.sleep(e.seconds + 1)
            continue
        chat = res.chats[0] if res.chats else None
        if chat is not None:
            kind = "channel" if getattr(chat, "broadcast", False) else "group"
            subs = ""
            try:
                full = await client(functions.channels.GetFullChannelRequest(chat))
                subs = full.full_chat.participants_count or ""
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                pass
            return kind, chat.title, subs
        user = res.users[0] if res.users else None
        if isinstance(user, User):
            kind = "bot" if user.bot else "user"
            title = " ".join(filter(None, [user.first_name, user.last_name]))
            return kind, title, ""
        return "taken", "", ""


async def main():
    p = argparse.ArgumentParser(description="Поиск каналов Telegram по шаблону word, word1..wordN")
    p.add_argument("word", help="базовое слово, например channel")
    p.add_argument("--start", type=int, default=1, help="с какого числа (по умолч. 1)")
    p.add_argument("--max", type=int, default=900, help="до какого числа (по умолч. 900)")
    p.add_argument("--no-base", action="store_true", help="не проверять имя без цифры")
    p.add_argument("--delay", type=float, default=1.5, help="пауза между запросами, c")
    p.add_argument("--show", choices=["found", "free", "all"], default="found",
                   help="что печатать: found=существующие, free=свободные, all=всё")
    p.add_argument("--csv", help="сохранить результат в CSV")
    a = p.parse_args()

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Задай TG_API_ID и TG_API_HASH (получить на https://my.telegram.org -> API development tools)")

    rows = []
    async with TelegramClient("tg_scan_session", int(api_id), api_hash) as client:
        for name in candidates(a.word, a.start, a.max, not a.no_base):
            status, title, subs = await check(client, name)
            rows.append((name, status, title, subs))
            is_free = status == "free"
            if a.show == "all" or (a.show == "free" and is_free) or \
               (a.show == "found" and status not in ("free", "invalid")):
                label = "СВОБОДНО" if is_free else status
                extra = f" | {title}" + (f" | {subs} подп." if subs != "" else "") if title else ""
                print(f"@{name:<32} {label}{extra}", flush=True)
            await asyncio.sleep(a.delay)

    found = [r for r in rows if r[1] not in ("free", "invalid")]
    free = [r for r in rows if r[1] == "free"]
    print(f"\nПроверено: {len(rows)} | существуют: {len(found)} | свободны: {len(free)}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["username", "status", "title", "subscribers"])
            w.writerows(rows)
        print(f"Сохранено в {a.csv}")


if __name__ == "__main__":
    asyncio.run(main())
