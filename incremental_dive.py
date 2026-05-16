#!/usr/bin/env python3
"""
SMART DIVE — умный апдейт ai_deep_dive_json.

Routing:
1. Если deep_dive нет в БД → делегируем на полный agent_contact_deep_dive.py
2. Если новых сообщений после ai_deep_dive_at нет → skip
3. Если новых < 3 → skip (слишком мало для AI)
4. Если новых > 50 ИЛИ deep_dive старше 14 дней → полный rebuild
5. Иначе → INCREMENTAL UPDATE (передаём Опусу только delta)

Incremental update:
- Загружаем существующий ai_deep_dive_json
- Берём только сообщения после ai_deep_dive_at (с транскрипциями)
- Опус получает: старый JSON + новые сообщения + user_notes (авторитетные)
- Возвращает PATCH (что добавить в timeline, что обновить в q5/q6/q9/tldr/user_notes_ready)
- Применяем patch к существующему JSON
- Сохраняем + обновляем ai_deep_dive_at

Запуск:
  python3 incremental_dive.py --chat-id N
  python3 incremental_dive.py --user @username
  python3 incremental_dive.py --all-stale         # все work с устаревшим dive
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Импорт shared helpers из основного скрипта (DRY)
sys.path.insert(0, str(Path(__file__).parent))
from agent_contact_deep_dive import (
    call_claude,
    format_msg_row,
    parse_json,
    CLAUDE_MODEL,
)

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"

# Thresholds
# MIN=1 — даже одно сообщение может быть важным отчётом ("задачу сделал").
# MAX=200 — для очень активных переписок (>200 за день) лучше full rebuild
# из-за объёма контекста, иначе incremental промт раздуется.
MIN_NEW_MSGS_TO_UPDATE = 1
MAX_NEW_MSGS_FOR_INCREMENTAL = 200
MAX_DAYS_FOR_INCREMENTAL = 14    # dive старше — лучше полный rebuild

INCREMENTAL_PROMPT = """Ты обновляешь существующий deep_dive анализ контакта Сергея Дышканта (руководитель в Victory Agency).

ЗАДАЧА: пришли новые сообщения за период с {since} до {until}. Обнови ТОЛЬКО динамичные секции существующего JSON-анализа. Статичные секции (q1-q4, q7, q8, q10) НЕ ТРОГАЙ.

═══ КОНТЕКСТ КОНТАКТА ═══
chat_id: {chat_id}
title: {title}
type: {chat_type}
username: {username}
relation: {relation}
triage_project: {project}

═══ АВТОРИТЕТНЫЕ ФАКТЫ ОТ СЕРГЕЯ (используй как ИСТИНУ, не оспаривай) ═══
{authoritative}

═══ ПРЕДЫДУЩИЙ ai_deep_dive_json ═══
```json
{old_json}
```

═══ НОВЫЕ СООБЩЕНИЯ за период {since} → {until} ({new_count} штук) ═══
{new_messages}

═══ ЧТО ВЕРНУТЬ — JSON PATCH ═══
{{
  "timeline_add": [
    {{"date": "YYYY-MM-DD", "event": "что произошло"}},
    ...
  ],
  "last_two_weeks": [
    // ПОЛНАЯ замена — события за последние 2 недели (включая что было + новое)
    {{"date": "...", "event": "..."}},
    ...
  ],
  "open_tasks": [
    // ПОЛНАЯ замена. Учти: некоторые старые задачи могли закрыться (убрать), новые — добавить.
    {{"task": "...", "assignee": "...", "owner": "...", "since": "...", "blocker": "..."}},
    ...
  ],
  "next_actions": [
    // ПОЛНАЯ замена — актуальные next actions
    {{"priority": "P1|P2|P3", "action": "...", "deadline": "YYYY-MM-DD"}},
    ...
  ],
  "tldr_new": "обновлённый TLDR (если изменилось ключевое) ИЛИ null",
  "user_notes_ready_new": "обновлённый user_notes_ready (если изменилось) ИЛИ null",
  "disputed_add": [
    // только НОВЫЕ disputed (если в новых сообщениях появились новые неопределённости)
    {{"point": "...", "reason": "..."}},
    ...
  ],
  "summary_of_changes": "1-2 фразы — что изменилось в этом апдейте"
}}

