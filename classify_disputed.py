"""
Классификатор disputed_questions через Opus 4.7 1M.

Один батч-запрос на контакт. Опус возвращает по каждому вопросу:
  • criticality: critical | noise
  • priority: high | medium | low (только для critical)
  • test: T1..T5 | none
  • answer_owner: anastasya | sergei | subordinate:NAME | nobody_knows
  • duplicate_of_id: int | null  (если этот вопрос — дубль другого ID из этого же батча)
  • deadline: YYYY-MM-DD | null
  • impact_estimate: <текст в свободной форме> | null
  • reason: 1 фраза почему так

5 ТЕСТОВ CRITICAL (любой → critical):
  T1. Финансовый импакт — ФОТ, бюджеты, маржа, расходы
  T2. Орг-структурный импакт — иерархия, статус работает/уволен, зона ответственности
  T3. Стратегический сигнал ТОПов — Влад/Тимур/Никита Доник/Кочерев
  T4. Блокер решения СЕЙЧАС — Сергей не двигает работу из-за пробела
  T5. Просрочка с ущербом — >7 дней + сигнал потери

Edge case 50/50 → NOISE.
nobody_knows → NOISE автоматически.
duplicate_of_id != null → NOISE (но с указателем на каноничный).
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
TOKENS_FILE = BASE / ".tokens.env"
CLAUDE_MODEL = "claude-opus-4-7[1m]"
CLAUDE_TIMEOUT = 1800

TOKENS = []
for line in TOKENS_FILE.read_text().splitlines():
    line = line.strip()
    if line.startswith("CLAUDE_OAUTH_TOKEN_") and "=" in line:
        _, val = line.split("=", 1)
        if val.strip():
            TOKENS.append(val.strip())
print(f"OAuth tokens: {len(TOKENS)}")


PROMPT = """Ты обогащаешь "спорные вопросы" (disputed) в системе TG Work Map для руководителя направления Сергея Дышканта.

ЦЕЛЬ системы: давать Сергею (1) точный утренний бриф «что важно сегодня», (2) готовый отчёт для CEO Влада, (3) живую карту работы. Disputed — это пробелы понимания. Закрываются ответами: голосом от Сергея, голосовым от Anastasya, или письменным запросом подчинённому.

Твоя задача: для каждого вопроса определить — нужно ли его реально закрывать, и если да, то с каким приоритетом и кто отвечает.

═══ 5 ТЕСТОВ CRITICAL ═══
Вопрос critical если он проходит ХОТЯ БЫ ОДИН тест:

T1. ФИНАНСОВЫЙ ИМПАКТ — ФОТ, бюджеты, маржа, расходы. «ЗП X?», «формула мотивации?», «$1500 — что?», «себес лида?», «когда повышение?».

T2. ОРГ-СТРУКТУРНЫЙ ИМПАКТ — иерархия, статус работает/уволен, зона ответственности. «X подчиняется Y или Z?», «X — peer или подчинённый?», «X уволен или на паузе?», «формальная роль X — менеджер/тимлид?».

T3. СТРАТЕГИЧЕСКИЙ СИГНАЛ ТОПОВ — реплика Влада @bjlyd / Тимура Гилимзянова / Никиты Доника / Андрея Кочерева, в которой может скрываться ультиматум, формализация роли, новое направление, закрытие проекта. «Влад сказал shut down к Х — это жёстко?», «1М₽ — разовый или новый оклад?», «Указание Х — приказ или мысль?»

T4. БЛОКЕР РЕШЕНИЯ СЕЙЧАС — Сергей сейчас не двигает работу именно из-за пробела. Не «приятно знать», а «без этого завис». «Продолжать гипотезу Х или закрывать?», «Запускать ли Y?», «Какой ОКВЭД?», «Брать ли клиента?».

T5. ПРОСРОЧКА С УЩЕРБОМ — задача >7 дней + сигнал что висение создаёт ущерб (деньги, лиды, демотивация, заморозка проекта). «X работает БЕЗ согласованной мотивации, риск ухода», «КК-3 не настроен, потеря Y лидов».

═══ ПРИОРИТЕТ (только для critical) ═══

