-- ============================================================
-- TG Analiz Database Schema
-- SQLite3
-- ============================================================

-- 1. ПОЛЬЗОВАТЕЛИ (все контакты, кто смотрел сторис, собеседники)
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    phone           TEXT,
    is_bot          INTEGER DEFAULT 0,
    is_premium      INTEGER DEFAULT 0,
    is_verified     INTEGER DEFAULT 0,
    is_contact      INTEGER DEFAULT 0,
    is_mutual       INTEGER DEFAULT 0,
    is_deleted      INTEGER DEFAULT 0,
    is_scam         INTEGER DEFAULT 0,
    is_fake         INTEGER DEFAULT 0,
    is_close_friend INTEGER DEFAULT 0,
    lang_code       TEXT,
    last_status      TEXT,           -- ONLINE, OFFLINE, RECENTLY, LAST_WEEK, LAST_MONTH, LONG_AGO
    last_online_at   TEXT,           -- ISO datetime
    first_seen_at    TEXT,           -- когда мы впервые увидели пользователя
    updated_at       TEXT            -- последнее обновление записи
);

-- 2. СТОРИС (архив всех моих историй)
CREATE TABLE IF NOT EXISTS stories (
    story_id        INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,   -- ISO datetime создания
    expire_date     TEXT,            -- когда истекает
    caption         TEXT,            -- текст/подпись
    media_type      TEXT,            -- photo, video, document
    is_pinned       INTEGER DEFAULT 0,
    is_public       INTEGER DEFAULT 0,
    is_close_friends INTEGER DEFAULT 0,
    is_contacts     INTEGER DEFAULT 0,
    is_edited       INTEGER DEFAULT 0,
    no_forwards     INTEGER DEFAULT 0,
    views_count     INTEGER DEFAULT 0,
    reactions_count INTEGER DEFAULT 0,
    forwards_count  INTEGER DEFAULT 0,
    collected_at    TEXT             -- когда мы собрали эти данные
);

-- 3. ПРОСМОТРЫ СТОРИС (кто конкретно смотрел)
CREATE TABLE IF NOT EXISTS story_views (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    viewed_at       TEXT,            -- ISO datetime просмотра
    reaction        TEXT,            -- эмодзи реакции (если поставил)
    collected_at    TEXT,
    FOREIGN KEY (story_id) REFERENCES stories(story_id),
    FOREIGN KEY (user_id)  REFERENCES users(user_id),
    UNIQUE(story_id, user_id)
);

-- 4. РЕАКЦИИ НА СТОРИС (детализация по типам)
CREATE TABLE IF NOT EXISTS story_reactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    reaction        TEXT NOT NULL,   -- эмодзи или custom_emoji_id
    reacted_at      TEXT,            -- ISO datetime
    FOREIGN KEY (story_id) REFERENCES stories(story_id),
    FOREIGN KEY (user_id)  REFERENCES users(user_id),
    UNIQUE(story_id, user_id)
);

-- 5. ДИАЛОГИ (все чаты / личные переписки / группы / каналы)
CREATE TABLE IF NOT EXISTS dialogs (
    chat_id         INTEGER PRIMARY KEY,
    chat_type       TEXT,            -- private, group, supergroup, channel
    title           TEXT,            -- название группы/канала или имя пользователя
    username        TEXT,
    members_count   INTEGER,
    unread_count    INTEGER DEFAULT 0,
    is_pinned       INTEGER DEFAULT 0,
    is_archived     INTEGER DEFAULT 0,
    is_muted        INTEGER DEFAULT 0,
    last_message_id INTEGER,
    last_message_at TEXT,
    last_msg_text   TEXT,            -- preview последнего сообщения
    last_msg_outgoing INTEGER,
    category        TEXT,            -- AI-категория: work_dept_X, client, partner, personal, promo, etc.
    relation        TEXT,            -- мой статус: boss / peer / subordinate / client / personal
    notes           TEXT,            -- свободные заметки от агента
    updated_at      TEXT
);

-- 6. СООБЩЕНИЯ (переписки)
CREATE TABLE IF NOT EXISTS messages (
    msg_id          INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    from_user_id    INTEGER,
    date            TEXT NOT NULL,    -- ISO datetime
    text            TEXT,
    caption         TEXT,            -- для медиа с подписью
    media_type      TEXT,            -- text, photo, video, voice, audio, video_note, document, sticker, contact, location, poll
    media_file_path TEXT,            -- путь к скачанному файлу
    media_duration  INTEGER,         -- длительность аудио/видео в секундах
    media_file_name TEXT,            -- имя файла документа
    is_outgoing     INTEGER DEFAULT 0,
    is_forwarded    INTEGER DEFAULT 0,
    forward_from_id INTEGER,
    reply_to_msg_id INTEGER,
    edit_date       TEXT,
    views_count     INTEGER,
    forwards_count  INTEGER,
    grouped_id      TEXT,            -- ID медиагруппы
    PRIMARY KEY (msg_id, chat_id),
    FOREIGN KEY (chat_id)       REFERENCES dialogs(chat_id),
    FOREIGN KEY (from_user_id)  REFERENCES users(user_id)
);

