#!/bin/bash
# Daily pipeline для TG Analiz — idempotent, catch-up, безопасный.
#
# Использование:
#   ./daily.sh                  # стандарт (catch-up с last successful run)
#   ./daily.sh --skip-collect   # пропустить шаг collector (если только что запускали)
#   ./daily.sh --since 2026-04-25  # принудительно с этой даты
#
# Логика:
#   - Состояние шагов в .pipeline_state.json (last successful run каждого шага)
#   - Каждый шаг update_state ТОЛЬКО при успехе (rc=0)
#   - При повторном запуске после краха — продолжает с того где упал
#   - since для collector/transcribe = last_successful date (если нет — 2 дня назад)

set -e  # прервать при ошибке (можно отменить через `|| true` per-step)
cd "$(dirname "$0")"

STATE_FILE=".pipeline_state.json"
LOCK_DIR=".pipeline.lock.d"

# ─── Lockfile (portable, работает на macOS/Linux): атомарный mkdir ────
if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo $$ > "$LOCK_DIR/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
else
  RUNNING_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "?")
  # Проверяем жив ли процесс
  if [ "$RUNNING_PID" != "?" ] && kill -0 "$RUNNING_PID" 2>/dev/null; then
    echo "❌ daily.sh уже запущен (PID=$RUNNING_PID). Жди завершения."
    exit 1
  else
    echo "⚠ Stale lock от мёртвого процесса PID=$RUNNING_PID. Удаляю и стартую."
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
    echo $$ > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
  fi
fi

# ─── Helpers ──────────────────────────────────────────────

# Get last_successful date for a step (default: 2 days ago)
get_state() {
  local step="$1"
  if [ -f "$STATE_FILE" ]; then
    python3 -c "
import json
try:
    s = json.load(open('$STATE_FILE'))
    print(s.get('$step', ''))
except Exception:
    print('')
"
  fi
}

# Update state file with current timestamp
update_state() {
  local step="$1"
  python3 -c "
import json, os
from datetime import datetime
s = {}
if os.path.exists('$STATE_FILE'):
    try: s = json.load(open('$STATE_FILE'))
    except Exception: s = {}
s['$step'] = datetime.now().isoformat()
with open('$STATE_FILE', 'w') as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
"
  echo "✅ state updated: $step"
}

# Date N days ago (yyyy-mm-dd)
days_ago() {
  date -v-"$1"d +%Y-%m-%d
}

# Получить since для шага: либо last_successful_date, либо 2 дня назад
since_for() {
  local step="$1"
  local last=$(get_state "$step")
  if [ -z "$last" ]; then
    days_ago 2
  else
    # Берём только дату YYYY-MM-DD (без времени)
    echo "${last:0:10}"
  fi
}

# Argument parsing
SKIP_COLLECT=false
FORCED_SINCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-collect) SKIP_COLLECT=true; shift ;;
    --since) FORCED_SINCE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

START_TS=$(date +%s)
echo "═══════════════════════════════════════════════════"
echo "🚀 Daily pipeline start: $(date)"
echo "═══════════════════════════════════════════════════"

# ─── PHASE 1: Telegram fetch (sequential, single Telethon session) ──

if [ "$SKIP_COLLECT" = "false" ]; then
  echo ""
  echo "─── 1.1 collector --dialogs ──────────────────────────"
  python3 collector.py --dialogs && update_state "collector_dialogs"

  SINCE_ACTIVE="${FORCED_SINCE:-$(since_for collector_active)}"
  echo ""
  echo "─── 1.2 collector --active --since $SINCE_ACTIVE ────"
  python3 collector.py --active --since "$SINCE_ACTIVE" --no-voice && update_state "collector_active"

  SINCE_TR="${FORCED_SINCE:-$(since_for transcribe)}"
  echo ""
  echo "─── 1.3 transcribe_gemini --since $SINCE_TR ─────────"
  python3 transcribe_gemini.py --since "$SINCE_TR" --concurrency 10 && update_state "transcribe"
else
  echo ""
  echo "⏭ SKIP collector (флаг --skip-collect)"
fi

# ─── PHASE 1.5: Collect answers from subordinates (parsed from incoming) ──

echo ""
echo "─── 1.5 collect_subordinate_answers ─────────────────"
python3 collect_subordinate_answers.py && update_state "collect_subordinate_answers"

# ─── PHASE 2: Audit categories (только подозрительные) ────────────────

echo ""
echo "─── 2. recategorize_all --audit-suspicious ──────────"
python3 recategorize_all.py --audit-suspicious --pass1-only && update_state "recategorize"

# ─── PHASE 3: Deep_dive parallel (no Telegram lock — все --skip-fetch) ─

echo ""
echo "─── 3. repair_parallel --workers 6 ──────────────────"
python3 repair_parallel.py --workers 6 && update_state "repair"

# ─── PHASE 3.5: Classify NEW disputed (criticality + priority + owner + dedup) ─

echo ""
echo "─── 3.5 classify_disputed (only new unclassified) ────"
python3 classify_disputed.py --workers 4 && update_state "classify_disputed"

# ─── PHASE 4: Final outputs ──────────────────────────────────────────

echo ""
echo "─── 3.6 import_sergei_answers (если Сергей заполнил critical_for_sergei.md) ──"
python3 import_sergei_answers.py && update_state "import_sergei"

echo ""
echo "─── 4.0 export critical_for_* (anastasya/sergei/subordinates) ──"
python3 export_critical_questions.py && update_state "export_critical"

echo ""
echo "─── 4.1 regenerate_review_md ────────────────────────"
python3 regenerate_review_md.py && update_state "regenerate"

# Brief: --since по дням с последнего успешного брифа (макс 7д)
LAST_BRIEF=$(get_state brief)
if [ -z "$LAST_BRIEF" ]; then
  BRIEF_SINCE="2d"
else
  # Считаем дни с last_brief
  DAYS=$(python3 -c "
from datetime import datetime
last = datetime.fromisoformat('$LAST_BRIEF')
days = (datetime.now() - last).days
print(max(1, min(7, days+1)))
")
  BRIEF_SINCE="${DAYS}d"
fi

echo ""
echo "─── 4.2 morning_brief --since $BRIEF_SINCE ──────────"
python3 morning_brief.py --since "$BRIEF_SINCE" && update_state "brief"

# ─── Final ───────────────────────────────────────────────────────────

END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))
echo ""
echo "═══════════════════════════════════════════════════"
echo "✅ Daily pipeline DONE за $((ELAPSED / 60))m $((ELAPSED % 60))s"
echo "═══════════════════════════════════════════════════"
python3 tg_notify.py send "✅ daily.sh пайплайн прошёл за $((ELAPSED / 60))m $((ELAPSED % 60))s. Бриф в TG."
