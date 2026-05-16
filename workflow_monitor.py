#!/usr/bin/env python3
"""
Daemon-мониторинг workflow.

Каждые 15 минут шлёт сводку через Telegram-бот:
- recategorize_all (PID + прогресс по log)
- dive_parallel (63 чата)
- watchdog
- БД статистика

Детектит завершения:
- recategorize done → сообщает + auto-запускает следующую волну deep_dive
  (для новых work которые появились после pass 2)
- dive_parallel done → сообщает
- final: всё готово → итоговая сводка

Запуск (после setup tg_notify):
  cd /Users/wsgp/SergD_TG_Analiz
  nohup python3 -u workflow_monitor.py > /tmp/workflow_monitor.log 2>&1 &
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"

# Импорт send из tg_notify
sys.path.insert(0, str(BASE))
import tg_notify

# Параметры
SUMMARY_INTERVAL = 15 * 60   # 15 минут
CHECK_INTERVAL = 30          # каждые 30 сек проверяем PIDs

# Эти PID Сергей запустил руками — обнаружим их по pattern в `ps`
PID_PATTERNS = {
    "recategorize_all": "recategorize_all.py",
    "dive_parallel": "dive_parallel.sh",
    "agent_contact_deep_dive": "agent_contact_deep_dive.py",
    "regenerate_review_md": "regenerate_review_md.py",
}

# Логи процессов
LOGS = {
    "recategorize_all": "/tmp/recat_all.log",
    "dive_parallel": "/tmp/dive_parallel.log",
}

STATE_FILE = BASE / ".workflow_monitor_state.json"


def find_pid(pattern: str) -> int | None:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True).strip()
        if out:
            # Берём первый PID (если несколько — это subprocess'ы)
            return int(out.split()[0])
    except subprocess.CalledProcessError:
        pass
    return None


def is_running(pid: int) -> bool:
    if not pid: return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def tail_log(path: str, n: int = 5) -> str:
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return "(нет лога)"


def db_stats() -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    out = {}
    out["total_dialogs"] = conn.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0]
    cats = conn.execute("""
        SELECT COALESCE(category,'(null)') AS cat, COUNT(*) AS n
        FROM dialogs GROUP BY category ORDER BY n DESC
    """).fetchall()
    out["by_category"] = {r["cat"]: r["n"] for r in cats}
    out["work_with_dive"] = conn.execute(
        "SELECT COUNT(*) FROM dialogs WHERE category='work' AND ai_deep_dive_at IS NOT NULL"
    ).fetchone()[0]
    out["work_pending_dive"] = conn.execute(
        "SELECT COUNT(*) FROM dialogs WHERE category='work' AND ai_deep_dive_at IS NULL "
        "AND last_message_at >= '2026-01-28'"
    ).fetchone()[0]
    out["silent_team_pending_dive"] = conn.execute(
        "SELECT COUNT(*) FROM dialogs WHERE category='silent_team' AND ai_deep_dive_at IS NULL"
    ).fetchone()[0]
    conn.close()
    return out


def recat_progress() -> dict:
    """Парсим лог recategorize_all для прогресса."""
    log = tail_log(LOGS["recategorize_all"], 200)
    out = {"pass1_chunks_done": 0, "pass1_chunks_total": "?",
           "pass2_chunks_done": 0, "pass2_chunks_total": "?",
           "phase": "?"}
    # Pass 1 progress
    p1 = re.findall(r'pass1_chunks_done=(\d+)', log)
    if p1: out["pass1_chunks_done"] = int(p1[-1])
    p1_total = re.findall(r'ПРОХОД 1.*Chunks: (\d+)', log)
    if p1_total: out["pass1_chunks_total"] = int(p1_total[-1])
    # Текущий chunk pass 1
    cur = re.findall(r'\[(\d+)/(\d+)\] (\d+) чатов', log)
    if cur:
        out["current_chunk"] = f"{cur[-1][0]}/{cur[-1][1]}"
    # Pass 2 detection
    if "ПРОХОД 2" in log:
        out["phase"] = "pass2"
        p2 = re.findall(r'\[(\d+)/(\d+)\] (\d+) чатов', log)
        if p2:
            # последние после "ПРОХОД 2"
            idx = log.rfind("ПРОХОД 2")
            after = log[idx:]
            p2_after = re.findall(r'\[(\d+)/(\d+)\] (\d+) чатов', after)
            if p2_after:
                out["current_chunk"] = f"{p2_after[-1][0]}/{p2_after[-1][1]}"
    elif "ПРОХОД 1" in log:
        out["phase"] = "pass1"
    return out


def dive_progress() -> dict:
    log = tail_log(LOGS["dive_parallel"], 200)
    out = {"current": "?", "total": "?", "done_chats": 0}
    cur = re.findall(r'\[(\d+)/\s*(\d+)\] chat_id=', log)
    if cur:
        out["current"] = cur[-1][0]
        out["total"] = cur[-1][1]
    # Сколько finished (Готово за или ✅)
    out["done_chats"] = len(re.findall(r'Готово за', log))
    return out


def build_summary() -> str:
    procs = {}
    for name, pat in PID_PATTERNS.items():
        pid = find_pid(pat)
        procs[name] = pid

    db = db_stats()
    recat = recat_progress()
    dive = dive_progress()

    lines = [f"📊 Workflow status ({datetime.now().strftime('%H:%M')})"]
    lines.append("")

    # Recategorize
    if procs.get("recategorize_all"):
        phase = recat.get("phase", "?")
        cur = recat.get("current_chunk", "?")
        lines.append(f"🔄 recategorize (PID {procs['recategorize_all']})")
        lines.append(f"   фаза: {phase} | chunk: {cur}")
    else:
        lines.append(f"✅ recategorize — DONE")

    # Dive parallel (63 чата)
    parallel_pid = find_pid("dive_parallel.sh")
    if parallel_pid:
        lines.append(f"🔄 dive_parallel (63 чата)")
        lines.append(f"   chat: {dive.get('current','?')}/{dive.get('total','?')} | done: {dive.get('done_chats',0)}")
    else:
        lines.append(f"✅ dive_parallel — DONE")

    # Wave 2
    wave2_pid = find_pid("dive_wave2.sh")
    if wave2_pid:
        lines.append(f"🔄 dive_wave2 (новые work после recat)")
    elif Path("/tmp/dive_wave2.log").exists():
        lines.append(f"✅ dive_wave2 — DONE")

    lines.append("")
    lines.append("📦 БД:")
    lines.append(f"   диалогов: {db['total_dialogs']}")
    cats = db["by_category"]
    work = cats.get("work", 0)
    silent = cats.get("silent_team", 0)
    border = cats.get("borderline", 0)
    other = sum(v for k, v in cats.items() if k not in ("work", "silent_team", "borderline"))
    lines.append(f"   work: {work} | silent_team: {silent} | borderline: {border} | прочее: {other}")
    lines.append(f"   work с deep_dive: {db['work_with_dive']}")
    lines.append(f"   work без deep_dive: {db['work_pending_dive']}")
    lines.append(f"   silent_team без deep_dive: {db['silent_team_pending_dive']}")

    return "\n".join(lines)


def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"recat_finished": False, "dive_finished": False,
            "next_dive_started": False, "started_at": datetime.now().isoformat()}


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))


def start_next_dive():
    """Запускает deep_dive для всех work + silent_team без ai_deep_dive_at.
    Использует start_new_session=True чтобы пережить выход monitor.
    ВАЖНО: вызывать только когда dive_parallel.sh ЗАВЕРШИЛСЯ — иначе Telethon session lock."""
    # Проверяем что dive_parallel не работает
    if find_pid("dive_parallel.sh"):
        print("[monitor] ⚠️ dive_parallel.sh ещё работает — НЕ запускаю wave 2 (Telethon lock)")
        return False
    print("[monitor] starting next dive wave…")
    script = """#!/bin/bash
