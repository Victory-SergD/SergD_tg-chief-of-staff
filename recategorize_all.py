#!/usr/bin/env python3
"""
Полная перекатегоризация ВСЕХ 6000+ диалогов через Opus 1M в ДВА прохода.

ПРОХОД 1 (fast triage, ~30 мин):
- Все 6182 диалога → Opus 1M по chunks (CHUNK_SIZE_PASS1=500)
- На вход только мета: title, type, my_msgs, total_msgs, last_activity, current_category
- Результат: грубая разметка work / silent_team / personal / service / community / skip
- Сохраняем pass1 в БД (dialogs.category) — это уже сильно очищает картину

ПРОХОД 2 (deep, ~1.5-2 часа):
- Только чаты которые в проходе 1 стали work / silent_team / borderline
- Для каждого: last 150 messages + транскрипции голосовых из БД
- Результат: точное project (один из 6 канонических) + точное relation
- Сохраняем в БД (dialogs.relation, отдельная колонка triage_project)

RESUME:
- Прогресс сохраняется в OUT/recat_all_progress.json после КАЖДОГО chunk
- При перезапуске пропускаем уже сделанные chunks
- Применяет в БД сразу же, не в конце

Запуск:
  python3 recategorize_all.py                  # full run with resume
  python3 recategorize_all.py --pass1-only     # только проход 1
  python3 recategorize_all.py --pass2-only     # только проход 2 (нужен done pass 1)
  python3 recategorize_all.py --reset          # удалить progress, начать заново
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
DB = BASE / "tg_analiz.db"
OUT = BASE / "output"
OUT.mkdir(exist_ok=True)

CLAUDE_MODEL = "claude-opus-4-7[1m]"
CLAUDE_TIMEOUT = 900    # 15 мин timeout (раньше 1800 — слишком долго ждали)

CHUNK_SIZE_PASS1 = 200   # уменьшил с 500 — меньше шанс timeout
CHUNK_SIZE_PASS2 = 25    # глубокий — сообщения с транскрипциями объёмные
LAST_MSGS_PASS2 = 150    # сколько последних сообщений на чат

PROGRESS_FILE = OUT / "recat_all_progress.json"

# ─── Промпты ──────────────────────────────────────────────

PROMPT_PASS1 = """Ты — Контакт-Аналитик Сергея Дышканта (руководитель направлений в Victory Agency).

ЗАДАЧА: для каждого чата определить грубую категорию (быстрый проход).

КОНТЕКСТ:
- Сергей руководит 6 проектами: VDL Lead Router (главный), SergD Claude Seo, Victory Email маркетинг, Замена копирайтинга (Кирилл), Авто улучшатор сайтов, Автогенерация описаний (Олег).
- Прямой руководитель — Влад @bjlyd. Помощница — @ansurina.
- Закрытые проекты (skip): Селлеры (Голубев @am_golubev, Бобылёв @bobyliov и связанные группы).

КАТЕГОРИИ (используй ТОЛЬКО эти, других не возвращай):
- **work** — сотрудники, исполнители, подрядчики, партнёры с активным взаимодействием, group-чаты по 6 проектам, service-боты ВНУТРИ проектов (@Victory_Lead_Router_bot, @SergD_Victory_Cafe_bot, @SergD_panic_bot — это инструменты Сергея)
- **silent_team** — group-чат где Сергей не пишет (my_msgs=0), но команда работает (если в названии Victory/VDL/SEO)
- **personal** — родственники, друзья, личные приятели, бытовые услуги (мото, ремонт квартиры, лизинг авто)
- **service** — внешние сервисные боты, магазины, разовые сервисы (Stickers, Wallet, telegram-cервисы), боты-аналитики не относящиеся к Сергею
- **community** — каналы (broadcast) с подписками: нейросети, маркетинг, IT, образование
- **skip** — закрытые проекты (Селлеры), отказавшиеся клиенты, холодные DM, спам, разовые контакты