-- 7. РЕАКЦИИ НА СООБЩЕНИЯ
CREATE TABLE IF NOT EXISTS message_reactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id          INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    user_id         INTEGER,
    reaction        TEXT NOT NULL,
    count           INTEGER DEFAULT 1,
    FOREIGN KEY (msg_id, chat_id) REFERENCES messages(msg_id, chat_id)
);

-- 8. ТРАНСКРИПЦИИ (Whisper)
CREATE TABLE IF NOT EXISTS transcriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id          INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    audio_file_path TEXT,
    transcription   TEXT,
    whisper_model   TEXT DEFAULT 'large',
    language        TEXT DEFAULT 'ru',
    transcribed_at  TEXT,
    FOREIGN KEY (msg_id, chat_id) REFERENCES messages(msg_id, chat_id),
    UNIQUE(msg_id, chat_id)
);

-- 9. АНАЛИЗЫ КОНТАКТОВ (результаты AI-анализа переписки)
CREATE TABLE IF NOT EXISTS contact_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    analysis_date   TEXT NOT NULL,
    period_from     TEXT,
    period_to       TEXT,
    total_messages  INTEGER,
    total_voice     INTEGER,
    summary         TEXT,            -- краткое резюме
    business_niches TEXT,            -- JSON: ниши и приоритеты
    pain_points     TEXT,            -- JSON: боли и проблемы
    opportunities   TEXT,            -- JSON: возможности для x100
    full_analysis   TEXT,            -- полный текст анализа
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 10. СТОРИС КОНТАКТОВ (сторис других людей, которые мы видим)
CREATE TABLE IF NOT EXISTS contact_stories (
    story_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    date            TEXT NOT NULL,
    expire_date     TEXT,
    caption         TEXT,
    media_type      TEXT,
    views_count     INTEGER,         -- NULL для чужих сторис (не видим)
    collected_at    TEXT,
    PRIMARY KEY (story_id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- ============================================================
-- ИНДЕКСЫ для быстрого поиска
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_story_views_user     ON story_views(user_id);
CREATE INDEX IF NOT EXISTS idx_story_views_story    ON story_views(story_id);
CREATE INDEX IF NOT EXISTS idx_messages_chat        ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_user        ON messages(from_user_id);
CREATE INDEX IF NOT EXISTS idx_messages_date        ON messages(date);
CREATE INDEX IF NOT EXISTS idx_stories_date         ON stories(date);
CREATE INDEX IF NOT EXISTS idx_transcriptions_msg   ON transcriptions(msg_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_users_username       ON users(username);

-- ============================================================
-- ПОЛЕЗНЫЕ VIEWS для аналитики
-- ============================================================

-- Топ зрителей сторис (кто чаще всего смотрит)
CREATE VIEW IF NOT EXISTS v_top_story_viewers AS
SELECT
    u.user_id,
    u.first_name || ' ' || COALESCE(u.last_name, '') AS name,
    u.username,
    COUNT(sv.story_id) AS stories_viewed,
    COUNT(sv.reaction) AS reactions_given,
    MIN(sv.viewed_at) AS first_view,
    MAX(sv.viewed_at) AS last_view
FROM story_views sv
JOIN users u ON u.user_id = sv.user_id
GROUP BY u.user_id
ORDER BY stories_viewed DESC;

-- Статистика по сторис: какие собирают больше просмотров
CREATE VIEW IF NOT EXISTS v_stories_stats AS
SELECT
    s.story_id,
    s.date,
    s.caption,
    s.media_type,
    s.views_count,
    s.reactions_count,
    s.forwards_count,
    ROUND(s.reactions_count * 100.0 / MAX(s.views_count, 1), 1) AS engagement_pct,
    CAST(strftime('%H', s.date) AS INTEGER) AS hour_posted,
    CAST(strftime('%w', s.date) AS INTEGER) AS weekday_posted
FROM stories s
ORDER BY s.date DESC;

-- Активность по переписке: кто сколько пишет
CREATE VIEW IF NOT EXISTS v_chat_activity AS
SELECT
    d.chat_id,
    d.title,
    d.chat_type,
    COUNT(m.msg_id) AS total_messages,
    SUM(CASE WHEN m.is_outgoing = 1 THEN 1 ELSE 0 END) AS sent,
    SUM(CASE WHEN m.is_outgoing = 0 THEN 1 ELSE 0 END) AS received,
    SUM(CASE WHEN m.media_type = 'voice' THEN 1 ELSE 0 END) AS voice_messages,
    MIN(m.date) AS first_message,
    MAX(m.date) AS last_message
FROM dialogs d
LEFT JOIN messages m ON m.chat_id = d.chat_id
GROUP BY d.chat_id
ORDER BY total_messages DESC;

-- Лучшее время для постинга сторис
CREATE VIEW IF NOT EXISTS v_best_posting_time AS
SELECT
    CAST(strftime('%H', date) AS INTEGER) AS hour,
    COUNT(*) AS stories_count,
    ROUND(AVG(views_count), 0) AS avg_views,
    ROUND(AVG(reactions_count), 1) AS avg_reactions,
    ROUND(AVG(reactions_count * 100.0 / MAX(views_count, 1)), 1) AS avg_engagement_pct
FROM stories
GROUP BY hour
ORDER BY avg_views DESC;
