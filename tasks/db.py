import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 10
INBOX_ID = 1
INBOX_NAME = "Inbox"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at REAL NOT NULL
);
INSERT OR IGNORE INTO lists (id, name, created_at) VALUES ({INBOX_ID}, '{INBOX_NAME}', CAST(strftime('%s', 'now') AS REAL));
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id),
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    due_date TEXT,
    due_time TEXT,
    priority INTEGER NOT NULL DEFAULT 4 CHECK (priority BETWEEN 1 AND 4),
    repeat_rule TEXT,
    next_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    completed_at REAL,
    trashed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (due_time IS NULL OR due_date IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS tasks_by_list ON tasks (list_id, completed_at, trashed_at);
CREATE INDEX IF NOT EXISTS tasks_by_parent ON tasks (parent_id);
CREATE INDEX IF NOT EXISTS tasks_by_due ON tasks (due_date, due_time);
CREATE INDEX IF NOT EXISTS tasks_by_completed ON tasks (completed_at);
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, notes, content='tasks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS tasks_fts_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts (rowid, title, notes) VALUES (new.id, new.title, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts (tasks_fts, rowid, title, notes) VALUES ('delete', old.id, old.title, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_update AFTER UPDATE OF title, notes ON tasks BEGIN
    INSERT INTO tasks_fts (tasks_fts, rowid, title, notes) VALUES ('delete', old.id, old.title, old.notes);
    INSERT INTO tasks_fts (rowid, title, notes) VALUES (new.id, new.title, new.notes);
END;
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connect(self, immediate: bool = False):
        connection = self._open()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_reachable(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT COUNT(*) FROM tasks").fetchone()
        except sqlite3.Error:
            return False
        return True
