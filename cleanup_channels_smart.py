#!/usr/bin/env python3
"""
AI-классификация каналов (broadcast) на keep_ai / keep_neutral / remove
через Opus 4.7 1M.

Логика:
- Берёт все channels из БД (chat_type='channel')
- Для каждого: title, username, last 8 сообщений из канала (если есть)
- Опус 1M в один chunk: 50-80 каналов → JSON с decision + reason
- Сохраняет в новую колонку dialogs.cleanup_proposed_action
- Output: cleanup_channels_proposed.md (батчами по 100) с чекбоксами:
  [x] — AI предложил remove (по умолчанию помечен)
  [ ] — AI предложил keep (по умолчанию не помечен)
- Сергей бегло проверяет, корректирует
- python3 cleanup_apply.py --file cleanup_channels_proposed_001.md → unsubscribe

Запуск:
  python3 cleanup_channels_smart.py                     # стандарт (категории community/skip)
  python3 cleanup_channels_smart.py --categories community  # только community
  python3 cleanup_channels_smart.py --resume            # продолжить с прошлого run
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
TOKENS_FILE = BASE / ".tokens.env"

# Оба OAuth токена — параллелим по 2 воркера (по 1 на токен)
TOKENS = []
if TOKENS_FILE.exists():
    for line in TOKENS_FILE.read_text().splitlines():
        if line.startswith("CLAUDE_OAUTH_TOKEN_SESS") and "=" in line:
            t = line.split("=", 1)[1].strip()
            if t: TOKENS.append(t)
print(f"OAuth токенов: {len(TOKENS)}")
db_lock = threading.Lock()

CLAUDE_MODEL = "claude-opus-4-7[1m]"
CLAUDE_TIMEOUT = 1800
CHUNK_SIZE = 60  # каналов в одном запросе

PROMPT = """Ты — оператор-классификатор каналов Telegram для Сергея Дышканта (руководитель в Victory Agency, занимается ИИ-автоматизацией бизнес-процессов).

ЗАДАЧА: классифицировать каждый канал на одно из:
- **keep_ai** — про ИИ / нейросети / Claude / GPT / автоматизацию / промпты / AI-инструменты / LLM. Сергей это активно использует и слушает.
- **keep_useful** — околоtech: программирование, no-code, маркетинг (но не воронка-инфоцыган), бизнес-новости, IT, кибербезопасность, продукт, дизайн. Полезное пространство.
- **keep_lifestyle** — Бали / Тай / путешествия / спорт / автомобили (личный интерес Сергея). Оставить.
- **keep_local** — Сочи, Краснодар, Екатеринбург — города где у Сергея бизнес или часть жизни. Оставить.
- **remove** — маркетинг-инфоцыгане, лидген-курсы (Сергей этим занимается профессионально, не нужно подписок), спам-каналы с товарами, новостной шум, гороскопы, мотивашки, нейросеть-обзоры с очевидным контентом, биржи труда. Точно удалять.

ВАЖНО:
- Если в названии «Нейро…», «AI…», «GPT…», «ИИ…», «промпт» → скорее всего keep_ai (но проверь — иногда инфоцыгане прикрываются)
- Если «Лидогенерация», «Маркетинг», «Воронки» от ноунейм автора → remove (Сергей сам этим занимается)
- Если канал-агрегатор без авторской ценности → remove
- Кибертопор / уважаемые tech-ресурсы → keep_useful

ВХОД (chunk #__CHUNK__ of __TOTAL__, __COUNT__ каналов):
__CHANNELS__

ВЫХОД (строго JSON, без markdown):
{
  "decisions": [
    {"chat_id": <int>, "decision": "<keep_ai|keep_useful|keep_lifestyle|keep_local|remove>",
     "reason": "<1 короткая фраза почему>"},
    ... ВСЕ __COUNT__ каналов
  ]
}
"""


def fetch_channels(args) -> list:
    cats = [c.strip() for c in args.categories.split(",")]
    placeholders = ",".join("?" * len(cats))
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT chat_id, COALESCE(username,'') AS u, title, COALESCE(category,'?') AS cat,
               last_message_at,
               (SELECT COUNT(*) FROM messages WHERE chat_id=d.chat_id) AS msgs
        FROM dialogs d
        WHERE chat_type='channel'
          AND category IN ({placeholders})
        ORDER BY last_message_at DESC
    """, cats).fetchall()
    return rows


