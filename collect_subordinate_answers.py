#!/usr/bin/env python3
"""
Сбор ответов от подчинённых на отправленные через ask_subordinates.py вопросы.

Логика:
  1. Берёт outbound_questions со status='waiting' или 'reminded'
  2. Для каждого: читает messages от этого chat_id (incoming) после sent_at
  3. Через Opus 1M парсит: какой ответ на какой вопрос
  4. Записывает в disputed_questions.answer + answered=1 для совпавших
  5. Если ответил на ВСЕ — outbound.status='answered'
  6. Если ответил частично — оставляет 'waiting' (доспросим в reminder)

Запуск:
  python3 collect_subordinate_answers.py             # обработать всё waiting
  python3 collect_subordinate_answers.py --dry-run   # только показать что нашёл
  python3 collect_subordinate_answers.py --chat-id N # только один контакт
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
TOKENS_FILE = BASE / ".tokens.env"
CLAUDE_MODEL = "claude-opus-4-7[1m]"
CLAUDE_TIMEOUT = 600

TOKENS = []
for line in TOKENS_FILE.read_text().splitlines():
    line = line.strip()
    if line.startswith("CLAUDE_OAUTH_TOKEN_") and "=" in line:
        _, val = line.split("=", 1)
        if val.strip():
            TOKENS.append(val.strip())


PROMPT_PARSE = """Ты парсишь ответ подчинённого на вопросы которые ему задал руководитель Сергей.

