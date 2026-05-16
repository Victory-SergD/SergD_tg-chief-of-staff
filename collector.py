#!/usr/bin/env python3
"""
Единый коллектор данных из Telegram в SQLite.
Всё на Telethon: сторис, сообщения, транскрипция (Premium), диалоги.

Использование:
    python3 collector.py --stories                   # архив сторис
    python3 collector.py --stories --viewers          # + кто смотрел
    python3 collector.py --analyze @nikiteee007       # анализ переписки (gap + транскрипция)
    python3 collector.py --messages @username         # все сообщения в БД
    python3 collector.py --dialogs                   # все диалоги
    python3 collector.py --all                       # stories + viewers + dialogs

Флаги:
    --stories        Собрать архив сторис
    --viewers        + список зрителей каждой сторис
    --analyze @user  Анализ: gap detection + сбор + транскрипция Premium + вывод
    --messages @user Собрать все сообщения в БД
    --dialogs        Собрать все диалоги
    --all            stories + viewers + dialogs
    --limit N        Лимит (для тестов)
    --gap N          Перерыв в днях для --analyze (по умолчанию 7)
    --whisper        Использовать Whisper вместо Telegram Premium
    --db PATH        Путь к БД (по умолчанию tg_analiz.db)
"""

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import TranscribeAudioRequest, GetForumTopicsRequest
from telethon.tl.functions.stories import (
    GetStoriesArchiveRequest,
    GetStoryViewsListRequest,
)
from telethon.tl.types import (
    InputPeerSelf,
    PeerUser,
    ReactionEmoji,
    ReactionCustomEmoji,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaContact,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
)
from telethon.errors import FloodWaitError

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]
SESSION_PATH = str(BASE_DIR / os.environ.get("SESSION_NAME", "session"))
DB_DEFAULT = str(BASE_DIR / "tg_analiz.db")
SCHEMA_PATH = BASE_DIR / "db_schema.sql"


# ─── Утилиты ─────────────────────────────────────────────

def iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def now_iso() -> str:
    return datetime.now().isoformat()


def reaction_to_str(reaction) -> str | None:
    if reaction is None:
        return None
    if isinstance(reaction, ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, ReactionCustomEmoji):
        return f"custom:{reaction.document_id}"
    return str(reaction)


def media_type_str(media) -> str | None:
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc and hasattr(doc, 'mime_type'):
            mime = doc.mime_type or ""
            if mime.startswith("video"):
                return "video"
            if mime.startswith("audio"):
                return "audio"
        return "document"
    return type(media).__name__


def msg_media_type(msg) -> tuple[str | None, int | None, str | None]:
    """Определить тип медиа, длительность и имя файла из Telethon Message."""
    media = msg.media
    if media is None:
        return ("text" if msg.message else None), None, None

    if isinstance(media, MessageMediaPhoto):
        return "photo", None, None

    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if not doc:
            return "document", None, None

        duration = None
        file_name = None
        is_voice = False
        is_round = False

        for attr in (doc.attributes or []):
            if isinstance(attr, DocumentAttributeAudio):
                duration = attr.duration
                is_voice = getattr(attr, 'voice', False)
            elif isinstance(attr, DocumentAttributeVideo):
                duration = attr.duration
                is_round = getattr(attr, 'round_message', False)
            elif hasattr(attr, 'file_name'):
                file_name = attr.file_name

        mime = doc.mime_type or ""
        if is_voice:
            return "voice", duration, None
        if is_round:
            return "video_note", duration, None
        if mime.startswith("video"):
            return "video", duration, file_name
        if mime.startswith("audio"):
            return "audio", duration, file_name
        if "sticker" in mime or "webp" in mime or "tgs" in mime:
            return "sticker", None, None
        return "document", None, file_name

    if isinstance(media, MessageMediaContact):
        return "contact", None, None

    return type(media).__name__, None, None


async def safe_call(client, request, context=""):
    """Вызов API с обработкой FloodWait."""
    try:
        return await client(request)
    except FloodWaitError as e:
        wait = e.seconds + 1
        print(f"  [FloodWait] Ждём {wait}с ({context})...")
        await asyncio.sleep(wait)
        return await client(request)


async def get_telethon_client():
    """Создать и запустить Telethon клиент (с прокси если задан TG_PROXY)."""
    from tg_proxy import get_tg_proxy
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH, proxy=get_tg_proxy())
    await client.start(phone=PHONE)
    return client


