#!/usr/bin/env python3
"""
Утренний бриф для Сергея — ежедневный дайджест из всех 139 work + 13 silent_team.

Логика:
- Читает все ai_deep_dive_json свежих work-чатов (last_msg >= 90д)
- Извлекает: q6_open_tasks, q9_next_actions, q5_last_two_weeks, disputed
- Группирует по проектам (триадж) и подчинённым
- Считает "новое за 24ч" по messages
- Считает "просрочки" по open_tasks без обновлений
- Топ-задачи на сегодня (из q9_next_actions с близкими дедлайнами)
- Шлёт в Telegram через бот.

Запуск:
  python3 morning_brief.py             # стандартный (за вчерашний день)
  python3 morning_brief.py --since 3d  # за последние 3 дня
  python3 morning_brief.py --dry-run   # только показать в консоли, не в TG
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"

sys.path.insert(0, str(BASE))
import tg_notify


def safe_str(v, max_len=200):
    if v is None: return ""
    if isinstance(v, str): return v[:max_len]
    return json.dumps(v, ensure_ascii=False)[:max_len]


def parse_since(s: str) -> datetime:
    if s.endswith("d"):
        days = int(s[:-1])
        return datetime.now() - timedelta(days=days)
    return datetime.fromisoformat(s)


def fetch_active_work(conn, since_date: datetime):
    """Все work + silent_team с deep_dive_json, активные за 90 дней."""
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT chat_id, COALESCE(username,'') AS u, title, chat_type,
               COALESCE(triage_project,'_other_work') AS project,
               COALESCE(relation,'?') AS rel,
               last_message_at, ai_deep_dive_json
        FROM dialogs
        WHERE category IN ('work','silent_team')
          AND ai_deep_dive_json IS NOT NULL
          AND last_message_at >= ?
        ORDER BY last_message_at DESC
    """, (cutoff,)).fetchall()
    return rows


