#!/usr/bin/env python3
"""
Дополняет существующие messages информацией о forum_topic.
Без полного пересбора — просто iter messages и UPDATE topic_id где NULL.

Также сохраняет forum_topics (id → title) для всех work-групп с форумом.

Использование:
    python3 update_topics.py
    python3 update_topics.py --since 2026-03-26
"""
import argparse
import asyncio
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.errors import FloodWaitError

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
DB = BASE / "tg_analiz.db"


def now_iso():
    from datetime import datetime
    return datetime.now().isoformat()


async def fetch_topics(client, entity) -> dict:
    """Возвращает {topic_id: {title, closed, pinned}}."""
    out = {}
    offset_topic = 0
    offset_id = 0
    offset_date = 0
    while True:
        try:
            r = await client(GetForumTopicsRequest(
                peer=entity, offset_date=offset_date,
                offset_id=offset_id, offset_topic=offset_topic,
                q=None, limit=100,
            ))
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:
            return out
        for t in r.topics:
            out[t.id] = {
                "title": getattr(t, "title", "?"),
                "closed": int(getattr(t, "closed", False) or False),
                "pinned": int(getattr(t, "pinned", False) or False),
            }
        if len(r.topics) < 100:
            break
        last = r.topics[-1]
        offset_topic = last.id
        offset_id = getattr(last, 'top_message', 0) or 0
    return out


def extract_topic_id(msg):
    rt = msg.reply_to
    if rt is None:
        return None
    top_id = getattr(rt, "reply_to_top_id", None)
    if top_id:
        return top_id
    if getattr(rt, "forum_topic", False):
        return getattr(rt, "reply_to_msg_id", None)
    return None


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2026-03-26")
    p.add_argument("--limit-chats", type=int, default=0)
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT chat_id, title FROM dialogs
        WHERE chat_type = 'group'
          AND COALESCE(is_archived, 0) = 0
          AND last_message_at >= ?
          AND COALESCE(category, '') NOT IN ('silent', '')
        ORDER BY last_message_at DESC
    """, (args.since,)).fetchall()
    if args.limit_chats > 0:
        rows = rows[:args.limit_chats]

    print(f"Кандидатов для топик-обновления: {len(rows)}", flush=True)

    from tg_proxy import get_tg_proxy
    client = TelegramClient(
        str(BASE / os.environ["SESSION_NAME"]),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        proxy=get_tg_proxy(),
    )
    await client.start(phone=os.environ["PHONE"])

    forum_count = 0
    msgs_updated_total = 0

    for i, r in enumerate(rows, 1):
        cid = r["chat_id"]
        title = r["title"] or "?"
        try:
            entity = await client.get_entity(cid)
        except Exception as e:
            print(f"[{i}/{len(rows)}] {title} → resolver err: {e}", flush=True)
            continue

        is_forum = getattr(entity, "forum", False)
        if not is_forum:
            print(f"[{i}/{len(rows)}] {title} — не форум, skip", flush=True)
            continue

        forum_count += 1
        topics = await fetch_topics(client, entity)
        if not topics:
            print(f"[{i}/{len(rows)}] {title} — форум, но GetForumTopics пусто",
                  flush=True)
            continue

        # Сохраняем топики
        for tid, info in topics.items():
            conn.execute("""
                INSERT INTO forum_topics (chat_id, topic_id, title, closed, pinned, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, topic_id) DO UPDATE SET
                    title=excluded.title, closed=excluded.closed,
                    pinned=excluded.pinned, updated_at=excluded.updated_at
            """, (cid, tid, info["title"], info["closed"], info["pinned"], now_iso()))
        conn.commit()

        # Идём по сообщениям и обновляем topic_id где NULL
        # Берём только те messages которые в DB уже есть и без topic_id
        existing = {r[0] for r in conn.execute(
            "SELECT msg_id FROM messages WHERE chat_id=? AND topic_id IS NULL "
            "AND date >= ?", (cid, args.since)
        ).fetchall()}

        if not existing:
            print(f"[{i}/{len(rows)}] {title} → топиков {len(topics)}, "
                  f"но нет сообщений без topic_id", flush=True)
            continue

        msgs_updated = 0
        try:
            async for msg in client.iter_messages(entity, ids=list(existing)):
                if msg is None:
                    continue
                tid = extract_topic_id(msg)
                if tid is not None:
                    conn.execute(
                        "UPDATE messages SET topic_id=? WHERE chat_id=? AND msg_id=?",
                        (tid, cid, msg.id)
                    )
                    msgs_updated += 1
        except FloodWaitError as e:
            print(f"  FloodWait {e.seconds}s — пауза", flush=True)
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            print(f"  iter_messages err: {e}", flush=True)
        conn.commit()
        msgs_updated_total += msgs_updated
        print(f"[{i}/{len(rows)}] {title} → топиков {len(topics)}, "
              f"обновлено сообщений {msgs_updated}", flush=True)

    await client.disconnect()
    print(f"\nИтого: форум-групп: {forum_count}/{len(rows)}, "
          f"сообщений обновлено: {msgs_updated_total}")


if __name__ == "__main__":
    asyncio.run(main())
