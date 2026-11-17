"""Разбор публичных страниц t.me — без аккаунта и без Telegram API.

https://t.me/<имя>   -> название, описание, аватарка, число подписчиков/участников
https://t.me/s/<имя> -> веб-превью канала с последними постами
Если имени нет, t.me показывает: "If you have Telegram, you can contact @имя right away."
"""
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

BASE = "https://t.me"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
VOID = {"br", "img", "meta", "link", "hr", "input", "source", "area", "base", "col", "embed", "wbr"}
TARGETS = {"tgme_page_title": "title", "tgme_page_extra": "extra",
           "tgme_page_description": "desc", "tgme_widget_message_text": "msg"}
GENERIC = re.compile(r"you can (view and join|contact)|if you have telegram", re.I)


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.texts, self.msgs, self.links = {}, {}, [], []
        self.cap, self.depth, self.buf = None, 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = a.get("property") or a.get("name")
            if k and a.get("content") is not None:
                self.meta.setdefault(k, a["content"])
            return
        if self.cap is not None:
            if tag in VOID:
                if tag == "br":
                    self.buf.append("\n")
                return
            self.depth += 1
            if tag == "a" and self.cap == "msg" and a.get("href"):
                self.links.append(a["href"])
            return
        if tag in VOID:
            return
        for c in (a.get("class") or "").split():
            k = TARGETS.get(c)
            if k:
                self.cap, self.depth, self.buf = k, 1, []
                break

    def handle_endtag(self, tag):
        if self.cap is None or tag in VOID:
            return
        self.depth -= 1
        if self.depth <= 0:
            text = re.sub(r"[ \t\xa0]+", " ", "".join(self.buf)).strip()
            if self.cap == "msg":
                self.msgs.append(text)
            else:
                self.texts.setdefault(self.cap, text)
            self.cap = None

    def handle_data(self, d):
        if self.cap is not None:
            self.buf.append(d)


def parse(text):
    p = Page()
    p.feed(text)
    p.close()
    return p


def _num(s):
    s = s.strip().replace("\xa0", " ")
    mult = 1
    if s and s[-1] in "kKmM":
        mult = 1000 if s[-1] in "kK" else 1_000_000
        s = s[:-1]
    if mult > 1:
        try:
            return int(float(s.replace(" ", "").replace(",", ".")) * mult)
        except ValueError:
            return None
    d = re.sub(r"\D", "", s)
    return int(d) if d else None


def classify(name, p):
    """-> {'state': 'free'|'unknown'|'ok', ...}"""
    title = (p.meta.get("og:title") or p.texts.get("title") or "").strip()
    og_desc = (p.meta.get("og:description") or "").strip()
    extra = " ".join(p.texts.get("extra", "").split())
    if title.lower().startswith("telegram: contact") or ("you can contact" in og_desc.lower() and not extra):
        return {"state": "free"}
    if not title:
        return {"state": "unknown"}
    subs, typ = None, "unknown"
    m = re.search(r"(\d[\d\s,.\xa0]*[kKmM]?)\s*(subscribers?|members?)", extra)
    if m:
        subs = _num(m.group(1))
        typ = "channel" if m.group(2).lower().startswith("subscriber") else "group"
    elif extra.startswith("@"):
        typ = "bot" if name.lower().endswith("bot") else "user"
    about = (p.texts.get("desc") or og_desc).strip()
    if GENERIC.search(about):
        about = ""
    return {"state": "ok", "title": title, "about": about, "subs": subs, "type": typ,
            "image": p.meta.get("og:image") or "", "extra": extra}


def http_get(url, binary=False, timeout=20):
    """-> (status|None, body, headers). status None = сетевая ошибка."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, (data if binary else data.decode("utf-8", "replace")), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, (data if binary else data.decode("utf-8", "replace")), dict(e.headers or {})
    except Exception:
        return None, (b"" if binary else ""), {}


def fetch_posts(name, n):
    """Последние n постов публичного канала: (тексты, ссылки). Пусто, если веб-превью отключено."""
    status, text, _ = http_get(f"{BASE}/s/{name}")
    if status != 200:
        return [], []
    p = parse(text)
    return p.msgs[-n:] if n > 0 else [], p.links
