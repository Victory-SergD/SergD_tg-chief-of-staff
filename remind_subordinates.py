#!/usr/bin/env python3
"""
Reminder для outbound_questions (повторный запрос подчинённому).

Логика:
  • outbound.status='waiting' AND sent_at < now-48h AND reminder_count < 3
    → переспрашиваем НЕ ОТВЕЧЕННЫЕ вопросы (на отвеченные answered=1 не дёргаем)
    → reminder_count++, last_reminder_at=now

  • reminder_count >= 3 → status='closed_no_reply'
    → соответствующие НЕ ОТВЕЧЕННЫЕ disputed_questions помечаем как noise
      (никто не закрывает, не спамим больше)

Запуск:
  python3 remind_subordinates.py            # dry-run превью
  python3 remind_subordinates.py --send     # реально отправить reminder'ы
  python3 remind_subordinates.py --hours-cooldown 48  # минимальный интервал
"""
import argparse
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
SESSION = BASE / "tg_access" / "SergD_telethon"

import dotenv
dotenv.load_dotenv(BASE / ".env")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]


def get_outbounds_to_remind(conn, cooldown_hours: int):
    """Те что ждут уже >cooldown_hours, имеют < 3 reminder и не закрыты."""
    cutoff = (datetime.now() - timedelta(hours=cooldown_hours)).isoformat()
    rows = conn.execute("""
        SELECT id, chat_id, question_ids, sent_at, reminder_count, last_reminder_at, message_id
        FROM outbound_questions
        WHERE status IN ('waiting', 'reminded')
          AND COALESCE(last_reminder_at, sent_at) < ?
          AND reminder_count < 3
        ORDER BY sent_at
    """, (cutoff,)).fetchall()
    return rows


def get_outbounds_to_close(conn):
    """Те у которых уже 3+ reminder но всё ещё waiting/reminded — закрываем."""
    rows = conn.execute("""
        SELECT id, chat_id, question_ids
        FROM outbound_questions
        WHERE status IN ('waiting', 'reminded')
          AND reminder_count >= 3
    """).fetchall()
    return rows


def get_unanswered_qids_for_outbound(conn, ob_question_ids: list[int]) -> list[int]:
    """Из списка ob.question_ids возвращает только те что answered=0."""
    if not ob_question_ids:
        return []
    placeholders = ",".join("?" * len(ob_question_ids))
    rows = conn.execute(
        f"SELECT id FROM disputed_questions WHERE id IN ({placeholders}) AND answered = 0",
        ob_question_ids
    ).fetchall()
    return [r[0] for r in rows]


def fetch_questions_text(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"""
        SELECT q.id, q.point, q.criticality_priority, q.deadline,
               COALESCE(d.title, '') AS title, COALESCE(d.username, '') AS username,
               COALESCE(d.user_notes, '') AS user_notes
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id = q.chat_id
        WHERE q.id IN ({placeholders})
    """, ids).fetchall()
    return [dict(zip([d[0] for d in rows[0].keys() if hasattr(rows[0], 'keys')], row)) if hasattr(rows[0], 'keys') else dict(row) for row in rows]


def fetch_questions_for_remind(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"""
        SELECT q.id, q.point, q.reason, q.criticality_priority, q.deadline
        FROM disputed_questions q
        WHERE q.id IN ({placeholders})
    """, ids).fetchall()
    return [{"id": r[0], "point": r[1], "reason": r[2] or "",
             "priority": r[3], "deadline": r[4]} for r in rows]


REMINDER_TEMPLATE = (
    "Привет ещё раз! Я уже писал по этим вопросам, но ответ ещё не получил. "
    "Помоги пожалуйста — даже короткий ответ полезен. Напомню вопросы:\n\n"
    "{questions}\n\n"
    "Если на какой-то реально нечего сказать — напиши «не знаю» или «спроси у Сергея». "
    "Главное чтоб мы понимали статус. Спасибо!"
)


