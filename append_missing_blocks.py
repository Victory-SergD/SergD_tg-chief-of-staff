#!/usr/bin/env python3
"""
Дописывает в work_chats_review.md блоки для тех work-чатов, у которых
есть ai_deep_dive_json в БД, но нет блока в .md (новые чаты после recategorize).
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
MD = BASE / "work_chats_review.md"
DB = BASE / "tg_analiz.db"

text = MD.read_text(encoding="utf-8")
ids_in_md = set(int(x) for x in re.findall(r'`id=(-?\d+)`', text))

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT chat_id, COALESCE(username,'') AS u, title, chat_type,
           COALESCE(category,'?') AS cat, COALESCE(relation,'?') AS rel,
           ai_deep_dive_json
    FROM dialogs WHERE ai_deep_dive_json IS NOT NULL
      AND category IN ('work','silent_team')
""").fetchall()

missing = []
for r in rows:
    if r["chat_id"] not in ids_in_md:
        try:
            j = json.loads(r["ai_deep_dive_json"])
        except:
            continue
        missing.append({
            "chat_id": r["chat_id"], "username": r["u"], "title": r["title"],
            "type": r["chat_type"], "cat": r["cat"], "rel": r["rel"],
            "tldr": j.get("tldr", ""),
            "user_notes_ready": j.get("user_notes_ready", ""),
            "open_tasks": j.get("q6_open_tasks", []),
        })

print(f"В .md: {len(ids_in_md)} | недостающих: {len(missing)}")
if not missing:
    print("Ничего дописывать не нужно")
    exit(0)

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
bak = MD.with_suffix(f".md.bak_{ts}")
import shutil
shutil.copy2(MD, bak)
print(f"📦 Backup: {bak}")

new_lines = ["", "---", "", f"## ➕ Добавлено {ts} ({len(missing)} новых чатов)", ""]

for m in missing:
    title = m["title"] or "?"
    uname = f"@{m['username']}" if m["username"] else ""
    new_lines.append(f"### `{m['type']}` {title} {uname}".rstrip())
    new_lines.append(f"`id={m['chat_id']}`  ")
    new_lines.append(f"**AI:** `{m['cat']}` / `{m['rel']}`  ")
    if m["tldr"]:
        new_lines.append(f"**Summary:** {m['tldr']}  ")
    if m["open_tasks"]:
        new_lines.append("**Открытые вопросы:**")
        # open_tasks могут быть строки или dict — преобразуем
        for t in m["open_tasks"][:5]:
            if isinstance(t, dict):
                txt = t.get("task") or t.get("text") or json.dumps(t, ensure_ascii=False)[:100]
            else:
                txt = str(t)[:200]
            new_lines.append(f"- {txt}")
    new_lines.append("")
    # Сразу вставляем "Мой ответ" с user_notes_ready + маркером (чтоб regenerate не трогал)
    answer = (m["user_notes_ready"] or "_____").strip()
    new_lines.append(f"**Мой ответ:** {answer}  ")
    new_lines.append(f"<!--regen:{m['chat_id']}-->  ")
    new_lines.append("")

MD.write_text(text + "\n".join(new_lines), encoding="utf-8")
print(f"✅ Дописано {len(missing)} блоков → {MD}")
