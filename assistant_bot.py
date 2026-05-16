#!/usr/bin/env python3
"""
Универсальный TG бот для системы TG Work Map.

Использует существующий Victory Pixel Finder bot (token из .tg_notify.json).

Различает пользователей:
  • Сергей (531712920) — получает брифы (через tg_notify.py отдельно), может слать команды
  • Anastasya (716365720) — отвечает на critical-вопросы голосом или текстом
  • Прочие → отказ

Команды (для обоих или для Anastasya):
  /start        — приветствие
  /today        — текущий список critical (для Anastasya — её, для Сергея — все)
  /pending      — сколько вопросов ждёт ответа
  /done         — завершить сессию
  /brief        — (только Сергей) переотправить актуальный morning_brief

На любое НЕ-команда сообщение от Anastasya:
  → парсим через Opus 1M против её открытых critical → пишем в БД + в user_notes

Запуск (long-running):
  python3 assistant_bot.py

Для production — через launchd plist (см. /Library/LaunchAgents/com.sergd.assistant_bot.plist).
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
TG_NOTIFY = BASE / ".tg_notify.json"
TOKENS_FILE = BASE / ".tokens.env"
SESSION = BASE / "tg_access" / "assistant_bot"

import dotenv
dotenv.load_dotenv(BASE / ".env")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

# Bot token из существующего Victory Pixel Finder
CFG = json.loads(TG_NOTIFY.read_text())
BOT_TOKEN = CFG["token"]
SERGEI_USER_ID = int(CFG["chat_id"])  # 531712920

# Anastasya — известный user_id из БД
ANASTASYA_USER_ID = 716365720

# OAuth для Opus
TOKENS = []
for line in TOKENS_FILE.read_text().splitlines():
    line = line.strip()
    if line.startswith("CLAUDE_OAUTH_TOKEN_") and "=" in line:
        _, val = line.split("=", 1)
        if val.strip():
            TOKENS.append(val.strip())
DEFAULT_TOKEN = TOKENS[0] if TOKENS else None
CLAUDE_MODEL = "claude-opus-4-7[1m]"


# ═══ Helpers ═══

def call_claude(prompt: str) -> str:
    env = os.environ.copy()
    if DEFAULT_TOKEN:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = DEFAULT_TOKEN
    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "text"]
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            timeout=300, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"claude exit={result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


def get_open_for(owner_filter: str | None):
    """Возвращает открытые critical-вопросы.
    owner_filter='anastasya' — только её; None — все."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    where = ["q.answered = 0", "q.criticality = 'critical'", "q.duplicate_of_id IS NULL"]
    params = []
    if owner_filter:
        where.append("q.answer_owner = ?")
        params.append(owner_filter)
    rows = conn.execute(f"""
        SELECT q.id, q.point, q.reason, q.criticality_priority,
               q.deadline, q.impact_estimate, q.answer_owner,
               COALESCE(d.title, '?') AS title, COALESCE(d.username, '') AS username
        FROM disputed_questions q
        LEFT JOIN dialogs d ON d.chat_id = q.chat_id
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE q.criticality_priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
          d.title, q.id
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


PARSE_PROMPT = """Anastasya — operating partner Сергея — отвечает на критичные вопросы.
Её сообщение может закрывать один или несколько вопросов.

═══ ОТКРЫТЫЕ ВОПРОСЫ ═══
{questions_block}

═══ СООБЩЕНИЕ ANASTASYA ═══
{message}

═══ ПРАВИЛА ═══
- Если сообщение явно отвечает на конкретный #ID — извлеки ответ
- Если упоминает имя/тему без ID — найди подходящие вопросы по смыслу
- "не знаю" / "спроси у Сергея" — это валидный ответ
- Если сообщение НЕ относится ни к одному вопросу — верни []
- Один ответ может закрывать несколько вопросов (если они дублируют тему)

═══ ФОРМАТ (только JSON, никаких ```) ═══
[{{"id": <число>, "answer": "<текст>"}}, ...]
"""


def parse_anastasya_message(text: str) -> list[dict]:
    open_qs = get_open_for("anastasya")
    if not open_qs:
        return []
    q_lines = []
    for q in open_qs:
        line = f"#{q['id']} ({q['title'][:30]}) {q['point']}"
        if q.get('reason'):
            line += f"\n      контекст: {q['reason'][:200]}"
        q_lines.append(line)
    prompt = PARSE_PROMPT.format(
        questions_block="\n\n".join(q_lines),
        message=text,
    )
    out = call_claude(prompt).strip()
    if out.startswith("```"):
        out = re.sub(r"^```(?:json)?\s*", "", out)
        out = re.sub(r"\s*```\s*$", "", out)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", out, re.DOTALL)
        if m: return json.loads(m.group(0))
        return []


def write_answers_with_merge(parsed: list[dict]) -> tuple[int, str | None]:
    """Записывает в disputed + мерджит в user_notes контактов. Возвращает (n_written, addition_preview)."""
    if not parsed:
        return 0, None
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    now = datetime.now().isoformat()

    n_written = 0
    by_chat = {}
    for item in parsed:
        if not isinstance(item, dict): continue
        qid = item.get("id")
        ans = item.get("answer")
        if not qid or not ans: continue
        # найти chat_id
        row = cur.execute(
            "SELECT chat_id, point FROM disputed_questions WHERE id = ? AND answered = 0",
            (qid,)
        ).fetchone()
        if not row: continue
        cid, point = row
        cur.execute("""
            UPDATE disputed_questions
            SET answer = ?, answered = 1, answered_at = ?
            WHERE id = ?
        """, (ans, now, qid))
        if cur.rowcount > 0:
            n_written += 1
            by_chat.setdefault(cid, []).append((qid, point, ans))
    conn.commit()

    # Merge в user_notes для каждого затронутого контакта
    addition_preview = None
    for cid, items in by_chat.items():
        try:
            from collect_subordinate_answers import merge_answers_to_user_notes
            parsed_for_merge = [{"id": qid, "status": "answered", "answer": ans}
                                 for qid, _, ans in items]
            addition = merge_answers_to_user_notes(
                conn, cid, parsed_for_merge, oauth_token=DEFAULT_TOKEN, dry_run=False
            )
            if addition and not addition_preview:
                addition_preview = addition[:120]
        except Exception as e:
            print(f"merge failed for cid={cid}: {e}")

    conn.close()
    return n_written, addition_preview


# ═══ Bot setup ═══

from tg_proxy import get_tg_proxy
bot = TelegramClient(str(SESSION), API_ID, API_HASH, proxy=get_tg_proxy())


def is_authorized(user_id: int) -> str | None:
    """Возвращает 'sergei' / 'anastasya' / None."""
    if user_id == SERGEI_USER_ID: return "sergei"
    if user_id == ANASTASYA_USER_ID: return "anastasya"
    return None


@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    role = is_authorized(event.sender_id)
    if not role:
        await event.reply("Этот бот только для команды Сергея.")
        return
    if role == "anastasya":
        await event.reply(
            "Привет, Настя! 🤖\n\n"
            "Я бот для сбора твоих ответов на критичные рабочие вопросы.\n\n"
            "Команды:\n"
            "/today — текущий список вопросов\n"
            "/pending — сколько ещё открыто\n"
            "/done — завершить сессию\n\n"
            "Можно отвечать **голосом** или текстом. Я понимаю когда ты говоришь "
            "«по Климину 25К в неделю», нахожу нужный вопрос и пишу ответ в базу.\n\n"
            "Через Handy надиктуй голосом — текст придёт ко мне."
        )
    else:  # sergei
        await event.reply(
            "Привет! Я готов.\n\n"
            "Команды:\n"
            "/today — все open critical (по тебе и Anastasya)\n"
            "/pending — сводка по статусам\n"
            "/brief — переотправить свежий morning_brief\n"
        )


@bot.on(events.NewMessage(pattern=r"^/today"))
async def cmd_today(event):
    role = is_authorized(event.sender_id)
    if not role: return

    owner = "anastasya" if role == "anastasya" else None
    qs = get_open_for(owner)
    if not qs:
        await event.reply("Открытых critical-вопросов нет 😎")
        return

    by_prio = {"high": [], "medium": [], "low": []}
    for q in qs:
        by_prio.setdefault(q["criticality_priority"] or "low", []).append(q)

    lines = [f"📋 Открыто: {len(qs)}\n"]
    for prio, emoji in [("high", "🔴 HIGH"), ("medium", "🟠 MEDIUM"), ("low", "🟡 LOW")]:
        items = by_prio.get(prio, [])
        if not items: continue
        lines.append(f"\n{emoji} ({len(items)}):")
        for q in items[:5]:
            lines.append(f"  #{q['id']} ({q['title'][:25]}): {q['point'][:80]}")
        if len(items) > 5:
            lines.append(f"  ... и ещё {len(items)-5}")

    await event.reply("\n".join(lines))

    # Файл
    md_file = BASE / ("critical_for_anastasya.md" if role == "anastasya" else "critical_for_sergei.md")
    if md_file.exists():
        await bot.send_file(event.chat_id, str(md_file))


@bot.on(events.NewMessage(pattern=r"^/pending"))
async def cmd_pending(event):
    role = is_authorized(event.sender_id)
    if not role: return
    owner = "anastasya" if role == "anastasya" else None
    qs = get_open_for(owner)
    n_h = sum(1 for q in qs if q["criticality_priority"] == "high")
    n_m = sum(1 for q in qs if q["criticality_priority"] == "medium")
    n_l = sum(1 for q in qs if q["criticality_priority"] == "low")
    await event.reply(f"Открыто: {len(qs)} (🔴{n_h} / 🟠{n_m} / 🟡{n_l})")


@bot.on(events.NewMessage(pattern=r"^/done"))
async def cmd_done(event):
    if not is_authorized(event.sender_id): return
    await event.reply("Окей, до встречи 👋")


@bot.on(events.NewMessage(pattern=r"^/brief"))
async def cmd_brief(event):
    if event.sender_id != SERGEI_USER_ID: return
    await event.reply("⏳ Генерирую свежий бриф…")
    try:
        result = subprocess.run(
            ["python3", str(BASE / "morning_brief.py"), "--since", "1d"],
            capture_output=True, text=True, timeout=300, cwd=BASE
        )
        if result.returncode != 0:
            await event.reply(f"❌ Ошибка: {result.stderr[:300]}")
        else:
            await event.reply("✅ Бриф отправлен")
    except Exception as e:
        await event.reply(f"❌ Ошибка: {e}")


@bot.on(events.NewMessage(func=lambda e: not (e.text or "").startswith("/")))
async def handle_message(event):
    if event.sender_id != ANASTASYA_USER_ID:
        # Сергей пишет — игнорируем (брифы шлются через tg_notify отдельно)
        return

    text = event.text or ""
    if event.voice or event.audio:
        await event.reply("🎙 Голосовое получил, но транскрипция в боте не подключена. "
                          "Через Handy переведи в текст и пришли — обработаю.")
        return
    if not text.strip():
        return

    await event.reply("🧠 Парсю…")
    try:
        parsed = parse_anastasya_message(text)
    except Exception as e:
        await event.reply(f"❌ Ошибка парсинга: {e}")
        return

    if not parsed:
        await event.reply(
            "Не нашла открытых critical-вопросов которым подходит этот ответ.\n"
            "Используй /today для списка."
        )
        return

    n_written, addition = write_answers_with_merge(parsed)
    if n_written == 0:
        await event.reply("Не записала (возможно вопросы уже закрыты).")
        return

    lines = [f"✅ Записала {n_written} ответ(ов):"]
    for p in parsed[:8]:
        lines.append(f"  #{p['id']}: {p['answer'][:90]}")
    if len(parsed) > 8:
        lines.append(f"  ... и ещё {len(parsed)-8}")
    if addition:
        lines.append(f"\n💾 user_notes расширено: {addition[:100]}")
    qs_left = len(get_open_for("anastasya"))
    lines.append(f"\nОсталось открытых: {qs_left}")
    await event.reply("\n".join(lines))


def main():
    print(f"Запуск assistant_bot (Pixel Bot {BOT_TOKEN[:15]}…)")
    print(f"  Сергей: {SERGEI_USER_ID}, Anastasya: {ANASTASYA_USER_ID}")
    print("Ctrl+C чтобы остановить.")
    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()


if __name__ == "__main__":
    main()
