#!/usr/bin/env python3
"""
Регенерация work_chats_review.md из ai_deep_dive_json в БД.

Для каждого чата с ai_deep_dive_json:
- Опус получает: ВЕСЬ deep_dive_json (q1-q10, timeline, finance, tasks, disputed)
                  + текущий ручной ответ Сергея (если есть)
- Опус пишет развёрнутый объединённый ответ ~3000+ символов с структурой:
  Кто/Иерархия → Что делаем → Финансы/числа → Открытые блокеры → План.
- Текст вставляется в `**Мой ответ:**` блока в work_chats_review.md.

Безопасность:
- Перед записью .md делает timestamped .bak.
- --dry-run печатает план без записи и без вызова Опуса.
- --only @user или chat_id — обработать только один блок.
- --skip-filled — не трогать блоки где уже есть непустой ручной ответ Сергея
  (по умолчанию обновляем все, давая Опусу возможность объединить).

Использование:
    python3 regenerate_review_md.py --dry-run
    python3 regenerate_review_md.py --only @bjlyd
    python3 regenerate_review_md.py             # все 12 готовых
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent

DB_DEFAULT = BASE / "tg_analiz.db"
FILE_DEFAULT = BASE / "work_chats_review.md"

CLAUDE_MODEL = "claude-opus-4-7[1m]"  # 1M context — для богатых deep_dive_json
CLAUDE_TIMEOUT = 1800  # 30 мин (большой контекст дольше думает)

MERGE_PROMPT = """Ты редактор карточки рабочего контакта Сергея в Telegram.

Ты пишешь блок `**Мой ответ:**` в файле `work_chats_review.md` от первого лица Сергея.

У тебя есть:
1. ПОЛНЫЙ deep-dive JSON по этому контакту (timeline, q1-q10, финансы, открытые задачи, спорные моменты).
2. КРАТКИЙ user_notes_ready, который ты сам написал ранее (он лежит в JSON под этим же ключом).
3. Текущий ручной ответ Сергея (если есть) — это его голосовая расшифровка с опечатками, может содержать нюансы и корректировки.

ЗАДАЧА: написать **развёрнутый, плотный, фактический** объединённый ответ для карточки. Целевой объём: **2500–3500 символов**, не короче.

СТРУКТУРА ОТВЕТА (свободные абзацы, без markdown-заголовков):

1) **Кто и иерархия**: имя, username, роль/позиция, кому подчиняется, кто у него подчинённые. Если Сергей пишет что иерархия другая — приоритет Сергею.

2) **Что делаем / основные направления**: проекты, ниши, ветки, ответственность. Конкретно: chat-id'ы веток если есть, названия проектов, регионы, технические штуки.

3) **Что было за период (последние 2-4 недели)**: ключевые события из timeline — что настроили, какие кризисы пережили, какие решения приняли. С датами.

4) **Финансы / числа**: бюджеты, оклады, маржа, P&L, объёмы лидов, цены за лид, % бонусов. Всё цифровое из q8_finance и других мест.

5) **Открытые блокеры** (q6_open_tasks + q7_debts) — список с конкретикой: что зависло, на ком, с какого числа.

6) **Verdict + план** (q9_next_actions): keep/change/scale, что делать дальше, ближайшие точки.

ПРАВИЛА:
- Тон: от первого лица Сергея, деловой, без воды и без формальностей. «Я веду…», «мой подчинённый…», «нам надо…».
- Если ручной ответ Сергея ПРОТИВОРЕЧИТ JSON (особенно по финансам/датам/иерархии) — **приоритет ручному**. Сергей знает истину, JSON — гипотеза AI.
- Если ручной ответ Сергея ДОПОЛНЯЕТ — встрой эти факты.
- Опечатки голосовой расшифровки сгладь («Дл-менеджер»→«VDL-менеджер»).
- Не теряй конкретику: имена, username'ы (с @), числа, %, даты, chat-id'ы, проекты.
- Не добавляй markdown-заголовки (`##`, `###`, `**Заголовок:**`).
- НЕ начинай с `**Мой ответ:**` — префикс уже в файле.
- НЕ добавляй преамбулы типа «Вот развёрнутый ответ:».

