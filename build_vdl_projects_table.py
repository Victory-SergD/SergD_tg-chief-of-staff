"""
Сборка таблицы проектов VDL по 4 людям через Opus 1M:
  - Симонов «спящий» @MrFastCode
  - Герман @german_deew
  - Сам Сергей (из vlad_supervised отчёта)
  - Рома @barm_in

Формат:
| # | Проект | Статус | Этап (1/2/3/4) | Ответственный | Прогноз эффекта | Факт |
"""
import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "tg_analiz.db"
TOKENS_FILE = BASE / ".tokens.env"
OUT = BASE / "vdl_projects_table.txt"

TOKENS = []
for line in TOKENS_FILE.read_text().splitlines():
    line = line.strip()
    if line.startswith("CLAUDE_OAUTH_TOKEN_") and "=" in line:
        _, val = line.split("=", 1)
        if val.strip():
            TOKENS.append(val.strip())
TOKEN = TOKENS[0] if TOKENS else None


def call_opus(prompt: str, use_token: bool = True) -> str:
    env = os.environ.copy()
    if use_token and TOKEN:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = TOKEN
    result = subprocess.run(
        ["claude", "-p", "--model", "claude-opus-4-7[1m]", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=1800, env=env
    )
    if result.returncode != 0:
        # Fallback на текущую сессию Сергея
        if use_token:
            print(f"  ⚠ token-сессия упала ({result.stderr[:200]}), пробую текущую сессию...")
            return call_opus(prompt, use_token=False)
        raise RuntimeError(f"opus exit={result.returncode}: stderr={result.stderr[:500]} stdout={result.stdout[:300]}")
    return result.stdout.strip()


def get_contact_payload(chat_id: int) -> str:
    """Собирает релевантный контекст по подчинённому."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT title, username, COALESCE(user_notes,'') AS user_notes,
               COALESCE(ai_deep_dive_json,'{}') AS dd
        FROM dialogs WHERE chat_id = ?
    """, (chat_id,)).fetchone()
    conn.close()

    title, username = row["title"], row["username"]
    pieces = [f"═══ {title} (@{username}) ═══"]
    if row["user_notes"]:
        pieces.append(f"USER_NOTES (АВТОРИТЕТНЫЕ ФАКТЫ Сергея):\n{row['user_notes']}")

    try:
        dd = json.loads(row["dd"])
    except Exception:
        dd = {}

    keys_of_interest = (
        "who_and_relation", "direction", "top_topics", "last_two_weeks",
        "open_tasks", "finance", "next_actions", "tldr", "user_notes_ready",
        "timeline"
    )
    for k in keys_of_interest:
        v = dd.get(k) or dd.get(f"q1_{k}") or dd.get(f"q5_{k}") or dd.get(f"q6_{k}")
        if v:
            pieces.append(f"\n[{k}]\n{json.dumps(v, ensure_ascii=False, indent=2)[:5000]}")
    return "\n".join(pieces)


def get_sergei_payload() -> str:
    """Берёт vlad_supervised отчёт + project_map для проектов которые Сергей лично ведёт."""
    pieces = ["═══ Сергей Дышкант (сам) — проекты которые ведёт лично ═══"]

    # Самый свежий отчёт Владу
    reports = sorted(BASE.glob("reports/vlad_supervised_*.md"))
    if reports:
        latest = reports[-1]
        text = latest.read_text()
        pieces.append(f"[reports/{latest.name}]")
        pieces.append(text[:25000])

    return "\n\n".join(pieces)


