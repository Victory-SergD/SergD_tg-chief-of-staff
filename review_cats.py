#!/usr/bin/env python3
"""Обзор категоризации: распределение, low-confidence, work-чаты."""
import json
import sqlite3
import sys
from pathlib import Path
from collections import Counter

DB = Path(__file__).parent / "tg_analiz.db"
since = sys.argv[1] if len(sys.argv) > 1 else "2026-03-26"

c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

print(f"=== Категоризация (с {since}) ===\n")

print("Категории:")
for r in c.execute("""
    SELECT COALESCE(category,'(none)') cat, COUNT(*) n
    FROM dialogs WHERE last_message_at >= ?
      AND chat_type IN ('private','group') AND COALESCE(is_archived,0)=0
    GROUP BY category ORDER BY n DESC
""", (since,)):
    print(f"  {r['cat']:14s} {r['n']}")

print("\nWork-чаты по relation:")
for r in c.execute("""
    SELECT COALESCE(relation,'(none)') rel, COUNT(*) n
    FROM dialogs WHERE category='work' AND last_message_at >= ?
    GROUP BY relation ORDER BY n DESC
""", (since,)):
    print(f"  {r['rel']:14s} {r['n']}")

print("\nWork-чаты по project (из notes):")
projects = Counter()
project_chats = {}
for r in c.execute("""
    SELECT chat_id, title, notes FROM dialogs
    WHERE category='work' AND last_message_at >= ?
""", (since,)):
    try:
        notes = json.loads(r['notes'] or '{}')
    except Exception:
        notes = {}
    p = (notes.get('project') or '?').strip() or '?'
    projects[p] += 1
    project_chats.setdefault(p, []).append(r['title'])

for p, n in projects.most_common():
    print(f"  {p:24s} {n}")

print("\nLow-confidence (<0.7):")
n_lowconf = 0
for r in c.execute("""
    SELECT chat_id, title, category, relation, notes FROM dialogs
    WHERE category IS NOT NULL AND category != 'silent'
      AND last_message_at >= ?
""", (since,)):
    try: notes = json.loads(r['notes'] or '{}')
    except: notes = {}
    conf = notes.get('confidence')
    if isinstance(conf, (int, float)) and conf < 0.7:
        n_lowconf += 1
        q = notes.get('question_for_sergey', '')
        print(f"  [{conf:.2f}] {r['category']}/{r['relation']} {r['title']}")
        if q:
            print(f"        Q: {q[:200]}")
print(f"\nLow-conf всего: {n_lowconf}")

c.close()