HIGH = горит. Один из:
  • Дедлайн в ближайшие 7 дней
  • Финимпакт >1М ₽ или риск потери ключевого подчинённого
  • Влад/Тимур/Доник прямо требуют ответ
  • Блокер запуска/остановки проекта прямо сейчас

MEDIUM = важно эту-следующую неделю. Один из:
  • Влияет на ФОТ или маржу проекта на >100К ₽
  • Дедлайн 7-30 дней
  • Будет нужно для отчёта Владу в ближайшие 2 недели

LOW = важно но не горит. Один из:
  • Формализация роли через буткемп / встречу через 1+ месяц
  • Импакт <100К ₽
  • Стратегический фон без жёсткого срока

═══ ВЛАДЕЛЕЦ ОТВЕТА ═══

`anastasya` — операционка, ЗП, статусы команды, оплаты, кто чем занят, кадровые статусы (уволен/работает), счета, бюджеты, личные дела Сергея. Anastasya — главный operating partner Сергея, знает 80% операционки.

`sergei` — стратегические решения которые знает только сам Сергей: формализация ролей, согласование с Владом 1-на-1, его собственные планы, выбор куда идти проектом.

`subordinate:USERNAME` — статусы конкретных задач у конкретного подчинённого. Например `subordinate:roma` для статуса vi-heat-map; `subordinate:viktoriya_potapova` для статуса исходов.

`nobody_knows` — историческая реконструкция, мелочь без последствий, давно ушедшее. → автоматически noise.

═══ ДУБЛИКАТЫ ═══

Если в этом батче несколько вопросов про ОДНУ И ТУ ЖЕ ТЕМУ (например 5 вопросов про ЗП Климина или 3 про подчинение Андрея):
  • один — каноничный (с самым полным контекстом / самым свежим)
  • остальные → criticality=noise, duplicate_of_id=ID каноничного
  • в reason: "duplicate of question N: <тема>"

═══ ИЗВЛЕЧЕНИЕ ДЕДЛАЙНА ═══

Если в reason или point упомянут дедлайн (15 мая, mid-may, к буткемпу 1-11 мая, через 2 недели и т.п.) — извлеки ISO-дату YYYY-MM-DD. Если расплывчато ("скоро", "к лету") — null.

═══ ИЗВЛЕЧЕНИЕ IMPACT ═══

Если из контекста понятно потенциальный денежный импакт — указывай: "1.2M ₽ потенциальная потеря", "245К ₽ ФОТ простоя", "shut down проекта 80K писем/день", "формализация на 10М₽ направление". Если непонятно — null.

═══ EDGE CASE RULE ═══
Если сомневаешься (50/50) → criticality=noise. Лучше пропустить чем затопить помощника шумом.

═══ КОНТЕКСТ КОНТАКТА ═══
{context_block}

═══ УЖЕ ОТВЕЧЕННЫЕ ВОПРОСЫ ПО ЭТОМУ КОНТАКТУ (для дедупа — не дублируй) ═══
{answered_block}

═══ ВОПРОСЫ ДЛЯ КЛАССИФИКАЦИИ ═══
Ниже {n_questions} вопросов. Для КАЖДОГО верни в JSON-объекте:
{{
  "id": <число>,
  "criticality": "critical" | "noise",
  "priority": "high" | "medium" | "low" | null,
  "test": "T1" | "T2" | "T3" | "T4" | "T5" | "none",
  "answer_owner": "anastasya" | "sergei" | "subordinate:USERNAME" | "nobody_knows",
  "duplicate_of_id": <id_из_этого_батча> | null,
  "deadline": "YYYY-MM-DD" | null,
  "impact_estimate": "<строка>" | null,
  "reason": "<1 короткое предложение почему>"
}}

