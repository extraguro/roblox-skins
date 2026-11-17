#!/usr/bin/env python3
"""Веб-GUI для сканера каналов Telegram. Запуск: python server.py -> http://127.0.0.1:5000"""
import asyncio
from datetime import datetime, timezone
import glob
import json
import os
import re
import time
import queue
import threading

from flask import Flask, Response, jsonify, request, send_from_directory
from telethon import TelegramClient, functions
from telethon.errors import (
    FloodWaitError, InviteHashExpiredError, InviteHashInvalidError, PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    PhoneNumberInvalidError, SessionPasswordNeededError, UsernameInvalidError, UsernameNotOccupiedError,
)

from tg_scan import USERNAME_RE, candidates

BASE = os.path.dirname(os.path.abspath(__file__))
AVATARS = os.path.join(BASE, "avatars")
os.makedirs(AVATARS, exist_ok=True)
SESSION = os.path.join(BASE, "tg_scan_session")

app = Flask(__name__, static_folder="static")
scan_lock = threading.Lock()  # один скан за раз (одна сессия Telegram)


class Stopped(BaseException):
    """Поиск остановлен пользователем (BaseException, чтобы его не глотали except Exception)."""


ACTIVE = {"stop": None, "q": None}  # событие остановки текущего скана


async def nap(sec):
    """sleep, который можно прервать кнопкой «Стоп» (в т.ч. посреди FloodWait)."""
    end = time.monotonic() + sec
    while True:
        st = ACTIVE["stop"]
        if st is not None and st.is_set():
            raise Stopped()
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(0.5, left))


DEGRADED = set()   # функции, отключённые до конца поиска из-за долгого FloodWait
SOFT_MAX = 90      # необязательные данные: ждём не дольше, иначе отключаем функцию
HARD_MAX = 600     # поиск имён: дольше — останавливаем поиск


class FloodAbort(Exception):
    def __init__(self, sec, method):
        super().__init__(f"{method}: {sec} с")
        self.sec, self.method = sec, method


def method_of(e):
    return type(getattr(e, "request", None)).__name__


def notice(msg):
    print(msg, flush=True)
    q = ACTIVE.get("q")
    if q is not None:
        q.put({"event": "notice", "msg": msg})


async def flood(sec, method=None):
    """Пауза по FloodWait: показываем в интерфейсе и консоли, чтобы было видно, что это не зависание."""
    q = ACTIVE.get("q")
    print(f"FloodWait: Telegram просит подождать {int(sec)} с (метод: {method or '?'})", flush=True)
    if q is not None:
        q.put({"event": "wait", "seconds": int(sec), "method": method})
    await nap(sec)
    if q is not None:
        q.put({"event": "resume"})


async def soft_flood(feature, e, label):
    """Необязательные данные: короткую паузу ждём, длинную — отключаем функцию до конца поиска."""
    if e.seconds > SOFT_MAX:
        DEGRADED.add(feature)
        notice(f"Лимит Telegram на {e.seconds // 60} мин ({method_of(e)}): {label} отключены до конца поиска")
    else:
        await flood(e.seconds + 1, method_of(e))


INVITE_RE = re.compile(r"(?:t(?:elegram)?\.me/(?:\+|joinchat/)|tg://join\?invite=)([A-Za-z0-9_-]{10,})", re.I)
EMPTY_MAX = 1  # канал с таким числом участников и меньше считаем «пустым» (там только создатель)
MAX_LINKS = 30  # максимум уникальных ссылок на канал


