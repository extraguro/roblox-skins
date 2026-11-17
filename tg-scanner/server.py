#!/usr/bin/env python3
"""Веб-GUI для сканера каналов Telegram. Запуск: python server.py -> http://127.0.0.1:5000"""
import asyncio
import glob
import json
import os
import queue
import threading

from flask import Flask, Response, jsonify, request, send_from_directory
from telethon import TelegramClient, functions
from telethon.errors import (
    FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    PhoneNumberInvalidError, SessionPasswordNeededError, UsernameInvalidError, UsernameNotOccupiedError,
)

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
    api_id, api_hash = get_creds()
    if not api_id or not api_hash:
        q.put({"event": "error", "msg": "Не заданы api_id / api_hash — введи их в блоке входа"})
        return
    client = TelegramClient(SESSION, int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            q.put({"event": "error", "msg": "Нет входа в Telegram — войди в аккаунт в блоке входа"})
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


# ---------------- вход в аккаунт через GUI ----------------
CONFIG = os.path.join(BASE, "tg_config.json")  # api_id/api_hash, если не заданы в env (в .gitignore)


def get_creds():
    i, h = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if i and h:
        return i, h
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
        return str(c.get("api_id") or ""), c.get("api_hash") or ""
    except (OSError, ValueError):
        return "", ""


auth_loop = asyncio.new_event_loop()
threading.Thread(target=auth_loop.run_forever, daemon=True).start()
auth = {"client": None, "phone": None, "hash": None, "state": None}  # state: code | password


def run_auth(coro):
    return asyncio.run_coroutine_threadsafe(coro, auth_loop).result(timeout=90)


async def _drop_client():
    c = auth["client"]
    auth.update(client=None, phone=None, hash=None, state=None)
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass


def me_dict(me):
    return {"name": " ".join(filter(None, [me.first_name, me.last_name])) or "—",
            "username": me.username or "", "phone": me.phone or ""}


async def _status():
    if auth["state"]:
        return {"authorized": False, "state": auth["state"], "has_creds": True}
    i, h = get_creds()
    if not i or not h:
        return {"authorized": False, "state": "phone", "has_creds": False}
    if not scan_lock.acquire(blocking=False):  # идёт скан — сессия занята, значит вход есть
        return {"authorized": True, "state": "ok", "has_creds": True, "busy": True}
    try:
        c = TelegramClient(SESSION, int(i), h)
        await c.connect()
        try:
            if await c.is_user_authorized():
                return {"authorized": True, "state": "ok", "has_creds": True, **me_dict(await c.get_me())}
        finally:
            await c.disconnect()
    finally:
        scan_lock.release()
    return {"authorized": False, "state": "phone", "has_creds": True}


async def _send(phone, api_id, api_hash):
    await _drop_client()
    if api_id and api_hash:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump({"api_id": int(api_id), "api_hash": api_hash.strip()}, f)
        try:
            os.chmod(CONFIG, 0o600)
        except OSError:
            pass
    i, h = get_creds()
    if not i or not h:
        raise ValueError("Нужны api_id и api_hash (my.telegram.org → API development tools)")
    c = TelegramClient(SESSION, int(i), h)
    await c.connect()
    try:
        sent = await c.send_code_request(phone)
    except Exception:
        await c.disconnect()
        raise
    auth.update(client=c, phone=phone, hash=sent.phone_code_hash, state="code")


async def _code(code):
    c = auth["client"]
    if not c:
        raise ValueError("Сначала отправь номер телефона")
    try:
        await c.sign_in(auth["phone"], code, phone_code_hash=auth["hash"])
    except SessionPasswordNeededError:
        auth["state"] = "password"
        return {"state": "password"}
    await _drop_client()
    return {"state": "ok"}


async def _password(pw):
    c = auth["client"]
    if not c:
        raise ValueError("Сначала отправь номер телефона")
    await c.sign_in(password=pw)
    await _drop_client()
    return {"state": "ok"}


async def _logout():
    await _drop_client()
    i, h = get_creds()
    if i and h:
        c = TelegramClient(SESSION, int(i), h)
        await c.connect()
        try:
            if await c.is_user_authorized():
                await c.log_out()
        finally:
            await c.disconnect()
    for f in glob.glob(SESSION + ".session*"):
        try:
            os.remove(f)
        except OSError:
            pass


ERRORS = {PhoneNumberInvalidError: "Неверный номер телефона (нужен формат +380...)",
          PhoneCodeInvalidError: "Неверный код", PhoneCodeExpiredError: "Код устарел — отправь номер заново",
          PasswordHashInvalidError: "Неверный пароль 2FA"}


def auth_call(fn, *a):
    try:
        return jsonify(run_auth(fn(*a)) or {"ok": True})
    except FloodWaitError as e:
        return jsonify(error=f"Слишком много попыток, подожди {e.seconds} с"), 429
    except Exception as e:  # noqa
        return jsonify(error=ERRORS.get(type(e), str(e) or type(e).__name__)), 400


@app.route("/api/auth/status")
def auth_status():
    return auth_call(_status)


@app.route("/api/auth/send", methods=["POST"])
def auth_send():
    j = request.get_json(force=True)
    return auth_call(_send, (j.get("phone") or "").strip(), str(j.get("api_id") or "").strip(), j.get("api_hash") or "")


@app.route("/api/auth/code", methods=["POST"])
def auth_code():
    return auth_call(_code, (request.get_json(force=True).get("code") or "").strip())


@app.route("/api/auth/password", methods=["POST"])
def auth_password():
    return auth_call(_password, request.get_json(force=True).get("password") or "")


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    if scan_lock.locked():
        return jsonify(error="Сначала останови поиск"), 409
    return auth_call(_logout)


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