ПРАВИЛА:
1. timeline_add — ТОЛЬКО новые события из новых сообщений. Старые не дублируй.
2. last_two_weeks / open_tasks / next_actions — ПОЛНАЯ замена (Опус сам решит что закрылось, что осталось, что добавилось).
3. tldr_new и user_notes_ready_new — null если ничего значимо не поменялось.
4. who_and_relation, direction, org_chain, top_topics, debts_and_obligations, finance, format_review, contact, display_name — НЕ ТРОГАЙ. Их в patch не возвращай.
5. confidence_overall — оставь старое.

ВЫХОД: только JSON-объект, без markdown-обёртки.
"""


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def check_state(c, chat_id: int) -> dict:
    """Определяет какой dive нужен: full / incremental / skip."""
    row = c.execute("""
        SELECT chat_id, COALESCE(username,'') AS u, title, chat_type,
               COALESCE(relation,'') AS rel,
               COALESCE(triage_project,'') AS proj,
               COALESCE(user_notes,'') AS user_notes,
               ai_deep_dive_json, ai_deep_dive_at, last_message_at
        FROM dialogs WHERE chat_id=?
    """, (chat_id,)).fetchone()
    if not row:
        return {"action": "not_found"}

    if not row["ai_deep_dive_json"] or not row["ai_deep_dive_at"]:
        return {"action": "full", "reason": "no existing deep_dive", "row": row}

    # Считаем новые сообщения после ai_deep_dive_at
    new_count = c.execute("""
        SELECT COUNT(*) FROM messages
        WHERE chat_id=? AND date > ?
    """, (chat_id, row["ai_deep_dive_at"])).fetchone()[0]

    days_old = (datetime.now() - datetime.fromisoformat(row["ai_deep_dive_at"])).days

    if new_count == 0:
        return {"action": "skip", "reason": f"no new messages (dive {days_old}д назад)", "row": row}
    if new_count < MIN_NEW_MSGS_TO_UPDATE:
        return {"action": "skip", "reason": f"only {new_count} new msgs (< {MIN_NEW_MSGS_TO_UPDATE})", "row": row}
    if new_count > MAX_NEW_MSGS_FOR_INCREMENTAL:
        return {"action": "full", "reason": f"{new_count} new msgs (> {MAX_NEW_MSGS_FOR_INCREMENTAL})", "row": row}
    if days_old > MAX_DAYS_FOR_INCREMENTAL:
        return {"action": "full", "reason": f"dive {days_old}д назад (> {MAX_DAYS_FOR_INCREMENTAL}д)", "row": row}

    return {
        "action": "incremental",
        "new_count": new_count,
        "days_old": days_old,
        "row": row,
    }


def fetch_new_messages(c, chat_id: int, since: str) -> list:
    """Сообщения после since с транскрипциями."""
    rows = c.execute("""
        SELECT m.msg_id, m.date, m.text, m.media_type, m.media_duration,
               m.is_outgoing, m.topic_id, m.reply_to_msg_id,
               u.username, u.first_name, u.last_name,
               COALESCE(u.is_bot, 0) AS is_bot,
               t.transcription
        FROM messages m
        LEFT JOIN users u ON u.user_id = m.from_user_id
        LEFT JOIN transcriptions t ON t.msg_id=m.msg_id AND t.chat_id=m.chat_id
        WHERE m.chat_id=? AND m.date > ? AND COALESCE(u.is_bot,0)=0
        ORDER BY m.date ASC
    """, (chat_id, since)).fetchall()
    return rows


def build_authoritative_block(c, chat_id: int, user_notes: str) -> str:
    """Авторитетные факты: user_notes этого чата + answered disputed_questions."""
    parts = []
    if user_notes:
        parts.append(f"User notes Сергея (важно):\n«{user_notes}»\n")

    # Answered disputed для этого чата
    answered = c.execute("""
        SELECT point, answer FROM disputed_questions
        WHERE chat_id=? AND answered=1
        ORDER BY answered_at DESC LIMIT 30
    """, (chat_id,)).fetchall()
    if answered:
        parts.append("\nОтветы Сергея на спорные вопросы:")
        for r in answered:
            parts.append(f"  • {r['point'][:200]}\n    → {r['answer'][:300]}")

    return "\n".join(parts) if parts else "(нет дополнительных фактов)"


def apply_patch(old_json: dict, patch: dict) -> dict:
    """Применяет patch к существующему JSON.
    Не трогает: q1-q4, q7, q8, q10, contact, display_name, confidence_overall.
    """
    new_json = dict(old_json)

    # timeline_add — append к существующему
    if patch.get("timeline_add"):
        existing_timeline = new_json.get("timeline", [])
        if isinstance(existing_timeline, list):
            existing_timeline.extend(patch["timeline_add"])
            new_json["timeline"] = existing_timeline

    # last_two_weeks — full replace (имя без префикса q5_, как в оригинальном prompt)
    if "last_two_weeks" in patch:
        new_json["last_two_weeks"] = patch["last_two_weeks"]
        # Чистим возможный legacy дубль с префиксом
        new_json.pop("q5_last_two_weeks", None)

    # open_tasks — full replace
    if "open_tasks" in patch:
        new_json["open_tasks"] = patch["open_tasks"]
        new_json.pop("q6_open_tasks", None)

    # next_actions — full replace
    if "next_actions" in patch:
        new_json["next_actions"] = patch["next_actions"]
        new_json.pop("q9_next_actions", None)

    # tldr_new — replace if not null
    if patch.get("tldr_new"):
        new_json["tldr"] = patch["tldr_new"]

    # user_notes_ready_new — replace if not null
    if patch.get("user_notes_ready_new"):
        new_json["user_notes_ready"] = patch["user_notes_ready_new"]

    # disputed_add — append (с дедупом по point)
    if patch.get("disputed_add"):
        existing = new_json.get("disputed", [])
        if not isinstance(existing, list):
            existing = []
        existing_points = set()
        for d in existing:
            if isinstance(d, dict):
                existing_points.add(d.get("point", "")[:100])
        for d in patch["disputed_add"]:
            if isinstance(d, dict) and d.get("point", "")[:100] not in existing_points:
                existing.append(d)
                existing_points.add(d.get("point", "")[:100])
        new_json["disputed"] = existing

    # Метаданные апдейта
    new_json["last_incremental_update"] = datetime.now().isoformat()
    new_json["last_change_summary"] = patch.get("summary_of_changes", "")

    return new_json


def do_full_dive(chat_id: int) -> bool:
    """Делегируем на полный agent_contact_deep_dive.py.
    Используем --skip-fetch чтобы НЕ дёргать Telegram (данные уже в БД).
    Это обходит SQLite session lock — можно запускать параллельно."""
    print(f"  → FULL dive (subprocess --skip-fetch)…", flush=True)
    result = subprocess.run(
        ["python3", "-u", "agent_contact_deep_dive.py",
         "--chat-id", str(chat_id), "--months", "3", "--skip-fetch"],
        cwd=BASE, capture_output=False, text=True, timeout=2400,
    )
    return result.returncode == 0


def do_incremental(c, chat_id: int, state: dict) -> bool:
    """Делает incremental update."""
    row = state["row"]
    print(f"  → INCREMENTAL ({state['new_count']} новых msgs, dive {state['days_old']}д назад)", flush=True)

    # Старый JSON
    try:
        old_json = json.loads(row["ai_deep_dive_json"])
    except:
        print(f"  ! ошибка парсинга старого JSON, делегирую на full")
        return do_full_dive(chat_id)

    # Новые сообщения
    new_msgs_rows = fetch_new_messages(c, chat_id, row["ai_deep_dive_at"])
    if not new_msgs_rows:
        print(f"  no new messages — skip"); return True

    new_msgs_text = "\n".join(format_msg_row(r) for r in new_msgs_rows)

    # Authoritative block
    auth = build_authoritative_block(c, chat_id, row["user_notes"])

    # Building prompt
    since = row["ai_deep_dive_at"]
    until = datetime.now().isoformat()

    prompt = INCREMENTAL_PROMPT.format(
        chat_id=chat_id,
        title=row["title"] or "?",
        chat_type=row["chat_type"] or "?",
        username=row["u"],
        relation=row["rel"],
        project=row["proj"],
        authoritative=auth,
        old_json=json.dumps(old_json, ensure_ascii=False, indent=2),
        new_messages=new_msgs_text,
        new_count=len(new_msgs_rows),
        since=since[:16],
        until=until[:16],
    )

    chars = len(prompt)
    print(f"  prompt: {chars:,} chars (~{chars//4:,} токенов)", flush=True)

    # Save prompt for debug
    out_dir = BASE / "output" / f"chat_{chat_id}"
    out_dir.mkdir(exist_ok=True, parents=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"incremental_prompt_{ts}.txt").write_text(prompt, encoding="utf-8")

    # Call Opus
    try:
        raw, dur = call_claude(prompt, model=CLAUDE_MODEL)
    except RuntimeError as e:
        print(f"  ! claude failed: {str(e)[:200]}")
        return False
    print(f"  · claude: {dur:.0f}s, {len(raw)} chars", flush=True)

    # Save raw for debug
    (out_dir / f"incremental_raw_{ts}.txt").write_text(raw, encoding="utf-8")

    # Parse patch
    try:
        patch = parse_json(raw)
    except Exception as e:
        print(f"  ! JSON parse: {e}"); return False

    # Apply
    new_json = apply_patch(old_json, patch)
    summary = patch.get("summary_of_changes", "(no summary)")
    print(f"  patch summary: {summary}", flush=True)

    # Save to DB
    c.execute("""
        UPDATE dialogs SET ai_deep_dive_json=?, ai_deep_dive_at=?
        WHERE chat_id=?
    """, (json.dumps(new_json, ensure_ascii=False), datetime.now().isoformat(), chat_id))
    c.commit()

    # Save updated JSON file
    (out_dir / f"deep_dive_{ts}_incremental.json").write_text(
        json.dumps(new_json, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  ✓ обновлён в БД", flush=True)
    return True


def smart_dive(c, chat_id: int) -> dict:
    """Главная функция — определяет стратегию и выполняет."""
    state = check_state(c, chat_id)
    action = state["action"]
    print(f"\n=== chat_id={chat_id} action={action} ===")
    if action == "not_found":
        print(f"  ! чат не найден в БД")
    elif action == "skip":
        print(f"  skip: {state['reason']}")
    elif action == "full":
        print(f"  full: {state.get('reason','')}")
        ok = do_full_dive(chat_id)
        return {"action": action, "ok": ok}
    elif action == "incremental":
        ok = do_incremental(c, chat_id, state)
        return {"action": action, "ok": ok}
    return {"action": action, "ok": True}


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--chat-id", type=int)
    g.add_argument("--user", type=str, help="@username")
    g.add_argument("--all-stale", action="store_true",
                   help="все work + silent_team где есть новые msgs или нет deep_dive")
    args = p.parse_args()

    c = conn()

    # Resolve chat_ids
    chat_ids = []
    if args.chat_id:
        chat_ids = [args.chat_id]
    elif args.user:
        uname = args.user.lstrip("@")
        row = c.execute("SELECT chat_id FROM dialogs WHERE LOWER(username)=LOWER(?)", (uname,)).fetchone()
        if not row:
            print(f"❌ user @{uname} не найден"); sys.exit(1)
        chat_ids = [row["chat_id"]]
    elif args.all_stale:
        rows = c.execute("""
            SELECT d.chat_id FROM dialogs d
            WHERE d.category IN ('work','silent_team')
              AND d.last_message_at >= date('now','-30 day')
              AND (d.ai_deep_dive_at IS NULL OR d.last_message_at > d.ai_deep_dive_at)
            ORDER BY (SELECT COUNT(*) FROM messages WHERE chat_id=d.chat_id AND date>='2026-01-28') DESC
        """).fetchall()
        chat_ids = [r["chat_id"] for r in rows]
        print(f"=== ALL-STALE: {len(chat_ids)} чатов для проверки ===")

    # Process
    counters = {"full": 0, "incremental": 0, "skip": 0, "not_found": 0, "fail": 0}
    for cid in chat_ids:
        try:
            res = smart_dive(c, cid)
            counters[res["action"]] = counters.get(res["action"], 0) + 1
            if res["action"] in ("full", "incremental") and not res.get("ok"):
                counters["fail"] += 1
        except Exception as e:
            print(f"  ! exception: {e}")
            counters["fail"] += 1

    print("\n" + "="*60)
    print("ИТОГ:")
    for k, v in counters.items():
        if v > 0:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
