#!/usr/bin/env python3
"""
Транскрипция голосовых через Gemini 2.0 Flash (через прокси Lead Router).

Скачивает .ogg из Telegram, отправляет в Gemini API инлайном (base64),
сохраняет транскрипцию в БД. Поддерживает параллелизм.

Использование:
    python3 transcribe_gemini.py --since 2026-03-26
    python3 transcribe_gemini.py --since 2026-03-26 --concurrency 5
    python3 transcribe_gemini.py --chat-id -1003530185195
"""
import argparse
import asyncio
import base64
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]
SESSION_PATH = str(BASE / os.environ.get("SESSION_NAME", "session"))
DB_DEFAULT = str(BASE / "tg_analiz.db")

# Из Lead Router (sync_geotags.py)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_PROXY = os.environ.get("GEMINI_PROXY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

TMP_DIR = BASE / ".voices_tmp"
TMP_DIR.mkdir(exist_ok=True)

PROMPT = (
    "Транскрибируй это голосовое сообщение. Если на русском — пиши на русском, "
    "если смешанный — сохраняй как есть. Возвращай ТОЛЬКО текст транскрипции, "
    "без вступления, без комментариев, без префиксов типа 'Транскрипция:'."
)


def find_voices(conn, since: str, chat_id: int = 0, limit: int = 0):
    where = ["m.media_type IN ('voice', 'audio', 'video_note')",
             "m.date >= ?",
             "t.transcription IS NULL"]
    params = [since]
    if chat_id:
        where.append("m.chat_id = ?")
        params.append(chat_id)

    sql = f"""
        SELECT m.chat_id, m.msg_id, m.date, m.media_duration, d.title
        FROM messages m
        LEFT JOIN dialogs d ON d.chat_id = m.chat_id
        LEFT JOIN transcriptions t ON t.msg_id = m.msg_id AND t.chat_id = m.chat_id
        WHERE {' AND '.join(where)}
        ORDER BY m.date ASC
    """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


async def download_voice(client, chat_id, msg_id) -> Path | None:
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
    except Exception:
        return None
    if msg is None:
        return None
    out = TMP_DIR / f"{chat_id}_{msg_id}.ogg"
    try:
        path = await msg.download_media(file=str(out))
        return Path(path) if path else None
    except Exception:
        return None


def call_gemini(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """Отправить аудио в Gemini, вернуть текст. Возвращает None при ошибке."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")

    body = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(audio_bytes).decode("ascii"),
                }},
            ]
        }],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 4096,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_CIVIC_INTEGRITY",   "threshold": "BLOCK_NONE"},
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Установить прокси на время этого запроса (только если задан)
    if GEMINI_PROXY:
        proxy_handler = urllib.request.ProxyHandler({
            "http": GEMINI_PROXY,
            "https": GEMINI_PROXY,
        })
        opener = urllib.request.build_opener(proxy_handler)
    else:
        # Без прокси — прямой запрос
        opener = urllib.request.build_opener()

    try:
        with opener.open(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"    HTTP {e.code}: {body}", flush=True)
        return None
    except Exception as e:
        print(f"    err: {e}", flush=True)
        return None

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"    parse: {e} | {str(data)[:200]}", flush=True)
        return None


async def process_voice(client, conn, chat_id, msg_id, date, dur, title, sema):
    async with sema:
        path = await download_voice(client, chat_id, msg_id)
        if not path or not path.exists():
            return ("download_fail", None)

        try:
            audio = path.read_bytes()
            t0 = time.time()
            text = await asyncio.get_event_loop().run_in_executor(
                None, call_gemini, audio, "audio/ogg"
            )
            elapsed = time.time() - t0

            if text:
                conn.execute("""
                    INSERT INTO transcriptions (msg_id, chat_id, transcription, whisper_model, transcribed_at)
                    VALUES (?, ?, ?, 'gemini-2.5-flash', ?)
                    ON CONFLICT(msg_id, chat_id) DO UPDATE SET
                      transcription=excluded.transcription,
                      whisper_model=excluded.whisper_model,
                      transcribed_at=excluded.transcribed_at
                """, (msg_id, chat_id, text, datetime.now().isoformat()))
                conn.commit()
                return ("ok", (elapsed, len(text), text[:80]))
            return ("api_fail", None)
        finally:
            try: path.unlink()
            except: pass


async def main_async():
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2026-03-26")
    p.add_argument("--chat-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=5,
                   help="параллельных запросов в Gemini (default 5)")
    p.add_argument("--db", default=DB_DEFAULT)
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    voices = find_voices(conn, args.since, args.chat_id, args.limit)
    print(f"К транскрипции: {len(voices)} (model={GEMINI_MODEL}, "
          f"concurrency={args.concurrency})", flush=True)
    if not voices:
        return

    print("Подключаюсь к Telegram…", flush=True)
    from tg_proxy import get_tg_proxy
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH, proxy=get_tg_proxy())
    await client.start(phone=PHONE)

    sema = asyncio.Semaphore(args.concurrency)
    started = time.time()
    done = 0
    fail = 0
    progress_lock = asyncio.Lock()

    async def run_one(i, chat_id, msg_id, date, dur, title):
        nonlocal done, fail
        title = title or str(chat_id)
        result = await process_voice(client, conn, chat_id, msg_id, date, dur,
                                     title, sema)
        status, info = result
        async with progress_lock:
            if status == "ok":
                done += 1
                elapsed, n_chars, preview = info
                if done % 25 == 0 or done <= 3:
                    rate = done / max(time.time() - started, 1)
                    print(f"[{done}/{len(voices)}] {title[:30]} "
                          f"({dur or '?'}s → {n_chars}c за {elapsed:.1f}с) | "
                          f"{rate:.1f}/s | {preview}{'…' if n_chars>80 else ''}",
                          flush=True)
            else:
                fail += 1
                if fail < 5 or fail % 25 == 0:
                    print(f"[#{i}] FAIL ({status}) {title[:30]}", flush=True)

    tasks = [run_one(i, *v) for i, v in enumerate(voices, 1)]
    # запускаем пачками чтобы не накачать в память все таски сразу
    BATCH = max(50, args.concurrency * 10)
    for j in range(0, len(tasks), BATCH):
        await asyncio.gather(*tasks[j:j+BATCH], return_exceptions=True)

    await client.disconnect()
    elapsed = time.time() - started
    rate = done / max(elapsed, 1)
    print(f"\n=== ИТОГО ===")
    print(f"OK: {done}/{len(voices)}, fail: {fail}, время: {elapsed:.0f}с, "
          f"средняя скорость: {rate:.2f}/s", flush=True)


if __name__ == "__main__":
    asyncio.run(main_async())
