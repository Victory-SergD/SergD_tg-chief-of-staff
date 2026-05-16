#!/usr/bin/env python3
"""
Отправка critical-вопросов подчинённым через Telethon (от лица Сергея).

Логика:
  1. Берёт все critical с answer_owner=subordinate:USERNAME
  2. Группирует по subordinate (один человек = одно сообщение)
  3. Через Opus 1M формирует дружелюбный текст сообщения с вопросами
     (тон: Сергей пишет лично, упомянуть AI-помощника, попросить ответить как считает)
  4. По умолчанию --dry-run — печатает все сообщения для проверки
  5. С --send — реально отправляет через Telethon
  6. Записывает в outbound_questions для последующего сбора ответов

Запуск:
  python3 ask_subordinates.py                  # dry-run, печатает что отправит
  python3 ask_subordinates.py --send           # реально отправляет
  python3 ask_subordinates.py --only @username # только одному (для теста)
  python3 ask_subordinates.py --regenerate     # перегенерить тексты (если уже есть)
"""
import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, UserPrivacyRestrictedError

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
SESSION = BASE / "tg_access" / "SergD_telethon"
TOKENS_FILE = BASE / ".tokens.env"
CLAUDE_MODEL = "claude-opus-4-7[1m]"
CLAUDE_TIMEOUT = 600

# Загрузка Telethon-credentials
import dotenv
dotenv.load_dotenv(BASE / ".env")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

# OAuth токены для Opus
TOKENS = []
for line in TOKENS_FILE.read_text().splitlines():
    line = line.strip()
    if line.startswith("CLAUDE_OAUTH_TOKEN_") and "=" in line:
        _, val = line.split("=", 1)
        if val.strip():
            TOKENS.append(val.strip())


PROMPT_COMPOSE = """Ты — AI-помощник руководителя направлений Сергея Дышканта (Victory Agency).
Твоя задача: составить ОДНО сообщение для подчинённого Сергея в Telegram, в котором собраны все вопросы по работе.

ТОН:
- Пишет САМ Сергей (от первого лица), но честно сообщает что использует AI-помощника
- Дружелюбный, рабочий, без лишней формальности
- На «ты» (это команда, общение неформальное)
- Кратко: одно вступительное предложение, потом вопросы списком, потом одно завершающее
- НЕ официальное письмо. Скорее как Сергей бы сам спросил голосом

СТРУКТУРА:
1. Привет + краткое обоснование (1-2 предложения):
   "Привет! Подключаю своего AI-помощника, который помогает мне держать в голове всю команду.
    Помоги пожалуйста ответить — собрал тут несколько рабочих вопросов чтоб я не дёргал тебя 10 раз."
   [можешь варьировать формулировку под ситуацию]

2. Список вопросов:
   - Каждый с пометкой ID
   - Чёткая формулировка но не сухо-официально
   - Если вопросов 1 — без пунктов, просто естественно
   - Если 2+ — нумерованный список или маркированный

3. Завершение (1 предложение):
   "Если какой-то вопрос кажется не совсем корректным — ответь как считаешь сам / как есть.
    Главное — твоя версия. Спасибо!"

ВАЖНО:
- НЕ добавляй смайлы кроме умеренных (не больше 1-2 во всём сообщении)
- НЕ используй markdown ** или __
- НЕ повторяй слово в слово точную формулировку из disputed.point — переформулируй
  в естественную живую речь Сергея, сохраняя суть
- Если вопрос про ЗП/деньги — формулируй деликатно («давай зафиксирую твою текущую ставку»)
- Если вопрос про статус задачи — конкретно («где сейчас по vi-heat-map?»)
- ID каждого вопроса должен быть в скобках сразу перед или после вопроса для трекинга:
  «1. (#481) Слушай, какая у тебя сейчас ставка по неделе...»

═══ КОНТЕКСТ ПОДЧИНЁННОГО ═══
{context_block}

═══ ВОПРОСЫ ДЛЯ ВКЛЮЧЕНИЯ В СООБЩЕНИЕ ═══
{questions_block}

═══ ФОРМАТ ОТВЕТА ═══
Просто текст готового сообщения. Никаких ```, никакого JSON, никаких пояснений.
Только сам текст что Сергей пошлёт.
"""


def call_claude(prompt: str, oauth_token: str | None = None) -> str:
    env = os.environ.copy()
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "text"]
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude exit={result.returncode}: {result.stderr[:500]}")
    return result.stdout.strip()


