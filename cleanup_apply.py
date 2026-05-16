#!/usr/bin/env python3
"""
Применяет cleanup-batch: парсит markdown с чекбоксами, передаёт chat_ids
в существующий unsubscribe.py.

Запуск:
  python3 cleanup_apply.py --file cleanup_batches/batch_001.md
  python3 cleanup_apply.py --file cleanup_batches/batch_001.md --dry-run  # только показать что сделает
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true",
                   help="пропустить подтверждение в unsubscribe (для автоматизации)")
    args = p.parse_args()

    f = Path(args.file)
    if not f.exists():
        print(f"❌ Файл не найден: {f}")
        sys.exit(1)

    text = f.read_text(encoding="utf-8")
    # Ищем строки `- [x] ... \`<chat_id>\``
    chat_ids = []
    pattern = re.compile(r'-\s*\[\s*[xXхХ]\s*\].*?`(-?\d+)`')
    for m in pattern.finditer(text):
        chat_ids.append(int(m.group(1)))
    chat_ids = list(dict.fromkeys(chat_ids))  # dedup

    print(f"Помечено к удалению: {len(chat_ids)}")
    if not chat_ids:
        print("Нечего удалять — никаких [x] не найдено")
        return

    # Превью
    print("\nChat IDs:")
    for cid in chat_ids[:20]:
        print(f"  {cid}")
    if len(chat_ids) > 20:
        print(f"  ...и ещё {len(chat_ids) - 20}")

    if args.dry_run:
        print("\n[DRY-RUN — ничего не вызвано]")
        return

    # Вызываем unsubscribe.py
    cmd = ["python3", "unsubscribe.py"] + [str(c) for c in chat_ids]
    if args.yes:
        cmd.append("--yes")
    print(f"\nЗапускаю: python3 unsubscribe.py {len(chat_ids)} chat_ids…")
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