ВХОД:

Контекст карточки:
- Чат: __CHAT_TITLE__
- chat_id: __CHAT_ID__
- Тип: __CHAT_TYPE__

ПОЛНЫЙ deep_dive JSON по контакту:
```json
__DEEP_DIVE_JSON__
```

Текущий ручной ответ Сергея в work_chats_review.md (может быть пустой или плейсхолдер):
\"\"\"
__MY_VERSION__
\"\"\"

⚠️ ОБЯЗАТЕЛЬНО: в самом конце ответа добавь блок «🚩 **Уточнить:**» — список вопросов из `disputed` deep_dive_json (если они есть и НЕ закрыты ручным ответом Сергея). Формат:
```
🚩 **Уточнить:**
- Вопрос 1 (короткая суть, без длинных reason'ов)
- Вопрос 2
```
Если все disputed закрыты или их нет — этот блок ОПУСКАЙ полностью.

ВЫХОД: только финальный текст развёрнутого объединённого ответа (2500–3500 символов + опциональный блок «🚩 Уточнить» в конце, без обёрток, без markdown-заголовков для основного текста, без префикса `**Мой ответ:**`).
"""


def call_claude_opus(prompt: str, model: str = CLAUDE_MODEL,
                     timeout: int = CLAUDE_TIMEOUT) -> str | None:
    """Запуск через `claude -p --model opus --output-format json`.
    Envelope: { result: "<текст>", ...}. Возвращает текст или None при ошибке."""
    start = time.time()
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model,
             "--no-session-persistence",
             "--output-format", "json"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  ! claude timeout {timeout}s", file=sys.stderr)
        return None
    dur = time.time() - start
    if result.returncode != 0:
        print(f"  ! claude rc={result.returncode}: "
              f"{(result.stderr or result.stdout)[:300]}", file=sys.stderr)
        return None
    try:
        env = json.loads(result.stdout)
        text = (env.get("result") or "").strip()
    except json.JSONDecodeError:
        text = result.stdout.strip()
    print(f"  · claude: {dur:.0f}s, {len(text)} chars", flush=True)
    return text or None


# Парсер блоков — нужен И пустой, И заполненный, и span ответа в исходном тексте.
BLOCK_HEADER_RE = re.compile(r'^### `([^`]+)` (.+?)$', re.MULTILINE)
ID_RE = re.compile(r'`id=(-?\d+)`')
ANSWER_RE = re.compile(
    r'(\*\*Мой ответ:\*\*)([^\n]*(?:\n(?!###|##\s|---|\Z)[^\n]*)*)',
    re.MULTILINE
)


def parse_blocks(text: str):
    """Возвращает список блоков:
    [{chat_id, chat_type, title, block_start, block_end,
      answer_match_start, answer_match_end, answer_text}, ...]
    span'ы — индексы в исходном `text`, чтобы можно было заменить точечно.
    """
    headers = list(BLOCK_HEADER_RE.finditer(text))
    blocks = []
    for i, h in enumerate(headers):
        block_start = h.start()
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block_text = text[block_start:block_end]

        id_m = ID_RE.search(block_text)
        if not id_m:
            continue
        chat_id = int(id_m.group(1))

        # type из заголовка (private / group / channel)
        chat_type = h.group(1).strip()
        title = h.group(2).strip()

        # Найти `**Мой ответ:**` и его текст в этом блоке
        ans_m = ANSWER_RE.search(block_text)
        if not ans_m:
            # Нет строки `**Мой ответ:**` вообще — пропускаем (не должно быть, но safe)
            continue

        # абсолютные координаты в text
        ans_abs_start = block_start + ans_m.start()
        ans_abs_end = block_start + ans_m.end()
        answer_text = ans_m.group(2).strip()

        blocks.append({
            "chat_id": chat_id,
            "chat_type": chat_type,
            "title": title,
            "block_start": block_start,
            "block_end": block_end,
            "answer_start": ans_abs_start,
            "answer_end": ans_abs_end,
            "answer_text": answer_text,
        })
    return blocks


def is_empty_answer(s: str) -> bool:
    """Пустой плейсхолдер: «», «_____», «—», «---», «.»."""
    s = s.strip()
    if not s:
        return True
    cleaned = s.strip("_-—.· \t\n")
    return cleaned == ""


def normalize(s: str) -> str:
    """Нормализуем для сравнения: убираем дубль `**Мой ответ:**`, лишние пробелы."""
    s = s.strip()
    # Убираем дубль префикса в начале
    while s.startswith("**Мой ответ:**"):
        s = s[len("**Мой ответ:**"):].strip()
    # Хвостовые пробелы и markdown line-break
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def similar(a: str, b: str, threshold: float = 0.92) -> bool:
    """Грубое сравнение: если нормализованные тексты совпадают на >threshold по
    пересечению по символам — считаем одинаковыми."""
    a_n = normalize(a)
    b_n = normalize(b)
    if not a_n or not b_n:
        return False
    if a_n == b_n:
        return True
    # быстрая проверка длины — если разница больше 10% и одна не строгий префикс другой
    if a_n.startswith(b_n) or b_n.startswith(a_n):
        return True
    # difflib quick ratio
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a_n, b_n).ratio() >= threshold


def build_replacement(new_answer_text: str) -> str:
    """Формирует замену: `**Мой ответ:** <text>  ` (два пробела на конце для md line-break)."""
    text = new_answer_text.strip()
    # На всякий случай отрежем дублирующий префикс
    while text.startswith("**Мой ответ:**"):
        text = text[len("**Мой ответ:**"):].strip()
    return f"**Мой ответ:** {text}  "


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB_DEFAULT))
    p.add_argument("--file", default=str(FILE_DEFAULT))
    p.add_argument("--only", help="@username или chat_id — только один блок")
    p.add_argument("--dry-run", action="store_true",
                   help="печатает план без вызова Опуса и без записи")
    p.add_argument("--skip-filled", action="store_true",
                   help="не трогать блоки где у Сергея уже есть непустой ручной ответ")
    p.add_argument("--no-bak", action="store_true",
                   help="не сохранять .bak (по умолчанию сохраняется timestamped backup)")
    p.add_argument("--model", default=CLAUDE_MODEL, help="claude model alias (default: opus)")
    p.add_argument("--limit", type=int, default=0,
                   help="обработать максимум N блоков (для тестирования)")
    p.add_argument("--force", action="store_true",
                   help="игнорировать маркеры <!--regen:CID--> и перегенерить всё")
    args = p.parse_args()

    db_path = Path(args.db)
    md_path = Path(args.file)
    if not md_path.exists():
        print(f"❌ Не найден {md_path}")
        sys.exit(1)
    if not db_path.exists():
        print(f"❌ Не найдена БД {db_path}")
        sys.exit(1)

    text = md_path.read_text(encoding="utf-8")
    blocks = parse_blocks(text)
    print(f"Распарсил блоков: {len(blocks)}")

    # Фильтр по --only
    only_chat_id = None
    only_username = None
    if args.only:
        if args.only.startswith("@"):
            only_username = args.only[1:].lower()
        else:
            try:
                only_chat_id = int(args.only)
            except ValueError:
                print(f"❌ --only ожидает @username или chat_id, получено: {args.only}")
                sys.exit(1)

    # Загружаем ai_deep_dive_json для всех нужных chat_id (целиком)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT chat_id, username, title, ai_deep_dive_json "
        "FROM dialogs WHERE ai_deep_dive_json IS NOT NULL"
    ).fetchall()
    by_id = {}
    for r in rows:
        raw = r["ai_deep_dive_json"]
        try:
            json.loads(raw)  # валидируем
        except Exception:
            continue
        by_id[r["chat_id"]] = {
            "username": r["username"],
            "title": r["title"],
            "raw_json": raw,
        }
    conn.close()
    print(f"В БД ai_deep_dive_json: {len(by_id)}")

    # Список блоков для обработки
    plan = []
    skip_no_dive = skip_only = skip_filled = 0
    for blk in blocks:
        cid = blk["chat_id"]
        d = by_id.get(cid)
        if not d:
            skip_no_dive += 1
            continue

        if only_chat_id is not None and cid != only_chat_id:
            skip_only += 1
            continue
        if only_username is not None:
            uname = (d["username"] or "").lower()
            if uname != only_username:
                skip_only += 1
                continue

        cur = blk["answer_text"]
        # Resume: блок с маркером <!--regen:CID--> уже сделан в этом или прошлом запуске
        if not args.force and f"<!--regen:{cid}-->" in cur:
            skip_filled += 1
            continue
        if args.skip_filled and not is_empty_answer(cur):
            skip_filled += 1
            continue

        plan.append(blk)

    if args.limit > 0:
        plan = plan[:args.limit]

    print(f"План:")
    print(f"  Опус-merge       : {len(plan)}")
    print(f"  пропуск (нет дайва): {skip_no_dive}")
    print(f"  пропуск (вне --only): {skip_only}")
    print(f"  пропуск (уже regen / skip-filled): {skip_filled}")

    if not plan:
        print("Нечего делать.")
        return

    # Опус-merge для каждого
    results = []  # (blk, new_text or None)
    for i, blk in enumerate(plan, 1):
        d = by_id[blk["chat_id"]]
        # Аккуратно: префикс `**Мой ответ:**` дублёр уберём из ручного текста
        my_clean = blk["answer_text"]
        while my_clean.strip().startswith("**Мой ответ:**"):
            my_clean = my_clean.strip()[len("**Мой ответ:**"):].strip()
        if is_empty_answer(my_clean):
            my_clean = "(пусто — Сергей пока не дал ручной ответ)"

        prompt = (MERGE_PROMPT
                  .replace("__DEEP_DIVE_JSON__", d["raw_json"])
                  .replace("__MY_VERSION__", my_clean)
                  .replace("__CHAT_TITLE__", blk["title"])
                  .replace("__CHAT_ID__", str(blk["chat_id"]))
                  .replace("__CHAT_TYPE__", blk["chat_type"]))

        print(f"  [{i}/{len(plan)}] {blk['title']} (chat_id={blk['chat_id']})…",
              flush=True)
        if args.dry_run:
            results.append((blk, None))
            continue
        merged = call_claude_opus(prompt, model=args.model)
        if not merged:
            print(f"    ! merge провалился, оставляю блок как есть")
            results.append((blk, None))
            continue
        merged = merged.strip().strip('"').strip()
        while merged.startswith("**Мой ответ:**"):
            merged = merged[len("**Мой ответ:**"):].strip()
        results.append((blk, merged))

        # ★ INCREMENTAL WRITE — после каждого блока сразу пишем .md.
        # Если процесс упадёт (интернет/ctrl-c) — все обработанные сохранены.
        # При повторном запуске: marker `<!-- regenerated:CHAT_ID -->` пометит сделанные,
        # remaining плейсхолдеры или старые версии будут перегенерены.
        if not args.no_bak and i == 1:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = md_path.with_suffix(f".md.bak_{ts}")
            shutil.copy2(md_path, bak)
            print(f"📦 Backup создан перед стартом: {bak}", flush=True)

        # Перечитываем .md (возможно изменился) и патчим только этот блок
        cur_text = md_path.read_text(encoding="utf-8")
        # Находим тот же блок по chat_id (spans поплыли — ищем заново через парсер)
        cur_blocks = parse_blocks(cur_text)
        cur_blk = next((b for b in cur_blocks if b["chat_id"] == blk["chat_id"]), None)
        if cur_blk:
            replacement = build_replacement(merged + f"\n<!--regen:{blk['chat_id']}-->")
            new_text = (cur_text[:cur_blk["answer_start"]]
                        + replacement
                        + cur_text[cur_blk["answer_end"]:])
            md_path.write_text(new_text, encoding="utf-8")
            print(f"    ✓ записан в md ({len(merged)} chars)", flush=True)

    if args.dry_run:
        print(f"\nDRY-RUN: применилось бы {len(results)} изменений. Файл НЕ записан.")
        return

    applied = sum(1 for _, m in results if m is not None)
    print(f"\n✅ Готово: {applied} блоков обновлено в {md_path}")


if __name__ == "__main__":
    main()
