"""
Парсинг прокси для Telethon из переменной окружения TG_PROXY.

Формат TG_PROXY: http://user:pass@host:port  или  socks5://user:pass@host:port
                 (или без auth: http://host:port)

Использование в любом скрипте с Telethon:
    from tg_proxy import get_tg_proxy
    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=get_tg_proxy())

Если TG_PROXY не задан → возвращает None → Telethon идёт напрямую.
"""
import os
from urllib.parse import urlparse


def get_tg_proxy():
    """Возвращает dict для Telethon `proxy=` или None если TG_PROXY не задан.

    Telethon поддерживает прокси через python_socks. Документация:
    https://docs.telethon.dev/en/latest/concepts/connection-modes.html#using-proxies
    """
    url = os.environ.get("TG_PROXY", "").strip()
    if not url:
        return None

    p = urlparse(url)
    scheme = (p.scheme or "http").lower()

    # Маппинг scheme → python_socks proxy_type
    type_map = {
        "http": "http",
        "https": "http",
        "socks4": "socks4",
        "socks5": "socks5",
    }
    proxy_type = type_map.get(scheme, "http")

    if not p.hostname or not p.port:
        raise ValueError(f"TG_PROXY некорректный (нет host/port): {url}")

    proxy = {
        "proxy_type": proxy_type,
        "addr": p.hostname,
        "port": p.port,
        "rdns": True,  # резолвить DNS через прокси (для обхода блокировок)
    }
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password

    return proxy


if __name__ == "__main__":
    import json
    import dotenv
    dotenv.load_dotenv()
    p = get_tg_proxy()
    if p:
        masked = dict(p)
        if "password" in masked:
            masked["password"] = "***"
        print(json.dumps(masked, indent=2))
    else:
        print("TG_PROXY не задан — Telethon будет идти напрямую")
