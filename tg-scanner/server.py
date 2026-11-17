#!/usr/bin/env python3
"""Веб-GUI для сканера каналов Telegram. Запуск: python server.py -> http://127.0.0.1:5000"""
import asyncio
import json
import os
import queue
import threading

from flask import Flask, Response, request, send_from_directory
from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError

from tg_scan import USERNAME_RE, candidates

BASE = os.path.dirname(os.path.abspath(__file__))
AVATARS = os.path.join(BASE, "avatars")
os.makedirs(AVATARS, exist_ok=True)
SESSION = os.path.join(BASE, "tg_scan_session")

app = Flask(__name__, static_folder="static")
scan_lock = threading.Lock()  # один скан за раз (одна сессия Telegram)


async def fetch(client, name, only_channels):
    """Возвращает dict карточки, 'free', 'skip' или None."""
    if not USERNAME_RE.match(name):
        return "skip"
    while True:
        try:
            res = await client(functions.contacts.ResolveUsernameRequest(name))
            break
        except UsernameNotOccupiedError:
            return "free"
        except UsernameInvalidError:
            return "skip"
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
    if not res.chats:
        return "skip"  # пользователь или бот
    chat = res.chats[0]
    is_channel = bool(getattr(chat, "broadcast", False))
    if only_channels and not is_channel:
        return "skip"
    about, subs = "", None
    try:
        full = await client(functions.channels.GetFullChannelRequest(chat))
        about = full.full_chat.about or ""
        subs = full.full_chat.participants_count
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
    except Exception:
        pass
    avatar = None
    try:
        path = await client.download_profile_photo(chat, file=os.path.join(AVATARS, f"{name}.jpg"))
        if path:
            avatar = f"/avatars/{name}.jpg"
    except Exception:
        pass
    return {"username": name, "title": chat.title, "about": about, "subs": subs,
            "avatar": avatar, "type": "channel" if is_channel else "group",
            "verified": bool(getattr(chat, "verified", False)),
            "url": f"https://t.me/{name}"}


async def scan(q, stop, word, start, end, limit, delay, only_channels, pattern):
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        q.put({"event": "error", "msg": "Не заданы TG_API_ID / TG_API_HASH"})
        return
    client = TelegramClient(SESSION, int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            q.put({"event": "error", "msg": "Нет входа в Telegram. Один раз запусти: python tg_scan.py test --max 1"})
            return
        found = checked = 0
        names = []
        if pattern in ("plain", "both"):
            names += list(candidates(word, start, end, True))
        if pattern in ("underscore", "both"):
            names += [f"{word}_{i}" for i in range(start, end + 1)]
        q.put({"event": "start", "total": len(names)})
        for name in names:
            if stop.is_set() or found >= limit:
                break
            r = await fetch(client, name, only_channels)
            checked += 1
            if isinstance(r, dict):
                found += 1
                q.put({"event": "found", "card": r})
            q.put({"event": "progress", "checked": checked, "found": found, "current": name})
            await asyncio.sleep(delay)
        q.put({"event": "done", "checked": checked, "found": found})
    finally:
        await client.disconnect()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/avatars/<path:f>")
def avatar(f):
    return send_from_directory(AVATARS, f)


@app.route("/api/scan")
def api_scan():
    g = request.args.get
    word = (g("word") or "").strip()
    if not word:
        return Response("word required", 400)
    params = dict(word=word, start=int(g("start", 1)), end=int(g("max", 900)),
                  limit=int(g("limit", 200)), delay=float(g("delay", 1.2)),
                  only_channels=g("channels_only", "1") == "1",
                  pattern=g("pattern", "plain"))
    if not scan_lock.acquire(blocking=False):
        return Response("scan already running", 409)
    q, stop = queue.Queue(), threading.Event()

    def worker():
        try:
            asyncio.run(scan(q, stop, **params))
        except Exception as e:  # noqa
            q.put({"event": "error", "msg": str(e)})
        finally:
            q.put(None)
            scan_lock.release()

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            stop.set()  # клиент закрыл вкладку/нажал Стоп

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
