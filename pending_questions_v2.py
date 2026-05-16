#!/usr/bin/env python3
"""
Улучшенный экспорт disputed:
- Сортировка по важности контактов: subordinate → boss → peer → partner → client
- Внутри — по свежести активности
- Для каждого контакта — header «Кто это / что за чат» (из user_notes_ready)
- Под каждым вопросом — короткий контекст из deep_dive
- TOP-30 ключевых контактов в начало (для быстрого старта)
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
OUT = BASE / "pending_questions_v2.md"

REL_PRIORITY = {
    "subordinate": 0,  # подчинённые — критичнее всего
    "boss": 1,         # Влад
    "peer": 2,         # коллеги-равные
    "partner": 3,
    "client": 4,
    "mixed": 5,
    "unknown": 9,
    None: 9,
    "": 9,
}


def safe_str(v) -> str:
    if v is None: return ""
    if isinstance(v, str): return v
    return json.dumps(v, ensure_ascii=False)


def get_short_role(user_notes_ready, tldr, max_len: int = 250) -> str:
    """Возвращает короткое описание роли — первое предложение из user_notes_ready или tldr."""
    text = (safe_str(user_notes_ready) or safe_str(tldr) or "").strip()
    if not text:
        return ""
    # Первое предложение
    for sep in ["—", "–", "."]:
        if sep in text[:max_len]:
            text = text[:text.find(sep, 50) + 1] if text.find(sep, 50) > 0 else text
            break
    return text[:max_len].strip()


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Все вопросы + meta чата
    rows = conn.execute("""
        SELECT q.id, q.chat_id, q.point, q.reason, q.raised_at,
               d.title, d.username, d.user_notes, d.relation,
               d.last_message_at, d.ai_deep_dive_json,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id=d.chat_id AND m.date>='2026-01-28') AS msgs
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id=q.chat_id
        WHERE q.answered = 0
        ORDER BY d.title, q.raised_at
    """).fetchall()

    if not rows:
        print("Нет открытых вопросов")
        return

    # Группируем по chat_id
    by_chat = {}
    for r in rows:
        cid = r["chat_id"]
        if cid not in by_chat:
            j = {}
            try: j = json.loads(r["ai_deep_dive_json"] or "{}")
            except: pass
            by_chat[cid] = {
                "chat_id": cid,
                "title": r["title"] or "?",
                "username": r["username"] or "",
                "relation": r["relation"] or "unknown",
                "user_notes": r["user_notes"] or "",
                "user_notes_ready": j.get("user_notes_ready", ""),
                "tldr": j.get("tldr", ""),
                "last_msg": r["last_message_at"] or "",
                "msgs": r["msgs"] or 0,
                "questions": [],
            }
        by_chat[cid]["questions"].append({
            "id": r["id"], "point": r["point"], "reason": r["reason"]
        })

    # Сортировка контактов: relation priority → активность (msgs DESC) → last_msg
    sorted_chats = sorted(by_chat.values(), key=lambda c: (
        REL_PRIORITY.get(c["relation"], 9),
        -c["msgs"],
        c["last_msg"] or "",
    ), reverse=False)

    # TOP-30
    top30 = sorted_chats[:30]
    rest = sorted_chats[30:]

    lines = []
    lines.append("# Спорные вопросы — для голосовых ответов (для Anastasya)")
    lines.append(f"\n_Сгенерировано {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    lines.append(f"_Открытых вопросов: **{len(rows)}** по **{len(by_chat)}** контактам_\n")
    lines.append("**Настя, привет.**\n")
    lines.append("Это вопросы которые AI задал по твоим коллегам и проектам. **Многие ты "
                 "уже знаешь** (про оплаты, кто что делает, статусы). Часть знает только "
                 "Сергей — на них он сам ответит позже.\n")
    lines.append("**Как пользоваться:**")
    lines.append("- Под каждым вопросом поле `**Ответ:**` — замени `_____` на ответ")
    lines.append("- Через **Handy (говорилку)** надиктуй — получится текст в файле")
    lines.append("- **Можно одним голосовым на ВСЕ 5 вопросов одного человека** (так быстрее)")
    lines.append("- Если не знаешь — **оставь `_____`** или напиши «не знаю»")
    lines.append("- Сначала пройди TOP-30 контактов — это самые важные\n")
    lines.append("**Когда закончишь** — отдай файл Сергею, он подтянет ответы в БД через:\n")
    lines.append("```bash\npython3 import_anastasya_answers.py\n```\n")
    lines.append("---\n")

    def render_chat(c, idx=None):
        u = f" @{c['username']}" if c['username'] else ""
        idx_str = f"[{idx}] " if idx else ""
        out = []
        out.append(f"\n## {idx_str}{c['title']}{u}  `id={c['chat_id']}`  ·  {c['relation']}\n")

        # Кто это (короткий header)
        role = get_short_role(c["user_notes_ready"], c["tldr"], 350)
        if role:
            out.append(f"_{role}_\n")

        # User notes если короткие
        if c["user_notes"] and len(c["user_notes"]) < 500:
            un = c["user_notes"].replace("\n", " ").strip()
            out.append(f"**Твоя заметка:** {un[:400]}\n")

        out.append(f"**Активность за 3 мес:** {c['msgs']} сообщений · последнее {c['last_msg'][:10] if c['last_msg'] else '?'}\n")

        for q in c["questions"]:
            out.append(f"\n### #{q['id']} {q['point']}")
            if q["reason"]:
                out.append(f"_Контекст: {q['reason'][:300]}_")
            out.append("\n**Ответ:** _____  \n")

        return "\n".join(out)

    # TOP-30 секция
    lines.append("# 🔥 TOP-30 — приоритетные контакты\n")
    for i, c in enumerate(top30, 1):
        lines.append(render_chat(c, idx=i))

    if rest:
        lines.append("\n---\n\n# Остальные контакты\n")
        for c in rest:
            lines.append(render_chat(c))

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {OUT}")
    print(f"  Открытых вопросов: {len(rows)}, контактов: {len(by_chat)}")
    print(f"  TOP-30 включает {sum(len(c['questions']) for c in top30)} вопросов")


if __name__ == "__main__":
    main()
