#!/usr/bin/env python3
"""Быстрая сводка по БД: что собрано, что категоризовано."""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent / "tg_analiz.db"
since = sys.argv[1] if len(sys.argv) > 1 else "2026-03-26"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

print(f"=== СТАТИСТИКА (с {since}) ===\n")

print("Диалоги:")
for r in c.execute("""
    SELECT chat_type, COUNT(*) as n,
      SUM(CASE WHEN is_archived=1 THEN 1 ELSE 0 END) ar,
      SUM(CASE WHEN last_message_at >= ? THEN 1 ELSE 0 END) active
    FROM dialogs GROUP BY chat_type
""", (since,)):
    print(f"  {r['chat_type']:8s}: {r['n']:5d}  active(с {since}): {r['active']:4d}  archived: {r['ar']}")

print("\nСообщения:")
for r in c.execute("""
    SELECT COUNT(*) total,
           SUM(CASE WHEN is_outgoing=1 THEN 1 ELSE 0 END) sent,
           SUM(CASE WHEN media_type='voice' THEN 1 ELSE 0 END) voices,
           SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) recent_total,
           SUM(CASE WHEN date >= ? AND media_type='voice' THEN 1 ELSE 0 END) recent_voices
    FROM messages
""", (since, since)):
    print(f"  всего:       {r['total']}")
    print(f"  с {since}: {r['recent_total']} (голосовых: {r['recent_voices']})")

print("\nТранскрипции:")
for r in c.execute("SELECT COUNT(*) n FROM transcriptions"):
    print(f"  всего: {r['n']}")

print("\nКатегоризация:")
for r in c.execute("""
    SELECT COALESCE(category, '(uncategorized)') cat, COUNT(*) n
    FROM dialogs
    WHERE last_message_at >= ? AND chat_type IN ('private','group') AND COALESCE(is_archived,0)=0
    GROUP BY category ORDER BY n DESC
""", (since,)):
    print(f"  {r['cat']:18s} {r['n']}")

print("\nТоп-15 чатов по объёму сообщений за период:")
for r in c.execute("""
    SELECT d.title, d.chat_type, d.category, d.relation, COUNT(m.msg_id) n
    FROM dialogs d
    JOIN messages m ON m.chat_id = d.chat_id AND m.date >= ?
    GROUP BY d.chat_id ORDER BY n DESC LIMIT 15
""", (since,)):
    cat = f"[{r['category']}/{r['relation']}]" if r['category'] else ""
    print(f"  {r['n']:5d}  {r['chat_type']:7s} {cat} {r['title']}")

c.close()
