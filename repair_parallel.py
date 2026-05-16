#!/usr/bin/env python3
"""
Параллельный repair: incremental_dive для stale + full re-dive для неполных.
Использует 2 OAuth токена (sess2 + sess3), 6 потоков (по 3 на токен).

Запуск:
  python3 repair_parallel.py                    # все stale + неполные
  python3 repair_parallel.py --workers 6        # параллелизм
  python3 repair_parallel.py --only-incomplete  # только 5 неполных
"""
import argparse
import json
import os
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

# Загрузка токенов из .tokens.env
TOKENS = []
if TOKENS_FILE.exists():
    for line in TOKENS_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("CLAUDE_OAUTH_TOKEN_") and "=" in line:
            _, val = line.split("=", 1)
            if val.strip():
                TOKENS.append(val.strip())
print(f"Загружено OAuth токенов: {len(TOKENS)}")
if not TOKENS:
    print("⚠️ Нет токенов в .tokens.env — будет использоваться текущая сессия")
    TOKENS = [None]


REQUIRED_KEYS = [
    "who_and_relation", "direction", "org_chain", "top_topics",
    "last_two_weeks", "open_tasks", "debts_and_obligations", "finance",
    "next_actions", "format_review", "tldr", "user_notes_ready",
    "timeline", "disputed", "confidence_overall",
]


def get_targets(only_incomplete: bool = False) -> list[tuple[int, str, str]]:
    """Возвращает список (chat_id, title, mode) для repair.
    mode: 'incremental' для stale, 'full' для неполных или новых."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    targets = []

    # Неполные (отсутствует user_notes_ready / timeline / etc)
    for r in conn.execute("""
        SELECT chat_id, title, ai_deep_dive_json FROM dialogs
        WHERE ai_deep_dive_json IS NOT NULL AND category IN ('work','silent_team')
    """):
        try: j = json.loads(r["ai_deep_dive_json"])
        except: continue
        missing = [k for k in REQUIRED_KEYS if k not in j or j[k] is None]
        if missing:
            targets.append((r["chat_id"], r["title"] or "?", "full"))

    if only_incomplete:
        return targets

    # Stale — есть новые сообщения после ai_deep_dive_at
    existing_ids = {t[0] for t in targets}
    for r in conn.execute("""
        SELECT d.chat_id, d.title FROM dialogs d
        WHERE d.category IN ('work','silent_team')
          AND d.last_message_at >= date('now','-30 day')
          AND (d.ai_deep_dive_at IS NULL OR d.last_message_at > d.ai_deep_dive_at)
        ORDER BY (SELECT COUNT(*) FROM messages WHERE chat_id=d.chat_id AND date>='2026-01-28') DESC
    """):
        if r["chat_id"] in existing_ids: continue
        targets.append((r["chat_id"], r["title"] or "?", "incremental"))

    return targets


def repair_one(chat_id: int, title: str, mode: str, token: str | None,
               worker_id: int) -> dict:
    """Запускает один repair через subprocess. Возвращает dict с результатом."""
    start = time.time()
    env = os.environ.copy()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if mode == "full":
        # --skip-fetch — НЕ дёргать Telegram (данные уже в БД из прошлых run'ов).
        # Это обходит session lock — full можно запускать параллельно.
        cmd = ["python3", "-u", "agent_contact_deep_dive.py",
               "--chat-id", str(chat_id), "--months", "3", "--skip-fetch"]
    else:
        cmd = ["python3", "-u", "incremental_dive.py", "--chat-id", str(chat_id)]
    log_file = BASE / "logs" / f"repair_{chat_id}.log"
    log_file.parent.mkdir(exist_ok=True)
    try:
        result = subprocess.run(
            cmd, cwd=BASE, env=env,
            capture_output=True, text=True, timeout=2400,
        )
        log_file.write_text(result.stdout + "\n--- STDERR ---\n" + result.stderr)
        ok = result.returncode == 0
    except subprocess.TimeoutExpired:
        log_file.write_text("TIMEOUT")
        ok = False
    dur = time.time() - start
    status = "✅" if ok else "❌"
    print(f"  [w{worker_id}] {status} {chat_id} ({title[:30]}) — {mode} {dur:.0f}s",
          flush=True)
    return {"chat_id": chat_id, "title": title, "mode": mode, "ok": ok, "dur": dur}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=6,
                   help="параллельных воркеров (default 6 = по 3 на токен)")
    p.add_argument("--only-incomplete", action="store_true",
                   help="только 5 неполных, без stale")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    targets = get_targets(only_incomplete=args.only_incomplete)
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        print("Нечего делать — всё актуально")
        return

    # Распределение токенов round-robin
    n_workers = min(args.workers, len(targets))
    print(f"\n=== REPAIR PARALLEL: {len(targets)} чатов, {n_workers} воркеров ===")
    print(f"Tokens: {len(TOKENS)}, по {n_workers // max(len(TOKENS),1)} воркеров на токен\n")

    print("Список:")
    for i, (cid, title, mode) in enumerate(targets, 1):
        print(f"  {i:>3}. [{mode}] {cid:>15} | {title[:50]}")
    print()

    results = []
    # ThreadPoolExecutor: каждый task запускается с round-robin tokenом
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {}
        for i, (cid, title, mode) in enumerate(targets):
            token = TOKENS[i % len(TOKENS)]
            worker_id = (i % n_workers) + 1
            f = executor.submit(repair_one, cid, title, mode, token, worker_id)
            futures[f] = (cid, title, mode)
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                cid, title, mode = futures[f]
                print(f"  ❌ {cid} crashed: {e}")
                results.append({"chat_id": cid, "ok": False, "error": str(e)})

    # Сводка
    ok = sum(1 for r in results if r.get("ok"))
    fail = len(results) - ok
    total_dur = sum(r.get("dur", 0) for r in results)
    print(f"\n=== ИТОГО ===")
    print(f"  OK: {ok}, FAIL: {fail}")
    print(f"  Суммарно работы: {total_dur:.0f}s ({total_dur/60:.1f} мин)")
    print(f"  Реальное время: ~{total_dur/n_workers:.0f}s ({total_dur/n_workers/60:.1f} мин)")
    if fail:
        print(f"\n  Failed chats:")
        for r in results:
            if not r.get("ok"):
                print(f"    {r['chat_id']} — см. logs/repair_{r['chat_id']}.log")

    # Notify TG
    try:
        sys.path.insert(0, str(BASE))
        import tg_notify
        tg_notify.send(f"✅ repair_parallel готов: {ok}/{len(results)} OK, {fail} fail. "
                      f"Время: {total_dur/n_workers/60:.0f} мин на {n_workers} воркерах.")
    except Exception:
        pass


if __name__ == "__main__":
    main()
