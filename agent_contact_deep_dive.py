#!/usr/bin/env python3
"""
Глубокий анализ одного контакта (или группы) через Claude Opus 4.7 (1M контекст).

Алгоритм:
1. Резолвит entity (private chat / group)
2. Догружает ВСЮ историю переписки через Telethon (idempotent upsert в БД)
3. Транскрибирует все новые голосовые через Gemini Flash Lite
4. Формирует промпт: метаданные + хронологический dump + 10 ключевых вопросов
5. Передаёт Опусу — получает структурированный JSON
6. Сохраняет: карточка .md + полный .json + пишет в БД (dialogs.ai_deep_dive_json)

Использование:
    python3 agent_contact_deep_dive.py --user @LankaStar
    python3 agent_contact_deep_dive.py --chat-id 716365720
    python3 agent_contact_deep_dive.py --user @bjlyd --since 2025-10-01
    python3 agent_contact_deep_dive.py --user @x --skip-fetch    # только из БД, не дёргать TG
"""
import argparse
import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import TranscribeAudioRequest, GetCommonChatsRequest
from telethon.utils import get_peer_id
from telethon.errors import FloodWaitError

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]
SESSION_PATH = str(BASE / os.environ.get("SESSION_NAME", "session"))
DB = str(BASE / "tg_analiz.db")
OUT_ROOT = BASE / "output"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_PROXY = os.environ.get("GEMINI_PROXY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

CLAUDE_TIMEOUT = 1800  # 30 мин на 1M-prompt (большие контексты дольше думают)
CLAUDE_MODEL = "claude-opus-4-7[1m]"  # Opus 4.7 1M context (5× больше дефолта 200K)

# Bot-cap: сколько последних сообщений от ботов брать на весь чат.
# От людей берём всё (cap по веткам — max_msgs_per_topic), от ботов — только N latest.
# Это убирает спам Lead-Router-бота в "Доноры" (50K msgs/мес) и в общих группах.
BOT_MSG_CAP = 50


DEEP_DIVE_PROMPT = """Ты — Контакт-Аналитик Сергея Дышканта (руководитель направления ВДЛ, lead generation).
Прямой руководитель Сергея — Влад (@bjlyd).

Тебе дана **полная** история переписки Сергея с одним контактом / в одной группе
(включая транскрипции голосовых и метаданные). Также — **авторитетные факты от Сергея**
(его ручные описания этих людей и подтверждённые им ответы), сообщения **сгруппированы
по веткам** форума (если это group). Используй ВСЁ это для максимально точного анализа.

ПРАВИЛА:
- Авторитетные факты от Сергея — это ИСТИНА. Не оспаривай их.
- В group deep_dive — отдельно опиши КАЖДУЮ ветку форума: что в ней обсуждается,
  кто отвечает, какие задачи открыты. Каждая ветка может быть отдельным проектом.
- Не выдумывай — если не уверен, добавь в `disputed`.
- Имена персонажей: если username совпадает с уже описанным человеком — это тот же
  человек (нормализуй имена через username).

Ты должен ответить на 10 ключевых вопросов в строгом JSON-формате.
Если данных недостаточно — пиши `"answer": "недостаточно сигнала"` для этого вопроса.
Если ты НЕ УВЕРЕН в ответе — добавь его в массив `disputed` с пояснением что нужно уточнить у Сергея.

ВОПРОСЫ:

1. **who_and_relation**: кто этот человек/группа + relation одной фразой.
   Пример: «Виталий @LankaStar — VDL-менеджер, мой подчинённый. Работает один на нескольких нишах».
   Для группы: «Рабочий чат отдела X, я там [боссом / коллегой / наблюдатель]».

2. **direction**: направление/проект (`VDL`, `Auto KRD`, `HR`, `SEO`, `Lead Router`,
   `Личное`, `Финансы` и т.п.). Если несколько — основной + второстепенные.

3. **org_chain**: кому он подчиняется и с кем ещё из команды Сергея работает.
   Известное: Влад → Сергей → (Даня Семёнов @hehehakiri, опер. директор) → VDL-менеджеры.

4. **top_topics**: топ-3 темы переписки (короткие фразы).

5. **last_two_weeks**: что обсуждали последние 2 недели (3-5 пунктов с датами если есть).

6. **open_tasks**: активные открытые задачи на этом контакте — ОБЯЗАТЕЛЬНО с указанием
   КТО кому что должен и С КАКОЙ ДАТЫ задача висит.
   Формат: `{ "task": "...", "assignee": "имя", "owner": "имя", "since": "ГГГГ-ММ-ДД",
              "blocker": null или "что мешает закрыть"}`.

7. **debts_and_obligations**: кто кому что должен (деньги/отчёт/файлы/доступы).
   Формат: `{"who_owes": "имя", "whom": "имя", "what": "...", "since": "...", "amount_if_money": "..." }`

8. **finance**: всё связанное с деньгами — суммы, договорённости по ЗП/мотивации/выплатам/долгам.
   Если нет финансовых сигналов — пустой массив.

9. **next_actions**: ТОП-3 действия Сергея на этом контакте на ближайшие 7 дней
   (конкретно, что написать/сделать/проконтролировать).

10. **format_review**: стоит ли пересмотреть формат отношений с этим человеком?
    Возможные ответы: `"keep"` (всё ок), `"upgrade"` (повысить/расширить), `"downgrade"` (урезать/реорганизовать),
    `"end"` (прекратить), `"action_needed"` (нужно срочно действовать) + 1-2 предложения почему.

ДОПОЛНИТЕЛЬНО ВЕРНУТЬ:

- **timeline**: 5-10 ключевых событий из истории с датами:
  `[{"date": "ГГГГ-ММ-ДД", "event": "..."}, ...]`
- **tldr**: 2-3 предложения резюме «кто это и что сейчас».
- **disputed**: массив `[{"point": "...", "reason": "почему не уверен / что уточнить"}]`
  ⚠️ ВАЖНО — disputed только для CRITICAL пробелов. НЕ задавай вопросы-шум.
  Спрашивай ТОЛЬКО если факт проходит хотя бы ОДИН тест:
   T1. Финансовый импакт (ФОТ, бюджеты, маржа, расходы)
   T2. Орг-структурный импакт (иерархия, статус работает/уволен, зона ответственности)
   T3. Стратегический сигнал ТОПов (Влад / Тимур / Никита Доник / Кочерев)
   T4. Блокер решения СЕЙЧАС (Сергей не двигает работу из-за пробела)
   T5. Просрочка с ущербом (>7 дней + сигнал потери денег / лидов / демотивации)
  НЕ задавай вопросов про: «кто такая Марина из 1 сообщения», «что было на встрече 25.03»,
  идентификацию второстепенных лиц, исторические мелочи, личные дела (билеты, парковка),
  технические детали которые исполнитель решит сам, статистику без блокера.
  Edge case 50/50 → НЕ ВКЛЮЧАЙ в disputed. Если вопрос реально критичен — он всплывёт снова.
- **confidence_overall**: 0.0-1.0 — общая уверенность в анализе.

- **user_notes_ready**: ⭐ ГОТОВЫЙ К ВСТАВКЕ блок текста для work_chats_review.md.
  Объедини в один связный абзац (~200-400 слов) от первого лица Сергея:
  кто этот человек/группа + иерархия (полная цепочка) + чем занимается +
  ключевые финансы (если есть) + verdict + открытые блокеры + что нужно делать.
  Пиши плотно, без воды, готовым к публикации. Пример хорошего стиля:

  «X @username — техспец по email-инфраструктуре. Прямой подчинённый Y (@y_handle, он же Cody, руководит технической частью). Иерархия: Влад → я → Y → X. Я руковожу отделом сверху, Y ведёт оперативку. За 3 мес X с нуля построил [конкретное описание]. ЗП считается Z в группе «Оплаты», USDT TRC-20, всё оплачено. Расходы ~$N/мес. Verdict: keep — без него проект встанет. Открытые блокеры: A 10 дней молчит по B; миграция C; 40 резервных доменов США. Нужна сводка по утрам — что планирует, отлёты доменов, миграции.»

  ВАЖНО: первое лицо («Я руковожу», «мне нужно»), без преамбулы, без markdown-обёртки.

🔥 ОСОБЫЙ РЕЖИМ — GROUP DEEP DIVE:
Если ЦЕЛЬ анализа — это **группа** (а не один контакт), и в блоке «ЛИЧКИ С КЛЮЧЕВЫМИ
УЧАСТНИКАМИ» что-то есть, ты ОБЯЗАТЕЛЬНО возвращаешь дополнительное поле:

- **participants_cards**: массив карточек на каждого ключевого участника. Формат:
  ```
  [{
    "user_id": int,
    "username": "...",
    "name": "Имя Фамилия",
    "who_and_relation": "одной фразой кто и в каких отношениях с Сергеем",
    "role_in_this_group": "что человек делает именно в этой группе",
    "direction": "VDL / Auto / HR / ...",
    "team_member_of": "кому подчиняется (если видно)",
    "manages": "кем руководит (если видно)",
    "open_tasks": [...],
    "next_actions": [...],
    "format_review": "keep / upgrade / downgrade / end / action_needed",
    "confidence": 0.0-1.0,
    "tldr": "2-3 предложения резюме про этого человека"
  }, ...]
  ```

Это нужно чтобы Сергей одним прогоном по ГРУППЕ получил карточки сразу на ВСЕХ
ключевых участников — и для самой группы, и для каждого участника по отдельности.

⛔ ВАЖНО ПРО ВЫВОД:
- НЕ пытайся записывать файлы, у тебя нет прав на файловую систему.
- НЕ оборачивай ответ в ```json ... ```.
- НЕ пиши никаких преамбул («сейчас выдам JSON», «Now I have data» и т.п.).
- НЕ пиши трейлингов («JSON выше», «если нужно сохранить — скажи»).
- ПЕРВЫЙ символ твоего ответа = `{`. ПОСЛЕДНИЙ = `}`. И больше ничего вокруг.

---

КОНТЕКСТ ЧАТА:
__CHAT_HEADER__

СТАТИСТИКА за всю историю:
__STATS__

АВТОРИТЕТНЫЕ ФАКТЫ ОТ СЕРГЕЯ (используй как ИСТИНУ, не оспаривай):
__AUTHORITATIVE__

ПОЛНАЯ ХРОНОЛОГИЯ ПЕРЕПИСКИ (от старого к новому):
__MESSAGES__

ПЕРЕСЕЧЕНИЯ В ОБЩИХ ГРУППАХ (для private deep_dive)
(сообщения этого человека и Сергея в группах где оба участники —
часто содержит контекст которого нет в личке: ответственный руководитель,
команда вокруг этого человека, реальный статус задач):
__COMMON_CHATS__

ЛИЧКИ С КЛЮЧЕВЫМИ УЧАСТНИКАМИ (для group deep_dive)
(топ-N активных участников этой группы + последние сообщения их лички с Сергеем —
для понимания кто эти люди и какие у Сергея с каждым прямые отношения):
__PARTICIPANTS_PRIVATE__
"""


def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Telethon helpers ──────────────────────────────────────

def iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def now_iso() -> str:
    return datetime.now().isoformat()


def msg_meta(msg) -> tuple[str, int | None]:
    """media_type, duration. Упрощённая версия из collector.py."""
    media = msg.media
    if media is None:
        return ("text" if msg.message else "empty"), None
    cls = type(media).__name__
    if cls == "MessageMediaPhoto":
        return "photo", None
    if cls == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        if not doc:
            return "document", None
        for attr in (doc.attributes or []):
            if hasattr(attr, "voice") and getattr(attr, "voice", False):
                return "voice", getattr(attr, "duration", None)
            if hasattr(attr, "round_message") and getattr(attr, "round_message", False):
                return "video_note", getattr(attr, "duration", None)
        mime = (doc.mime_type or "").lower()
        if mime.startswith("audio"):
            return "audio", None
        if mime.startswith("video"):
            return "video", None
        return "document", None
    return cls, None


def upsert_user(conn, user):
    if user is None or not hasattr(user, "id"):
        return
    conn.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, is_bot, is_premium,
            is_verified, is_deleted, is_scam, is_fake, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=COALESCE(excluded.username, users.username),
            first_name=COALESCE(excluded.first_name, users.first_name),
            last_name=COALESCE(excluded.last_name, users.last_name),
            is_bot=excluded.is_bot, is_premium=excluded.is_premium,
            updated_at=excluded.updated_at
    """, (
        user.id, getattr(user, "username", None),
        getattr(user, "first_name", None), getattr(user, "last_name", None),
        int(getattr(user, "bot", False) or False),
        int(getattr(user, "premium", False) or False),
        int(getattr(user, "verified", False) or False),
        int(getattr(user, "deleted", False) or False),
        int(getattr(user, "scam", False) or False),
        int(getattr(user, "fake", False) or False),
        now_iso(),
    ))


def upsert_message(conn, msg, chat_id):
    mt, dur = msg_meta(msg)
    reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
    topic_id = None
    rt = msg.reply_to
    if rt is not None:
        topic_id = getattr(rt, "reply_to_top_id", None)
        if topic_id is None and getattr(rt, "forum_topic", False):
            topic_id = getattr(rt, "reply_to_msg_id", None)

    conn.execute("""
        INSERT INTO messages (msg_id, chat_id, from_user_id, date, text,
            media_type, media_duration, is_outgoing, is_forwarded,
            reply_to_msg_id, edit_date, views_count, forwards_count, topic_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(msg_id, chat_id) DO UPDATE SET
            text=COALESCE(excluded.text, messages.text),
            edit_date=excluded.edit_date,
            topic_id=COALESCE(excluded.topic_id, messages.topic_id)
    """, (msg.id, chat_id, msg.sender_id, iso(msg.date),
          msg.message, mt, dur, int(msg.out or False),
          int(bool(msg.fwd_from)), reply_to,
          iso(msg.edit_date), msg.views, msg.forwards, topic_id))


async def fetch_full_history(client, conn, entity, since: datetime | None = None,
                             incremental: bool = True, max_msgs: int = 0):
    """Догружает messages этого чата в БД.

    incremental=True (default):
      - Если в БД для этого chat_id уже есть messages, и они покрывают since
        (т.е. MIN(date) <= since), тянем только msg_id > MAX(msg_id из БД).
      - Иначе — full fetch с since cutoff.
    """
    chat_id = entity.id if hasattr(entity, "id") else int(entity)
    if hasattr(entity, "broadcast") or hasattr(entity, "megagroup"):
        try:
            chat_id = int(f"-100{entity.id}") if getattr(entity, "megagroup", False) else int(f"-100{entity.id}")
        except Exception:
            chat_id = entity.id

    iter_kwargs = {"limit": None}
    use_incremental = False
    if incremental:
        row = conn.execute(
            "SELECT MAX(msg_id), MIN(date) FROM messages WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
        max_id_db, min_date_db = (row[0], row[1]) if row else (None, None)
        if max_id_db is not None:
            can_skip = True
            if since is not None and min_date_db is not None:
                try:
                    min_date_dt = datetime.fromisoformat(min_date_db).replace(tzinfo=None)
                    if min_date_dt > since.replace(tzinfo=None):
                        # БД не покрывает since — нужен full fetch
                        can_skip = False
                except Exception:
                    can_skip = False
            if can_skip:
                iter_kwargs["min_id"] = max_id_db
                use_incremental = True
                print(f"  ⚡ incremental: пропускаю msg_id <= {max_id_db} "
                      f"(уже в БД, MIN date={min_date_db[:10] if min_date_db else '?'})",
                      flush=True)

    if not use_incremental:
        print(f"  Догружаю полную историю (chat_id={chat_id})…", flush=True)

    total = 0
    voice_ids = []
    since_naive = since.replace(tzinfo=None) if since else None
    try:
        async for msg in client.iter_messages(entity, **iter_kwargs):
            if since_naive and msg.date and msg.date.replace(tzinfo=None) < since_naive:
                break
            if max_msgs > 0 and total >= max_msgs:
                print(f"  ⛔ hard cap {max_msgs} достигнут — стоп", flush=True)
                break
            upsert_message(conn, msg, chat_id)
            sender = msg.sender
            if sender is not None and hasattr(sender, "first_name"):
                upsert_user(conn, sender)
            mt, _ = msg_meta(msg)
            if mt in ("voice", "audio", "video_note"):
                voice_ids.append(msg.id)
            total += 1
            if total % 500 == 0:
                conn.commit()
                print(f"    {total} сообщений…", flush=True)
    except FloodWaitError as e:
        print(f"  FloodWait {e.seconds}s — пауза", flush=True)
        await asyncio.sleep(e.seconds + 1)
    conn.commit()
    if use_incremental:
        print(f"  Догружено {total} НОВЫХ сообщений (с момента last sync), "
              f"голосовых: {len(voice_ids)}", flush=True)
    else:
        print(f"  Догружено {total} сообщений, голосовых: {len(voice_ids)}", flush=True)
    return chat_id, voice_ids


# ─── Gemini transcription ──────────────────────────────────

def call_gemini_audio(audio_bytes: bytes) -> str | None:
    """Транскрипция аудио через Gemini API + 3 retry на network/timeout."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    body = {
        "contents": [{"parts": [
            {"text": "Транскрибируй это голосовое сообщение. Возвращай только текст транскрипции, без вступления."},
            {"inline_data": {"mime_type": "audio/ogg",
                             "data": base64.b64encode(audio_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"}
            for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                      "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
        ],
    }

    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        if GEMINI_PROXY:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                "http": GEMINI_PROXY, "https": GEMINI_PROXY,
            }))
        else:
            opener = urllib.request.build_opener()
        try:
            with opener.open(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 + attempt * 3)  # 2s, 5s
                continue
        except Exception as e:
            # parse / non-retryable
            print(f"    gemini err (no retry): {str(e)[:150]}", flush=True)
            return None
    print(f"    gemini fail after 3 retry: {str(last_err)[:120]}", flush=True)
    return None


async def fetch_common_chats(client, conn, target_entity, since: datetime | None) -> list[dict]:
    """
    Для private contact — найти все группы где Сергей и target вместе участвуют.
    Возвращает список {chat_id, title, msgs_in_window} — отсортирован по активности.
    """
    if not hasattr(target_entity, "first_name"):
        return []  # это не User (а группа), не имеет смысла
    try:
        r = await client(GetCommonChatsRequest(
            user_id=target_entity, max_id=0, limit=100,
        ))
    except Exception as e:
        print(f"  GetCommonChats err: {e}", flush=True)
        return []

    common = []
    target_id = target_entity.id
    for ch in r.chats:
        cid_db = get_peer_id(ch)
        # Считаем ВСЕ сообщения в общей группе за окно (включая других участников —
        # ZP/иерархия часто видны через их слова)
        sql = "SELECT COUNT(*) FROM messages WHERE chat_id = ?"
        params = [cid_db]
        if since is not None:
            sql += " AND date >= ?"
            params.append(since.isoformat())
        cnt_row = conn.execute(sql, params).fetchone()
        cnt_all = cnt_row[0] if cnt_row else 0

        # Подсчёт сообщений именно target+me — для фильтра «есть ли вообще активность нашей пары»
        sql_my = "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND (from_user_id = ? OR is_outgoing = 1)"
        params_my = [cid_db, target_id]
        if since is not None:
            sql_my += " AND date >= ?"
            params_my.append(since.isoformat())
        cnt_my = conn.execute(sql_my, params_my).fetchone()[0] or 0

        common.append({
            "chat_id": cid_db,
            "title": getattr(ch, "title", "?"),
            "msgs_in_window": cnt_all,  # всё для excerpt
            "msgs_target_me": cnt_my,   # для индикации релевантности
            "in_db": cnt_all > 0,
        })
    # Sort: где больше сообщений target+me, на первом месте
    common.sort(key=lambda x: -x["msgs_target_me"])
    return common


def build_common_chat_excerpt(conn, common: list[dict], target_user_id: int,
                               since: datetime | None,
                               max_msgs_per_topic: int = 1000,
                               top_n_groups: int = 5) -> str:
    """Для top_n_groups самых активных common-групп — берём сообщения за окно
    по ВСЕМ веткам. Лимит max_msgs_per_topic — на каждую ветку отдельно.
    Если групп больше top_n_groups — остальные показываем КРАТКОЙ строкой (без сообщений),
    чтобы Опус знал об их существовании, но prompt не раздувался.
    Сообщения без topic_id попадают в виртуальную ветку «Без темы»."""
    # common уже отсортирован по msgs_target_me (см. fetch_common_chats)
    active = [cc for cc in common if cc["msgs_in_window"] > 0]
    primary = active[:top_n_groups]
    overflow = active[top_n_groups:]

    sections = []
    for cc in primary:
        # Bot-cap: люди — до max_msgs_per_topic на ветку; боты — cap BOT_MSG_CAP
        # latest на весь чат (антиспам Lead-Router-бота в общих группах)
        sql = """
            SELECT * FROM (
                SELECT m.date, m.text, m.media_type, m.media_duration, m.is_outgoing,
                       m.topic_id, u.username, u.first_name, u.last_name,
                       COALESCE(u.is_bot, 0) AS is_bot,
                       t.transcription,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(m.topic_id, 0)
                           ORDER BY m.date DESC
                       ) AS rn_per_topic,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(u.is_bot, 0)
                           ORDER BY m.date DESC
                       ) AS rn_bot_or_human
                FROM messages m
                LEFT JOIN users u ON u.user_id=m.from_user_id
                LEFT JOIN transcriptions t ON t.msg_id=m.msg_id AND t.chat_id=m.chat_id
                WHERE m.chat_id=?
        """
        params = [cc["chat_id"]]
        if since is not None:
            sql += " AND m.date >= ?"
            params.append(since.isoformat())
        sql += f"""
            ) WHERE
                (is_bot = 1 AND rn_bot_or_human <= {int(BOT_MSG_CAP)})
                OR (is_bot = 0 AND rn_per_topic <= {int(max_msgs_per_topic)})
            ORDER BY date ASC
        """
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            continue
        # Топики этой группы
        topics_map = {
            r["topic_id"]: r["title"]
            for r in conn.execute(
                "SELECT topic_id, title FROM forum_topics WHERE chat_id=?",
                (cc["chat_id"],)
            )
        }

        lines = [f"\n### Общая группа: {cc['title']} (id={cc['chat_id']}) — "
                 f"{cc['msgs_in_window']} сообщений в окне, показываю до {len(rows)}:"]
        for r in rows:
            line = format_msg_row(r)
            if r["topic_id"] and r["topic_id"] in topics_map:
                line = line.replace(f"#t{r['topic_id']}",
                                     f"#тема:{topics_map[r['topic_id']]}")
            lines.append(line)
        sections.append("\n".join(lines))

    # Overflow: остальные группы перечислим компактно, без сообщений
    if overflow:
        names = [f"{cc['title']} ({cc['msgs_in_window']} msgs)" for cc in overflow]
        sections.append(
            f"\n### Ещё общих групп: {len(overflow)} (показаны top-{len(primary)} "
            f"по активности с тобой; остальные — только список):\n  - "
            + "\n  - ".join(names)
        )
    return "\n".join(sections)


def fetch_top_participants_with_private(conn, group_chat_id: int,
                                        since: datetime | None,
                                        top_n: int = 15,
                                        msgs_per_private: int = 0
                                        ) -> tuple[list[dict], list[dict]]:
    """
    Для group deep_dive — найти топ-N активных участников (не ботов, не Сергея).
    Возвращает (with_private, without_private):
      with_private = [{user_id, username, name..., private_rows}, ...]
      without_private = [{user_id, username, first_name, msgs_in_group}, ...]
    msgs_per_private=0 → без лимита по count, только period-based.
    """
    sql = """
        SELECT m.from_user_id, COUNT(*) as cnt,
               u.username, u.first_name, u.last_name, COALESCE(u.is_bot,0) as is_bot
        FROM messages m
        LEFT JOIN users u ON u.user_id = m.from_user_id
        WHERE m.chat_id = ? AND m.is_outgoing = 0
          AND COALESCE(u.is_bot, 0) = 0
          AND u.first_name IS NOT NULL
    """
    params = [group_chat_id]
    if since is not None:
        sql += " AND m.date >= ?"
        params.append(since.isoformat())
    sql += " GROUP BY m.from_user_id ORDER BY cnt DESC LIMIT ?"
    params.append(top_n)
    top = conn.execute(sql, params).fetchall()

    out = []
    no_private = []
    for r in top:
        # Личка с этим user_id (private chat_id == user_id для контакта)
        priv_sql = """
            SELECT m.date, m.text, m.media_type, m.media_duration, m.is_outgoing,
                   m.topic_id, u.username, u.first_name, u.last_name,
                   t.transcription
            FROM messages m
            LEFT JOIN users u ON u.user_id = m.from_user_id
            LEFT JOIN transcriptions t ON t.msg_id=m.msg_id AND t.chat_id=m.chat_id
            WHERE m.chat_id = ?
        """
        priv_params = [r["from_user_id"]]
        if since is not None:
            priv_sql += " AND m.date >= ?"
            priv_params.append(since.isoformat())
        # Хронология ASC — естественно. Лимит только если явно задан.
        priv_sql += " ORDER BY m.date ASC"
        if msgs_per_private > 0:
            priv_sql += " LIMIT ?"
            priv_params.append(msgs_per_private)
        priv_rows = list(conn.execute(priv_sql, priv_params).fetchall())

        if not priv_rows:
            no_private.append({
                "user_id": r["from_user_id"],
                "username": r["username"],
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "msgs_in_group": r["cnt"],
            })
            continue

        out.append({
            "user_id": r["from_user_id"],
            "username": r["username"],
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "msgs_in_group": r["cnt"],
            "private_rows": priv_rows,
        })
    return out, no_private


def fetch_authoritative_facts(conn, target_chat_id: int,
                              participant_user_ids: list[int]) -> str:
    """
    Авторитетные факты от Сергея, которые Опус ОБЯЗАН учесть как истину:
    1. user_notes из других чатов про этих людей (где target_chat_id отличается)
    2. answered=1 disputed_questions по этому или связанным чатам
    """
    parts = []

    # 1. user_notes других чатов про этих участников
    # (приватный chat_id == user_id для контактов)
    if participant_user_ids:
        placeholders = ",".join("?" * len(participant_user_ids))
        rows = conn.execute(f"""
            SELECT chat_id, title, username, user_notes
            FROM dialogs
            WHERE chat_id IN ({placeholders})
              AND user_notes IS NOT NULL AND user_notes != ''
              AND chat_id != ?
        """, list(participant_user_ids) + [target_chat_id]).fetchall()
        if rows:
            parts.append("📌 ИЗВЕСТНЫЕ ОПИСАНИЯ УЧАСТНИКОВ ОТ СЕРГЕЯ "
                         "(из его ручной ревизии других чатов — это ИСТИНА, не оспаривай):")
            for r in rows:
                u = f" @{r['username']}" if r["username"] else ""
                parts.append(f"\n• {r['title']}{u} (chat_id={r['chat_id']}):")
                parts.append(f"  «{r['user_notes']}»")

    # 2. user_notes самого target chat (если есть)
    own = conn.execute(
        "SELECT user_notes FROM dialogs WHERE chat_id=?", (target_chat_id,)
    ).fetchone()
    if own and own[0]:
        parts.append(f"\n📌 СОБСТВЕННОЕ ОПИСАНИЕ СЕРГЕЯ ОБ ЭТОМ ЧАТЕ "
                     f"(если что-то было заполнено ранее — учти):")
        parts.append(f"  «{own[0]}»")

    # 3. Подтверждённые ответы по этому чату или участникам
    ids_to_check = [target_chat_id] + list(participant_user_ids)
    placeholders = ",".join("?" * len(ids_to_check))
    answered = conn.execute(f"""
        SELECT q.chat_id, q.point, q.answer, d.title
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id=q.chat_id
        WHERE q.answered = 1 AND q.answer IS NOT NULL
          AND q.chat_id IN ({placeholders})
        ORDER BY q.answered_at DESC LIMIT 50
    """, ids_to_check).fetchall()
    if answered:
        parts.append("\n\n📌 ПОДТВЕРЖДЁННЫЕ ОТВЕТЫ СЕРГЕЯ НА ПРОШЛЫЕ ВОПРОСЫ AI "
                     "(не задавай эти же вопросы повторно):")
        for a in answered:
            parts.append(f"\n• [{a['title'] or '?'}] Вопрос: {a['point']}")
            parts.append(f"  Ответ Сергея: {a['answer']}")

    return "\n".join(parts) if parts else ""


def group_messages_by_topic(rows, topics_map: dict) -> dict:
    """Возвращает OrderedDict {label: [rows]}. Без topic_id → 'Без темы'."""
    by_topic = {}
    for r in rows:
        tid = r["topic_id"]
        if tid is None:
            label = "Без темы"
        elif tid in topics_map:
            label = f"#тема:{topics_map[tid]} (id={tid})"
        else:
            label = f"#тема:БЕЗ_НАЗВАНИЯ (id={tid})"
        by_topic.setdefault(label, []).append(r)
    return by_topic


def build_participants_private_excerpt(participants: list[dict],
                                        no_private: list[dict] = None) -> str:
    parts = []
    if participants:
        sections = []
        for p in participants:
            name = ((p["first_name"] or "") +
                    (" " + p["last_name"] if p["last_name"] else "")).strip() or "?"
            uname = f" @{p['username']}" if p["username"] else ""
            head = (f"\n### Личка Сергея с {name}{uname} "
                    f"(в группе он/она написал {p['msgs_in_group']} msgs; "
                    f"в личке за период {len(p['private_rows'])} сообщений):")
            lines = [head]
            for r in p["private_rows"]:
                lines.append(format_msg_row(r))
            sections.append("\n".join(lines))
        parts.append("\n".join(sections))
    if no_private:
        parts.append("\n### Активные участники БЕЗ лички в БД "
                     "(можно судить только по их сообщениям внутри этой группы):")
        for p in no_private:
            name = ((p["first_name"] or "") +
                    (" " + p["last_name"] if p["last_name"] else "")).strip() or "?"
            uname = f" @{p['username']}" if p["username"] else ""
            parts.append(f"  - {name}{uname} (id={p['user_id']}, в группе: {p['msgs_in_group']} msgs)")
    return "\n".join(parts)


async def transcribe_missing(client, conn, chat_id: int,
                             since: datetime | None = None,
                             max_voices: int = 0):
    """Транскрибирует voice-сообщения этого чата без транскрипции через Gemini.
    since — фильтр периода. max_voices — hard cap (0 = нет лимита).
    При cap берём САМЫЕ СВЕЖИЕ голосовые (LIMIT через ORDER BY date DESC)."""
    sql = """
        SELECT m.msg_id FROM messages m
        LEFT JOIN transcriptions t ON t.msg_id=m.msg_id AND t.chat_id=m.chat_id
        WHERE m.chat_id=? AND m.media_type IN ('voice','audio','video_note')
          AND t.transcription IS NULL
    """
    params = [chat_id]
    if since is not None:
        sql += " AND m.date >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY m.date DESC"
    if max_voices > 0:
        sql += " LIMIT ?"
        params.append(max_voices)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return 0
    print(f"  Транскрибирую {len(rows)} голосовых через Gemini (параллельно × 10)…",
          flush=True)
    tmp = BASE / ".voices_tmp"
    tmp.mkdir(exist_ok=True)

    sema = asyncio.Semaphore(10)
    db_lock = asyncio.Lock()
    done = 0

    async def worker(msg_id):
        nonlocal done
        async with sema:
            try:
                msg = await client.get_messages(chat_id, ids=msg_id)
                if msg is None:
                    return
                path = await msg.download_media(
                    file=str(tmp / f"{chat_id}_{msg_id}.ogg"))
                if not path:
                    return
                audio = Path(path).read_bytes()
                text = await asyncio.get_event_loop().run_in_executor(
                    None, call_gemini_audio, audio
                )
                if text:
                    async with db_lock:
                        conn.execute("""
                            INSERT INTO transcriptions (msg_id, chat_id, transcription,
                                whisper_model, transcribed_at)
                            VALUES (?, ?, ?, 'gemini-deep-dive', ?)
                            ON CONFLICT(msg_id, chat_id) DO UPDATE SET
                                transcription=excluded.transcription,
                                transcribed_at=excluded.transcribed_at
                        """, (msg_id, chat_id, text, now_iso()))
                        done += 1
                try: Path(path).unlink()
                except: pass
            except Exception as e:
                print(f"    msg {msg_id}: {e}", flush=True)

    tasks = [worker(r["msg_id"]) for r in rows]
    await asyncio.gather(*tasks, return_exceptions=True)
    conn.commit()
    print(f"  Транскрибировано: {done}/{len(rows)}", flush=True)
    return done


# ─── Build context for Opus ────────────────────────────────

def format_msg_row(r) -> str:
    date = r["date"][:16].replace("T", " ")
    if r["is_outgoing"]:
        sender = "Я"
    else:
        name = (r["first_name"] or "") + (" " + r["last_name"] if r["last_name"] else "")
        name = name.strip() or "?"
        if r["username"]:
            name = f"{name} (@{r['username']})"
        sender = name

    mt = r["media_type"]
    text = r["text"] or ""
    tr = r["transcription"]
    if mt in ("voice", "audio", "video_note"):
        content = f"[голос/{r['media_duration']}с]: {tr or '[без транскрипции]'}"
    elif mt == "photo":
        content = f"[фото] {text[:100]}".strip()
    elif mt == "video":
        content = f"[видео] {text[:100]}".strip()
    elif mt == "document":
        content = f"[документ] {text[:100]}".strip()
    elif mt == "sticker":
        content = "[стикер]"
    else:
        content = text or "[empty]"

    if r["topic_id"]:
        topic_str = f" #t{r['topic_id']}"
    else:
        topic_str = ""

    if len(content) > 800:
        content = content[:800] + "…"
    return f"[{date}{topic_str}] {sender}: {content}"


def gather_chat_data(conn, chat_id: int, since: datetime | None = None,
                     max_msgs_per_topic: int = 1000):
    """Per-topic cap: 1000 на каждую ветку форума (либо на «Без темы»).
    Если в чате 12 веток → возьмём до 12 000 сообщений (по 1000 свежих per topic).
    Для private (без topics) — это будет cap 1000 на «Без темы»."""
    dlg = conn.execute(
        "SELECT chat_id, chat_type, title, username, members_count, "
        "category, relation, notes, user_notes "
        "FROM dialogs WHERE chat_id=?", (chat_id,)
    ).fetchone()

    sql_since = ""
    params = [chat_id]
    if since is not None:
        sql_since = "AND m.date >= ?"
        params.append(since.isoformat())

    # Bot-cap: люди — до max_msgs_per_topic на ветку (как было); боты — cap 50 latest
    # на весь чат (антиспам Lead-Router-бота в "Доноры", который пишет 50K за месяц)
    rows = conn.execute(f"""
        SELECT * FROM (
            SELECT m.msg_id, m.date, m.text, m.media_type, m.media_duration,
                   m.is_outgoing, m.topic_id, m.reply_to_msg_id,
                   u.username, u.first_name, u.last_name,
                   COALESCE(u.is_bot, 0) AS is_bot,
                   t.transcription,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(m.topic_id, 0)
                       ORDER BY m.date DESC
                   ) AS rn_per_topic,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(u.is_bot, 0)
                       ORDER BY m.date DESC
                   ) AS rn_bot_or_human
            FROM messages m
            LEFT JOIN users u ON u.user_id=m.from_user_id
            LEFT JOIN transcriptions t ON t.msg_id=m.msg_id AND t.chat_id=m.chat_id
            WHERE m.chat_id=? {sql_since}
        ) WHERE
            (is_bot = 1 AND rn_bot_or_human <= {int(BOT_MSG_CAP)})
            OR (is_bot = 0 AND rn_per_topic <= {int(max_msgs_per_topic)})
        ORDER BY date ASC
    """, params).fetchall()

    topics = conn.execute(
        "SELECT topic_id, title FROM forum_topics WHERE chat_id=?",
        (chat_id,)
    ).fetchall()
    topics_map = {t["topic_id"]: t["title"] for t in topics}

    return dlg, rows, topics_map


def build_context(dlg, rows, topics_map,
                  common_chats_text: str = "",
                  participants_text: str = "",
                  authoritative_text: str = "") -> tuple[str, dict]:
    if not rows:
        raise RuntimeError("Нет сообщений в БД для этого чата")

    title = dlg["title"] if dlg else "?"
    chat_type = dlg["chat_type"] if dlg else "?"
    uname = dlg["username"] if dlg else None

    header_lines = [
        f"chat_id: {dlg['chat_id'] if dlg else '?'}",
        f"тип: {chat_type}",
        f"название: {title}",
    ]
    if uname:
        header_lines.append(f"username: @{uname}")
    if chat_type == "group" and dlg and dlg["members_count"]:
        header_lines.append(f"участников: {dlg['members_count']}")
    if topics_map:
        header_lines.append(f"топики (forum-темы): {len(topics_map)}")
        for tid, ttl in list(topics_map.items())[:30]:
            header_lines.append(f"  #t{tid}: {ttl}")
    if dlg and dlg["user_notes"]:
        header_lines.append(f"\nЗаметки Сергея об этом чате (его слова):")
        header_lines.append(f"  «{dlg['user_notes']}»")

    header = "\n".join(header_lines)

    sent = sum(1 for r in rows if r["is_outgoing"])
    voices = sum(1 for r in rows if r["media_type"] in ("voice", "audio", "video_note"))
    transcribed = sum(1 for r in rows if r["transcription"])
    first_dt = rows[0]["date"][:10]
    last_dt = rows[-1]["date"][:10]

    stats = (
        f"всего сообщений: {len(rows)}\n"
        f"от Сергея: {sent}, входящих: {len(rows)-sent}\n"
        f"голосовых: {voices}, транскрибировано: {transcribed}\n"
        f"период: {first_dt} → {last_dt}"
    )

    # Если group и есть topics — группируем по веткам, чтобы Опус видел структуру
    is_group = (dlg["chat_type"] == "group") if dlg else False
    if is_group and topics_map:
        by_topic = group_messages_by_topic(rows, topics_map)
        # Сортировка: General/Без темы первым, потом ветки по числу сообщений
        order = sorted(by_topic.keys(),
                       key=lambda k: (
                           0 if "Без темы" in k or "General" in k else 1,
                           -len(by_topic[k])
                       ))
        sections = []
        for label in order:
            tr = by_topic[label]
            head = f"\n━━━ {label} ({len(tr)} сообщений) ━━━"
            sections.append(head)
            for r in tr:
                sections.append(format_msg_row(r))
        msgs_text = "\n".join(sections)
    else:
        msgs_text = "\n".join(format_msg_row(r) for r in rows)

    return (DEEP_DIVE_PROMPT
            .replace("__CHAT_HEADER__", header)
            .replace("__STATS__", stats)
            .replace("__AUTHORITATIVE__", authoritative_text or "(нет ранее зафиксированных фактов от Сергея)")
            .replace("__MESSAGES__", msgs_text)
            .replace("__COMMON_CHATS__", common_chats_text or "(общих групп не найдено или это сама группа)")
            .replace("__PARTICIPANTS_PRIVATE__", participants_text or "(не group deep_dive — этот блок пуст)")
            ), {
                "total_msgs": len(rows),
                "sent": sent,
                "first_date": first_dt,
                "last_date": last_dt,
            }


# ─── Claude ──────────────────────────────────────────────

def call_claude(prompt: str, model: str = CLAUDE_MODEL,
                oauth_token: str | None = None) -> tuple[str, float]:
    """Запуск через --output-format json — даёт structured envelope.
    Внутри .result лежит финальный текст Опуса (наш JSON).

    oauth_token — если передан, используется через CLAUDE_CODE_OAUTH_TOKEN env var
    (для параллельной работы на разных сессиях sess2/sess3, чтобы не жечь основную)."""
    start = time.time()
    env = os.environ.copy()
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    result = subprocess.run(
        ["claude", "-p", "--model", model,
         "--no-session-persistence",
         "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
        env=env,
    )
    dur = time.time() - start
    if result.returncode != 0:
        raise RuntimeError(
            f"claude failed code={result.returncode}: "
            f"{result.stderr[:500] or result.stdout[:500]}"
        )
    # Envelope: { result: "<наш JSON>", usage: {...}, ... }
    try:
        env = json.loads(result.stdout)
        return env.get("result", "").strip(), dur
    except json.JSONDecodeError:
        # Fallback — старое поведение
        return result.stdout.strip(), dur


def parse_json(text: str) -> dict:
    """
    Извлекает JSON даже если Опус добавил преамбулу/обёртку.
    Поддерживает: чистый JSON, ```json ... ```, текст-перед-```json, голый объект.
    """
    text = text.strip()
    # 1. Любой ```json блок где-то в тексте
    if "```json" in text:
        after = text.split("```json", 1)[1]
        body = after.split("```", 1)[0].strip()
        return json.loads(body)
    # 2. Любой ``` блок где-то в тексте
    if "```" in text:
        after = text.split("```", 1)[1]
        body = after.split("```", 1)[0].strip()
        if body and body[0] in "{[":
            return json.loads(body)
    # 3. Найти первый { до соответствующего закрывающего }
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False; continue
            if c == "\\":
                esc = True; continue
            if c == '"' and not esc:
                in_str = not in_str; continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i+1])
    return json.loads(text)


# ─── Output ──────────────────────────────────────────────

def render_card(result: dict, dlg: dict | None, stats: dict) -> str:
    """Короткая markdown-карточка для быстрого ревью."""
    title = dlg["title"] if dlg else "?"
    uname = f"@{dlg['username']}" if dlg and dlg["username"] else ""
    lines = [f"# Deep-dive: {title} {uname}".rstrip(), ""]
    lines.append(f"_Сгенерировано {datetime.now().strftime('%Y-%m-%d %H:%M')}_  ")
    lines.append(f"_Сообщений в БД: {stats['total_msgs']}, "
                 f"от Сергея: {stats['sent']}, "
                 f"период: {stats['first_date']} → {stats['last_date']}_")
    lines.append("")

    if result.get("user_notes_ready"):
        lines.append("## ⭐ Готовый блок для work_chats_review.md\n")
        lines.append("```")
        lines.append(f"**Мой ответ:** {result['user_notes_ready']}")
        lines.append("```\n")

    if "tldr" in result:
        lines.append(f"## TL;DR\n\n{result['tldr']}\n")

    qmap = [
        ("who_and_relation", "1. Кто и в каких отношениях"),
        ("direction", "2. Направление / проект"),
        ("org_chain", "3. Орг-цепочка"),
        ("top_topics", "4. Топ-3 темы"),
        ("last_two_weeks", "5. Последние 2 недели"),
        ("open_tasks", "6. Открытые задачи"),
        ("debts_and_obligations", "7. Кто кому что должен"),
        ("finance", "8. Финансы"),
        ("next_actions", "9. Что написать сейчас"),
        ("format_review", "10. Пересмотр формата"),
    ]
    for key, label in qmap:
        v = result.get(key)
        if v in (None, "", []):
            continue
        lines.append(f"### {label}")
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    lines.append(f"- " + "; ".join(f"**{k}**: {vv}" for k, vv in item.items() if vv))
                else:
                    lines.append(f"- {item}")
        elif isinstance(v, dict):
            for k, vv in v.items():
                lines.append(f"- **{k}**: {vv}")
        else:
            lines.append(str(v))
        lines.append("")

    if result.get("timeline"):
        lines.append("## Timeline ключевых событий")
        for ev in result["timeline"]:
            d = ev.get("date", "?")
            e = ev.get("event", "?")
            lines.append(f"- **{d}** — {e}")
        lines.append("")

    if result.get("disputed"):
        lines.append("## 🚩 Спорные моменты (нужно подтвердить)")
        for d in result["disputed"]:
            if not isinstance(d, dict):
                lines.append(f"- {d}"); continue
            head = d.get("point") or d.get("claim") or "?"
            tail = d.get("reason") or d.get("issue") or ""
            conf = d.get("confidence")
            line = f"- **{head}**: {tail}"
            if conf is not None:
                line += f" _(conf {conf})_"
            lines.append(line)
        lines.append("")

    # Карточки участников (для group deep_dive). Опус возвращает разные ключи —
    # рендерим гибко, печатаем любые поля что вернул.
    pcards = result.get("participants_cards") or []
    if pcards:
        lines.append("\n---\n\n## 👥 Карточки ключевых участников группы\n")
        # Известные ключи (русский label)
        labels = {
            "name": "Имя", "username": "Username", "user_id": "user_id",
            "role": "Роль",
            "role_in_this_group": "Роль в группе",
            "who_and_relation": "Кто и в каких отношениях",
            "direction": "Направление",
            "team_member_of": "Кому подчиняется",
            "manages": "Кем руководит",
            "activity_level": "Активность",
            "communication_style": "Стиль общения",
            "relation_to_sergey": "Отношение к Сергею",
            "risk": "Риск",
            "key_decisions": "Ключевые решения",
            "format_review": "Verdict",
            "open_tasks": "Открытые задачи",
            "next_actions": "Что написать / сделать",
            "tldr": "TL;DR",
            "confidence": "Confidence",
        }
        skip_in_body = {"name", "username", "user_id", "tldr"}
        for p in pcards:
            if not isinstance(p, dict):
                continue
            uname = p.get("username", "")
            uname_str = f" @{uname.lstrip('@')}" if uname else ""
            uid_str = f" (id={p['user_id']})" if p.get("user_id") and p["user_id"] != "?" else ""
            lines.append(f"### {p.get('name', '?')}{uname_str}{uid_str}")
            if p.get("tldr"):
                lines.append(f"_{p['tldr']}_\n")
            for key, val in p.items():
                if key in skip_in_body or val is None or val == "":
                    continue
                label = labels.get(key, key)
                if isinstance(val, list):
                    if not val:
                        continue
                    lines.append(f"- **{label}:**")
                    for item in val:
                        if isinstance(item, dict):
                            items = "; ".join(f"{kk}={vv}" for kk, vv in item.items() if vv)
                            lines.append(f"  - {items}")
                        else:
                            lines.append(f"  - {item}")
                elif isinstance(val, dict):
                    items = "; ".join(f"{kk}={vv}" for kk, vv in val.items() if vv)
                    lines.append(f"- **{label}:** {items}")
                else:
                    lines.append(f"- **{label}:** {val}")
            lines.append("")

    if "confidence_overall" in result:
        lines.append(f"---\n_Confidence: {result['confidence_overall']}_")

    return "\n".join(lines)


# ─── Main ────────────────────────────────────────────────

async def run_all(args):
    """Batch — прогон deep_dive по всем work-чатам без ai_deep_dive_at.
    Resumable: каждый успешный прогон пишет в БД, следующий запуск пропустит уже сделанные.
    Под капотом — subprocess того же скрипта с --chat-id (изолирует ошибки)."""
    conn = connect()
    rows = conn.execute(f"""
        SELECT d.chat_id, COALESCE(d.title, '?') AS title, d.chat_type, d.importance
        FROM dialogs d
        WHERE d.category = 'work'
          AND d.ai_deep_dive_at IS NULL
          AND d.last_message_at >= ?
        ORDER BY
          CASE d.importance WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 3
                            ELSE 4 END,
          CASE d.chat_type WHEN 'private' THEN 1 ELSE 2 END,
          d.last_message_at DESC
    """, (args.all_active_since,)).fetchall()
    conn.close()

    if not rows:
        print("Очередь пуста — все work-чаты уже обработаны.")
        return

    # SAFETY DEFAULTS for batch
    batch_months = args.months if args.months != 12 else 3
    batch_top_participants = args.top_participants if args.top_participants != 15 else 8
    batch_max_msgs_private = args.max_msgs if args.max_msgs > 0 else 10000
    batch_max_msgs_group = batch_max_msgs_private * 5  # group: x5 для покрытия топиков

    print(f"=== BATCH DEEP_DIVE ({len(rows)} чатов в очереди) ===")
    print(f"Safety-defaults: --months {batch_months}, "
          f"--top-participants {batch_top_participants}, "
          f"--max-msgs private={batch_max_msgs_private}, group={batch_max_msgs_group}",
          flush=True)
    print(f"Render context: 1000 messages per topic.", flush=True)
    print(f"Override: --months N --top-participants K --max-msgs M\n",
          flush=True)

    started_at = time.time()
    ok = 0
    fail = 0

    for i, r in enumerate(rows, 1):
        cid = r["chat_id"]
        title = r["title"]
        elapsed = time.time() - started_at
        eta = (elapsed / i * (len(rows) - i)) if i > 0 else 0
        print(f"\n══════ [{i}/{len(rows)}] {title} (id={cid}, "
              f"importance={r['importance'] or '-'}, type={r['chat_type']}) "
              f"════ elapsed {elapsed/60:.0f}m, ETA {eta/60:.0f}m ══════",
              flush=True)

        # Per-chat-type max_msgs (group получает x5)
        is_group = r["chat_type"] != "private"
        cap = batch_max_msgs_group if is_group else batch_max_msgs_private

        cmd = [sys.executable, "-u", __file__,
               "--chat-id", str(cid),
               "--months", str(batch_months),
               "--top-participants", str(batch_top_participants),
               "--msgs-per-private", str(args.msgs_per_private),
               "--max-msgs", str(cap)]
        if args.full: cmd.append("--full")
        if args.skip_transcribe: cmd.append("--skip-transcribe")
        if args.no_claude: cmd.append("--no-claude")

        try:
            rc = subprocess.run(cmd).returncode
            if rc == 0:
                ok += 1
            else:
                fail += 1
                print(f"  ⚠️ subprocess returncode={rc}", flush=True)
        except KeyboardInterrupt:
            print("\n⏸  Прервано пользователем. Прогресс сохранён, "
                  "запусти --all снова чтобы продолжить.", flush=True)
            break
        except Exception as e:
            fail += 1
            print(f"  ⚠️ ошибка: {e}", flush=True)

    print(f"\n=== ИТОГО ===")
    print(f"OK: {ok}, fail: {fail}, время: {(time.time()-started_at)/60:.0f} мин")


async def main_async():
    p = argparse.ArgumentParser()
    p.add_argument("--user", help="@username или просто username")
    p.add_argument("--chat-id", type=int)
    p.add_argument("--since", help="ISO date — точная нижняя граница периода")
    p.add_argument("--months", type=int, default=12,
                   help="История за N последних месяцев (default 12). 0 = unlimited.")
    p.add_argument("--full", action="store_true",
                   help="Override: забрать всю историю (даже если переписке 5 лет)")
    p.add_argument("--skip-fetch", action="store_true",
                   help="не дёргать Telegram, использовать только то что уже в БД")
    p.add_argument("--skip-transcribe", action="store_true",
                   help="не транскрибировать новые голосовые")
    p.add_argument("--model", default=CLAUDE_MODEL)
    p.add_argument("--no-claude", action="store_true",
                   help="только подготовить контекст и сохранить, без Опуса (для дебага)")
    p.add_argument("--top-participants", type=int, default=15,
                   help="Сколько активных участников брать с их личкой (default 15)")
    p.add_argument("--msgs-per-private", type=int, default=150,
                   help="Сколько последних сообщений из лички каждого (default 150)")
    p.add_argument("--all", action="store_true",
                   help="Прогнать deep_dive по всем work-чатам без ai_deep_dive_at "
                        "(идёт от high importance к low; private перед group; resumable). "
                        "В --all режиме применяются safety-defaults: months=3, "
                        "top-participants=8, max_msgs=10000, transcribe только за последний месяц")
    p.add_argument("--all-active-since", default="2026-01-01",
                   help="Для --all: брать только чаты с last_message_at >= этой даты")
    p.add_argument("--max-msgs", type=int, default=0,
                   help="Hard cap на messages в main chat (0=нет). В --all режиме = 10000")
    p.add_argument("--max-voices", type=int, default=1000,
                   help="Cap на голосовые для транскрипции (default 1000). "
                        "Для расширенных чатов (common groups / participants) "
                        "берётся max-voices/5.")
    args = p.parse_args()

    # Batch режим — прогон по всем work-чатам
    if args.all:
        await run_all(args)
        return

    if not args.user and not args.chat_id:
        print("Укажи --user @username ИЛИ --chat-id ID (либо --all)"); sys.exit(1)

    conn = connect()

    # Step 1: догружаем историю
    chat_id = args.chat_id
    target_label = f"@{args.user.lstrip('@')}" if args.user else f"chat_id={chat_id}"
    print(f"=== DEEP DIVE: {target_label} ===", flush=True)

    # Adaptive period: --full > --since > --months (default 12)
    if args.full:
        since = None
    elif args.since:
        since = datetime.fromisoformat(args.since)
    elif args.months > 0:
        since = datetime.now() - timedelta(days=args.months * 30)
    else:
        since = None

    common_chats_text = ""
    participants_text = ""
    participant_user_ids: list[int] = []
    common_chats_for_transcribe: list[int] = []

    if not args.skip_fetch:
        print("Подключаюсь к Telegram…", flush=True)
        from tg_proxy import get_tg_proxy
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH, proxy=get_tg_proxy())
        await client.start(phone=PHONE)
        try:
            entity = await client.get_entity(args.user.lstrip("@") if args.user else args.chat_id)
        except Exception as e:
            print(f"Не нашёл entity: {e}"); sys.exit(1)

        # Для private — chat_id = entity.id; для group/channel — Telethon хранит как negative
        if hasattr(entity, "first_name"):  # User
            chat_id = entity.id
        elif getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
            chat_id = int(f"-100{entity.id}")
        else:
            chat_id = -entity.id  # старая группа
        is_group = chat_id < 0

        upsert_user(conn, entity)

        if args.full:
            print("  Лимит: --full — забираю всю историю", flush=True)
        elif args.since:
            print(f"  Лимит: с {args.since}", flush=True)
        elif args.months > 0:
            print(f"  Лимит: последние {args.months} месяцев "
                  f"(с {since.strftime('%Y-%m-%d')})", flush=True)
        else:
            print("  Лимит: unlimited (--months 0)", flush=True)

        # 1. Догрузка main-чата (incremental по умолчанию)
        await fetch_full_history(client, conn, entity, since=since,
                                  max_msgs=args.max_msgs)

        # 2. Расширение скоупа: common groups (private) или participants (group)
        if is_group:
            participants, no_private = fetch_top_participants_with_private(
                conn, chat_id, since,
                top_n=args.top_participants,
                msgs_per_private=args.msgs_per_private,
            )
            if participants or no_private:
                participant_user_ids = [p["user_id"] for p in participants]
                print(f"  Топ-{len(participants)} участников с личкой + "
                      f"{len(no_private)} без лички добавлены в контекст",
                      flush=True)
                participants_text = build_participants_private_excerpt(
                    participants, no_private
                )
        else:
            print("  Ищу общие группы…", flush=True)
            try:
                common = await fetch_common_chats(client, conn, entity, since)
                if common:
                    print(f"  Общих групп: {len(common)} "
                          f"(top-3: {[c['title'] for c in common[:3]]})",
                          flush=True)
                    common_chats_text = build_common_chat_excerpt(
                        conn, common[:10], chat_id, since,
                        max_msgs_per_topic=1000,
                    )
                    common_chats_for_transcribe = [c["chat_id"] for c in common[:10]]

                    # Для private deep_dive: собираем top активных юзеров из общих групп —
                    # их user_notes пойдут как авторитетные факты (кто Леонид/Богданов и т.д.).
                    common_users = set()
                    for c in common[:5]:
                        for r in conn.execute("""
                            SELECT DISTINCT m.from_user_id
                            FROM messages m
                            WHERE m.chat_id = ? AND m.is_outgoing = 0
                              AND m.from_user_id IS NOT NULL
                            ORDER BY m.date DESC LIMIT 30
                        """, (c["chat_id"],)):
                            common_users.add(r[0])
                    if common_users:
                        participant_user_ids = list(common_users)
                        print(f"  📎 user_id'ов для авторитетных фактов: {len(participant_user_ids)}",
                              flush=True)
            except Exception as e:
                print(f"  common-chats expand err: {e}", flush=True)

        # 3. Транскрипция: main + расширенный скоуп.
        # Лимиты: main 1000 voices, расширенные чаты 200 (узкий срез).
        if not args.skip_transcribe:
            await transcribe_missing(client, conn, chat_id, since=since,
                                      max_voices=args.max_voices)
            cap_extra = max(args.max_voices // 5, 200)  # для extra чатов — меньше
            if is_group and participant_user_ids:
                for uid in participant_user_ids:
                    await transcribe_missing(client, conn, uid, since=since,
                                              max_voices=cap_extra)
            elif not is_group and common_chats_for_transcribe:
                for cid in common_chats_for_transcribe:
                    await transcribe_missing(client, conn, cid, since=since,
                                              max_voices=cap_extra)

        await client.disconnect()
    else:
        # skip-fetch path: только из БД, без Telegram
        if not chat_id and args.user:
            row = conn.execute(
                "SELECT chat_id FROM dialogs WHERE username=? OR LOWER(username)=LOWER(?)",
                (args.user.lstrip("@"), args.user.lstrip("@"))
            ).fetchone()
            if not row:
                print(f"Не нашёл chat_id для {args.user} в dialogs (используй --chat-id)")
                sys.exit(1)
            chat_id = row[0]
        is_group = chat_id < 0

        # DB-side participants enrichment (для group в skip-fetch режиме)
        if is_group:
            participants, no_private = fetch_top_participants_with_private(
                conn, chat_id, since,
                top_n=args.top_participants,
                msgs_per_private=args.msgs_per_private,
            )
            if participants or no_private:
                participant_user_ids = [p["user_id"] for p in participants]
                print(f"  Топ-{len(participants)} участников с личкой + "
                      f"{len(no_private)} без лички (из БД)", flush=True)
                participants_text = build_participants_private_excerpt(
                    participants, no_private
                )

    since_for_db = since  # для gather_chat_data — тот же фильтр
    dlg, rows, topics_map = gather_chat_data(conn, chat_id, since=since_for_db)

    if not rows:
        print(f"Нет сообщений для chat_id={chat_id}"); sys.exit(1)

    # Авторитетные факты от Сергея (user_notes других чатов + answered disputed)
    # • group → IDs топ-N участников
    # • private → ID самого target + user_id'ы людей из общих групп (для понимания
    #   кто такой Леонид, Богданов и т.д. когда target про них пишет)
    if is_group:
        ids_for_facts = participant_user_ids
    else:
        ids_for_facts = list({chat_id} | set(participant_user_ids))
    authoritative_text = fetch_authoritative_facts(conn, chat_id, ids_for_facts)
    if authoritative_text:
        bullets = authoritative_text.count('•')
        own_note = "СОБСТВЕННОЕ ОПИСАНИЕ" in authoritative_text
        ans_block = "ПОДТВЕРЖДЁННЫЕ ОТВЕТЫ" in authoritative_text
        print(f"  📌 Авторитетные факты: {bullets} bullets"
              + (" + own_notes" if own_note else "")
              + (" + answered_q's" if ans_block else ""),
              flush=True)

    prompt, stats = build_context(dlg, rows, topics_map,
                                   common_chats_text=common_chats_text,
                                   participants_text=participants_text,
                                   authoritative_text=authoritative_text)
    print(f"Контекст: {len(prompt):,} символов "
          f"(~{len(prompt)//4:,} токенов)", flush=True)

    # Step 3: куда сохраняем
    folder_name = f"@{dlg['username']}" if dlg and dlg["username"] else f"chat_{chat_id}"
    out_dir = OUT_ROOT / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prompt_path = out_dir / f"deep_dive_prompt_{ts}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    print(f"Промпт сохранён: {prompt_path}", flush=True)

    if args.no_claude:
        print("--no-claude — стоп тут.")
        return

    # Step 4: Опус (с validate + retry если структура неполная)
    # Имена ключей соответствуют именам из DEEP_DIVE_PROMPT (без префикса q1_*)
    REQUIRED_KEYS = [
        "who_and_relation", "direction", "org_chain", "top_topics",
        "last_two_weeks", "open_tasks", "debts_and_obligations",
        "finance", "next_actions", "format_review",
        "tldr", "user_notes_ready", "timeline", "disputed", "confidence_overall",
    ]

    def validate_full(j: dict) -> list:
        return [k for k in REQUIRED_KEYS if k not in j or j[k] is None]

    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None
    print(f"Запускаю claude --model {args.model}"
          f"{' (с custom token)' if oauth_token else ''}…", flush=True)

    result = None
    for attempt in range(2):  # 1 retry если структура неполная
        raw, dur = call_claude(prompt, model=args.model, oauth_token=oauth_token)
        print(f"Готово за {dur:.0f}с (попытка {attempt+1})", flush=True)
        raw_path = out_dir / f"deep_dive_raw_{ts}_a{attempt+1}.txt"
        raw_path.write_text(raw, encoding="utf-8")
        try:
            candidate = parse_json(raw)
        except Exception as e:
            print(f"⚠ JSON parse err: {e}\nRaw start: {raw[:300]}")
            if attempt == 0:
                prompt = prompt + "\n\nВАЖНО: предыдущий ответ не распарсился как JSON. Верни СТРОГО валидный JSON без markdown-обёртки, без преамбулы. Только {…}."
                continue
            return
        missing = validate_full(candidate)
        if not missing:
            result = candidate
            break
        # Структура неполная — retry с явным напоминанием
        print(f"⚠ JSON неполный, отсутствуют ключи: {missing}", flush=True)
        if attempt == 0:
            prompt = prompt + (
                f"\n\n⚠️ ВАЖНО — ВАЛИДАЦИЯ ПРОВАЛИЛАСЬ:\n"
                f"Предыдущий ответ НЕ содержал обязательные ключи: {missing}\n"
                f"Верни ПОЛНЫЙ JSON со ВСЕМИ ключами:\n"
                f"{', '.join(REQUIRED_KEYS)}\n"
                f"q1-q10 — это структурированные секции, обязательные поля. "
                f"Не пропускай их даже если кажутся очевидными."
            )
        else:
            print(f"⚠ Не удалось получить полный JSON после retry. Сохраняю частичный.")
            result = candidate  # сохраняем что есть
            break

    if result is None:
        print("❌ Не получили валидный результат")
        return

    json_path = out_dir / f"deep_dive_{ts}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    md_path = out_dir / f"deep_dive_{ts}.md"
    md_path.write_text(render_card(result, dlg, stats), encoding="utf-8")

    # В БД — deep_dive результат
    conn.execute("""
        UPDATE dialogs SET ai_deep_dive_json=?, ai_deep_dive_at=? WHERE chat_id=?
    """, (json.dumps(result, ensure_ascii=False), now_iso(), chat_id))

    # Сохраняем disputed как отдельные вопросы (для накопления знания)
    disputed = result.get("disputed", []) or []
    for d in disputed:
        if not isinstance(d, dict): continue
        point = d.get("point", "").strip()
        if not point: continue
        # Если такой вопрос уже задан и не отвечен — не дублируем
        existing = conn.execute("""
            SELECT id, answered FROM disputed_questions
            WHERE chat_id=? AND point=?
        """, (chat_id, point)).fetchone()
        if existing:
            continue
        conn.execute("""
            INSERT INTO disputed_questions (chat_id, point, reason, raised_at, raised_by_model)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, point, d.get("reason", ""), now_iso(),
              f"opus-{args.model}"))

    conn.commit()

    print(f"\n=== ГОТОВО ===")
    print(f"Карточка: {md_path}")
    print(f"JSON: {json_path}")
    print(f"Confidence: {result.get('confidence_overall', '?')}")
    if result.get("disputed"):
        print(f"🚩 Спорных моментов: {len(result['disputed'])}")


if __name__ == "__main__":
    asyncio.run(main_async())
