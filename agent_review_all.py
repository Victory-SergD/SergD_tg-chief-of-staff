#!/usr/bin/env python3
"""
Разовый «агент-супервизор» через Claude Opus 4.7.

Цель: вместо параллельного парсинга каждого чата отдельной модели — даём ОПУСУ
ВЕСЬ срез данных одним промптом. Он видит:
- метаданные чата (title, type, msg-stats)
- AI-категоризацию (Gemini Flash Lite)
- raw user_notes (свободный текст Сергея)

И возвращает структурированный JSON:
- dedup_map (нормализация имён)
- corrected categorizations
- org_chart (mermaid)
- top_open_questions per project
- final_report_md (готовый отчёт Владу)

Использование:
    python3 agent_review_all.py
    python3 agent_review_all.py --since 2026-03-26
    python3 agent_review_all.py --only-reviewed   # только чаты где user_notes заполнены
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB_DEFAULT = BASE / "tg_analiz.db"
OUT_DIR = BASE / "reports"
OUT_DIR.mkdir(exist_ok=True)

CLAUDE_TIMEOUT = 1800  # 30 мин на 1M-prompt
CLAUDE_MODEL = "claude-opus-4-7[1m]"  # Opus 4.7 1M context — макс качество для отчёта Владу


SUPERVISOR_PROMPT = """Ты — Супервизор-агент Сергея Дышканта (руководитель ВДЛ-направления, lead generation).
Прямой руководитель Сергея — Влад (@bjlyd, собственник).
Цель: проверить и нормализовать данные о его рабочих чатах за последний месяц,
после чего собрать ПРАВИЛЬНЫЙ отчёт для Влада.

Тебе даны 2 источника:

1. **AI-категоризация** (Gemini Flash Lite, быстрая) — может содержать ошибки/дубли.
2. **Сергея ручные заметки** в свободной форме — ЭТО ИСТИНА. Если AI противоречит — верь Сергею.

ПРАВИЛА:

A. **Дедупликация имён** — Сергей пишет одно имя в разных формах:
   - «Даня Семёнов / Даниил Семёнов / Данила Семёнов» = один человек, @hehehakiri
   - Любые имена с одинаковым username — это один человек, нормализуй к канону
   - Один и тот же человек может встречаться по username, по имени, по русской записи

B. **Орг-структура**: построй иерархию.
   Сергей знает её: Влад → Сергей → (Даня Семёнов | Anastasya | программисты | VDL-менеджеры).
   У Дани Семёнова свой штат: Леонид, Никита XAH, ANN, Виктория Потапова, Влад Неуймин и др.

C. **Категория и роль (relation)** — если user_note явно говорит «я босс / руководитель / здесь главный»,
   то relation должен быть `boss`. AI-version может быть неверной.

D. **importance** — если user_note явно «не следить, неактуально, скип, мьют» → `skip`/`low`.

E. **active_ownership** — true если Сергей сам отвечает за чат; false если делегировал.

F. **responsible_user** — если в user_note сказано «ответственный — X», запиши X.

G. **needs_action** + **notes_for_agent** — конкретные просьбы Сергея агенту.
   Например: «делай утреннюю сводку по этому чату», «следи за закрытием open_questions».

ОБЯЗАТЕЛЬНО ВЫЯВИ:
- Где AI ошибся (например, поставил «subordinate» где Сергей сказал «я босс»)
- Дубликаты имён — какие к какому канону свести (с указанием username)
- Кто чьи подчинённые (по user_notes)
- Какие чаты skip / mute
- Какие чаты требуют утренней сводки

ОТВЕТ СТРОГО В JSON, без markdown:

{
  "name_dedup": [
    {"canonical": "Даня Семёнов (@hehehakiri)",
     "aliases": ["Даня Семёнов", "Даниил Семёнов", "Данила Семёнов"],
     "username": "@hehehakiri"}
  ],
  "corrections": [
    {"chat_id": -1234, "field": "relation", "from": "subordinate",
     "to": "boss", "reason": "Сергей пишет 'тут я руководитель'"}
  ],
  "org_chart_mermaid": "graph TD\\n    Vlad[Влад @bjlyd] --> Sergey[Сергей]\\n    ...",
  "team_summary": [
    {"manager": "Сергей", "direct_reports": ["Anastasya", "Даня Семёнов"]},
    {"manager": "Даня Семёнов", "direct_reports": ["Леонид", "Никита XAH"]}
  ],
  "top_open_questions_by_project": [
    {"project": "VDL", "questions": [
      {"chat": "Доноры", "responsible": "Леонид",
       "question": "...", "blocked_for_days": 5}
    ]}
  ],
  "skip_chats": [
    {"chat_id": -1003670348941, "title": "SergD Marketolog Searcher",
     "reason": "Гипотеза неактуальна до мая-июня"}
  ],
  "needs_morning_brief": [
    {"chat_id": 716365720, "title": "Anastasya Помощница",
     "reason": "Сергей просит утреннюю сводку с открытыми вопросами"}
  ],
  "final_report_md": "# Отчёт для Влада ...\\n\\n## TL;DR\\n..."
}

ВНИМАНИЕ:
- Не выдумывай. Если данных недостаточно — пиши «недостаточно сигнала».
- final_report_md — markdown для Telegram (использует ** для жирного, _ для курсива).
- Russian, деловой стиль.