async def main_async(args):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1. Закрываем те что превысили reminder_count >= 3
    to_close = get_outbounds_to_close(conn)
    if to_close:
        print(f"\n📕 Закрываем {len(to_close)} outbound без ответа после 3 reminder'ов:")
        for ob in to_close:
            qids = json.loads(ob["question_ids"])
            unanswered = get_unanswered_qids_for_outbound(conn, qids)
            if unanswered:
                # Помечаем НЕ ОТВЕЧЕННЫЕ disputed_questions как noise (criticality)
                placeholders = ",".join("?" * len(unanswered))
                conn.execute(f"""
                    UPDATE disputed_questions
                    SET criticality = 'noise',
                        criticality_reason = COALESCE(criticality_reason, '') || ' [auto-closed: 3 reminders no reply]'
                    WHERE id IN ({placeholders})
                """, unanswered)
            now = datetime.now().isoformat()
            conn.execute("""
                UPDATE outbound_questions
                SET status = 'closed_no_reply', closed_at = ?, closed_reason = '3 reminders no reply'
                WHERE id = ?
            """, (now, ob["id"]))
            print(f"  ✗ cid={ob['chat_id']} qids={qids} → closed (помечено noise: {len(unanswered)})")
        conn.commit()

    # 2. Reminder для тех у кого <3 повторов и прошло >cooldown_hours
    to_remind = get_outbounds_to_remind(conn, args.hours_cooldown)
    if not to_remind:
        print("\nНет outbound для reminder'а (никто не превысил cooldown).")
        conn.close()
        return

    print(f"\n📨 Кандидатов на reminder: {len(to_remind)}")

    # Композим reminder-сообщения
    composed = []  # [(chat_id, ob_id, qids_unanswered, text)]
    for ob in to_remind:
        qids_all = json.loads(ob["question_ids"])
        qids_unanswered = get_unanswered_qids_for_outbound(conn, qids_all)
        if not qids_unanswered:
            # Все ответы уже есть — можно закрыть
            print(f"  cid={ob['chat_id']} ob_id={ob['id']} — все вопросы уже отвечены, закрываю")
            conn.execute("""
                UPDATE outbound_questions
                SET status = 'answered', closed_at = ?, closed_reason = 'all answered before reminder'
                WHERE id = ?
            """, (datetime.now().isoformat(), ob["id"]))
            conn.commit()
            continue

        questions = fetch_questions_for_remind(conn, qids_unanswered)
        # Простой шаблон reminder (без Opus, чтоб быстро и предсказуемо)
        q_lines = []
        for i, q in enumerate(questions, 1):
            line = f"{i}. (#{q['id']}) {q['point']}"
            q_lines.append(line)

        text = REMINDER_TEMPLATE.format(questions="\n".join(q_lines))
        composed.append((ob["chat_id"], ob["id"], qids_unanswered, text))

    # Превью
    print("\n═══ ПРЕВЬЮ REMINDER'ОВ ═══")
    for cid, ob_id, qids, text in composed:
        title_row = conn.execute(
            "SELECT COALESCE(title,'?'), COALESCE(username,'') FROM dialogs WHERE chat_id=?",
            (cid,)
        ).fetchone()
        title, username = title_row if title_row else ("?", "")
        print(f"\n→ {title} (@{username}, id={cid}) — {len(qids)} вопрос(ов)")
        print("─" * 50)
        print(text)
        print("─" * 50)

    if not args.send:
        print("\n[DRY-RUN] Чтобы отправить — добавь --send")
        conn.close()
        return

    # Реально шлём через Telethon
    from telethon import TelegramClient
    from tg_proxy import get_tg_proxy
    print("\n📤 Отправляю reminder'ы…")
    client = TelegramClient(str(SESSION), API_ID, API_HASH, proxy=get_tg_proxy())
    await client.start()

    sent = 0
    for cid, ob_id, qids, text in composed:
        try:
            msg = await client.send_message(cid, text)
            now = datetime.now().isoformat()
            conn.execute("""
                UPDATE outbound_questions
                SET status = 'reminded',
                    last_reminder_at = ?,
                    reminder_count = reminder_count + 1
                WHERE id = ?
            """, (now, ob_id))
            conn.commit()
            print(f"  ✓ cid={cid} → reminded (msg_id={msg.id})")
            sent += 1
            await asyncio.sleep(2)
        except Exception as e:
            print(f"  ✗ cid={cid}: {e}")

    await client.disconnect()
    conn.close()
    print(f"\n✅ Отправлено reminder'ов: {sent}/{len(composed)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true")
    p.add_argument("--hours-cooldown", type=int, default=48,
                   help="Минимальный интервал в часах между sent/reminder и следующим reminder (default 48)")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