ВАЖНО: ответ ТОЛЬКО валидный JSON массив. Никакого markdown ```json. Только массив объектов.

Вопросы:
{questions_block}
"""


def call_claude(prompt: str, oauth_token: str | None = None) -> tuple[str, float]:
    env = os.environ.copy()
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "text"]
    t0 = time.time()
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT, env=env,
    )
    dt = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(f"claude exit={result.returncode}: {result.stderr[:500]}")
    return result.stdout.strip(), dt


def build_context(chat_id: int, conn) -> str:
    row = conn.execute("""
        SELECT COALESCE(title, ''), COALESCE(username, ''), COALESCE(category, ''),
               COALESCE(relation, ''), COALESCE(triage_project, ''),
               COALESCE(user_notes, ''), COALESCE(ai_deep_dive_json, '')
        FROM dialogs WHERE chat_id = ?
    """, (chat_id,)).fetchone()
    if not row:
        return f"chat_id={chat_id}: НЕТ В БД"

    title, username, cat, rel, proj, notes, dd_json = row
    pieces = [
        f"Контакт: {title} (@{username or '-'}, id={chat_id})",
        f"Категория: {cat} · Отношения: {rel} · Проект: {proj}",
    ]
    if notes:
        pieces.append(f"\nUSER_NOTES (АВТОРИТЕТНЫЕ ФАКТЫ Сергея, включая ответы Anastasya):\n{notes[:2500]}")

    if dd_json:
        try:
            dd = json.loads(dd_json)
            dd_summary = {}
            for k in ("who_and_relation", "direction", "org_chain", "finance"):
                v = dd.get(k) or dd.get(f"q1_{k}") or dd.get(f"q5_{k}") or dd.get(f"q6_{k}")
                if v:
                    dd_summary[k] = str(v)[:600]
            if dd_summary:
                pieces.append("\nDEEP_DIVE summary:")
                for k, v in dd_summary.items():
                    pieces.append(f"  {k}: {v}")
        except Exception:
            pass

    return "\n".join(pieces)


def build_answered_block(chat_id: int, conn) -> str:
    """Уже отвеченные вопросы по этому контакту (через --import) — для дедупа."""
    rows = conn.execute("""
        SELECT id, point, COALESCE(answer, '')
        FROM disputed_questions
        WHERE chat_id = ? AND answered = 1
        ORDER BY id
    """, (chat_id,)).fetchall()
    if not rows:
        return "(нет ранее отвеченных вопросов)"
    pieces = []
    for qid, point, answer in rows:
        pieces.append(f"  [#{qid}] Q: {point[:120]}")
        if answer:
            pieces.append(f"           A: {answer[:200]}")
    return "\n".join(pieces)


def classify_one_contact(chat_id: int, questions: list[dict], oauth_token: str) -> tuple[list[dict], float]:
    conn = sqlite3.connect(DB)
    try:
        ctx = build_context(chat_id, conn)
        answered = build_answered_block(chat_id, conn)
    finally:
        conn.close()

    q_lines = []
    for q in questions:
        q_lines.append(f"[{q['id']}] {q['point']}")
        if q.get('reason'):
            q_lines.append(f"      контекст: {q['reason']}")
        if q.get('raised_at'):
            q_lines.append(f"      дата: {q['raised_at'][:10]}")
        q_lines.append("")

    prompt = PROMPT.format(
        context_block=ctx,
        answered_block=answered,
        n_questions=len(questions),
        questions_block="\n".join(q_lines),
    )

    out, dt = call_claude(prompt, oauth_token=oauth_token)
    out = out.strip()
    if out.startswith("```"):
        out = re.sub(r"^```(?:json)?\s*", "", out)
        out = re.sub(r"\s*```\s*$", "", out)
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", out, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON array in response: {out[:300]}")
        result = json.loads(m.group(0))

    return result, dt


def get_pending_batches(only_chat_id=None, limit_contacts=None, only_unclassified=True):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    where = ["answered = 0"]
    if only_unclassified:
        where.append("criticality IS NULL")
    if only_chat_id is not None:
        where.append(f"chat_id = {only_chat_id}")
    sql = f"""
      SELECT id, chat_id, point, reason, raised_at
      FROM disputed_questions
      WHERE {' AND '.join(where)}
      ORDER BY chat_id, id
    """
    rows = cur.execute(sql).fetchall()
    conn.close()
    by_contact = {}
    for qid, cid, point, reason, raised in rows:
        by_contact.setdefault(cid, []).append({
            "id": qid, "point": point, "reason": reason, "raised_at": raised,
        })
    batches = list(by_contact.items())
    if limit_contacts:
        batches = batches[:limit_contacts]
    return batches