⛔ ВАЖНО: НЕ используй category='silent' — это устаревший alias от старой системы. Если у чата сейчас category=silent, ты ОБЯЗАН переклассифицировать его в одну из правильных: silent_team / personal / skip / service / community / work.
⛔ НЕ используй borderline — финальное решение ты должен принять. Если совсем не определился — выбери "skip" с reason="неуверен, требует ручного просмотра".

ПРАВИЛА БЫСТРОГО ПРОХОДА:
1. Если username заканчивается на `_bot` и НЕ один из инструментов Сергея (Victory_Lead_Router_bot, SergD_Victory_Cafe_bot, SergD_panic_bot, Afitraing_bot, NetWorkGPTs_bot) → service
2. Если type=channel (broadcast) → community
3. Если current_category уже work — оставь work (мы это подтвердили)
4. Если group и в title есть Victory|VDL|SEO|HR|Email|нейро — work или silent_team (если my_msgs=0)
5. Если private и my_msgs ≥ 5 — скорее всего work, кроме явно личного
6. Если сомневаешься — `borderline` (на проход 2 решим точнее)

ВХОД (chunk #__CHUNK__ of __TOTAL__, __COUNT__ чатов):
__CHATS__

ВЫВОД (строго один JSON-объект, без markdown-обёртки):
{
  "decisions": [
    {"chat_id": <int>, "new_category": "<work|silent_team|personal|service|community|skip|borderline>",
     "reason": "<short 1 фраза>"},
    ... ВСЕ __COUNT__ чатов
  ]
}

КРИТИЧНО: верни decisions для ВСЕХ __COUNT__ чатов в одном массиве.
"""


PROMPT_PASS2 = """Ты — Контакт-Аналитик Сергея Дышканта (Victory Agency).

ЗАДАЧА: для каждого чата (work/silent_team/borderline из прохода 1) определить ТОЧНО:
- project: один из 6 канонических или _other_work / not_work
- relation: boss / peer / subordinate / partner / client / mixed / unknown
- final_category: work / silent_team / borderline → final / personal / skip
- key_role: 1 фраза о роли человека/чата

КАНОНИЧЕСКИЕ ПРОЕКТЫ:
1. **VDL Lead Router** — лидген, чат Доноры, автосалоны/стоматки/займы/СВО/недвижка, VDL-менеджеры (Леонид @cgdrexxger, Виталий @LankaStar, Кириллов @donimai, Даня @hehehakiri, Никита XAH @hearthieflaco и др.)
2. **SergD Claude Seo** — Senior SEO + AI-генерация (writer/checker/humanizer/auditor), NeuroBaba, КП-бот SEO, Александр Штеле бизнес-получатель
3. **Victory. Email маркетинг** — B2B рассылки РФ/США/EU, MailWizz/Postal/MailCow, ОРМ Service. Cody @ajdkdow, Климин @Andrey_Klimin
4. **Victory — Замена копирайтинга (Кирилл)** — Кирилл @wsspk, замена внешних копирайтеров AI
5. **Victory — Авто улучшатор сайтов** — vi-heat-map, CRO нейронка. Виктор Исаев, Валентин, Рома
6. **Victory — Автогенерация описаний (Олег)** — Олег @canawesome, массовая автогенерация

ВХОД (для каждого чата — last 150 msgs с транскрипциями голосовых):
__CHATS_DEEP__

ВЫВОД (строго один JSON-объект):
{
  "decisions": [
    {"chat_id": <int>,
     "final_category": "<work|silent_team|personal|skip>",
     "project": "<one of 6 canonical | _other_work | not_work>",
     "relation": "<boss|peer|subordinate|partner|client|mixed|unknown>",
     "key_role": "<1 фраза>",
     "reason": "<short>"},
    ... ВСЕ чаты этого chunk
  ]
}

КРИТИЧНО: верни ВСЕ __COUNT__ чатов в массиве.
"""


# ─── Хелперы ──────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except:
            pass
    return {
        "started_at": datetime.now().isoformat(),
        "pass1_completed_chunks": [],
        "pass1_decisions": [],
        "pass2_completed_chunks": [],
        "pass2_decisions": [],
    }


def save_progress(p: dict):
    p["updated_at"] = datetime.now().isoformat()
    PROGRESS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def call_claude(prompt: str, retries: int = 1) -> dict | None:
    for attempt in range(retries + 1):
        start = time.time()
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", CLAUDE_MODEL,
                 "--no-session-persistence", "--output-format", "json"],
                input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(f"  ! claude timeout {CLAUDE_TIMEOUT}s (attempt {attempt+1})", flush=True)
            if attempt < retries: continue
            return None
        dur = time.time() - start
        print(f"  · claude: {dur:.0f}s, rc={result.returncode}", flush=True)
        if result.returncode != 0:
            print(f"  ! {(result.stderr or result.stdout)[:300]}")
            if attempt < retries:
                print("  ↻ retry…", flush=True)
                time.sleep(5)
                continue
            return None
        try:
            env = json.loads(result.stdout)
            text = env.get("result", "").strip()
        except:
            text = result.stdout.strip()
        # Парсим JSON из ответа
        text_strip = text.strip()
        if "```json" in text_strip:
            text_strip = text_strip.split("```json", 1)[1].split("```", 1)[0]
        elif text_strip.startswith("```"):
            text_strip = text_strip.split("```", 1)[1].split("```", 1)[0]
        start_idx = text_strip.find("{")
        if start_idx < 0:
            print("  ! JSON не найден в ответе")
            if attempt < retries: continue
            return None
        depth = 0; in_str = False; esc = False; end_idx = -1
        for i in range(start_idx, len(text_strip)):
            c = text_strip[i]
            if esc: esc = False; continue
            if c == "\\": esc = True; continue
            if c == '"' and not esc: in_str = not in_str; continue
            if in_str: continue
            if c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0: end_idx = i + 1; break
        if end_idx < 0:
            print("  ! JSON не закрыт")
            if attempt < retries: continue
            return None
        body = text_strip[start_idx:end_idx]
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            print(f"  ! JSON parse: {e}")
            Path(OUT / f"recat_failed_chunk_{int(time.time())}.txt").write_text(text)
            if attempt < retries: continue
            return None
    return None


def fmt_pass1_line(r):
    parts = [f"chat_id={r['chat_id']}", f"type={r['chat_type']}", f"cat={r['cat']}"]
    if r["u"]: parts.append(f"@{r['u']}")
    parts.append(f"title={(r['title'] or '?')[:60]}")
    parts.append(f"my={r['my_3mo']}/{r['msgs_3mo']}")
    if r["last_message_at"]: parts.append(f"last={r['last_message_at'][:10]}")
    if r["is_archived"]: parts.append("ARCHIVED")
    if r["user_notes"]: parts.append(f"notes='{r['user_notes'][:80]}'")
    return " | ".join(parts)


def fetch_dialogs_meta(conn):
    return conn.execute("""
        SELECT d.chat_id, COALESCE(d.username,'') AS u, COALESCE(d.title,'') AS title,
               d.chat_type, COALESCE(d.category,'(null)') AS cat,
               COALESCE(d.relation,'') AS rel,
               COALESCE(d.user_notes,'') AS user_notes,
               d.last_message_at, d.unread_count, COALESCE(d.is_archived,0) AS is_archived,
               (SELECT COUNT(*) FROM messages WHERE chat_id=d.chat_id AND date>='2026-01-28') AS msgs_3mo,
               (SELECT COUNT(*) FROM messages WHERE chat_id=d.chat_id AND is_outgoing=1 AND date>='2026-01-28') AS my_3mo
        FROM dialogs d
        ORDER BY d.chat_type, msgs_3mo DESC
    """).fetchall()


def fetch_chat_sample(conn, chat_id: int, limit: int = LAST_MSGS_PASS2) -> str:
    """Last N msgs with transcriptions, человекочитаемо."""
    rows = conn.execute("""
        SELECT m.date, m.text, m.media_type, m.is_outgoing,
               u.username, u.first_name, COALESCE(u.is_bot,0) AS is_bot,
               t.transcription
        FROM messages m
        LEFT JOIN users u ON u.user_id=m.from_user_id
        LEFT JOIN transcriptions t ON t.msg_id=m.msg_id AND t.chat_id=m.chat_id
        WHERE m.chat_id=? AND COALESCE(u.is_bot,0)=0
        ORDER BY m.date DESC LIMIT ?
    """, (chat_id, limit)).fetchall()
    out = []
    for r in reversed(rows):
        sender = "Я" if r["is_outgoing"] else (r["username"] or r["first_name"] or "?")
        text = (r["text"] or "")[:300]
        if r["media_type"] in ("voice", "audio", "video_note"):
            tr = r["transcription"] or "[без транскрипции]"
            text = f"[голос {r['media_type']}] {tr[:300]}"
        elif r["media_type"]:
            text = f"[{r['media_type']}] {text}"
        out.append(f"  [{r['date'][:10]}] {sender}: {text}")
    return "\n".join(out) if out else "  (нет сообщений)"


# ─── Pass 1 ──────────────────────────────────────────────

def run_pass1(conn, progress: dict, audit_suspicious: bool = False) -> list:
    """Resume по chat_id (а не по chunk-номеру). Pending = все - decided.

    audit_suspicious=True — режим точечного аудита:
    подозрительные чаты (category в 'silent', 'borderline', NULL или 'work'
    с last_message_at > 90 дней назад) выкидываются из decided_ids
    и попадают обратно в pending для переклассификации.
    Это предотвращает накопление ошибок без дорогого --reset.
    """
    rows = fetch_dialogs_meta(conn)
    decisions = list(progress.get("pass1_decisions", []))
    decided_ids = {d["chat_id"] for d in decisions}

    if audit_suspicious:
        sus_ids = set()
        for r in rows:
            cat = (r["cat"] or "").lower()
            # Устаревшая 'silent' категория (от Gemini Flash Lite до recat)
            # или borderline (Опус не уверен) или пустая
            if cat in ("silent", "borderline", "(null)", "", "_pass1_failed"):
                sus_ids.add(r["chat_id"])
        before = len(decided_ids)
        decided_ids -= sus_ids
        cleared = before - len(decided_ids)
        print(f"\n🔍 AUDIT-SUSPICIOUS: переклассифицируем {cleared} подозрительных "
              f"(category в silent/borderline/null/_pass1_failed)")
        # Также убираем их из decisions список чтобы не было дублей
        decisions = [d for d in decisions if d["chat_id"] not in sus_ids]

    pending = [r for r in rows if r["chat_id"] not in decided_ids]
    print(f"\n=== ПРОХОД 1: триаж {len(rows)} диалогов "
          f"(уже сделано: {len(decided_ids)}, осталось: {len(pending)}) ===")
    if not pending:
        print("✅ Все классифицировано")
        return decisions

    chunks = [pending[i:i+CHUNK_SIZE_PASS1] for i in range(0, len(pending), CHUNK_SIZE_PASS1)]
    print(f"Chunks: {len(chunks)} (по {CHUNK_SIZE_PASS1})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    failed_count = 0

    for i, chunk in enumerate(chunks, 1):
        chat_lines = "\n".join(fmt_pass1_line(r) for r in chunk)
        prompt = (PROMPT_PASS1
                  .replace("__CHUNK__", str(i)).replace("__TOTAL__", str(len(chunks)))
                  .replace("__COUNT__", str(len(chunk))).replace("__CHATS__", chat_lines))
        print(f"\n[{i}/{len(chunks)}] {len(chunk)} чатов, {len(prompt):,} chars (~{len(prompt)//4:,} токенов)…")
        Path(OUT / f"recat_pass1_chunk_{i}_prompt_{ts}.txt").write_text(prompt, encoding="utf-8")

        data = call_claude(prompt, retries=2)
        if not data or not data.get("decisions"):
            print(f"  ❌ chunk {i} провалился — попробуем заново в следующий запуск")
            failed_count += 1
            # Не помечаем в БД — оставим в pending. При новом запуске попадут заново.
            # Если упало много подряд — лучше подождать и перезапустить скрипт целиком.
            if failed_count >= 3:
                print(f"\n⚠️  3 подряд chunk-а провалились — Claude видимо тормозит или auth issue.")
                print(f"    Останавливаю pass1, чтобы не мучить API. Запусти ту же команду снова через 5 мин.")
                save_progress(progress)
                return decisions
            continue

        chunk_decisions = data["decisions"]
        applied = 0
        for d in chunk_decisions:
            cid = d.get("chat_id"); cat = d.get("new_category")
            if not cid or not cat: continue
            if cid in decided_ids: continue
            conn.execute("UPDATE dialogs SET category=? WHERE chat_id=?", (cat, cid))
            decisions.append({"chat_id": cid, "new_category": cat,
                             "reason": d.get("reason", "")})
            decided_ids.add(cid)
            applied += 1
        conn.commit()
        progress["pass1_decisions"] = decisions
        save_progress(progress)
        print(f"  ✓ {applied} → БД (всего: {len(decisions)})")

    # Сводка
    by_cat = {}
    for d in decisions:
        c = d.get("new_category", "?")
        by_cat[c] = by_cat.get(c, 0) + 1
    print(f"\n=== ПРОХОД 1 ИТОГ: {len(decisions)} классифицировано "
          f"(failed chunks: {failed_count}) ===")
    for k, v in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    return decisions


# ─── Pass 2 ──────────────────────────────────────────────

def run_pass2(conn, progress: dict) -> list:
    # Берём все чаты которые после pass1 = work / silent_team / borderline
    rows = conn.execute("""
        SELECT chat_id FROM dialogs
        WHERE category IN ('work','silent_team','borderline')
    """).fetchall()
    chat_ids = [r[0] for r in rows]
    print(f"\n=== ПРОХОД 2: глубокий по {len(chat_ids)} чатам ===")
    if not chat_ids:
        return []

    # Подгружаем deep data
    rows_full = conn.execute(f"""
        SELECT chat_id, COALESCE(username,'') AS u, COALESCE(title,'') AS title,
               chat_type, COALESCE(category,'') AS cat
        FROM dialogs WHERE chat_id IN ({','.join('?'*len(chat_ids))})
    """, chat_ids).fetchall()

    chunks = [rows_full[i:i+CHUNK_SIZE_PASS2] for i in range(0, len(rows_full), CHUNK_SIZE_PASS2)]
    print(f"Chunks: {len(chunks)} (по {CHUNK_SIZE_PASS2})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    completed = set(progress.get("pass2_completed_chunks", []))
    decisions = list(progress.get("pass2_decisions", []))
    decided_ids = {d["chat_id"] for d in decisions}

    # Добавим колонку triage_project если нет
    try:
        conn.execute("ALTER TABLE dialogs ADD COLUMN triage_project TEXT")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e): raise

    for i, chunk in enumerate(chunks, 1):
        if i in completed:
            print(f"[{i}/{len(chunks)}] SKIP")
            continue
        # Формируем большой prompt с last 150 msgs per chat
        sections = []
        for r in chunk:
            sample = fetch_chat_sample(conn, r["chat_id"], LAST_MSGS_PASS2)
            section = (
                f"\n--- chat_id={r['chat_id']} | type={r['chat_type']} | "
                f"cat_pass1={r['cat']} | @{r['u']} | title={(r['title'] or '?')[:60]} ---\n"
                f"{sample}\n"
            )
            sections.append(section)
        prompt = (PROMPT_PASS2
                  .replace("__COUNT__", str(len(chunk)))
                  .replace("__CHATS_DEEP__", "\n".join(sections)))
        print(f"\n[{i}/{len(chunks)}] {len(chunk)} чатов, {len(prompt):,} chars (~{len(prompt)//4:,} токенов)…")
        Path(OUT / f"recat_pass2_chunk_{i}_prompt_{ts}.txt").write_text(prompt, encoding="utf-8")

        data = call_claude(prompt, retries=1)
        if not data or not data.get("decisions"):
            print(f"  ❌ chunk {i} провалился")
            continue

        chunk_decisions = data["decisions"]
        for d in chunk_decisions:
            cid = d.get("chat_id")
            if not cid or cid in decided_ids: continue
            final_cat = d.get("final_category") or d.get("new_category")
            project = d.get("project")
            relation = d.get("relation")
            updates = []
            params = []
            if final_cat:
                updates.append("category=?"); params.append(final_cat)
            if project:
                updates.append("triage_project=?"); params.append(project)
            if relation:
                updates.append("relation=COALESCE(NULLIF(?,''),relation)")
                params.append(relation)
            if updates:
                params.append(cid)
                conn.execute(f"UPDATE dialogs SET {', '.join(updates)} WHERE chat_id=?", params)
            decisions.append({
                "chat_id": cid, "final_category": final_cat,
                "project": project, "relation": relation,
                "key_role": d.get("key_role", ""), "reason": d.get("reason", ""),
            })
            decided_ids.add(cid)
        conn.commit()
        completed.add(i)
        progress["pass2_completed_chunks"] = sorted(completed)
        progress["pass2_decisions"] = decisions
        save_progress(progress)
        print(f"  ✓ {len(chunk_decisions)} → БД (всего pass2: {len(decisions)})")

    # Сводка
    by_proj = {}
    for d in decisions:
        p = d.get("project", "?")
        by_proj[p] = by_proj.get(p, 0) + 1
    print(f"\n=== ПРОХОД 2 ИТОГ: {len(decisions)} расклассифицировано по проектам ===")
    for k, v in sorted(by_proj.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    return decisions


# ─── Main ────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pass1-only", action="store_true")
    p.add_argument("--pass2-only", action="store_true")
    p.add_argument("--reset", action="store_true",
                   help="удалить progress, начать заново")
    p.add_argument("--audit-suspicious", action="store_true",
                   help="переклассифицировать подозрительные категории (silent/borderline/null) "
                        "без полного reset")
    args = p.parse_args()

    if args.reset:
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()
            print(f"🗑 Удалён {PROGRESS_FILE}")
        else:
            print(f"📂 Прогресс-файл не существовал")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    progress = load_progress()
    print(f"Progress: pass1 chunks done={len(progress.get('pass1_completed_chunks',[]))}, "
          f"pass2 chunks done={len(progress.get('pass2_completed_chunks',[]))}")

    if not args.pass2_only:
        run_pass1(conn, progress, audit_suspicious=args.audit_suspicious)
    if not args.pass1_only:
        run_pass2(conn, progress)

    # Финальный отчёт
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final = {
        "ts": ts,
        "pass1_decisions": progress.get("pass1_decisions", []),
        "pass2_decisions": progress.get("pass2_decisions", []),
    }
    Path(OUT / f"recat_all_final_{ts}.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📦 Final: {OUT}/recat_all_final_{ts}.json")

    # macOS notification + звук когда всё закончится
    try:
        n_pass1 = len(final["pass1_decisions"])
        n_pass2 = len(final["pass2_decisions"])
        msg = f"recategorize_all готов: pass1={n_pass1}, pass2={n_pass2}"
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "TG Analiz" sound name "Glass"'
        ], check=False)
    except Exception:
        pass

    # Сколько work без deep_dive
    rows = conn.execute("""
        SELECT chat_id, COALESCE(username,'') AS u, title FROM dialogs
        WHERE category='work' AND ai_deep_dive_at IS NULL AND last_message_at>='2026-01-28'
    """).fetchall()
    print(f"\n🆕 Work-чатов без deep_dive (нужен догон): {len(rows)}")
    for r in rows[:50]:
        u = f"@{r['u']}" if r["u"] else ""
        print(f"  {r['chat_id']:>15} | {u:<25} | {(r['title'] or '?')[:50]}")
    conn.close()


if __name__ == "__main__":
    main()