---

ДАННЫЕ:

__DATA__
"""


def connect():
    conn = sqlite3.connect(DB_DEFAULT)
    conn.row_factory = sqlite3.Row
    return conn


def gather_data(conn, since: str, only_reviewed: bool = False):
    where = ["chat_type IN ('private','group')",
             "COALESCE(is_archived,0)=0",
             "last_message_at >= ?",
             "category IS NOT NULL", "category != 'silent'"]
    if only_reviewed:
        where.append("user_notes IS NOT NULL AND user_notes != ''")

    rows = conn.execute(f"""
        SELECT chat_id, chat_type, title, username, members_count,
               unread_count, category, relation, notes, user_notes,
               responsible_user, active_ownership, importance,
               org_position, team_member_of
        FROM dialogs
        WHERE {' AND '.join(where)}
        ORDER BY last_message_at DESC
    """, (since,)).fetchall()

    out = []
    for r in rows:
        cnt = conn.execute("""
            SELECT COUNT(*) n,
                   SUM(CASE WHEN is_outgoing=1 THEN 1 ELSE 0 END) sent,
                   SUM(CASE WHEN media_type='voice' THEN 1 ELSE 0 END) voices
            FROM messages WHERE chat_id=? AND date>=?
        """, (r["chat_id"], since)).fetchone()

        notes = {}
        try:
            notes = json.loads(r["notes"] or "{}")
        except Exception:
            pass

        out.append({
            "chat_id": r["chat_id"],
            "title": r["title"],
            "username": r["username"],
            "type": r["chat_type"],
            "members": r["members_count"],
            "msgs_total": cnt["n"] or 0,
            "msgs_sergei": cnt["sent"] or 0,
            "voices": cnt["voices"] or 0,
            "ai_category": r["category"],
            "ai_relation": r["relation"],
            "ai_summary": notes.get("summary", ""),
            "ai_open_questions": notes.get("open_questions", []),
            "user_note_raw": r["user_notes"] or "",
            "user_responsible": r["responsible_user"] or "",
            "user_active_ownership": r["active_ownership"],
            "user_importance": r["importance"] or "",
            "user_org_position": r["org_position"] or "",
            "user_team_member_of": r["team_member_of"] or "",
        })
    return out


def call_claude(prompt: str, model: str = CLAUDE_MODEL) -> tuple[str, float]:
    start = time.time()
    result = subprocess.run(
        ["claude", "-p", "--model", model,
         "--no-session-persistence",
         "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
    )
    dur = time.time() - start
    if result.returncode != 0:
        raise RuntimeError(
            f"claude failed code={result.returncode}: "
            f"{result.stderr[:500] or result.stdout[:500]}"
        )
    return result.stdout.strip(), dur


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2026-03-26")
    p.add_argument("--only-reviewed", action="store_true",
                   help="только чаты где user_notes заполнены")
    p.add_argument("--model", default=CLAUDE_MODEL)
    p.add_argument("--data-only", action="store_true",
                   help="дамп данных без вызова Claude")
    args = p.parse_args()

    conn = connect()
    data = gather_data(conn, args.since, args.only_reviewed)
    print(f"Чатов в датасете: {len(data)}", flush=True)

    data_text = json.dumps(data, ensure_ascii=False, indent=2)
    print(f"Размер данных: {len(data_text)} символов "
          f"(~{len(data_text)//4} токенов)", flush=True)

    if args.data_only:
        print(data_text[:5000])
        return

    prompt = SUPERVISOR_PROMPT.replace("__DATA__", data_text)
    print(f"Промпт всего: {len(prompt)} символов. "
          f"Запускаю claude --model {args.model}…", flush=True)

    raw, dur = call_claude(prompt, model=args.model)
    print(f"Готово за {dur:.0f}с\n", flush=True)

    # Сохраняем raw на всякий случай
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = OUT_DIR / f"supervisor_raw_{ts}.txt"
    raw_path.write_text(raw, encoding="utf-8")
    print(f"Raw response saved: {raw_path}", flush=True)

    try:
        result = parse_json(raw)
    except Exception as e:
        print(f"⚠ JSON parse error: {e}", flush=True)
        print(f"Raw start: {raw[:500]}", flush=True)
        return

    # Сохраняем JSON
    json_path = OUT_DIR / f"supervisor_{ts}.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Parsed JSON saved: {json_path}", flush=True)

    # Сохраняем финальный отчёт отдельно
    if "final_report_md" in result:
        report_path = OUT_DIR / f"vlad_supervised_{ts}.md"
        report_path.write_text(result["final_report_md"], encoding="utf-8")
        print(f"Vlad report saved: {report_path}", flush=True)

    # Краткая сводка
    print("\n=== БЫСТРЫЙ ОБЗОР ===")
    if "name_dedup" in result:
        print(f"Дубликатов имён найдено: {len(result['name_dedup'])}")
    if "corrections" in result:
        print(f"Корректировок к AI: {len(result['corrections'])}")
    if "skip_chats" in result:
        print(f"Чатов в skip: {len(result['skip_chats'])}")
    if "needs_morning_brief" in result:
        print(f"Чатов для утренней сводки: {len(result['needs_morning_brief'])}")


if __name__ == "__main__":
    main()