def get_subordinate_critical(conn, only_username: str | None = None):
    """Возвращает {recipient_chat_id: {meta, questions: [...]}}.

    КЛЮЧЕВАЯ ЛОГИКА: группируем НЕ по chat_id вопроса (контакт-о-ком-вопрос),
    а по chat_id РЕЦИПИЕНТА — того подчинённого кто должен ответить.
    Реципиент берётся из answer_owner='subordinate:USERNAME' через lookup в dialogs.

    БЕЗОПАСНОСТЬ:
    - Только если username реципиента существует в dialogs
    - Только если у реципиента chat_id > 0 (личка, не группа)
    - Только если у реципиента relation in ('subordinate', 'mixed', '') —
      ТОЧНО НЕ ('client', 'partner', 'boss')
    """
    rows = conn.execute("""
        SELECT q.id, q.chat_id AS subject_chat_id, q.point, q.reason,
               q.criticality_priority, q.deadline, q.impact_estimate,
               q.criticality_reason, q.answer_owner,
               d_subj.title AS subject_title,
               d_subj.username AS subject_username
        FROM disputed_questions q
        LEFT JOIN dialogs d_subj ON d_subj.chat_id = q.chat_id
        WHERE q.answered = 0
          AND q.criticality = 'critical'
          AND q.duplicate_of_id IS NULL
          AND q.answer_owner LIKE 'subordinate:%'
        ORDER BY
          CASE q.criticality_priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
          q.id
    """).fetchall()

    by_recipient = {}
    skipped = []
    for r in rows:
        owner = r["answer_owner"] or ""
        # subordinate:USERNAME → USERNAME
        recipient_username = owner.split(":", 1)[1].strip() if ":" in owner else ""
        if not recipient_username:
            skipped.append((r["id"], "empty username"))
            continue

        # Lookup chat_id по username (case-insensitive). Если не найден — пробуем alias.
        recipient_row = conn.execute(
            "SELECT chat_id, COALESCE(title,''), COALESCE(relation,''), COALESCE(user_notes,'')"
            " FROM dialogs WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (recipient_username,)
        ).fetchone()

        if not recipient_row:
            # Проверяем username_aliases
            alias_row = conn.execute(
                "SELECT real_username FROM username_aliases WHERE LOWER(alias) = LOWER(?)",
                (recipient_username,)
            ).fetchone()
            if alias_row:
                real_username = alias_row[0]
                recipient_row = conn.execute(
                    "SELECT chat_id, COALESCE(title,''), COALESCE(relation,''), COALESCE(user_notes,'')"
                    " FROM dialogs WHERE LOWER(username) = LOWER(?) LIMIT 1",
                    (real_username,)
                ).fetchone()
                if recipient_row:
                    recipient_username = real_username  # обновляем для дальнейшей логики

        if not recipient_row:
            skipped.append((r["id"], f"username @{recipient_username} not in dialogs (no alias)"))
            continue

        recipient_chat_id = recipient_row["chat_id"]
        if recipient_chat_id < 0:
            skipped.append((r["id"], f"@{recipient_username} is group, not personal chat"))
            continue

        recipient_relation = (recipient_row[2] or "").lower()
        if recipient_relation in ("client", "partner", "boss"):
            skipped.append((r["id"], f"@{recipient_username} relation={recipient_relation}, refuse to ask"))
            continue

        # Фильтр --only по username РЕЦИПИЕНТА (не subject'а)
        if only_username:
            u = only_username.lstrip("@").lower()
            if recipient_username.lower() != u:
                continue

        if recipient_chat_id not in by_recipient:
            by_recipient[recipient_chat_id] = {
                "chat_id": recipient_chat_id,
                "title": recipient_row[1] or "?",
                "username": recipient_username,
                "user_notes": recipient_row[3] or "",
                "questions": [],
            }
        by_recipient[recipient_chat_id]["questions"].append({
            "id": r["id"],
            "point": r["point"],
            "reason": r["reason"] or "",
            "priority": r["criticality_priority"],
            "deadline": r["deadline"],
            "impact": r["impact_estimate"],
            "rationale": r["criticality_reason"],
            "subject_title": r["subject_title"] or "",  # о ком вопрос — для контекста в Опус
            "subject_username": r["subject_username"] or "",
        })

    if skipped:
        print(f"⚠ Пропущено {len(skipped)} вопросов:")
        for qid, reason in skipped[:10]:
            print(f"  q#{qid}: {reason}")

    return by_recipient


def already_sent_ids(conn, chat_id: int) -> set:
    """Какие question_ids уже были отправлены этому chat_id (status != closed)."""
    rows = conn.execute(
        "SELECT question_ids FROM outbound_questions WHERE chat_id=? AND status IN ('waiting', 'reminded')",
        (chat_id,)
    ).fetchall()
    sent = set()
    for r in rows:
        try:
            sent.update(json.loads(r["question_ids"]))
        except Exception:
            pass
    return sent