PROMPT = """Ты помогаешь Сергею Дышканту (директор VDL Service в Victory Agency) собрать таблицу
проектов по 4 ключевым людям его IT-контура: Симонов «спящий», Герман Дью, сам Сергей, Рома.

═══ КОНТЕКСТ ═══
ВДЛ-команда автоматизации (4 человека):
  - Симонов Александр «спящий» @MrFastCode — автоматизатор VDL (парсеры, инфра, VictorySender, НейроКК)
  - Герман Дью @german_deew — технарь-универсал (Афи Superset, парсеры, КП-бот, кандидаты-парсеры)
  - Сам Сергей — стратегические треки (ML Order Prediction, AI SEO Senior, Email-маркетинг, Auto-улучшатор, VK парсер)
  - Рома @barm_in — DevOps + tech lead (4 ветки апрель-май: vi-heat-map, дизайн, ...)

═══ ЭТАПЫ (1-4) ═══
Этап 1. Анализ — изучение процессов подразделения, выявление потерь и точек автоматизации.
Этап 2. Подготовка — описание решения, расчёт эффекта, согласование, ТЗ.
Этап 3. Разработка и внедрение — создание, тестирование, запуск, обучение сотрудников.
Этап 4. Поддержка и развитие — стабильность, доработки, масштабирование.

═══ СТАТУСЫ ═══
- В работе / На паузе / Закрыт / Заморожен / Заброшен / Передан другому

═══ ЗАДАЧА ═══

Из переписок и заметок ниже извлеки ВСЕ проекты которые делают эти 4 человека.
Для каждого верни строку таблицы.

ФОРМАТ (текстовая таблица в txt, с пайпами):

| # | Проект | Статус | Этап | Ответственный | Прогноз эффекта (₽ или ч/мес) | Факт после внедрения |
|---|--------|--------|------|---------------|-------------------------------|----------------------|
| 1 | ...    | ...    | 1    | Симонов       | ...                           | ...                  |

ПРАВИЛА:
- Один человек = несколько проектов (выписать каждый отдельно)
- Если проект совместный (например Сергей + Рома) — Ответственный = "Сергей + Рома"
- Если в переписках НЕТ цифры прогноза эффекта — пиши "—" (НЕ выдумывай)
- Если нет факта после внедрения — пиши "—"
- Если проект только описан в идее — Этап 1
- Если есть ТЗ / решение — Этап 2
- Если идёт разработка / запуск — Этап 3
- Если работает в проде и доделывается — Этап 4
- Сортируй: сначала проекты Симонова, затем Германа, затем Сергея, затем Ромы
- Группируй визуально с подзаголовками "## СИМОНОВ", "## ГЕРМАН", "## СЕРГЕЙ", "## РОМА"

═══ ДАННЫЕ ═══

{simonov_data}

{german_data}

{sergei_data}

{roma_data}

═══ ВЫХОД ═══
Только текстовая таблица в указанном формате. Никаких ```, никаких пояснений до или после.
В начале — заголовок "ПРОЕКТЫ КОМАНДЫ АВТОМАТИЗАЦИИ VDL · 2026-05-10".
"""


def main():
    print("Собираю данные по 4 людям...")
    simonov = get_contact_payload(5796896708)
    german = get_contact_payload(1096407871)
    sergei = get_sergei_payload()
    roma = get_contact_payload(62565394)

    print(f"  Симонов:  {len(simonov)} chars")
    print(f"  Герман:   {len(german)} chars")
    print(f"  Сергей:   {len(sergei)} chars")
    print(f"  Рома:     {len(roma)} chars")

    prompt = PROMPT.format(
        simonov_data=simonov,
        german_data=german,
        sergei_data=sergei,
        roma_data=roma,
    )
    print(f"  Total prompt: {len(prompt)} chars (~{len(prompt)//4} tokens)")

    print("\nЗапрос Opus 4.7 1M...")
    out = call_opus(prompt)

    OUT.write_text(out + "\n")
    print(f"\n✓ Готово: {OUT}")
    print(f"  размер вывода: {len(out)} chars")
    print()
    print("=" * 70)
    print(out[:3000])
    if len(out) > 3000:
        print(f"\n... (ещё {len(out)-3000} chars в файле)")


if __name__ == "__main__":
    main()
