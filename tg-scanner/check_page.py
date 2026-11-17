#!/usr/bin/env python3
"""Диагностика режима t.me: показывает, что скрипт «видит» на странице t.me/<имя>.
    py check_page.py perehod4
Сохраняет page_<имя>.html — пришли его вместе с выводом, если что-то разбирается неверно.
"""
import os
import sys

import webscan as W

name = sys.argv[1].lstrip("@")
status, text, headers = W.http_get(f"{W.BASE}/{name}")
print("HTTP:", status, "| Retry-After:", headers.get("Retry-After"))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"page_{name}.html"), "w", encoding="utf-8") as f:
    f.write(text)
p = W.parse(text)
print("og:title      :", p.meta.get("og:title"))
print("og:description:", p.meta.get("og:description"))
print("og:image      :", p.meta.get("og:image"))
print("tgme_page_title/extra/desc:", p.texts)
print("РЕЗУЛЬТАТ     :", W.classify(name, p))
