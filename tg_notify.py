#!/usr/bin/env python3
"""
Telegram bot notifier — отправка сообщений через бота.
Использование: импорт из других скриптов.

import tg_notify
tg_notify.send("текст сообщения")

Setup (запустить ОДИН РАЗ): python3 tg_notify.py setup
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
CONFIG = BASE / ".tg_notify.json"

DEFAULT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")


def get_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    print(f"💾 Config saved: {CONFIG}")


def _get_opener():
    """urllib opener с HTTP-прокси если задан BOT_API_PROXY / GEMINI_PROXY.

    Bot API доступен через HTTPS — HTTP-прокси с CONNECT работает (curl-тест прошёл).
    Используем GEMINI_PROXY как общий HTTP-прокси для исходящих HTTPS запросов
    (если только не задан отдельный BOT_API_PROXY).
    """
    proxy = os.environ.get("BOT_API_PROXY") or os.environ.get("GEMINI_PROXY") or ""
    proxy = proxy.strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def get_updates(token: str) -> list:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    opener = _get_opener()
    with opener.open(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("result", [])


def send_message(token: str, chat_id: int, text: str,
                 parse_mode: str | None = None) -> bool:
    """Отправить сообщение боту. Long messages — split на части.
    parse_mode=None — plain text, надёжнее (markdown ломается на _ в технических словах)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)] if len(text) > MAX else [text]
    success = True
    opener = _get_opener()
    for chunk in chunks:
        body = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(body).encode("utf-8")
        try:
            with opener.open(url, data=data, timeout=30) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if not resp_data.get("ok"):
                    print(f"  ! Telegram: {resp_data}")
                    success = False
        except Exception as e:
            print(f"  ! Send err: {e}")
            success = False
    return success


def setup():
    """Поиск chat_id из getUpdates и сохранение."""
    cfg = get_config()
    token = cfg.get("token") or DEFAULT_TOKEN
    print(f"Запрашиваю /getUpdates у бота…")
    updates = get_updates(token)
    print(f"Updates: {len(updates)}")
    if not updates:
        print("⚠️ Updates пусто. Напиши боту любое сообщение (например /start) и запусти ещё раз.")
        sys.exit(1)
    chat_ids = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat", {})
        if chat.get("id"):
            chat_ids[chat["id"]] = (chat.get("first_name", "") + " " +
                                     chat.get("last_name", "")).strip() or chat.get("username", "?")
    print("\nНайденные chat_id:")
    for cid, name in chat_ids.items():
        print(f"  {cid}  {name}")

    if len(chat_ids) == 1:
        cid = list(chat_ids.keys())[0]
        cfg["chat_id"] = cid
        cfg["token"] = token
        save_config(cfg)
        print(f"\n✅ Сохранил chat_id={cid}")
        if send_message(token, cid, "🤖 *Бот настроен.* Готов слать уведомления."):
            print("✅ Тестовое сообщение отправлено")
    else:
        print("\nНайдено несколько chat_id. Передай нужный как: python3 tg_notify.py set-chat-id <ID>")


def set_chat_id(cid: int):
    cfg = get_config()
    cfg["chat_id"] = cid
    cfg["token"] = cfg.get("token") or DEFAULT_TOKEN
    save_config(cfg)
    print(f"✅ chat_id={cid} сохранён")


def send(text: str) -> bool:
    """Public API — отправить сообщение Сергею."""
    cfg = get_config()
    token = cfg.get("token") or DEFAULT_TOKEN
    chat_id = cfg.get("chat_id")
    if not chat_id:
        print("❌ chat_id не настроен. Запусти: python3 tg_notify.py setup")
        return False
    return send_message(token, chat_id, text)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "setup":
        setup()
    elif cmd == "set-chat-id" and len(sys.argv) >= 3:
        set_chat_id(int(sys.argv[2]))
    elif cmd == "test":
        text = " ".join(sys.argv[2:]) or "🤖 тест"
        send(text)
    elif cmd == "send":
        text = " ".join(sys.argv[2:])
        if not text:
            text = sys.stdin.read()
        send(text)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
