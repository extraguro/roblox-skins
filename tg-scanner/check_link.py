#!/usr/bin/env python3
"""Диагностика: что именно Telegram отвечает на инвайт-ссылку.
Останови server.py перед запуском (одна сессия на двоих не работает).
    py check_link.py https://t.me/+AbCdEfGhIjKl
"""
import asyncio
import json
import os
import re
import sys

from telethon import TelegramClient, functions

BASE = os.path.dirname(os.path.abspath(__file__))


async def main(link):
    m = re.search(r"(?:\+|joinchat/|invite=)([A-Za-z0-9_-]+)", link)
    h = m.group(1) if m else link.strip()
    i, ah = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not (i and ah):
        with open(os.path.join(BASE, "tg_config.json"), encoding="utf-8") as f:
            c = json.load(f)
        i, ah = c["api_id"], c["api_hash"]
    async with TelegramClient(os.path.join(BASE, "tg_scan_session"), int(i), ah) as cl:
        print("hash:", h)
        try:
            r = await cl(functions.messages.CheckChatInviteRequest(h))
            print("ОТВЕТ:", type(r).__name__)
            print(r.stringify())
        except Exception as e:
            print("ОШИБКА:", type(e).__name__, "-", e)


asyncio.run(main(sys.argv[1]))
