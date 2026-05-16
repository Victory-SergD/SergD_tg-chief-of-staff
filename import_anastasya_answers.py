#!/usr/bin/env python3
"""
Импорт ответов Anastasya из заполненного critical_for_anastasya_*.md.

Парсит блоки вида:
  ### 🔴 HIGH #685: Закрыта ли выплата ...
  ...
  **Ответ:** Все закрыто

→ disputed_questions.answer + answered=1
→ user_notes контакта (через Opus, как авторитетный факт)

Запуск:
  python3 import_anastasya_answers.py [--file FILE] [--dry-run]
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
DEFAULT_TOKEN = TOKENS[0] if TOKENS else None


def call_claude(prompt: str, oauth_token=None) -> str:
    env = os.environ.copy()
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "text"]
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            timeout=CLAUDE_TIMEOUT, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"claude exit={result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


def parse_filled_md(path: Path) -> list[dict]:
    """Возвращает список {qid, answer} из заполненного файла."""
    text = path.read_text()
    # Находим блоки: ### ... #ID: ... до следующего ### или ##
    pattern = r'###\s+[^\n]*?#(\d+)[^\n]*\n(.*?)(?=\n###|\n##|\Z)'
    matches = re.findall(pattern, text, re.DOTALL)

    results = []
    for qid_str, body in matches:
        # Найти **Ответ:** в body
        m = re.search(r'\*\*Ответ:\*\*\s*(.*?)(?=\Z)', body, re.DOTALL)
        if not m:
            continue
        answer = m.group(1).strip()
        # Skip пустые / placeholder
        if not answer or answer.startswith("_____") or answer == "_____":
            continue
        # Чистка: убрать лишние строки
        answer = re.sub(r'\n\s*\n+', '\n\n', answer).strip()
        results.append({"qid": int(qid_str), "answer": answer})
    return results


PROMPT_MERGE = """Ты обновляешь user_notes контакта Сергея — авторитетный слой фактов.
Anastasya только что ответила на вопросы. Нужно вписать её ответы естественной фразой
в user_notes, чтобы на следующих deep_dive факт был в авторитетном слое.

═══ ТЕКУЩИЕ USER_NOTES ═══
{current_notes}

═══ НОВЫЕ ОТВЕТЫ ОТ ANASTASYA ═══
{answers_block}

═══ ЗАДАЧА ═══
Верни ОДНУ короткую приписку которую нужно ДОБАВИТЬ в конец user_notes.
Формат: «[2026-05-08 от Anastasya] факт1. факт2. факт3.»
Только новые факты, без повтора того что уже в user_notes.
Сжато (1-3 предложения). Если нет новых фактов — пустую строку.

═══ ФОРМАТ ОТВЕТА ═══
Только сам текст приписки. Никаких ```, преамбул, JSON.
"""


def merge_to_user_notes(conn, chat_id: int, items: list[dict],
                        oauth_token=None, dry_run=False) -> str | None:
    row = conn.execute(
        "SELECT COALESCE(user_notes, ''), COALESCE(title, '')"
        " FROM dialogs WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if not row:
        return None
    current_notes, title = row[0], row[1]

    qids = [it["qid"] for it in items]
    placeholders = ",".join("?" * len(qids))
    qrows = conn.execute(
        f"SELECT id, point FROM disputed_questions WHERE id IN ({placeholders})",
        qids
    ).fetchall()
    points = {q[0]: q[1] for q in qrows}

    blocks = []
    for it in items:
        blocks.append(f"Q (#{it['qid']}): {points.get(it['qid'], '?')}\nA: {it['answer']}")

    prompt = PROMPT_MERGE.format(
        current_notes=current_notes[:2000] or "(пусто)",
        answers_block="\n\n".join(blocks),
    )

    addition = call_claude(prompt, oauth_token=oauth_token).strip()
    if not addition or len(addition) < 10:
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", default="critical_for_anastasya_filled.md")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    path = BASE / args.file
    if not path.exists():
        print(f"❌ Не найден: {path}")
        return

    answers = parse_filled_md(path)
    print(f"Нашли {len(answers)} заполненных ответов в {path.name}")

    if not answers:
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Шаг 1: записать в disputed_questions
    n_written = 0
    n_skipped_already_answered = 0
    affected_chats = {}  # chat_id → [{qid, answer}]
    now = datetime.now().isoformat()

    for it in answers:
        # Получить chat_id
        row = conn.execute(
            "SELECT chat_id, answered FROM disputed_questions WHERE id = ?",
            (it["qid"],)
        ).fetchone()
        if not row:
            print(f"  ⚠ q#{it['qid']} не найден в disputed_questions")
            continue
        cid, already_answered = row[0], row[1]
        if already_answered == 1:
            n_skipped_already_answered += 1
            continue

        if not args.dry_run:
            conn.execute("""
                UPDATE disputed_questions
                SET answer = ?, answered = 1, answered_at = ?
                WHERE id = ?
            """, (it["answer"], now, it["qid"]))
        n_written += 1
        affected_chats.setdefault(cid, []).append(it)

    if not args.dry_run:
        conn.commit()

    print(f"\n✓ Записано в disputed_questions: {n_written}")
    print(f"  пропущено (уже answered): {n_skipped_already_answered}")
    print(f"  затронутых контактов: {len(affected_chats)}")

    # Шаг 2: мердж в user_notes для каждого затронутого контакта (через Opus)
    print(f"\n=== Мердж ответов в user_notes ===")
    for i, (cid, items) in enumerate(affected_chats.items()):
        title_row = conn.execute(
            "SELECT COALESCE(title,'?'), COALESCE(username,'') FROM dialogs WHERE chat_id = ?",
            (cid,)
        ).fetchone()
        title, username = (title_row[0], title_row[1]) if title_row else ("?", "")

        tok = TOKENS[i % len(TOKENS)] if TOKENS else None
        try:
            addition = merge_to_user_notes(conn, cid, items, oauth_token=tok,
                                           dry_run=args.dry_run)
            if addition:
                print(f"  [{i+1}/{len(affected_chats)}] @{username or '-'} ({title[:30]}): "
                      f"{len(items)} ответ(ов)")
                print(f"    ↪ {addition[:120]}")
            else:
                print(f"  [{i+1}/{len(affected_chats)}] @{username or '-'}: пусто (нечего добавить)")
        except Exception as e:
            print(f"  [{i+1}/{len(affected_chats)}] @{username or '-'}: ⚠ merge failed: {e}")

    conn.close()
    print(f"\n=== ИТОГО ===")
    print(f"  ✓ {n_written} ответов записано")
    print(f"  ✓ {len(affected_chats)} user_notes обновлены")
    if args.dry_run:
        print("  [DRY-RUN — БД не изменена]")


if __name__ == "__main__":
    main()
