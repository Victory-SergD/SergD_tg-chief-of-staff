#!/usr/bin/env python3
"""
Экспорт классифицированных disputed в 3 файла по владельцам:
  • critical_for_anastasya.md — для Anastasya (operating partner)
  • critical_for_sergei.md    — стратегические голосом самому Сергею
  • critical_for_subordinates.md — вопросы конкретным подчинённым (для пересылки)

Фильтрация:
  • answered = 0
  • criticality = 'critical'
  • duplicate_of_id IS NULL (только каноничные, дубли уже схлопнуты Опусом)

Сортировка внутри owner:
  • priority: high → medium → low
  • внутри priority — по контакту (группировка)
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
# Без timestamp в имени — один файл, всегда свежий (предыдущий перезаписывается).
# Дата запуска фиксируется в самом файле.

PRIO_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}
PRIO_LABEL = {"high": "🔴 HIGH", "medium": "🟠 MEDIUM", "low": "🟡 LOW"}


def safe_str(v):
    if v is None: return ""
    if isinstance(v, str): return v
    return json.dumps(v, ensure_ascii=False)


def short_role(user_notes, max_len=200):
    text = (user_notes or "").strip()
    if not text:
        return ""
    # Первое предложение или абзац
    for sep in ["—", "–", "."]:
        idx = text.find(sep, 30, max_len)
        if idx > 0:
            return text[:idx+1].strip()
    return text[:max_len].strip()


def count_dups(conn, canonical_id):
    """Сколько вопросов помечены как дубль этого каноничного."""
    return conn.execute(
        "SELECT COUNT(*) FROM disputed_questions WHERE duplicate_of_id = ? AND answered = 0",
        (canonical_id,)
    ).fetchone()[0]


def fetch_critical(conn):
    """Берёт открытые critical БЕЗ дубликатов и БЕЗ тех что сейчас в outbound (ждут ответа подчинённого).

    Логика: если вопрос уже отправлен подчинённому через ask_subordinates и ждёт ответа —
    не выгружаем его в файлы (иначе Anastasya/Сергей будут дублировать запрос).
    """
    rows = conn.execute("""
        SELECT q.id, q.chat_id, q.point, q.reason, q.raised_at,
               q.criticality_priority, q.answer_owner, q.deadline,
               q.impact_estimate, q.criticality_reason,
               d.title, d.username, d.user_notes, d.relation
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id = q.chat_id
        WHERE q.answered = 0
          AND q.criticality = 'critical'
          AND q.duplicate_of_id IS NULL
          AND q.id NOT IN (
            SELECT json_each.value
            FROM outbound_questions ob, json_each(ob.question_ids)
            WHERE ob.status IN ('waiting', 'reminded')
          )
        ORDER BY
          CASE q.criticality_priority
            WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4
          END,
          d.title, q.id
    """).fetchall()
    return rows


def render_question_block(conn, q, dup_count: int):
    prio = q["criticality_priority"] or "low"
    parts = []
    parts.append(f"### {PRIO_LABEL.get(prio, '?')} #{q['id']}: {q['point']}")
    meta = []
    if q["criticality_reason"]:
        meta.append(f"_{q['criticality_reason']}_")
    if q["deadline"]:
        meta.append(f"⏰ дедлайн: **{q['deadline']}**")
    if q["impact_estimate"]:
        meta.append(f"💰 импакт: {q['impact_estimate']}")
    if dup_count:
        meta.append(f"🔗 +{dup_count} похожих вопросов закроются с этим ответом")
    if q["raised_at"]:
        meta.append(f"📅 поднят: {q['raised_at'][:10]}")
    if meta:
        parts.append(" · ".join(meta))
    if q["reason"]:
        parts.append(f"\nКонтекст: {q['reason']}")
    parts.append(f"\n**Ответ:** _____\n")
    return "\n".join(parts)


def render_contact_header(rows_for_contact):
    r = rows_for_contact[0]
    title = r["title"] or "?"
    username = r["username"] or "-"
    role = short_role(r["user_notes"])
    n = len(rows_for_contact)
    h = [
        f"\n---\n",
        f"## {title} (@{username}) · {n} вопрос{'ов' if n > 1 else ''}",
    ]
    if role:
        h.append(f"_{role}_\n")
    return "\n".join(h)


def write_file(path: Path, header: str, intro: str, contact_groups: list[tuple]):
    """contact_groups: [(rows_for_contact, [(q_row, dup_count)]), ...]"""
    pieces = [header, "", intro, ""]
    for rows_for_contact, qs in contact_groups:
        pieces.append(render_contact_header(rows_for_contact))
        for q, dup_count in qs:
            pieces.append(render_question_block(None, q, dup_count))
    path.write_text("\n".join(pieces))
    print(f"  → {path.name} ({sum(len(qs) for _, qs in contact_groups)} вопросов в {len(contact_groups)} контактах)")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = fetch_critical(conn)
    print(f"Всего critical (без дубликатов): {len(rows)}")

    # Раскладываем по 3 ведра по answer_owner
    # ВАЖНО: subordinate:X в групповом чате (chat_id < 0) — не можем отправить как личку,
    # перенаправляем в файл Сергея с пометкой что нужно решить вручную
    buckets = {"anastasya": [], "sergei": [], "subordinate": []}
    for r in rows:
        owner = r["answer_owner"] or "sergei"
        if owner.startswith("subordinate:"):
            if r["chat_id"] > 0:
                buckets["subordinate"].append(r)
            else:
                # Групповой чат — нельзя отправить в личку, в bucket Сергея
                buckets["sergei"].append(r)
        elif owner == "anastasya":
            buckets["anastasya"].append(r)
        else:
            buckets["sergei"].append(r)

    print(f"  Anastasya: {len(buckets['anastasya'])}")
    print(f"  Сергей сам: {len(buckets['sergei'])}")
    print(f"  Подчинённым: {len(buckets['subordinate'])}")

    for owner, items in buckets.items():
        # Группируем по chat_id, сохраняем порядок (priority уже отсортирован)
        groups = []
        seen_chat = {}
        for r in items:
            cid = r["chat_id"]
            if cid not in seen_chat:
                seen_chat[cid] = ([r], [])
                groups.append(seen_chat[cid])
            else:
                seen_chat[cid][0].append(r)
        # Для каждого вопроса считаем сколько дубликатов
        contact_groups = []
        for rows_for_contact, _ in groups:
            qs_with_dup = [(q, count_dups(conn, q["id"])) for q in rows_for_contact]
            contact_groups.append((rows_for_contact, qs_with_dup))

        if owner == "anastasya":
            path = BASE / f"critical_for_anastasya.md"
            n_high = sum(1 for r in items if r["criticality_priority"] == "high")
            n_med = sum(1 for r in items if r["criticality_priority"] == "medium")
            n_low = sum(1 for r in items if r["criticality_priority"] == "low")
            header = f"# Критичные вопросы для Anastasya — {datetime.now():%Y-%m-%d}\n\n_Всего: **{len(items)} вопросов** ({n_high} high, {n_med} medium, {n_low} low) по {len(contact_groups)} контактам_"
            intro = (
                "**Настя, привет.**\n\n"
                "Это **только реально важные** вопросы — система Опуса уже отфильтровала "
                "шум и дубли. Каждый ответ влияет на твою работу с командой / отчётностью / финансами.\n\n"
                "**Как работать:**\n"
                "- Под каждым вопросом поле `**Ответ:**` — замени `_____` на ответ\n"
                "- Через **Handy** надиктуй — текст появится в файле\n"
                "- Можно одним голосовым на ВСЕ вопросы одного человека (так быстрее)\n"
                "- Если не знаешь — оставь `_____` или напиши «не знаю»\n"
                "- Сначала **🔴 HIGH** (горит), потом 🟠 MEDIUM, потом 🟡 LOW\n"
                "- Если видишь ⏰ дедлайн — этот вопрос приоритетнее по сроку\n"
                "- 🔗 «+N похожих» — значит твой один ответ закроет ещё N связанных вопросов\n\n"
                "Когда закончишь — отдай файл Сергею.\n"
            )
            write_file(path, header, intro, contact_groups)

        elif owner == "sergei":
            path = BASE / f"critical_for_sergei.md"
            n_high = sum(1 for r in items if r["criticality_priority"] == "high")
            n_med = sum(1 for r in items if r["criticality_priority"] == "medium")
            n_low = sum(1 for r in items if r["criticality_priority"] == "low")
            header = f"# Стратегические вопросы — Сергей голосом ({datetime.now():%Y-%m-%d})\n\n_Всего: **{len(items)} вопросов** ({n_high} high, {n_med} medium, {n_low} low) по {len(contact_groups)} контактам_"
            intro = (
                "Это вопросы которые **только ты можешь закрыть** — стратегические решения, "
                "формализации с Владом, твои планы.\n\n"
                "**Как работать:** заполни поля `**Ответ:**` (текстом или Handy-голосом) → следующий запуск `./daily.sh` сам импортирует ответы через `import_sergei_answers.py`.\n\n"
                "Сначала **🔴 HIGH**, потом 🟠 MEDIUM, потом 🟡 LOW.\n"
            )
            write_file(path, header, intro, contact_groups)

        elif owner == "subordinate":
            path = BASE / f"critical_for_subordinates.md"
            n_high = sum(1 for r in items if r["criticality_priority"] == "high")
            header = f"# Вопросы подчинённым — для пересылки ({datetime.now():%Y-%m-%d})\n\n_Всего: **{len(items)} вопросов** ({n_high} high) по {len(contact_groups)} подчинённым_"
            intro = (
                "Эти вопросы можно отдать конкретным подчинённым (через Anastasya или напрямую от твоего имени). "
                "Под каждым контактом — кому он адресован.\n"
            )
            write_file(path, header, intro, contact_groups)

    conn.close()
    print("\n✓ Готово.")


if __name__ == "__main__":
    main()