async def check_invite(client, h, cache):
    """-> (status, info): alive | dead | unknown. Кэшируется по хэшу ссылки."""
    if h in cache:
        return cache[h]
    if "invite" in DEGRADED:
        return ("unknown", {"reason": "проверка ссылок отключена из-за лимита Telegram"})
    while True:
        try:
            r = await client(functions.messages.CheckChatInviteRequest(h))
            chat = getattr(r, "chat", None)
            res = ("alive", {"kind": type(r).__name__,
                             "title": getattr(r, "title", None) or getattr(chat, "title", None),
                             "members": getattr(r, "participants_count", None),
                             "request_needed": bool(getattr(r, "request_needed", False))})
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            res = ("dead", {"reason": type(e).__name__})
        except FloodWaitError as e:
            if e.seconds > SOFT_MAX:
                DEGRADED.add("invite")
                notice(f"Лимит Telegram на {e.seconds // 60} мин ({method_of(e)}): проверка ссылок отключена до конца поиска")
                return ("unknown", {"reason": f"FloodWait {e.seconds} с"})
            await flood(e.seconds + 1, method_of(e))
            continue
        except Exception as e:  # noqa
            res = ("unknown", {"reason": f"{type(e).__name__}: {e}"})
        cache[h] = res
        return res


# ---------------- сохранение найденных каналов ----------------
RESULTS = os.path.join(BASE, "results.json")  # в .gitignore
results_lock = threading.Lock()
try:
    with open(RESULTS, encoding="utf-8") as _f:
        results = {c["username"]: c for c in json.load(_f)}
except (OSError, ValueError, KeyError, TypeError):
    results = {}


def save_results():
    with results_lock:
        tmp = RESULTS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(results.values()), f, ensure_ascii=False)
        os.replace(tmp, RESULTS)


async def fetch_posts(client, chat, n):
    """Последние n постов: (тексты, ссылки из entities)."""
    texts, urls = [], []
    if n <= 0 or "posts" in DEGRADED:
        return texts, urls
    try:
        async for m in client.iter_messages(chat, limit=n):
            if m.message:
                texts.append(m.message)
            for e in (m.entities or []):
                u = getattr(e, "url", None)
                if u:
                    urls.append(u)
    except FloodWaitError as e:
        await soft_flood("posts", e, "посты")
    except Exception:
        pass
    return texts, urls


async def scan_links(client, sources, cache, delay):
    hashes = list(dict.fromkeys(h for t in sources for h in INVITE_RE.findall(t)))[:MAX_LINKS]
    items = []
    for h in hashes:
        new = h not in cache
        status, info = await check_invite(client, h, cache)
        items.append({"url": f"https://t.me/+{h}", "status": status, **info})
        if new:
            await nap(delay)
    dead = [i["url"] for i in items if i["status"] == "dead"]
    alive = [i for i in items if i["status"] == "alive"]
    empty = sum(i.get("members") is not None and i["members"] <= EMPTY_MAX for i in alive)
    request = sum(bool(i.get("request_needed")) for i in alive)
    return {"total": len(items), "dead": len(dead), "dead_list": dead,
            "empty": empty, "request": request,
            "unknown": sum(i["status"] == "unknown" for i in items), "items": items, "checked": True}


async def fetch(client, name, only_channels, posts=0, check_links=False, cache=None, delay=1.0):
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
            if e.seconds > HARD_MAX:
                raise FloodAbort(e.seconds, method_of(e))
            await flood(e.seconds + 1, method_of(e))
    if not res.chats:
        return "skip"  # пользователь или бот
    chat = res.chats[0]
    is_channel = bool(getattr(chat, "broadcast", False))
    if only_channels and not is_channel:
        return "skip"
    about, subs = "", None
    if "full" not in DEGRADED:
        try:
            full = await client(functions.channels.GetFullChannelRequest(chat))
            about = full.full_chat.about or ""
            subs = full.full_chat.participants_count
        except FloodWaitError as e:
            await soft_flood("full", e, "описание и подписчики")
        except Exception:
            pass
    post_texts, post_urls = await fetch_posts(client, chat, posts)
    sources = [about] + post_texts + post_urls
    if check_links:
        links = await scan_links(client, sources, cache, delay)
    else:  # только считаем ссылки, без запросов к Telegram
        hs = list(dict.fromkeys(h for t in sources for h in INVITE_RE.findall(t)))[:MAX_LINKS]
        links = {"total": len(hs), "dead": 0, "dead_list": [], "unknown": 0, "items": [], "checked": False}
    created = getattr(chat, "date", None)  # для не-участника это дата создания канала
    avatar = None
    if "photo" not in DEGRADED:
        try:
            path = await client.download_profile_photo(chat, file=os.path.join(AVATARS, f"{name}.jpg"))
            if path:
                avatar = f"/avatars/{name}.jpg"
        except FloodWaitError as e:
            await soft_flood("photo", e, "аватарки")
        except Exception:
            pass
    return {"username": name, "title": chat.title, "about": about, "subs": subs,
            "avatar": avatar, "links": links,
            "posts": [t[:1500] for t in post_texts],
            "created": created.isoformat() if created else None, "type": "channel" if is_channel else "group",
            "verified": bool(getattr(chat, "verified", False)),
            "url": f"https://t.me/{name}"}


