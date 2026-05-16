# tg-chief-of-staff

AI-помощник руководителя, который читает все твои рабочие Telegram-переписки и:

- 📋 знает каждого человека в команде (роль, иерархия, ЗП, открытые задачи)
- 🌅 шлёт **утренний бриф в Telegram-бот**: что важно сегодня, кто ждёт ответа, какие просрочки
- 🚩 сам задаёт критичные вопросы подчинённым/помощнику и собирает их ответы голосом
- 📊 готовит отчёты CEO с метриками по проектам
- 🗺 поддерживает живую карту команды через классификацию диалогов

Работает на **Telethon** + **Claude Opus 4.7 (1M context)** + **Gemini Flash Lite** (для транскрипции).

---

## Кому это нужно

Руководителю направления у которого:

- 5+ прямых подчинённых
- 6000+ Telegram-диалогов в год
- Несколько параллельных проектов
- Помощник-operating partner на голосовом workflow

Если у тебя 3 человека в команде — система избыточна. Если 10+ и ты регулярно «забыл написать X / упустил статус Y» — это для тебя.

---

## Архитектура

```
                     ┌─────────────────┐
                     │   Telegram API  │
                     └────────┬────────┘
                              │
                ┌─────────────▼──────────────┐
                │  collector.py / Telethon   │
                │  (диалоги + сообщения)     │
                └─────────────┬──────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
┌───────────────┐                       ┌───────────────────────┐
│ transcribe_   │   голосовые →         │   recategorize_all.py │
│ gemini.py     │   текст (Gemini)      │   (work/skip/personal)│
└───────┬───────┘                       └──────────┬────────────┘
        │                                          │
        └─────────────────┬────────────────────────┘
                          │
              ┌───────────▼──────────────┐
              │ agent_contact_deep_dive  │
              │  (Opus 1M на чат)        │
              │  → user_notes,           │
              │    open_tasks, disputed  │
              └───────────┬──────────────┘
                          │
                          ▼
              ┌──────────────────────────┐
              │ classify_disputed.py     │
              │  (Opus → criticality:    │
              │   critical / noise)      │
              └───────────┬──────────────┘
                          │
                          ▼
              ┌──────────────────────────┐
              │ export_critical_*.py     │
              │  → 3 файла .md:          │
              │    sergei / anastasya /  │
              │    subordinates          │
              └───────────┬──────────────┘
                          │
              ┌───────────┴───────────────┐
              ▼                           ▼
   ┌──────────────────┐         ┌─────────────────────┐
   │ ask_subordinates │         │ assistant_bot.py    │
   │ (Telethon DM)    │         │ (TG bot:            │
   │                  │         │   /today /pending)  │
   └──────────────────┘         └─────────────────────┘
              │
              ▼
   ┌──────────────────┐
   │ collect_         │
   │ subordinate_     │
   │ answers.py       │
   └──────────────────┘
              │
              ▼
   ┌──────────────────┐
   │ morning_brief.py │
   │ → бриф в TG      │
   └──────────────────┘
```

---

## Установка

```bash
git clone <repo>
cd tg-chief-of-staff

# 1. Python зависимости
python3 -m pip install telethon python-dotenv

# 2. Telegram credentials (https://my.telegram.org/apps)
cp .env.example .env
# отредактируй .env

# 3. Gemini API key (https://aistudio.google.com/apikey)
# добавь GEMINI_API_KEY в .env

# 4. Claude CLI (claude-code)
# https://docs.anthropic.com/claude/docs/claude-code
# Залогинься: `claude` → авторизация в браузере

# 5. Создай БД
python3 -c "import sqlite3; conn=sqlite3.connect('tg_analiz.db'); conn.executescript(open('db_schema.sql').read())"

# 6. Первая авторизация Telegram (отдаст SMS-код)
python3 collector.py --dialogs
```

---

## Ежедневный workflow

```bash
# Один раз утром (или по cron):
./daily.sh
```

Что делает `daily.sh`:

