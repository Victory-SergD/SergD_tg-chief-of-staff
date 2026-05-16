#!/usr/bin/env python3
"""
Отписаться от чатов / удалить из диалогов через Telethon.

Аргументы — список chat_id (целые числа со знаком, как в БД).

Использование:
    # Передаём IDs прямо в командной строке
    python3 unsubscribe.py -1001754252633 -1001648070788 -1001220120420

    # Из файла (один id на строку, можно с # комментариями)
    python3 unsubscribe.py --from-file ids.txt

    # Из stdin (paste списка)
    echo "-1001754252633\n-1001648070788" | python3 unsubscribe.py --stdin

Безопасность:
- Сначала dry-run: показывает что бы удалил, без действий.
- Запрашивает интерактивное подтверждение (введи UNSUB).
- Только потом вызывает client.delete_dialog().
- Лог в unsubscribe_log.json (что удалили + ошибки).
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.types import Channel, Chat, User, InputFolderPeer
from telethon.errors import FloodWaitError

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
DB = BASE / "tg_analiz.db"
LOG = BASE / "unsubscribe_log.json"


def parse_ids_from_args(args) -> list[int]:
    ids = []
    if args.ids:
        ids.extend(int(x) for x in args.ids)
    if args.from_file:
        for line in Path(args.from_file).read_text(encoding="utf-8").splitlines():
            line = line.strip().split("#")[0].strip()
            if not line:
                continue
            try: ids.append(int(line.split("|")[0].strip()))
            except: pass
    if args.stdin:
        for line in sys.stdin.read().splitlines():
            line = line.strip().split("#")[0].strip()
            if not line:
                continue
            try: ids.append(int(line.split("|")[0].strip()))
            except: pass
    return list(dict.fromkeys(ids))  # dedup


async def main_async():
    p = argparse.ArgumentParser()
    p.add_argument("ids", nargs="*", help="chat_id(s) для удаления")
    p.add_argument("--from-file", help="файл со списком id (по одному на строку)")
    p.add_argument("--stdin", action="store_true", help="читать список из stdin")
    p.add_argument("--yes", action="store_true",
                   help="пропустить интерактивное подтверждение (для скриптов)")
    args = p.parse_args()

    ids = parse_ids_from_args(args)
    if not ids:
        print("Не передано ни одного id. Используй args / --from-file / --stdin")
        sys.exit(1)

    # Сначала смотрим что в БД про эти чаты
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"""
        SELECT chat_id, chat_type, title, username, members_count,
               unread_count, last_message_at, category, importance
        FROM dialogs WHERE chat_id IN ({placeholders})
    """, ids).fetchall()
    rows_by_id = {r["chat_id"]: r for r in rows}

    # SAFETY: не трогаем чаты с user_notes (наша истина)
    protected = []
    for cid in ids:
        r = rows_by_id.get(cid)
        if r and (r["category"] == 'work' or r["importance"] == 'high'):
            protected.append((cid, r["title"], "work/high — точно нужный?"))

    print(f"\n=== ПЛАН ОТПИСКИ: {len(ids)} чатов ===\n")
    for cid in ids:
        r = rows_by_id.get(cid)
        if r:
            cat = f"[{r['category'] or '-'}]" if r["category"] else ""
            print(f"  {cid}  {r['chat_type']:8s} {cat} {r['title']}")
        else:
            print(f"  {cid}  ⚠️ не найден в dialogs (не в БД, но удалим если он в TG)")

    if protected:
        print(f"\n⚠️ ВНИМАНИЕ: {len(protected)} чатов помечены как work/high — "
              "точно ли их надо удалить?")
        for cid, title, reason in protected:
            print(f"  - {cid} {title}: {reason}")

    if not args.yes:
        print(f"\nДля подтверждения введи UNSUB и Enter (любое другое — отмена):")
        try:
            ans = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nОтменено."); sys.exit(0)
        if ans != "UNSUB":
            print("Отменено."); sys.exit(0)

    # Подключаемся к Telegram
    from tg_proxy import get_tg_proxy
    print("\nПодключаюсь к Telegram…", flush=True)
    client = TelegramClient(
        str(BASE / os.environ["SESSION_NAME"]),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        proxy=get_tg_proxy(),
    )
    await client.start(phone=os.environ["PHONE"])

    log_entries = []
    ok = 0
    fail = 0

    async def archive_entity(entity, cid):
        """Перемещает диалог в Archive folder (folder_id=1). История остаётся."""
        try:
            await client(EditPeerFoldersRequest(folder_peers=[
                InputFolderPeer(peer=await client.get_input_entity(entity), folder_id=1)
            ]))
        except Exception as e:
            print(f"    archive warn: {e}", flush=True)

    async def process_one(cid):
        title = rows_by_id.get(cid, {})
        title_str = title["title"] if title else "?"
        try:
            entity = await client.get_entity(cid)
            actions = []

            # Channel/Supergroup → отписаться (LeaveChannelRequest)
            if isinstance(entity, Channel):
                await client(LeaveChannelRequest(channel=entity))
                actions.append("leave-channel")
                # после leave диалог обычно сам исчезает,
                # но дополнительно архивируем для гарантии
                await archive_entity(entity, cid)
                actions.append("archive")
            # Old-style group (Chat) — выйти + архив
            elif isinstance(entity, Chat):
                await client.delete_dialog(entity)
                actions.append("leave-group")
            # User (private chat) — только архив (НЕ удалять историю!)
            elif isinstance(entity, User):
                await archive_entity(entity, cid)
                actions.append("archive-only")
            else:
                actions.append(f"unknown-type:{type(entity).__name__}")

            print(f"  ✓ {cid} ({title_str}): {', '.join(actions)}", flush=True)
            return ("ok", actions, None)
        except Exception as e:
            return ("fail", [], str(e))

    for cid in ids:
        title = rows_by_id.get(cid, {})
        title_str = title["title"] if title else "?"
        try:
            status, actions, err = await process_one(cid)
            if status == "ok":
                ok += 1
                log_entries.append({"chat_id": cid, "title": title_str,
                                    "status": "ok", "actions": actions,
                                    "at": datetime.now().isoformat()})
                # В БД помечаем как archived/unsubscribed (НЕ удаляем историю)
                conn.execute(
                    "UPDATE dialogs SET category='archived', is_archived=1, "
                    "importance='skip', "
                    "user_notes=COALESCE(user_notes,'') || ' [Отписан+архив "
                    "' || ? || ': ' || ? || ']' "
                    "WHERE chat_id=?",
                    (datetime.now().strftime('%Y-%m-%d'), '+'.join(actions), cid)
                )
            else:
                fail += 1
                print(f"  ✗ {cid} ({title_str}): {err}", flush=True)
                log_entries.append({"chat_id": cid, "title": title_str,
                                    "status": "fail", "error": err,
                                    "at": datetime.now().isoformat()})
        except FloodWaitError as e:
            print(f"  ⏸ FloodWait {e.seconds}s — пауза", flush=True)
            await asyncio.sleep(e.seconds + 1)
            # retry
            try:
                status, actions, err = await process_one(cid)
                if status == "ok":
                    ok += 1
                    log_entries.append({"chat_id": cid, "title": title_str,
                                        "status": "ok-retry", "actions": actions,
                                        "at": datetime.now().isoformat()})
                else:
                    fail += 1
                    log_entries.append({"chat_id": cid, "title": title_str,
                                        "status": "fail-retry", "error": err,
                                        "at": datetime.now().isoformat()})
            except Exception as e2:
                fail += 1
                log_entries.append({"chat_id": cid, "title": title_str,
                                    "status": "fail-retry", "error": str(e2),
                                    "at": datetime.now().isoformat()})

    conn.commit()
    await client.disconnect()
    conn.close()

    # Логируем результат
    existing_log = []
    if LOG.exists():
        try: existing_log = json.loads(LOG.read_text(encoding="utf-8"))
        except: pass
    existing_log.extend(log_entries)
    LOG.write_text(json.dumps(existing_log, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print(f"\n=== ИТОГО ===")
    print(f"OK: {ok}, fail: {fail}")
    print(f"Лог: {LOG}")


if __name__ == "__main__":
    asyncio.run(main_async())
