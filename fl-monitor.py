#!/usr/bin/env python3
"""Монитор заказов FL.ru по RSS — законно, без парсинга.

Читает https://www.fl.ru/rss/all.xml, фильтрует по ключевым словам,
новые заказы отправляет в Telegram.

Запуск: python3 /root/fl-monitor/fl-monitor.py
Токен и chat_id — в /root/fl-monitor/config.json (права 600).
"""
import fcntl
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
STATE = BASE / "state.json"
LOG = BASE / "fl-monitor.log"
LOCK = BASE / "fl-monitor.lock"

RSS_URL = "https://www.fl.ru/rss/all.xml"
MAX_PER_RUN = 8          # не спамить: максимум сообщений за проход
MAX_STATE = 800          # сколько id помнить

# Ключевые слова: (слово в нижнем регистре, вес)
KEYWORDS = {
    # наше, сильные
    "python": 3, "c#": 3, "csharp": 3, "c sharp": 3,
    "linux": 3, "nginx": 3, "ssl": 3, "сервер": 3, "vps": 3,
    "почт": 3, "postfix": 3, "exim": 3, "roundcube": 3, "imap": 2,
    "парсинг": 3, "парсер": 3, "scrapy": 3, "selenium": 3, "playwright": 3,
    "api": 2, "интеграц": 2, "вебхук": 2, "webhook": 2,
    "бот": 2, "telegram": 2, "aiogram": 2,
    "автоматиз": 2, "скрипт": 2, "bash": 2, "docker": 2, "devops": 3,
    "база данных": 2, "postgres": 2, "mysql": 2, "sqlite": 2,
    "bash": 2, "powershell": 2, "windows": 1,
    # исключения — вычитаются
    "обход античит": -10, "античит": -10, "накрутк": -10,
    "авторегистрац": -10, "авторегер": -10, "читер": -10,
    "читы": -10, "читы ": -10,
    "1с": -3, "битрикс": -2, "tilda": -2, "тильда": -2,
    "логотип": -5, "презентац": -5, "нейминг": -5, "дизайн": -3,
    "seo": -5, "продвижен": -5, "реклама": -5, "копирайт": -5,
    "статьи": -5, "текст на заказ": -5,
}

THRESHOLD = 3     # минимальный вес, чтобы прислать


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_feed() -> str:
    req = urllib.request.Request(
        RSS_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; FL-monitor/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_items(xml: str):
    out = []
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def grab(tag):
            m = re.search(
                rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", raw, re.S
            )
            return clean(m.group(1)) if m else ""
        link = grab("link") or grab("guid")
        if not link:
            continue
        out.append({
            "title": grab("title"),
            "link": link,
            "desc": grab("description"),
            "cat": grab("category"),
            "date": grab("pubDate"),
        })
    return out


def score(item) -> int:
    text = f"{item['title']} {item['cat']} {item['desc']}".lower()
    total = 0
    for word, weight in KEYWORDS.items():
        if word in text:
            total += weight
    return total


def budget_of(title: str) -> str:
    m = re.search(r"Бюджет:\s*([\d\s\u00a0]+)\s*&#8381;", title)
    if not m:
        m = re.search(r"Бюджет:\s*([\d\s\u00a0]+)", title)
    if m:
        val = re.sub(r"\D", "", m.group(1))
        if val:
            return f"{int(val):,} ₽".replace(",", " ")
    return "по договорённости"


def tg_send(token: str, chat_id, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if not body.get("ok"):
            log(f"telegram error: {body.get('description')}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log(f"telegram exception: {e}")
        return False


def main() -> int:
    cfg = load_json(CONFIG, {})
    token = cfg.get("token")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        log("нет токена или chat_id в config.json")
        return 2

    # один экземпляр за раз
    lock_f = LOCK.open("w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    try:
        xml = fetch_feed()
    except Exception as e:  # noqa: BLE001
        log(f"rss недоступен: {e}")
        return 1

    items = parse_items(xml)
    state = load_json(STATE, {"seen": []})
    seen = set(state.get("seen", []))

    fresh = []
    for it in items:
        key = it["link"]
        if key in seen:
            continue
        s = score(it)
        it["score"] = s
        fresh.append(it)

    # все новые запоминаем, но присылаем только подходящие
    new_ids = [it["link"] for it in fresh]
    good = [it for it in fresh if it["score"] >= THRESHOLD]

    sent = 0
    for it in good[:MAX_PER_RUN]:
        title = it["title"]
        # убрать хвост "(Бюджет: ...)" и "(для всех)"
        shown = re.sub(r"\s*\((?:Бюджет[^)]*|для всех)\)\s*$", "", title).strip()
        text = (
            f"🎯 <b>{html.escape(shown)}</b>\n"
            f"💰 {budget_of(title)}\n"
            f"📂 {html.escape(it['cat'])}\n"
            f"⚖️ вес {it['score']}\n"
            f"{html.escape(it['link'])}"
        )
        if tg_send(token, chat_id, text):
            sent += 1
            time.sleep(1)

    merged = new_ids + state.get("seen", [])
    state["seen"] = merged[:MAX_STATE]
    state["updated"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE, state)

    if fresh:
        log(f"новых: {len(fresh)}, подходящих: {len(good)}, отправлено: {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