def compose_message(person: dict, oauth_token: str) -> str:
    """Через Opus формирует дружелюбный текст сообщения.
    person['questions'] могут быть про ДРУГИХ людей — указываем subject_title/username
    чтоб Опус вставил контекст «по проекту X / по Y» в формулировку.
    """
    user_notes = (person["user_notes"] or "")[:1500]
    ctx = (
        f"Получатель: {person['title']} (@{person['username']})\n"
        f"USER_NOTES (что Сергей знает о получателе):\n{user_notes}"
    )
    qs = []
    for q in person["questions"]:
        subj_label = q.get("subject_title", "")
        subj_user = q.get("subject_username", "")
        if subj_user and subj_user.lower() != person["username"].lower():
            subj_marker = f"[ВОПРОС ПРО: {subj_label} @{subj_user}]"
        elif subj_label and subj_label != person["title"]:
            subj_marker = f"[ВОПРОС ПРО: {subj_label}]"
        else:
            subj_marker = ""
        line = f"#{q['id']} [{q['priority']}] {subj_marker} {q['point']}".strip()
        if q.get("reason"):
            line += f"\n   контекст: {q['reason'][:200]}"
        if q.get("deadline"):
            line += f"\n   дедлайн: {q['deadline']}"
        qs.append(line)
    prompt = PROMPT_COMPOSE.format(
        context_block=ctx,
        questions_block="\n\n".join(qs),
    )
    return call_claude(prompt, oauth_token=oauth_token)


def record_outbound(conn, chat_id: int, question_ids: list[int],
                    channel: str, message_id: int | None):
    conn.execute("""
        INSERT INTO outbound_questions
        (chat_id, question_ids, sent_at, channel, message_id, status)
        VALUES (?, ?, ?, ?, ?, 'waiting')
    """, (chat_id, json.dumps(question_ids), datetime.now().isoformat(),
          channel, message_id))
    conn.commit()


async def send_one(client: TelegramClient, chat_id: int, text: str) -> int | None:
    """Отправляет сообщение через Telethon, возвращает message_id."""
    try:
        msg = await client.send_message(chat_id, text)
        return msg.id
    except FloodWaitError as e:
        print(f"  ⚠ FloodWait {e.seconds}s — ждём…")
        await asyncio.sleep(e.seconds)
        msg = await client.send_message(chat_id, text)
        return msg.id
    except UserPrivacyRestrictedError:
        print(f"  ✗ user privacy restricted: {chat_id}")
        return None
    except Exception as e:
        print(f"  ✗ send error chat_id={chat_id}: {e}")
        return None


async def main_async(args):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    by_chat = get_subordinate_critical(conn, only_username=args.only)
    if not by_chat:
        print("Нет critical-вопросов с answer_owner=subordinate:*. Пусто.")
        return

    # Фильтр: убираем уже отправленные question_ids
    for cid, person in list(by_chat.items()):
        sent = already_sent_ids(conn, cid)
        person["questions"] = [q for q in person["questions"] if q["id"] not in sent]
        if not person["questions"]:
            del by_chat[cid]

    if not by_chat:
        print("Все critical-вопросы для подчинённых уже отправлены ранее. Используй --regenerate.")
        return

    print(f"Подчинённых для отправки: {len(by_chat)}")
    total_q = sum(len(p["questions"]) for p in by_chat.values())
    print(f"Всего вопросов: {total_q}")
    print()

    # Composer step
    composed = []
    for i, (cid, person) in enumerate(by_chat.items()):
        tok = TOKENS[i % len(TOKENS)] if TOKENS else None
        print(f"[{i+1}/{len(by_chat)}] Композирую сообщение для @{person['username']} ({len(person['questions'])} вопрос)…")
        text = compose_message(person, oauth_token=tok)
        composed.append((cid, person, text))
        print()

    # Print preview
    print("\n" + "═" * 70)
    print("ПРЕВЬЮ СООБЩЕНИЙ")
    print("═" * 70)
    for cid, person, text in composed:
        print(f"\n→ @{person['username']} (id={cid}, title={person['title'][:40]})")
        print(f"  Вопросы: {[q['id'] for q in person['questions']]}")
        print("─" * 50)
        print(text)
        print("─" * 50)
    print("\n" + "═" * 70)
    print(f"ИТОГО: {len(composed)} сообщений готовы к отправке")
    print("═" * 70)

    if not args.send:
        print("\n[DRY-RUN] Чтобы реально отправить — перезапусти с --send")
        return

    # Real send
    print("\n📤 Отправляю через Telethon (твой аккаунт)…")
    from tg_proxy import get_tg_proxy
    client = TelegramClient(str(SESSION), API_ID, API_HASH, proxy=get_tg_proxy())
    await client.start()

    sent_count = 0
    for cid, person, text in composed:
        print(f"\n→ @{person['username']} (id={cid})")
        msg_id = await send_one(client, cid, text)
        if msg_id:
            qids = [q["id"] for q in person["questions"]]
            record_outbound(conn, cid, qids, "telethon", msg_id)
            print(f"  ✓ отправлено, msg_id={msg_id}")
            sent_count += 1
            await asyncio.sleep(2)  # антиспам пауза

    await client.disconnect()
    conn.close()
    print(f"\n✅ Отправлено: {sent_count}/{len(composed)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true",
                   help="РЕАЛЬНО отправить (default — dry-run)")
    p.add_argument("--only", type=str, default=None,
                   help="@username — отправить только одному (для теста)")
    p.add_argument("--regenerate", action="store_true",
                   help="перегенерить даже если уже есть outbound_questions (для повторов)")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