Сергей ранее отправил человеку ОДНО сообщение со списком пронумерованных вопросов с ID (#NNN).
Подчинённый прислал ответ — может быть одним сообщением, может несколькими, может голосовым.
Может ответить на все, на часть, или вообще не ответить.

Твоя задача: для каждого ID вопроса понять — есть ли ответ в его реплике, и если да, извлечь его.

═══ ВОПРОСЫ КОТОРЫЕ ЗАДАЛ СЕРГЕЙ ═══
{questions_block}

═══ ОТВЕТ ПОДЧИНЁННОГО (может быть несколько сообщений) ═══
{reply_block}

═══ ПРАВИЛА ПАРСИНГА ═══
- Если он ОТВЕТИЛ на вопрос #NNN — извлеки его ответ. Чисти от лишнего, оставь смысл.
- Если он ПРОИГНОРИРОВАЛ вопрос — пометь "skipped".
- Если он сказал «не знаю / не помню / спроси у X» — это тоже ответ ("не знаю" сам по себе — валидный ответ).
- Если ответ относится к нескольким ID одновременно — пиши его в каждом.
- НЕ выдумывай ответы. Только то что реально написано/сказано.

═══ ФОРМАТ ОТВЕТА ═══
Только валидный JSON массив. Никаких ```. По одному объекту на каждый ID:
[
  {{"id": NNN, "status": "answered" | "skipped" | "deferred", "answer": "<текст ответа или null>"}},
  ...
]

status:
  • "answered" — есть конкретный ответ (включая «не знаю», «не помню»)
  • "skipped" — в реплике этого вопроса нет вообще
  • "deferred" — обещал ответить позже («посмотрю и напишу»)
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


def get_pending_outbounds(conn, only_chat_id=None):
    where = ["status IN ('waiting', 'reminded')"]
    if only_chat_id:
        where.append(f"chat_id = {only_chat_id}")
    rows = conn.execute(f"""
        SELECT id, chat_id, question_ids, sent_at, message_id, last_collect_at
        FROM outbound_questions
        WHERE {' AND '.join(where)}
        ORDER BY sent_at
    """).fetchall()
    return rows


def effective_since(ob) -> str:
    """Берём максимум из sent_at и last_collect_at — для идемпотентности."""
    sent_at = ob["sent_at"]
    last_collect_at = ob["last_collect_at"]
    if last_collect_at and last_collect_at > sent_at:
        return last_collect_at
    return sent_at


def fetch_questions_text(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, point, reason FROM disputed_questions WHERE id IN ({placeholders})",
        ids
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_replies_after(conn, chat_id: int, since: str) -> list[dict]:
    """Все incoming сообщения подчинённого после since + транскрипции голосовых.
    since = MAX(sent_at, last_collect_at) — для идемпотентности.
    """
    rows = conn.execute("""
        SELECT m.msg_id, m.date, m.text, m.is_outgoing, t.transcription
        FROM messages m
        LEFT JOIN transcriptions t ON t.msg_id = m.msg_id AND t.chat_id = m.chat_id
        WHERE m.chat_id = ? AND m.date > ? AND m.is_outgoing = 0
        ORDER BY m.date
    """, (chat_id, since)).fetchall()
    out = []
    for r in rows:
        text = r["text"] or r["transcription"] or ""
        if text.strip():
            out.append({"msg_id": r["msg_id"], "date": r["date"], "text": text})
    return out


PROMPT_MERGE_NOTES = """Ты обновляешь user_notes контакта Сергея — авторитетный слой фактов.
Подчинённый только что ответил на вопросы. Нужно вписать его ответы естественной фразой
в user_notes, чтобы на следующих deep_dive факт был в АВТОРИТЕТНОМ слое (не задавался снова).

═══ ТЕКУЩИЕ USER_NOTES контакта ═══
{current_notes}

═══ НОВЫЕ ОТВЕТЫ ОТ ПОДЧИНЁННОГО ═══
{answers_block}

═══ ЗАДАЧА ═══
Верни ОДНУ короткую приписку которую нужно ДОБАВИТЬ в конец user_notes.
Формат: «[YYYY-MM-DD от {who}] факт1. факт2. факт3.»
Только новые факты, без повтора того что уже в user_notes.
Сжато (1-3 предложения максимум).

═══ ФОРМАТ ОТВЕТА ═══
Только сам текст приписки. Никаких ```, преамбул, JSON.
Если нет новых фактов (всё уже в user_notes) — верни пустую строку.
"""


def merge_answers_to_user_notes(conn, chat_id: int, parsed: list[dict],
                                  oauth_token: str | None = None,
                                  dry_run: bool = False) -> str | None:
    """Через Opus добавляет ответы как авторитетный факт в user_notes."""
    answers = [(p["id"], p["answer"]) for p in parsed
               if isinstance(p, dict) and p.get("status") == "answered" and p.get("answer")]
    if not answers:
        return None

    row = conn.execute(
        "SELECT COALESCE(user_notes, ''), COALESCE(title, ''), COALESCE(username, '')"
        " FROM dialogs WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if not row:
        return None
    current_notes, title, username = row[0], row[1], row[2]
    who = f"{title}/@{username}" if username else title

    # подгружаем тексты вопросов
    qids = [a[0] for a in answers]
    placeholders = ",".join("?" * len(qids))
    qrows = conn.execute(
        f"SELECT id, point FROM disputed_questions WHERE id IN ({placeholders})",
        qids
    ).fetchall()
    points = {q[0]: q[1] for q in qrows}

    answers_block = []
    for qid, answer in answers:
        answers_block.append(f"Q (#{qid}): {points.get(qid, '?')}\nA: {answer}")

    prompt = PROMPT_MERGE_NOTES.format(
        current_notes=current_notes[:2000] or "(пусто)",
        answers_block="\n\n".join(answers_block),
        who=who,
    )

    addition = call_claude(prompt, oauth_token=oauth_token).strip()
    if not addition or len(addition) < 20:
        return None

    if dry_run:
        return addition

    new_notes = (current_notes.rstrip() + "\n\n" + addition) if current_notes else addition
    conn.execute(
        "UPDATE dialogs SET user_notes = ?, user_notes_at = ? WHERE chat_id = ?",
        (new_notes, datetime.now().isoformat(), chat_id)
    )
    conn.commit()
    return addition


def parse_replies(questions: list[dict], replies: list[dict], oauth_token: str) -> list[dict]:
    q_block = []
    for q in questions:
        line = f"#{q['id']} {q['point']}"
        if q.get('reason'):
            line += f"\n      контекст: {q['reason'][:200]}"
        q_block.append(line)

    r_block = []
    for r in replies:
        r_block.append(f"[{r['date'][:16]}] {r['text']}")

    prompt = PROMPT_PARSE.format(
        questions_block="\n\n".join(q_block),
        reply_block="\n\n".join(r_block) if r_block else "(подчинённый ничего не ответил)",
    )

    out = call_claude(prompt, oauth_token=oauth_token)
    out = out.strip()
    if out.startswith("```"):
        out = re.sub(r"^```(?:json)?\s*", "", out)
        out = re.sub(r"\s*```\s*$", "", out)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", out, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"No JSON in response: {out[:300]}")


def write_answers(conn, parsed: list[dict], dry_run: bool = False):
    """Записывает ответы в disputed_questions."""
    now = datetime.now().isoformat()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        qid = item.get("id")
        status = item.get("status")
        answer = item.get("answer")
        if status != "answered" or not answer:
            continue
        if dry_run:
            print(f"    [dry] would set #{qid} answered=1, answer={answer[:80]}")
        else:
            conn.execute("""
                UPDATE disputed_questions
                SET answer = ?, answered = 1, answered_at = ?
                WHERE id = ?
            """, (answer, now, qid))
    if not dry_run:
        conn.commit()


def update_outbound_status(conn, outbound_id: int, parsed: list[dict]):
    """Обновляет статус outbound_questions: answered если все ответы получены, иначе waiting.
    Также пишет last_collect_at для идемпотентности следующих collect-проходов."""
    statuses = [p.get("status") for p in parsed if isinstance(p, dict)]
    n_answered = sum(1 for s in statuses if s == "answered")
    n_total = len(statuses)
    if n_answered == n_total and n_total > 0:
        new_status = "answered"
    else:
        new_status = "waiting"  # частично — оставляем waiting, доспросим в reminder
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE outbound_questions SET status = ?, last_collect_at = ? WHERE id = ?",
        (new_status, now, outbound_id)
    )
    conn.commit()
    return new_status, n_answered, n_total


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--chat-id", type=int, default=None)
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    outbounds = get_pending_outbounds(conn, only_chat_id=args.chat_id)
    if not outbounds:
        print("Нет pending outbound_questions.")
        return

    print(f"Pending outbounds: {len(outbounds)}")
    n_processed = 0
    n_full_answered = 0
    n_partial = 0

    for i, ob in enumerate(outbounds):
        qids = json.loads(ob["question_ids"])
        questions = fetch_questions_text(conn, qids)
        # Идемпотентность: только сообщения ПОСЛЕ последнего collect
        since = effective_since(ob)
        replies = fetch_replies_after(conn, ob["chat_id"], since)

        # Нет новых сообщений — пропускаем (но НЕ обновляем last_collect_at —
        # чтобы при следующем collect мы могли дойти до старых если они появятся)
        if not replies:
            print(f"  [{ob['chat_id']}] нет НОВЫХ incoming после {since[:10]}, пропуск")
            continue

        print(f"\n[{i+1}/{len(outbounds)}] cid={ob['chat_id']}, вопросов={len(qids)}, "
              f"новых replies={len(replies)} (с {since[:16]})")

        tok = TOKENS[i % len(TOKENS)] if TOKENS else None
        try:
            parsed = parse_replies(questions, replies, oauth_token=tok)
        except Exception as e:
            print(f"  ✗ parse failed: {e}")
            continue

        n_ans = sum(1 for p in parsed if p.get("status") == "answered")
        n_skip = sum(1 for p in parsed if p.get("status") == "skipped")
        n_def = sum(1 for p in parsed if p.get("status") == "deferred")
        print(f"    parsed: {n_ans} answered, {n_skip} skipped, {n_def} deferred")

        # Запись ответов в disputed_questions
        write_answers(conn, parsed, dry_run=args.dry_run)

        # Merge ответов в user_notes — авторитетный слой для будущих deep_dive
        if not args.dry_run and n_ans > 0:
            try:
                addition = merge_answers_to_user_notes(
                    conn, ob["chat_id"], parsed, oauth_token=tok, dry_run=False
                )
                if addition:
                    print(f"    ↪ user_notes расширено: {addition[:120]}")
            except Exception as e:
                print(f"    ⚠ merge_user_notes failed: {e}")

        # Обновление статуса outbound (+ last_collect_at для идемпотентности)
        if not args.dry_run:
            new_status, na, nt = update_outbound_status(conn, ob["id"], parsed)
            print(f"    → outbound.status = {new_status} ({na}/{nt})")
            if new_status == "answered":
                n_full_answered += 1
            else:
                n_partial += 1

        n_processed += 1

    print(f"\n=== DONE ===")
    print(f"  Processed:        {n_processed}")
    print(f"  Fully answered:   {n_full_answered}")
    print(f"  Partial/waiting:  {n_partial}")
    if args.dry_run:
        print("  [DRY-RUN — БД не изменена]")


if __name__ == "__main__":
    main()