def write_results(results: list[dict]):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    valid_owners_prefix = ("anastasya", "sergei", "subordinate:", "nobody_knows")
    for r in results:
        crit = r.get("criticality")
        if crit not in ("critical", "noise"):
            continue
        prio = r.get("priority")
        if prio not in ("high", "medium", "low"):
            prio = None
        test = r.get("test", "none")
        owner = r.get("answer_owner") or "nobody_knows"
        if not str(owner).startswith(valid_owners_prefix):
            owner = "nobody_knows"
        dup = r.get("duplicate_of_id")
        if not isinstance(dup, int):
            dup = None
        deadline = r.get("deadline")
        if deadline and not re.match(r"^\d{4}-\d{2}-\d{2}", str(deadline)):
            deadline = None
        impact = r.get("impact_estimate")
        if impact in ("", "null"):
            impact = None
        reason = (r.get("reason") or "")[:500]
        full_reason = f"[{test}] {reason}" if test and test != "none" else reason

        cur.execute("""
            UPDATE disputed_questions
            SET criticality = ?, criticality_reason = ?, classified_at = ?,
                criticality_priority = ?, duplicate_of_id = ?, answer_owner = ?,
                deadline = ?, impact_estimate = ?
            WHERE id = ?
        """, (crit, full_reason, now, prio, dup, owner, deadline, impact, r["id"]))
    conn.commit()
    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit-contacts", type=int, default=0)
    p.add_argument("--chat-id", type=int, default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--reclassify", action="store_true")
    args = p.parse_args()

    batches = get_pending_batches(
        only_chat_id=args.chat_id,
        limit_contacts=args.limit_contacts or None,
        only_unclassified=not args.reclassify,
    )
    total_q = sum(len(qs) for _, qs in batches)
    print(f"Контактов: {len(batches)}, вопросов: {total_q}")
    if not batches:
        print("Нет вопросов для классификации.")
        return

    t0 = time.time()
    n_critical, n_noise, n_dup, n_failed = 0, 0, 0, 0
    by_priority = {"high": 0, "medium": 0, "low": 0}
    by_owner = {}

    def work(chat_id, questions, tok):
        try:
            res, dt = classify_one_contact(chat_id, questions, tok)
            return chat_id, res, dt, None
        except Exception as e:
            return chat_id, None, 0, str(e)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = []
        for i, (cid, qs) in enumerate(batches):
            tok = TOKENS[i % len(TOKENS)] if TOKENS else None
            futures.append(ex.submit(work, cid, qs, tok))

        for fut in as_completed(futures):
            cid, res, dt, err = fut.result()
            if err:
                print(f"  ✗ cid={cid}: {err[:120]}")
                n_failed += 1
                continue
            try:
                clean = []
                for item in res:
                    if not isinstance(item, dict):
                        continue
                    if "id" not in item or item.get("criticality") not in ("critical", "noise"):
                        continue
                    clean.append(item)
                write_results(clean)
                n_c = sum(1 for x in clean if x["criticality"] == "critical")
                n_n = sum(1 for x in clean if x["criticality"] == "noise")
                n_d = sum(1 for x in clean if x.get("duplicate_of_id"))
                n_critical += n_c
                n_noise += n_n
                n_dup += n_d
                for x in clean:
                    if x["criticality"] == "critical":
                        prio = x.get("priority") or "low"
                        if prio in by_priority:
                            by_priority[prio] += 1
                    o = x.get("answer_owner", "nobody_knows")
                    o_key = "subordinate" if str(o).startswith("subordinate:") else o
                    by_owner[o_key] = by_owner.get(o_key, 0) + 1
                print(f"  ✓ cid={cid:>15}: {len(clean):>3} класс ({n_c}c/{n_n}n, dup={n_d}) · {dt:.1f}s")
            except Exception as e:
                print(f"  ✗ cid={cid}: write failed: {e}")
                n_failed += 1

    elapsed = time.time() - t0
    print(f"\n=== DONE in {elapsed/60:.1f} min ===")
    print(f"  CRITICAL: {n_critical}  ({by_priority['high']}h / {by_priority['medium']}m / {by_priority['low']}l)")
    print(f"  NOISE:    {n_noise}  (включая {n_dup} дубликатов)")
    print(f"  Owners:")
    for o, n in sorted(by_owner.items(), key=lambda x: -x[1]):
        print(f"    {o:<20}: {n}")
    print(f"  failed:   {n_failed}")


if __name__ == "__main__":
    main()