def fetch_sample(conn, chat_id: int, limit: int = 8) -> str:
    """Last N сообщения канала (preview)."""
    rows = conn.execute("""
        SELECT date, text FROM messages
        WHERE chat_id=? AND text IS NOT NULL AND length(text) > 20
        ORDER BY date DESC LIMIT ?
    """, (chat_id, limit)).fetchall()
    return " | ".join(r[1][:80].replace("\n", " ") for r in rows)


def call_claude(prompt: str, token: str | None = None) -> str | None:
    env = os.environ.copy()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    start = time.time()
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", CLAUDE_MODEL,
             "--no-session-persistence", "--output-format", "json"],
            input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0: return None
    try:
        env_data = json.loads(result.stdout)
        return env_data.get("result", "").strip()
    except Exception:
        return result.stdout.strip()


def process_chunk(idx: int, total: int, chunk: list, token: str) -> dict:
    """Один chunk-запрос. Возвращает dict с decisions или None."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    descs = []
    for r in chunk:
        sample = fetch_sample(conn, r["chat_id"], 5)
        u = f" @{r['u']}" if r["u"] else ""
        descs.append(
            f"chat_id={r['chat_id']} | {r['title'][:60]}{u} | "
            f"cat={r['cat']} | msgs={r['msgs']} | "
            f"last={r['last_message_at'][:10] if r['last_message_at'] else '?'}\n"
            f"  sample: {sample[:400]}"
        )
    conn.close()
    prompt = (PROMPT
              .replace("__CHUNK__", str(idx))
              .replace("__TOTAL__", str(total))
              .replace("__COUNT__", str(len(chunk)))
              .replace("__CHANNELS__", "\n\n".join(descs)))

    start = time.time()
    raw = call_claude(prompt, token=token)
    dur = time.time() - start
    if not raw:
        print(f"  [{idx}/{total}] ❌ claude failed ({dur:.0f}s)", flush=True)
        return {"idx": idx, "ok": False}
    data = parse_json(raw)
    if not data or not data.get("decisions"):
        print(f"  [{idx}/{total}] ❌ JSON parse failed", flush=True)
        return {"idx": idx, "ok": False}
    decisions = data["decisions"]

    # Apply to DB (with lock)
    with db_lock:
        c = sqlite3.connect(DB)
        for d in decisions:
            cid = d.get("chat_id"); dec = d.get("decision"); reas = d.get("reason", "")
            if not cid or not dec: continue
            c.execute("UPDATE dialogs SET cleanup_proposed_action=?, cleanup_proposed_reason=? WHERE chat_id=?",
                      (dec, reas, cid))
        c.commit()
        c.close()
    print(f"  [{idx}/{total}] ✓ {len(decisions)} → БД ({dur:.0f}s)", flush=True)
    return {"idx": idx, "ok": True, "decisions": decisions}


def parse_json(text: str):
    text = text.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif text.startswith("```"):
        text = text.split("```", 1)[1].split("```", 1)[0]
    s = text.find("{")
    if s < 0: return None
    depth = 0; in_str = False; esc = False
    for i in range(s, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == '"' and not esc: in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(text[s:i+1])
                except: return None
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--categories", default="community,skip,service",
                   help="comma-separated")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--resume", action="store_true",
                   help="не переобрабатывать тех у кого уже есть cleanup_proposed_action")
    args = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Колонка для решения (idempotent)
    try:
        conn.execute("ALTER TABLE dialogs ADD COLUMN cleanup_proposed_action TEXT")
        conn.execute("ALTER TABLE dialogs ADD COLUMN cleanup_proposed_reason TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    rows = fetch_channels(args)
    if args.resume:
        rows = [r for r in rows if not conn.execute(
            "SELECT cleanup_proposed_action FROM dialogs WHERE chat_id=?",
            (r["chat_id"],)).fetchone()[0]]
    if args.limit:
        rows = rows[:args.limit]
    print(f"Каналов для классификации: {len(rows)}")

    # Чанки
    chunks = [rows[i:i+CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
    print(f"Чанков: {len(chunks)} (по {CHUNK_SIZE})")

    # Параллельно через ThreadPool: по 1 воркер на токен
    n_workers = max(1, len(TOKENS))
    print(f"\nЗапускаю {n_workers} параллельных воркеров (по 1 на OAuth токен)…\n")
    all_decisions = []
    failed_chunks = 0

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {}
        for i, chunk in enumerate(chunks, 1):
            token = TOKENS[(i-1) % len(TOKENS)] if TOKENS else None
            f = executor.submit(process_chunk, i, len(chunks), chunk, token)
            futures[f] = i
        for f in as_completed(futures):
            try:
                res = f.result()
                if res.get("ok"):
                    all_decisions.extend(res["decisions"])
                else:
                    failed_chunks += 1
            except Exception as e:
                print(f"  ! chunk crashed: {e}")
                failed_chunks += 1

    # Stats
    print(f"\n=== ИТОГО ===")
    counters = {}
    for d in all_decisions:
        k = d.get("decision", "?")
        counters[k] = counters.get(k, 0) + 1
    for k, v in sorted(counters.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print(f"  failed chunks: {failed_chunks}")

    # Generate batch .md files (как в cleanup_proposal.py, но с AI-предложениями)
    print(f"\n=== Генерация cleanup_channels_proposed_*.md ===")
    proposed_rows = conn.execute("""
        SELECT chat_id, COALESCE(username,'') AS u, title, COALESCE(category,'?') AS cat,
               cleanup_proposed_action AS dec, cleanup_proposed_reason AS reas,
               last_message_at,
               (SELECT COUNT(*) FROM messages WHERE chat_id=d.chat_id) AS msgs
        FROM dialogs d
        WHERE chat_type='channel' AND cleanup_proposed_action IS NOT NULL
        ORDER BY
          CASE cleanup_proposed_action
            WHEN 'remove' THEN 0 WHEN 'keep_useful' THEN 1
            WHEN 'keep_lifestyle' THEN 2 WHEN 'keep_local' THEN 3
            WHEN 'keep_ai' THEN 4 ELSE 5
          END,
          last_message_at DESC
    """).fetchall()

    OUT_DIR = BASE / "cleanup_channels_batches"
    OUT_DIR.mkdir(exist_ok=True)
    for f in OUT_DIR.glob("*.md"):
        f.unlink()

    BATCH = 100
    batches = [proposed_rows[i:i+BATCH] for i in range(0, len(proposed_rows), BATCH)]
    for idx, batch in enumerate(batches, 1):
        lines = []
        lines.append(f"# Каналы для cleanup — batch {idx}/{len(batches)}")
        lines.append(f"\n_Сгенерировано {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n")
        lines.append("**Как это работает:**")
        lines.append("- AI (Opus 4.7 1M) уже предложил decision для каждого канала")
        lines.append("- `[x]` рядом с remove (по умолчанию помечены) — точно удалить")
        lines.append("- `[ ]` рядом с keep_* (по умолчанию НЕ помечены) — оставить")
        lines.append("- **Если AI ошибся** — переставь `[x]` ↔ `[ ]` вручную")
        lines.append("- Под каждым каналом видна причина решения AI\n")
        lines.append("**Применить (после твоей проверки):**")
        lines.append("```bash")
        lines.append(f"python3 cleanup_apply.py --file cleanup_channels_batches/cleanup_channels_{idx:03d}.md")
        lines.append("```\n---\n")

        # Группа по decision
        by_dec = {}
        for r in batch:
            by_dec.setdefault(r["dec"], []).append(r)
        for dec_label, dec_emoji in [
            ("remove", "🗑️"), ("keep_useful", "🛠️"),
            ("keep_lifestyle", "🌴"), ("keep_local", "📍"), ("keep_ai", "🤖"),
        ]:
            items = by_dec.get(dec_label, [])
            if not items: continue
            lines.append(f"\n## {dec_emoji} {dec_label} ({len(items)})\n")
            for r in items:
                u = f"@{r['u']}" if r["u"] else ""
                last = (r["last_message_at"] or "")[:10] or "?"
                check = "[x]" if dec_label == "remove" else "[ ]"
                lines.append(f"- {check} **{r['title'][:55]}** {u} `{r['chat_id']}`")
                lines.append(f"  · cat=`{r['cat']}` · last={last} · msgs={r['msgs']}")
                if r["reas"]:
                    lines.append(f"  · _AI: {r['reas'][:150]}_")

        path = OUT_DIR / f"cleanup_channels_{idx:03d}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  → {path}")

    print(f"\n✅ Создано {len(batches)} файлов в {OUT_DIR}/")
    print(f"\nПробеги первый файл:")
    print(f"  open {OUT_DIR}/cleanup_channels_001.md")


if __name__ == "__main__":
    main()