async def scan(q, stop, word, start, end, limit, delay, only_channels, pattern, posts, check_links):
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
        cache = {}
        DEGRADED.clear()
        names = []
        if pattern in ("plain", "both"):
            names += list(candidates(word, start, end, True))
        if pattern in ("underscore", "both"):
            names += [f"{word}_{i}" for i in range(start, end + 1)]
        q.put({"event": "start", "total": len(names)})
        try:
            for name in names:
                if stop.is_set() or found >= limit:
                    break
                r = await fetch(client, name, only_channels, posts, check_links, cache, delay)
                checked += 1
                if isinstance(r, dict):
                    found += 1
                    r["scanned_at"] = datetime.now(timezone.utc).isoformat()
                    results[r["username"]] = r
                    if found % 10 == 0:
                        save_results()
                    q.put({"event": "found", "card": r})
                q.put({"event": "progress", "checked": checked, "found": found, "current": name})
                await nap(delay)
        except Stopped:
            pass
        except FloodAbort as e:
            save_results()
            q.put({"event": "error", "msg": f"Telegram заблокировал поиск имён на {e.sec // 60} мин ({e.method}). "
                   f"Найдено до остановки: {found}. Попробуй позже, лучше с большей паузой."})
            return
        q.put({"event": "done", "checked": checked, "found": found})
    finally:
        save_results()
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


@app.route("/api/results")
def api_results():
    return jsonify(list(results.values()))


@app.route("/api/results/clear", methods=["POST"])
def api_results_clear():
    if scan_lock.locked():
        return jsonify(error="Сначала останови поиск"), 409
    results.clear()
    save_results()
    for f in glob.glob(os.path.join(AVATARS, "*.jpg")):
        try:
            os.remove(f)
        except OSError:
            pass
    return jsonify(ok=True)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/avatars/<path:f>")
def avatar(f):
    return send_from_directory(AVATARS, f)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if ACTIVE["stop"] is not None:
        ACTIVE["stop"].set()
    return jsonify(ok=True)


@app.route("/api/scan")
def api_scan():
    g = request.args.get
    word = (g("word") or "").strip()
    if not word:
        return Response("word required", 400)
    params = dict(word=word, start=int(g("start", 1)), end=int(g("max", 900)),
                  limit=int(g("limit", 200)), delay=float(g("delay", 1.2)),
                  only_channels=g("channels_only", "1") == "1",
                  pattern=g("pattern", "plain"),
                  posts=max(0, min(50, int(g("posts", 10)))),
                  check_links=g("check_links") == "1")
    if not scan_lock.acquire(blocking=False):
        # прошлый скан ещё жив (например, после обновления страницы) — останавливаем и ждём
        if ACTIVE["stop"] is not None:
            ACTIVE["stop"].set()
        if not scan_lock.acquire(timeout=10):
            return Response("scan already running", 409)
    q, stop = queue.Queue(), threading.Event()
    ACTIVE["stop"] = stop
    ACTIVE["q"] = q

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
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    yield ": ping\n\n"  # заодно замечаем, что клиент ушёл
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            stop.set()  # клиент закрыл вкладку/нажал Стоп

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