# ─── База данных ──────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    # FK off: в группах senders бывают channels/боты которых нет в users
    conn.execute("PRAGMA foreign_keys=OFF")
    if SCHEMA_PATH.exists():
        conn.executescript(SCHEMA_PATH.read_text())
    # Migrations — добавляем колонки если их ещё нет
    existing = {row[1] for row in conn.execute("PRAGMA table_info(dialogs)")}
    for col, ddl in [
        ("is_archived",       "INTEGER DEFAULT 0"),
        ("is_muted",          "INTEGER DEFAULT 0"),
        ("last_msg_text",     "TEXT"),
        ("last_msg_outgoing", "INTEGER"),
        ("category",          "TEXT"),
        ("relation",          "TEXT"),
        ("notes",             "TEXT"),
        ("user_notes",        "TEXT"),
        ("user_notes_at",     "TEXT"),
        ("responsible_user",  "TEXT"),
        ("active_ownership",  "INTEGER"),
        ("importance",        "TEXT"),
        ("org_position",      "TEXT"),
        ("team_member_of",    "TEXT"),
        ("ai_deep_dive_json", "TEXT"),
        ("ai_deep_dive_at",   "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE dialogs ADD COLUMN {col} {ddl}")
    conn.commit()
    return conn


def upsert_user(conn: sqlite3.Connection, user) -> None:
    if user is None:
        return
    conn.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, is_bot, is_premium,
                          is_verified, is_deleted, is_scam, is_fake, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = COALESCE(excluded.username, users.username),
            first_name = COALESCE(excluded.first_name, users.first_name),
            last_name = COALESCE(excluded.last_name, users.last_name),
            is_bot = excluded.is_bot, is_premium = excluded.is_premium,
            is_verified = excluded.is_verified, is_deleted = excluded.is_deleted,
            is_scam = excluded.is_scam, is_fake = excluded.is_fake,
            updated_at = excluded.updated_at
    """, (
        user.id, getattr(user, 'username', None),
        getattr(user, 'first_name', None), getattr(user, 'last_name', None),
        int(getattr(user, 'bot', False) or False),
        int(getattr(user, 'premium', False) or False),
        int(getattr(user, 'verified', False) or False),
        int(getattr(user, 'deleted', False) or False),
        int(getattr(user, 'scam', False) or False),
        int(getattr(user, 'fake', False) or False),
        now_iso(),
    ))


def upsert_message(conn, msg_id, chat_id, from_user_id, date, text, caption,
                    media_type, media_duration, media_file_name, is_outgoing,
                    is_forwarded, reply_to_msg_id, edit_date, views_count, forwards_count,
                    topic_id=None):
    conn.execute("""
        INSERT INTO messages (msg_id, chat_id, from_user_id, date, text, caption,
            media_type, media_duration, media_file_name, is_outgoing,
            is_forwarded, reply_to_msg_id, edit_date, views_count, forwards_count,
            topic_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(msg_id, chat_id) DO UPDATE SET
            text = COALESCE(excluded.text, messages.text),
            edit_date = excluded.edit_date,
            views_count = excluded.views_count,
            topic_id = COALESCE(excluded.topic_id, messages.topic_id)
    """, (msg_id, chat_id, from_user_id, date, text, caption,
          media_type, media_duration, media_file_name, is_outgoing,
          is_forwarded, reply_to_msg_id, edit_date, views_count, forwards_count,
          topic_id))


def extract_topic_id(msg):
    """Возвращает topic_id из msg.reply_to (если это сообщение в ветке форума)."""
    rt = msg.reply_to
    if rt is None:
        return None
    # MessageReplyHeader has reply_to_top_id (иначе reply_to_msg_id это топик)
    top_id = getattr(rt, 'reply_to_top_id', None)
    if top_id:
        return top_id
    # forum_topic=True значит само reply_to_msg_id это id топика
    if getattr(rt, 'forum_topic', False):
        return getattr(rt, 'reply_to_msg_id', None)
    return None


async def fetch_topics(client, entity) -> dict:
    """Возвращает {topic_id: {title, icon_emoji, closed, pinned}} для группы-форума.

    Если группа не форум или нет прав — пустой dict.
    """
    topics_map = {}
    offset_topic = 0
    offset_id = 0
    offset_date = 0
    try:
        while True:
            r = await safe_call(
                client,
                GetForumTopicsRequest(
                    peer=entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    q=None,
                    limit=100,
                ),
                context=f"fetch_topics chat={getattr(entity, 'id', '?')}",
            )
            for t in r.topics:
                title = getattr(t, 'title', None) or "?"
                icon = ""
                ie = getattr(t, 'icon_emoji_id', None)
                if ie:
                    icon = f"emoji_id:{ie}"
                topics_map[t.id] = {
                    "title": title,
                    "icon_emoji": icon,
                    "closed": int(getattr(t, 'closed', False) or False),
                    "pinned": int(getattr(t, 'pinned', False) or False),
                }
            if len(r.topics) < 100:
                break
            last = r.topics[-1]
            offset_topic = last.id
            offset_id = getattr(last, 'top_message', 0) or 0
    except Exception as e:
        # не форум или нет прав — это ОК, просто пусто
        return topics_map
    return topics_map


def upsert_topics(conn, chat_id: int, topics: dict):
    """Сохраняем форумные топики в forum_topics."""
    if not topics:
        return
    now = now_iso()
    for topic_id, info in topics.items():
        conn.execute("""
            INSERT INTO forum_topics (chat_id, topic_id, title, icon_emoji,
                closed, pinned, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, topic_id) DO UPDATE SET
                title = excluded.title,
                icon_emoji = excluded.icon_emoji,
                closed = excluded.closed,
                pinned = excluded.pinned,
                updated_at = excluded.updated_at
        """, (chat_id, topic_id, info.get("title"), info.get("icon_emoji"),
              info.get("closed", 0), info.get("pinned", 0), now))


def upsert_transcription(conn, msg_id, chat_id, transcription, source="telegram_premium"):
    conn.execute("""
        INSERT INTO transcriptions (msg_id, chat_id, transcription, whisper_model, transcribed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(msg_id, chat_id) DO UPDATE SET
            transcription = excluded.transcription,
            transcribed_at = excluded.transcribed_at
    """, (msg_id, chat_id, transcription, source, now_iso()))


# ─── Транскрипция через Telegram Premium ──────────────────

async def transcribe_batch(client, peer, msg_ids: list[int],
                           batch_size: int = 10, max_retries: int = 8) -> dict:
    """
    Транскрибировать пачку голосовых через Telegram Premium.
    Возвращает {msg_id: text}.
    """
    results = {}
    pending_ids = list(msg_ids)

    # Первый проход — отправляем все запросы батчами
    for i in range(0, len(pending_ids), batch_size):
        batch = pending_ids[i:i + batch_size]
        tasks = []
        for mid in batch:
            tasks.append(safe_call(
                client,
                TranscribeAudioRequest(peer=peer, msg_id=mid),
                context=f"transcribe msg={mid}",
            ))
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for mid, r in zip(batch, batch_results):
            if isinstance(r, Exception):
                print(f"    Ошибка транскрипции {mid}: {r}")
                continue
            if not r.pending and r.text:
                results[mid] = r.text
            # pending — попробуем позже

    # Retry pending
    still_pending = [mid for mid in msg_ids if mid not in results]
    for attempt in range(max_retries):
        if not still_pending:
            break
        await asyncio.sleep(3)

        tasks = []
        for mid in still_pending:
            tasks.append(safe_call(
                client,
                TranscribeAudioRequest(peer=peer, msg_id=mid),
                context=f"retry transcribe msg={mid}",
            ))
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        newly_done = []
        for mid, r in zip(still_pending, batch_results):
            if isinstance(r, Exception):
                continue
            if not r.pending and r.text:
                results[mid] = r.text
                newly_done.append(mid)

        still_pending = [mid for mid in still_pending if mid not in results]
        if newly_done:
            print(f"    Транскрибировано: +{len(newly_done)} (попытка {attempt + 1})")

    if still_pending:
        print(f"    Не удалось транскрибировать: {len(still_pending)} сообщений")

    return results


# ─── Сборщик Stories ─────────────────────────────────────

async def collect_stories(client, conn, limit=0, collect_viewers=False):
    print("\n=== СБОР СТОРИС ===")
    offset_id = 0
    batch_size = 50
    total = 0
    remaining = limit if limit > 0 else float('inf')

    while remaining > 0:
        fetch = min(batch_size, int(remaining)) if limit > 0 else batch_size
        result = await safe_call(client,
            GetStoriesArchiveRequest(peer=InputPeerSelf(), offset_id=offset_id, limit=fetch),
            context=f"stories offset={offset_id}")

        stories = result.stories
        if not stories:
            break

        for user in getattr(result, 'users', []):
            upsert_user(conn, user)

        for s in stories:
            if not hasattr(s, 'views'):
                continue
            views = s.views
            conn.execute("""
                INSERT INTO stories (story_id, date, expire_date, caption, media_type,
                    is_pinned, is_public, is_close_friends, is_contacts, is_edited,
                    no_forwards, views_count, reactions_count, forwards_count, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(story_id) DO UPDATE SET
                    views_count=excluded.views_count, reactions_count=excluded.reactions_count,
                    forwards_count=excluded.forwards_count, collected_at=excluded.collected_at
            """, (
                s.id, iso(s.date), iso(getattr(s, 'expire_date', None)),
                getattr(s, 'caption', None), media_type_str(getattr(s, 'media', None)),
                int(getattr(s, 'pinned', False) or False),
                int(getattr(s, 'public', False) or False),
                int(getattr(s, 'close_friends', False) or False),
                int(getattr(s, 'contacts', False) or False),
                int(getattr(s, 'edited', False) or False),
                int(getattr(s, 'noforwards', False) or False),
                getattr(views, 'views_count', 0) if views else 0,
                getattr(views, 'reactions_count', 0) if views else 0,
                getattr(views, 'forwards_count', None) if views else None,
                now_iso(),
            ))
            total += 1

            if collect_viewers and views and getattr(views, 'has_viewers', False):
                await collect_story_viewers(client, conn, s.id)

        conn.commit()
        print(f"  Собрано: {total} сторис ({stories[-1].date.strftime('%Y-%m-%d')} — {stories[0].date.strftime('%Y-%m-%d')})")
        offset_id = stories[-1].id
        remaining -= len(stories)
        if len(stories) < fetch:
            break

    print(f"Итого сторис: {total}")
    return total


async def collect_story_viewers(client, conn, story_id):
    offset = ""
    count = 0
    while True:
        result = await safe_call(client,
            GetStoryViewsListRequest(peer=InputPeerSelf(), id=story_id, offset=offset, limit=100, q=""),
            context=f"viewers story={story_id}")
        for user in getattr(result, 'users', []):
            upsert_user(conn, user)
        for v in result.views:
            if not hasattr(v, 'user_id'):
                continue
            conn.execute("""
                INSERT INTO story_views (story_id, user_id, viewed_at, reaction, collected_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(story_id, user_id) DO UPDATE SET
                    reaction=COALESCE(excluded.reaction, story_views.reaction),
                    collected_at=excluded.collected_at
            """, (story_id, v.user_id, iso(getattr(v, 'date', None)),
                  reaction_to_str(getattr(v, 'reaction', None)), now_iso()))
            count += 1
        conn.commit()
        next_offset = getattr(result, 'next_offset', None)
        if not next_offset or not result.views:
            break
        offset = next_offset
    if count > 0:
        print(f"    Story {story_id}: {count} зрителей")


# ─── Сборщик Диалогов (Telethon) ─────────────────────────

async def collect_dialogs(client, conn, limit=0, include_archived=True):
    print("\n=== СБОР ДИАЛОГОВ ===")
    total = 0
    now_ts = datetime.now().timestamp()
    async for dialog in client.iter_dialogs(
        limit=limit if limit > 0 else None,
        archived=None if include_archived else False,
    ):
        entity = dialog.entity
        chat_type = "private"
        title = dialog.title or dialog.name
        username = getattr(entity, 'username', None)
        members = getattr(entity, 'participants_count', None)

        if dialog.is_group:
            chat_type = "group"
        elif dialog.is_channel:
            chat_type = "channel"

        # Archived / muted
        archived = int(getattr(dialog, 'archived', False) or False)
        muted = 0
        notify = getattr(dialog.dialog, 'notify_settings', None)
        if notify is not None:
            mu = getattr(notify, 'mute_until', None)
            if mu is not None:
                try:
                    muted = int(mu.timestamp() > now_ts)
                except Exception:
                    muted = 1

        # last message
        last_msg = dialog.message
        last_msg_id = getattr(last_msg, 'id', None) if last_msg else None
        last_msg_at = iso(getattr(last_msg, 'date', None)) if last_msg else None
        last_msg_text = (getattr(last_msg, 'message', None) or "")[:200] if last_msg else None
        last_msg_outgoing = int(bool(getattr(last_msg, 'out', False))) if last_msg else None

        conn.execute("""
            INSERT INTO dialogs (chat_id, chat_type, title, username, members_count,
                unread_count, is_pinned, is_archived, is_muted,
                last_message_id, last_message_at, last_msg_text, last_msg_outgoing,
                updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title, username=excluded.username,
                members_count=excluded.members_count, unread_count=excluded.unread_count,
                is_pinned=excluded.is_pinned, is_archived=excluded.is_archived,
                is_muted=excluded.is_muted,
                last_message_id=excluded.last_message_id,
                last_message_at=excluded.last_message_at,
                last_msg_text=excluded.last_msg_text,
                last_msg_outgoing=excluded.last_msg_outgoing,
                updated_at=excluded.updated_at
        """, (dialog.id, chat_type, title, username, members,
              dialog.unread_count or 0, int(dialog.pinned or False),
              archived, muted,
              last_msg_id, last_msg_at, last_msg_text, last_msg_outgoing,
              now_iso()))

        if chat_type == "private" and hasattr(entity, 'id'):
            upsert_user(conn, entity)

        total += 1
        if total % 200 == 0:
            conn.commit()
            print(f"  Собрано диалогов: {total}")

    conn.commit()
    print(f"Итого диалогов: {total}")
    return total


# ─── Сборщик Сообщений (Telethon) ────────────────────────

async def collect_messages(client, conn, username, limit=0, transcribe=False):
    """Собрать сообщения + опционально транскрибировать голосовые."""
    username = username.lstrip("@")
    print(f"\n=== СБОР СООБЩЕНИЙ: @{username} ===")

    entity = await client.get_entity(username)
    user_id = entity.id
    name = f"{getattr(entity, 'first_name', '') or ''} {getattr(entity, 'last_name', '') or ''}".strip()
    print(f"Чат с: {name} (@{username}, ID: {user_id})")

    upsert_user(conn, entity)
    conn.execute("""
        INSERT INTO dialogs (chat_id, chat_type, title, username, updated_at)
        VALUES (?, 'private', ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at
    """, (user_id, name, username, now_iso()))

    return await _collect_dialog_messages(client, conn, entity, user_id, limit, transcribe)


async def _collect_dialog_messages(client, conn, entity, chat_id, limit=0,
                                   transcribe=False, since_date=None):
    """Internal: собрать сообщения для уже резолвнутого entity (private/group/channel).

    since_date — datetime, останавливаемся когда упёрлись в более ранние сообщения.
    """
    total = 0
    voice_ids = []
    msg_limit = limit if limit > 0 else None

    async for msg in client.iter_messages(entity, limit=msg_limit):
        if since_date is not None and msg.date and msg.date.replace(tzinfo=None) < since_date:
            break

        media_type, media_duration, media_file_name = msg_media_type(msg)
        from_id = msg.sender_id
        reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
        topic_id = extract_topic_id(msg)

        upsert_message(conn, msg.id, chat_id, from_id, iso(msg.date),
                       msg.message, None, media_type, media_duration, media_file_name,
                       int(msg.out or False), int(bool(msg.fwd_from)),
                       reply_to, iso(msg.edit_date), msg.views, msg.forwards,
                       topic_id=topic_id)

        # Кэшируем участников групп если они приходят с сообщениями
        sender = msg.sender
        if sender is not None and not getattr(sender, 'broadcast', False):
            # users only (не channels)
            if hasattr(sender, 'first_name'):
                upsert_user(conn, sender)

        if media_type in ("voice", "audio", "video_note") and transcribe:
            voice_ids.append(msg.id)

        total += 1
        if total % 200 == 0:
            conn.commit()
            print(f"    собрано {total} сообщений…")

    conn.commit()
    print(f"  Сообщений: {total}, голосовых на транскрипцию: {len(voice_ids)}")

    if voice_ids and transcribe:
        peer = await client.get_input_entity(entity)
        transcriptions = await transcribe_batch(client, peer, voice_ids)
        for mid, text in transcriptions.items():
            upsert_transcription(conn, mid, chat_id, text)
        conn.commit()
        print(f"  Транскрибировано: {len(transcriptions)}/{len(voice_ids)}")

    return total


async def collect_active_smart(client, conn, since_date,
                               cap_private=1000, cap_group=500,
                               transcribe=True, skip_chat_ids=None):
    """
    «Умный» сбор: только чаты где Сергей писал хоть раз за период.

    Алгоритм для каждого активного диалога (с since_date):
      1. Quick-check: есть ли outgoing messages Сергея в период (from_user='me')
      2. Если нет → пометить category='silent', skip
      3. Если есть → сохранить все его outgoing + контекст (cap последних)
      4. Транскрибировать голосовые
    """
    import json as _json
    skip = set(skip_chat_ids or [])

    rows = conn.execute("""
        SELECT chat_id, chat_type, title FROM dialogs
        WHERE chat_type IN ('private','group')
          AND COALESCE(is_archived, 0) = 0
          AND last_message_at >= ?
        ORDER BY last_message_at DESC
    """, (since_date.isoformat(),)).fetchall()
    targets = [r for r in rows if r[0] not in skip]

    print(f"\n=== SMART COLLECT ({len(targets)} активных диалогов) ===")
    print(f"  cap private = {cap_private}, cap group = {cap_group}")
    print(f"  правило: Сергей не писал → skip")

    silent = []
    collected = []
    failed = []

    since_naive = since_date if since_date.tzinfo is None else since_date.replace(tzinfo=None)

    for i, (cid, ctype, title) in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] [{ctype}] {title} (id={cid})")
        try:
            entity = await client.get_entity(cid)
        except Exception as e:
            print(f"  resolver error: {e}")
            failed.append((cid, title, str(e))); continue

        # Step 0: для group — забираем топики (если форум)
        if ctype == 'group' and getattr(entity, 'forum', False):
            try:
                topics = await fetch_topics(client, entity)
                if topics:
                    upsert_topics(conn, cid, topics)
                    conn.commit()
                    print(f"  📂 топиков (форум): {len(topics)}")
            except Exception as e:
                print(f"  fetch_topics err: {e}")

        # Step 1: Сергея outgoing за период
        my_msgs = []
        try:
            async for msg in client.iter_messages(entity, from_user='me'):
                if msg.date is None:
                    continue
                if msg.date.replace(tzinfo=None) < since_naive:
                    break
                my_msgs.append(msg)
        except FloodWaitError as e:
            print(f"  [FloodWait] {e.seconds}s, ждём…")
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:
            print(f"  err in my-msgs check: {e}")
            failed.append((cid, title, str(e))); continue

        if not my_msgs:
            print(f"  Сергей молчит → skip body")
            conn.execute(
                "UPDATE dialogs SET category=?, notes=? WHERE chat_id=?",
                ("silent", _json.dumps({
                    "reason": "sergei_silent_in_period",
                    "since": since_date.isoformat(),
                }, ensure_ascii=False), cid)
            )
            conn.commit()
            silent.append((cid, title, ctype))
            continue

        # Step 2: сохраняем outgoing
        for msg in my_msgs:
            mt, dur, fn = msg_media_type(msg)
            reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
            topic_id = extract_topic_id(msg)
            upsert_message(conn, msg.id, cid, msg.sender_id, iso(msg.date),
                           msg.message, None, mt, dur, fn,
                           1, int(bool(msg.fwd_from)),
                           reply_to, iso(msg.edit_date), msg.views, msg.forwards,
                           topic_id=topic_id)

        # Step 3: контекст — последние cap сообщений за период (фильтр: только люди)
        cap = cap_private if ctype == 'private' else cap_group
        ctx_count = 0
        bot_skipped = 0
        voice_ids = []
        try:
            async for msg in client.iter_messages(entity, limit=cap):
                if msg.date is None:
                    continue
                if msg.date.replace(tzinfo=None) < since_naive:
                    break

                # Bot-filter: пропускаем сообщения от ботов / каналов / анонимных
                sender = msg.sender
                is_bot = False
                if sender is None:
                    is_bot = True
                elif getattr(sender, 'bot', False):
                    is_bot = True
                elif not hasattr(sender, 'first_name'):
                    # Channel sender в супергруппе → не человек
                    is_bot = True
                if is_bot:
                    bot_skipped += 1
                    continue

                mt, dur, fn = msg_media_type(msg)
                reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
                topic_id = extract_topic_id(msg)
                upsert_message(conn, msg.id, cid, msg.sender_id, iso(msg.date),
                               msg.message, None, mt, dur, fn,
                               int(msg.out or False), int(bool(msg.fwd_from)),
                               reply_to, iso(msg.edit_date), msg.views, msg.forwards,
                               topic_id=topic_id)

                upsert_user(conn, sender)

                if mt in ('voice', 'audio', 'video_note') and transcribe:
                    voice_ids.append(msg.id)
                ctx_count += 1
        except FloodWaitError as e:
            print(f"  [FloodWait ctx] {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)

        conn.commit()
        print(f"  outgoing(Сергей): {len(my_msgs)}, контекст(люди): {ctx_count}, "
              f"бот-сообщений отсеяно: {bot_skipped}, голосовых: {len(voice_ids)}")

        # Step 4: транскрипция
        if voice_ids and transcribe:
            try:
                peer = await client.get_input_entity(entity)
                transcriptions = await transcribe_batch(client, peer, voice_ids)
                for mid, text in transcriptions.items():
                    upsert_transcription(conn, mid, cid, text)
                conn.commit()
                if transcriptions:
                    print(f"  транскрибировано: {len(transcriptions)}/{len(voice_ids)}")
            except Exception as e:
                print(f"  ошибка транскрипции: {e}")

        collected.append((cid, title, ctype, len(my_msgs), ctx_count))

    print(f"\n=== ИТОГО ===")
    print(f"Активных: {len(targets)}")
    print(f"Собрано: {len(collected)}")
    print(f"Молчун-чаты (skip): {len(silent)}")
    print(f"Ошибок: {len(failed)}")
    if failed:
        for cid, t, err in failed[:10]:
            print(f"  - {t} (id={cid}): {err[:100]}")
    return {"collected": len(collected), "silent": len(silent), "failed": len(failed)}


async def collect_top_dialogs(client, conn, top_n=50, per_chat_limit=500,
                              since_date=None, transcribe=True,
                              include_groups=True, include_private=True,
                              include_channels=False, skip_chat_ids=None):
    """Iter: top-N most-active dialogs → собрать сообщения + транскрибировать."""
    skip_chat_ids = set(skip_chat_ids or [])
    types = []
    if include_private:  types.append("'private'")
    if include_groups:   types.append("'group'")
    if include_channels: types.append("'channel'")
    type_filter = ",".join(types)

    since_filter = ""
    params = []
    if since_date is not None:
        since_filter = "AND last_message_at >= ?"
        params.append(since_date.isoformat())

    if top_n > 0:
        limit_clause = "LIMIT ?"
        params.append(top_n + len(skip_chat_ids) + 5)
    else:
        limit_clause = ""

    rows = conn.execute(f"""
        SELECT chat_id, chat_type, title, username, last_message_at, unread_count
        FROM dialogs
        WHERE chat_type IN ({type_filter})
          AND COALESCE(is_archived, 0) = 0
          AND last_message_at IS NOT NULL
          {since_filter}
        ORDER BY last_message_at DESC
        {limit_clause}
    """, params).fetchall()

    targets = [r for r in rows if r[0] not in skip_chat_ids]
    if top_n > 0:
        targets = targets[:top_n]

    label = f"TOP-{top_n}" if top_n > 0 else f"ВСЕ АКТИВНЫЕ"
    print(f"\n=== {label} ДИАЛОГОВ ({len(targets)}) ===")
    for i, (cid, ctype, title, uname, lma, unread) in enumerate(targets, 1):
        print(f"  {i:2d}. [{ctype}] {title or '?'} (id={cid}, last={lma[:10] if lma else '?'}, unread={unread})")

    grand_total = 0
    grand_voices = 0
    failed = []

    for i, (cid, ctype, title, uname, lma, unread) in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] {title} (id={cid})")
        try:
            entity = await client.get_entity(cid)
            count = await _collect_dialog_messages(
                client, conn, entity, cid,
                limit=per_chat_limit,
                transcribe=transcribe,
                since_date=since_date,
            )
            grand_total += count
        except FloodWaitError as e:
            print(f"  [FloodWait] {e.seconds}s — ждём…")
            await asyncio.sleep(e.seconds + 1)
            failed.append((cid, title, str(e)))
        except Exception as e:
            print(f"  Ошибка: {e}")
            failed.append((cid, title, str(e)))

    print(f"\n=== ИТОГО ===")
    print(f"Чатов обработано: {len(targets) - len(failed)}/{len(targets)}")
    print(f"Сообщений собрано: {grand_total}")
    if failed:
        print(f"Не удалось ({len(failed)}):")
        for cid, t, err in failed:
            print(f"  - {t} (id={cid}): {err[:100]}")

    return grand_total


# ─── Анализ переписки (gap detection + транскрипция) ──────

async def analyze_contact(client, conn, username, gap_days=7, use_whisper=False):
    """
    Полный анализ: найти gap, собрать сообщения после него,
    транскрибировать голосовые, сохранить в БД, вывести текст.
    """
    username = username.lstrip("@")
    print(f"\n=== АНАЛИЗ: @{username} ===")

    entity = await client.get_entity(username)
    user_id = entity.id
    name = f"{getattr(entity, 'first_name', '') or ''} {getattr(entity, 'last_name', '') or ''}".strip()
    print(f"Контакт: {name} (@{username}, ID: {user_id})")

    upsert_user(conn, entity)
    conn.execute("""
        INSERT INTO dialogs (chat_id, chat_type, title, username, updated_at)
        VALUES (?, 'private', ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at
    """, (user_id, name, username, now_iso()))
    conn.commit()

    # 1. Ищем gap — идём от новых к старым
    collected = []
    prev_date = None
    gap_found = False

    print(f"Ищу перерыв > {gap_days} дней...")
    async for msg in client.iter_messages(entity, limit=None):
        if prev_date is not None:
            diff = prev_date - msg.date
            if diff.days >= gap_days:
                print(f"Перерыв: {diff.days} дней ({msg.date.strftime('%Y-%m-%d')} — {prev_date.strftime('%Y-%m-%d')})")
                gap_found = True
                break
        collected.append(msg)
        prev_date = msg.date

    if not gap_found:
        print(f"Перерыв > {gap_days} дней не найден — беру все {len(collected)}")

    collected.reverse()
    print(f"Сообщений после перерыва: {len(collected)}")

    if not collected:
        print("Нет сообщений!")
        return

    print(f"Период: {collected[0].date.strftime('%Y-%m-%d %H:%M')} — {collected[-1].date.strftime('%Y-%m-%d %H:%M')}")

    # 2. Записываем в БД
    voice_ids = []
    for msg in collected:
        media_type, media_duration, media_file_name = msg_media_type(msg)
        from_id = msg.sender_id
        reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None

        upsert_message(conn, msg.id, user_id, from_id, iso(msg.date),
                       msg.message, None, media_type, media_duration, media_file_name,
                       int(msg.out or False), int(bool(msg.fwd_from)),
                       reply_to, iso(msg.edit_date), msg.views, msg.forwards)

        if media_type in ("voice", "audio", "video_note"):
            voice_ids.append(msg.id)

    conn.commit()
    print(f"Записано в БД: {len(collected)} сообщений")

    # 3. Транскрибируем голосовые
    transcriptions = {}
    if voice_ids:
        if use_whisper:
            print("Транскрипция через Whisper не реализована в новой версии.")
            print("Используйте без --whisper для Telegram Premium транскрипции.")
        else:
            print(f"\nТранскрибирую {len(voice_ids)} голосовых через Telegram Premium...")
            peer = await client.get_input_entity(entity)
            transcriptions = await transcribe_batch(client, peer, voice_ids)

            for mid, text in transcriptions.items():
                upsert_transcription(conn, mid, user_id, text)

            conn.commit()
            print(f"Транскрибировано: {len(transcriptions)}/{len(voice_ids)}")

    # 4. Формируем текстовый вывод
    output_dir = Path(__file__).parent / "output" / f"@{username}"
    output_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for msg in collected:
        date_str = msg.date.strftime("%Y-%m-%d %H:%M")
        sender_entity = await msg.get_sender()
        sender = getattr(sender_entity, 'first_name', '?') or '?'
        sender_uname = getattr(sender_entity, 'username', '') or ''
        if sender_uname:
            sender_uname = f"(@{sender_uname})"

        media_type, _, _ = msg_media_type(msg)

        if msg.message:
            content = msg.message
        elif media_type in ("voice", "audio", "video_note"):
            label = {"voice": "Голосовое", "audio": "Аудио", "video_note": "Видеосообщение"}[media_type]
            text = transcriptions.get(msg.id, "[не транскрибировано]")
            content = f"[{label}]: {text}"
        elif media_type == "photo":
            content = "[Фото]"
        elif media_type == "video":
            content = "[Видео]"
        elif media_type == "document":
            content = "[Документ]"
        elif media_type == "sticker":
            content = "[Стикер]"
        else:
            content = "[другое]"

        lines.append(f"[{date_str}] {sender} {sender_uname}: {content}")

    full_text = "\n\n".join(lines)

    with open(output_dir / "full_conversation.txt", "w") as f:
        f.write(f"Переписка с {name} (@{username})\n")
        f.write(f"Период: {collected[0].date.strftime('%Y-%m-%d')} — {collected[-1].date.strftime('%Y-%m-%d')}\n")
        f.write(f"Всего сообщений: {len(collected)}\n")
        f.write(f"Из них голосовых: {len(voice_ids)}\n")
        f.write(f"Транскрибировано: {len(transcriptions)}\n")
        f.write("=" * 60 + "\n\n")
        f.write(full_text)

    # user_info.json
    import json
    with open(output_dir / "user_info.json", "w") as f:
        json.dump({
            "id": user_id, "first_name": entity.first_name,
            "last_name": entity.last_name, "username": entity.username,
            "phone": getattr(entity, 'phone', None),
        }, f, ensure_ascii=False, indent=2)

    # last_msg_id
    with open(output_dir / "last_msg_id.txt", "w") as f:
        f.write(str(collected[-1].id))

    print(f"\nГотово! Результаты: {output_dir}/")
    print("  full_conversation.txt — переписка с транскрипциями")
    print("  user_info.json — инфо о пользователе")
    return len(collected)


# ─── CLI ──────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Коллектор данных из Telegram в SQLite")
    p.add_argument("--stories", action="store_true", help="Собрать архив сторис")
    p.add_argument("--viewers", action="store_true", help="+ зрители каждой сторис")
    p.add_argument("--analyze", type=str, metavar="@USER", help="Анализ переписки")
    p.add_argument("--messages", type=str, metavar="@USER", help="Все сообщения в БД")
    p.add_argument("--dialogs", action="store_true", help="Все диалоги")
    p.add_argument("--all", action="store_true", help="stories + viewers + dialogs")
    p.add_argument("--top", type=int, default=0, metavar="N",
                   help="Собрать сообщения для top-N самых активных диалогов")
    p.add_argument("--active", action="store_true",
                   help="Собрать все активные с --since (private + group, не архив, не каналы)")
    p.add_argument("--per-chat", type=int, default=500,
                   help="Сколько сообщений тянуть на чат при --top (default 500)")
    p.add_argument("--since", type=str, default=None, metavar="YYYY-MM-DD",
                   help="Только сообщения с этой даты (для --top / --messages)")
    p.add_argument("--no-voice", action="store_true", help="Не транскрибировать голосовые")
    p.add_argument("--limit", type=int, default=0, help="Лимит (для тестов)")
    p.add_argument("--gap", type=int, default=7, help="Перерыв в днях для --analyze")
    p.add_argument("--whisper", action="store_true", help="Whisper вместо Premium")
    p.add_argument("--db", type=str, default=DB_DEFAULT, help="Путь к БД")
    return p.parse_args()


async def main():
    args = parse_args()

    has_task = (args.stories or args.dialogs or args.messages or args.analyze
                or args.all or args.top or args.active)
    if not has_task:
        print("Использование:")
        print("  python3 collector.py --stories              # сторис")
        print("  python3 collector.py --stories --viewers     # + зрители")
        print("  python3 collector.py --analyze @username     # анализ переписки")
        print("  python3 collector.py --messages @username    # сообщения в БД")
        print("  python3 collector.py --dialogs              # диалоги")
        print("  python3 collector.py --all                  # всё")
        sys.exit(1)

    conn = init_db(args.db)
    started = time.time()

    client = await get_telethon_client()
    me = await client.get_me()
    print(f"Подключён: {me.first_name} (@{me.username})\n")

    if args.stories or args.all:
        await collect_stories(client, conn, limit=args.limit,
                             collect_viewers=args.viewers or args.all)

    if args.dialogs or args.all:
        await collect_dialogs(client, conn, limit=args.limit)

    if args.messages:
        await collect_messages(client, conn, args.messages, limit=args.limit, transcribe=True)

    if args.analyze:
        await analyze_contact(client, conn, args.analyze, gap_days=args.gap,
                              use_whisper=args.whisper)

    if args.top or args.active:
        since = None
        if args.since:
            since = datetime.fromisoformat(args.since)
        me_id = (await client.get_me()).id
        skip = {93372553, me_id}  # BotFather, Saved Messages (self)
        if args.active:
            await collect_active_smart(
                client, conn, since_date=since,
                cap_private=1000, cap_group=500,
                transcribe=not args.no_voice,
                skip_chat_ids=skip,
            )
        else:
            await collect_top_dialogs(
                client, conn,
                top_n=args.top, per_chat_limit=args.per_chat,
                since_date=since, transcribe=not args.no_voice,
                skip_chat_ids=skip,
            )

    await client.disconnect()

    elapsed = time.time() - started
    print(f"\n{'=' * 60}")
    print(f"Готово за {elapsed:.1f}с")

    for table in ["users", "stories", "story_views", "dialogs", "messages", "transcriptions"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count > 0:
            print(f"  {table}: {count} записей")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