cd /Users/wsgp/SergD_TG_Analiz
CHATS=$(sqlite3 tg_analiz.db "
SELECT chat_id FROM dialogs
WHERE category IN ('work','silent_team')
  AND ai_deep_dive_at IS NULL
  AND last_message_at >= '2026-01-28'
ORDER BY (SELECT COUNT(*) FROM messages WHERE chat_id=dialogs.chat_id) DESC")
count=$(echo "$CHATS" | wc -l)
[ -z "$CHATS" ] && { echo "═══ Нет новых чатов для dive ═══"; exit 0; }
echo "═══ DIVE WAVE 2: $count чатов ═══"
i=0
for cid in $CHATS; do
  i=$((i+1))
  echo "═══ [$i/$count] chat_id=$cid ═══"
  python3 -u agent_contact_deep_dive.py --chat-id "$cid" --months 3 2>&1 | tail -8
done
echo "═══ DIVE WAVE 2 DONE ═══"
osascript -e 'display notification "deep_dive wave 2 завершён" with title "TG Analiz" sound name "Glass"' 2>/dev/null
"""
    Path("/tmp/dive_wave2.sh").write_text(script)
    os.chmod("/tmp/dive_wave2.sh", 0o755)
    subprocess.Popen(
        ["/tmp/dive_wave2.sh"],
        stdout=open("/tmp/dive_wave2.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # ← переживёт выход monitor
    )
    print("[monitor] dive wave 2 started, log: /tmp/dive_wave2.log")
    return True


def main():
    print(f"=== Workflow Monitor started {datetime.now()} ===")
    # State machine:
    #  recat_done — recategorize_all.py больше не в pgrep
    #  parallel_done — dive_parallel.sh больше не в pgrep
    #  wave2_started — start_next_dive вернул True
    #  wave2_done — dive_wave2.sh больше не в pgrep
    state = load_state()
    for k in ["recat_done", "parallel_done", "wave2_started", "wave2_done"]:
        state.setdefault(k, False)

    last_summary = 0
    iteration = 0

    while True:
        iteration += 1
        now = time.time()

        # === Periodic summary ===
        if now - last_summary >= SUMMARY_INTERVAL or iteration == 1:
            summary = build_summary()
            print(f"\n[{datetime.now()}] sending summary…")
            ok = tg_notify.send(summary)
            print(f"  send: {'OK' if ok else 'FAIL'}")
            last_summary = now

        # === PIDs ===
        recat_pid = find_pid("recategorize_all.py")
        parallel_pid = find_pid("dive_parallel.sh")
        wave2_pid = find_pid("dive_wave2.sh")

        # === Detect: recategorize done ===
        if not state["recat_done"] and not recat_pid:
            state["recat_done"] = True
            save_state(state)
            print("[monitor] recategorize_all DONE")
            tg_notify.send("✅ recategorize_all завершён. Жду пока dive_parallel закончит — потом запущу wave 2 по новым work.")

        # === Detect: dive_parallel done ===
        if not state["parallel_done"] and not parallel_pid:
            state["parallel_done"] = True
            save_state(state)
            print("[monitor] dive_parallel.sh DONE")
            tg_notify.send("✅ dive_parallel (63 чата) завершён.")

        # === Trigger: запустить wave 2 когда recat И parallel ОБА done ===
        if (state["recat_done"] and state["parallel_done"]
                and not state["wave2_started"]):
            print("[monitor] both recat и parallel завершены — запускаю wave 2")
            ok = start_next_dive()
            if ok:
                state["wave2_started"] = True
                save_state(state)
                tg_notify.send("🚀 Запустил deep_dive wave 2 для новых work/silent_team.")
            else:
                # start_next_dive вернул False (parallel ещё работает или ошибка)
                print("[monitor] wave 2 не запустился, попробую через 30 сек")

        # === Detect: wave2 done — финал ===
        if state["wave2_started"] and not state["wave2_done"] and not wave2_pid:
            # Дополнительная проверка: дать ему время появиться в pgrep после Popen
            # (это срабатывает только если start_next_dive был >60 сек назад)
            time.sleep(5)
            wave2_pid = find_pid("dive_wave2.sh")
            if not wave2_pid:
                state["wave2_done"] = True
                save_state(state)
                final_db = db_stats()
                msg = (f"🎉 Workflow завершён!\n\n"
                       f"📦 Итог в БД:\n"
                       f"  диалогов: {final_db['total_dialogs']}\n"
                       f"  work: {final_db['by_category'].get('work',0)}\n"
                       f"  silent_team: {final_db['by_category'].get('silent_team',0)}\n"
                       f"  work с deep_dive: {final_db['work_with_dive']}\n"
                       f"  work без deep_dive: {final_db['work_pending_dive']}\n\n"
                       f"Дальше: regenerate_review_md → morning brief → отчёт Владу.")
                tg_notify.send(msg)
                print("[monitor] EVERYTHING DONE — exiting")
                sys.exit(0)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