def count_new_messages(conn, chat_id: int, since: datetime) -> tuple[int, int]:
    """Возвращает (всего новых, от Сергея)."""
    total = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN is_outgoing=1 THEN 1 ELSE 0 END)
        FROM messages WHERE chat_id=? AND date >= ?
    """, (chat_id, since.isoformat())).fetchone()
    return (total[0] or 0, total[1] or 0)


def extract_open_tasks(j: dict) -> list:
    # Поддержка обоих имён: с префиксом (legacy) и без (новый стандарт)
    raw = j.get("open_tasks") or j.get("q6_open_tasks", [])
    out = []
    if isinstance(raw, list):
        for t in raw:
            if isinstance(t, dict):
                out.append({
                    "task": t.get("task") or t.get("text") or "",
                    "since": t.get("since", ""),
                    "blocker": t.get("blocker", ""),
                    "owner": t.get("owner", ""),
                    "assignee": t.get("assignee", ""),
                })
            elif isinstance(t, str):
                out.append({"task": t, "since": "", "blocker": "", "owner": "", "assignee": ""})
    return out


def extract_next_actions(j: dict) -> list:
    raw = j.get("next_actions") or j.get("q9_next_actions", [])
    out = []
    if isinstance(raw, list):
        for a in raw:
            if isinstance(a, dict):
                out.append(a.get("action") or a.get("text") or json.dumps(a, ensure_ascii=False)[:120])
            else:
                out.append(str(a)[:200])
    return out


def days_since(date_str: str) -> int:
    if not date_str: return 0
    try:
        d = datetime.fromisoformat(date_str.replace("Z", ""))
        return (datetime.now() - d.replace(tzinfo=None)).days
    except Exception:
        return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="1d", help="окно для 'новых сообщений' (1d/3d/...)")
    p.add_argument("--dry-run", action="store_true", help="не слать в TG")
    p.add_argument("--stale-days", type=int, default=3,
                   help="просрочка задачи: сколько дней без активности (default 3)")
    args = p.parse_args()

    since = parse_since(args.since)
    print(f"Окно «новое»: с {since.strftime('%Y-%m-%d %H:%M')} | "
          f"просрочка от {args.stale_days} дней")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = fetch_active_work(conn, since)
    print(f"Активных work+silent_team с deep_dive: {len(rows)}")

    # Сборка данных
    new_today = []          # чаты где есть новые сообщения за окно
    open_tasks_all = []     # все open_tasks с meta
    next_actions = []       # q9_next_actions
    disputed_count = 0
    by_project = defaultdict(list)

    for r in rows:
        try:
            j = json.loads(r["ai_deep_dive_json"])
        except:
            continue
        n_total, n_my = count_new_messages(conn, r["chat_id"], since)
        contact_label = (f"@{r['u']}" if r["u"] else r["title"] or "?")[:40]

        if n_total > 0:
            new_today.append({
                "label": contact_label, "n_total": n_total, "n_my": n_my,
                "title": r["title"] or "?", "tldr": safe_str(j.get("tldr"), 150),
                "project": r["project"],
            })

        for t in extract_open_tasks(j):
            d_since = days_since(t.get("since", ""))
            open_tasks_all.append({
                "contact": contact_label, "task": t["task"][:200],
                "since_str": t.get("since", ""), "days_idle": d_since,
                "blocker": t.get("blocker", ""), "owner": t.get("owner", ""),
                "assignee": t.get("assignee", ""), "project": r["project"],
            })

        for a in extract_next_actions(j):
            next_actions.append({"contact": contact_label, "action": a[:200],
                                 "project": r["project"]})

        # Disputed
        disputed = j.get("disputed", [])
        if isinstance(disputed, list):
            unanswered = [d for d in disputed
                          if not (isinstance(d, dict) and d.get("answered"))]
            disputed_count += len(unanswered)

        by_project[r["project"]].append(contact_label)

    # === Build brief ===
    today = datetime.now().strftime("%d.%m.%Y")
    weekday = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][datetime.now().weekday()]
    lines = [f"🌅 Утренний бриф — {weekday} {today}", ""]

    # 1. Новое за окно
    new_today.sort(key=lambda x: -x["n_total"])
    if new_today:
        lines.append(f"🆕 Активность за {args.since} ({len(new_today)} контактов):")
        for n in new_today[:10]:
            owe = " · от тебя 0" if n["n_my"] == 0 else f" · от тебя {n['n_my']}"
            lines.append(f"  • {n['label']} — {n['n_total']} msg{owe}")
            if n["tldr"] and n["n_my"] == 0:
                lines.append(f"    └ {n['tldr'][:120]}")
        if len(new_today) > 10:
            lines.append(f"  …ещё {len(new_today)-10}")
        lines.append("")

    # 2. Просрочки (open_tasks с большим days_idle)
    stale = [t for t in open_tasks_all if t["days_idle"] >= args.stale_days]
    stale.sort(key=lambda x: -x["days_idle"])
    if stale:
        lines.append(f"⏳ Просрочки (>{args.stale_days} дней без движения):")
        for t in stale[:10]:
            lines.append(f"  • {t['contact']}: {t['task'][:120]} — {t['days_idle']}д")
            if t["blocker"]:
                lines.append(f"    └ blocker: {t['blocker'][:100]}")
        if len(stale) > 10:
            lines.append(f"  …ещё {len(stale)-10}")
        lines.append("")

    # 3. Все next_actions (без жёсткого фильтра — Сергей сам разберётся что важно)
    # Берём максимум 15 — это уже плотная сводка действий
    if next_actions:
        # Дедуп по тексту (часто Опус возвращает похожие)
        seen = set()
        uniq_actions = []
        for a in next_actions:
            key = (a["contact"], a["action"][:80])
            if key in seen: continue
            seen.add(key)
            uniq_actions.append(a)
        shown = uniq_actions[:15]
        lines.append(f"🚀 На тебе ({len(uniq_actions)} задач, показываю первые {len(shown)}):")
        for a in shown:
            lines.append(f"  • {a['contact']}: {a['action'][:160]}")
        lines.append("")

    # 4. Сводка по проектам — все, не только топ
    if by_project:
        lines.append("📊 По проектам:")
        # Сначала канонические 6 + новые 2 трека, потом остальные
        canonical = ["VDL Lead Router", "SergD Claude Seo", "Victory. Email маркетинг",
                     "Victory — Замена копирайтинга (Кирилл)", "Victory — Авто улучшатор сайтов",
                     "Victory — Автогенерация описаний (Олег)",
                     "Код собиратель инфы", "VK парсер"]
        shown = set()
        for proj in canonical:
            if proj in by_project:
                lines.append(f"  • {proj}: {len(by_project[proj])} контактов")
                shown.add(proj)
        for proj, contacts in sorted(by_project.items(), key=lambda x: -len(x[1])):
            if proj in shown: continue
            lines.append(f"  • {proj}: {len(contacts)} контактов")
        lines.append("")

    # 5. КРИТИЧНО СЕГОДНЯ — TOP-5 high-priority вопросов с дедлайнами/импактом
    high_now = conn.execute("""
        SELECT q.id, q.point, q.criticality_reason, q.deadline, q.impact_estimate,
               q.answer_owner, COALESCE(d.title,'?') AS title, COALESCE(d.username,'') AS u
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id = q.chat_id
        WHERE q.answered = 0
          AND q.criticality = 'critical'
          AND q.criticality_priority = 'high'
          AND q.duplicate_of_id IS NULL
        ORDER BY
          CASE WHEN q.deadline IS NOT NULL THEN 0 ELSE 1 END,
          q.deadline ASC, q.id
        LIMIT 5
    """).fetchall()
    if high_now:
        lines.append("🚩 КРИТИЧНО СЕГОДНЯ (TOP-5 high-priority, дедлайн/импакт):")
        for r in high_now:
            u = f" @{r['u']}" if r["u"] else ""
            tag = ""
            if r["deadline"]:
                tag += f" ⏰{r['deadline']}"
            if r["impact_estimate"]:
                imp = r["impact_estimate"][:40]
                tag += f" 💰{imp}"
            owner = (r["answer_owner"] or "").split(":")[0]
            owner_emoji = {"anastasya": "🧑‍💼", "sergei": "👤", "subordinate": "🔧"}.get(owner, "")
            lines.append(f"  {owner_emoji} {r['title'][:30]}{u}: {r['point'][:80]}")
            if tag.strip():
                lines.append(f"      {tag.strip()}")
        lines.append("")

    # 6. Сводка disputed по владельцам ответа
    by_owner = conn.execute("""
        SELECT
          CASE
            WHEN answer_owner = 'anastasya' THEN 'anastasya'
            WHEN answer_owner = 'sergei' THEN 'sergei'
            WHEN answer_owner LIKE 'subordinate:%' THEN 'subordinate'
            ELSE 'other'
          END AS owner,
          criticality_priority AS prio,
          COUNT(*) AS n
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id = q.chat_id
        WHERE q.answered = 0
          AND q.criticality = 'critical'
          AND q.duplicate_of_id IS NULL
          AND d.category IN ('work','silent_team')
        GROUP BY owner, prio
    """).fetchall()
    if by_owner:
        agg = {}
        for r in by_owner:
            agg.setdefault(r["owner"], {"high": 0, "medium": 0, "low": 0})
            if r["prio"] in agg[r["owner"]]:
                agg[r["owner"]][r["prio"]] += r["n"]
        lines.append("📋 Critical-вопросы (без дублей):")
        emoji_map = {"anastasya": "🧑‍💼 К Anastasya", "sergei": "👤 К тебе голосом", "subordinate": "🔧 Подчинённым"}
        for owner_key, label in emoji_map.items():
            if owner_key in agg:
                a = agg[owner_key]
                total = a["high"] + a["medium"] + a["low"]
                lines.append(f"  {label}: {total} ({a['high']}🔴 / {a['medium']}🟠 / {a['low']}🟡)")
        lines.append("   Файлы: critical_for_anastasya_*.md / critical_for_sergei_*.md / critical_for_subordinates_*.md")
        lines.append(f"   (всего disputed-noise отфильтровано: {disputed_count - sum(a['high']+a['medium']+a['low'] for a in agg.values())} шт)")
        lines.append("")

    lines.append(f"📦 База: {len(rows)} активных рабочих контактов с deep_dive.")

    text = "\n".join(lines)
    print("\n" + "="*60)
    print(text)
    print("="*60)

    if args.dry_run:
        print("\n[DRY-RUN — в TG не отправлено]")
        return

    print("\n[sending to TG…]")
    ok = tg_notify.send(text)
    print(f"  TG send: {'OK' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