1. Собирает новые TG-сообщения
2. Транскрибирует голосовые (Gemini)
3. Парсит ответы подчинённых из incoming (если им ранее отправляли вопросы)
4. Перекатегоризация (только подозрительные)
5. Re-deep_dive контактов с новыми данными (Opus 1M, параллельно)
6. Классификация новых disputed на critical/noise
7. Импорт твоих ответов из `critical_for_sergei.md` (если заполнил)
8. Экспорт 3 файлов: для тебя / помощницы / подчинённых
9. Регенерация карты `work_chats_review.md`
10. Отправка утреннего брифа в твой TG-бот

---

## Стек

| Компонент | Что использует |
|---|---|
| Telegram API | Telethon 1.42+ (один аккаунт + Premium TranscribeAudio) |
| Транскрипция | Google Gemini Flash Lite (через прокси если нужно) |
| Аналитика | Claude Opus 4.7 на **1M контексте** (`claude-opus-4-7[1m]`) |
| Параллелизм | ThreadPoolExecutor + N OAuth токенов Claude |
| БД | SQLite (WAL mode, ~200 MB после 3 мес работы) |
| Бот | Telethon + `tg_notify.py` |

---

## Ключевые особенности

### Авторитетный слой фактов (`user_notes`)
Каждый dialog имеет `dialogs.user_notes` — заметки от руководителя. Они попадают в **каждый** prompt Opus как АВТОРИТЕТНЫЕ ФАКТЫ — Опус не оспаривает.

### Self-healing цикл
- Подчинённый отвечает на вопрос → парсинг через Opus → запись в `disputed_questions.answer` + мердж в `user_notes` контакта → на следующем dive Опус видит факт → не задаёт повторно.

### Классификация шум/важно (5 тестов CRITICAL)
- T1 — финимпакт (ФОТ, маржа)
- T2 — оргструктура (иерархия, статус)
- T3 — стратегический сигнал от ТОП-уровня (CEO/инвесторы)
- T4 — блокер решения СЕЙЧАС
- T5 — просрочка с подтверждённым ущербом

Edge case 50/50 → noise. Помощник не топится в шуме.

### Multi-OAuth для параллельного Opus
Несколько токенов Claude в `.tokens.env` (CLAUDE_OAUTH_TOKEN_SESS2/SESS3/...) → 6 параллельных воркеров через `repair_parallel.py`. Не сжигает основную сессию.

### Защита от ложных рассылок
`ask_subordinates.py` имеет 3 защиты:
- Только private chats (chat_id > 0)
- relation NOT IN ('client', 'partner', 'boss')
- username_aliases для Опус-указателей (`subordinate:cody` → `@ajdkdow`)

---

## Структура

```
tg-chief-of-staff/
├── daily.sh                          # Главный pipeline
├── collector.py                      # Сборщик TG (Telethon)
├── transcribe_gemini.py              # Транскрипция голосовых
├── recategorize_all.py               # Категоризация диалогов
├── agent_contact_deep_dive.py        # Deep dive контакта (Opus 1M)
├── incremental_dive.py               # Smart router: incremental/full re-dive
├── repair_parallel.py                # Параллельные dive (multi-OAuth)
├── classify_disputed.py              # Критичность вопросов
├── export_critical_questions.py      # Экспорт 3 файлов
├── regenerate_review_md.py           # Карта команды
├── morning_brief.py                  # Утренний бриф
├── ask_subordinates.py               # Отправка вопросов подчинённым
├── collect_subordinate_answers.py    # Парсинг ответов
├── remind_subordinates.py            # Reminder через 48ч
├── import_sergei_answers.py          # Импорт ответов руководителя
├── import_anastasya_answers.py       # Импорт ответов помощника
├── assistant_bot.py                  # TG-бот (для помощника)
├── agent_review_all.py               # Отчёт CEO
├── db_schema.sql                     # Схема БД
├── BACKLOG.md                        # Стратегический roadmap
└── README.md
```

---

## Безопасность

⚠️ **Это система с полным доступом к твоей переписке.** Никогда не пушь:

- `.env`, `.tokens.env`, `.tg_notify.json`
- `tg_access/*.session` (полный доступ к аккаунту)
- `tg_analiz.db` (вся переписка)
- `output/`, `reports/`, `critical_for_*.md` (персональные данные команды)

Всё это уже в `.gitignore`.

---

## Лицензия

MIT
