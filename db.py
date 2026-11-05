"""
SQLite storage for the feeder-monitoring app.

One database file (runs/fsj.db). Each thread gets its own connection
(sqlite3 connections can't be shared across threads); WAL mode lets the
web threads keep reading while the job worker writes.

Writes are serialized with a single process-wide lock. SQLite would do this
on its own via its file lock, but waiting on a Python lock avoids
"database is locked" errors under bursts of concurrent edits.
"""

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    seq           INTEGER NOT NULL,
    batch_id      TEXT,
    display_name  TEXT,
    position      INTEGER,
    status        TEXT NOT NULL,          -- queued | running | syncing | done | error | cancelling | cancelled
    created_at    TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    folder        TEXT,
    output_json   TEXT,
    log_file      TEXT,
    cmd           TEXT,                   -- JSON list
    file_count    INTEGER,
    created_by    TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT,
    folder             TEXT NOT NULL,
    filename           TEXT NOT NULL,
    position           INTEGER,
    camera_id          TEXT,              -- EXIF UserComment ID=, e.g. C57
    image_seq          TEXT,              -- EXIF UserComment LO=, e.g. 013
    hardware_id        TEXT,              -- EXIF UserComment MA=: fixed per physical unit
    battery            TEXT,              -- EXIF UserComment BP=, e.g. 50%
    battery_source     TEXT,              -- EXIF UserComment BS=, e.g. EXT
    trigger            TEXT,              -- EXIF UserComment TR=, e.g. MOTION
    camera_model       TEXT,              -- EXIF Model, e.g. LL-3A
    firmware           TEXT,              -- EXIF Software
    exif_comment       TEXT,              -- the full UserComment, for keys not split out
    captured_at        TEXT,              -- YYYY-MM-DDTHH:MM:SS, camera-local time
    time_source        TEXT,              -- exif | manual | NULL
    diel_period        TEXT,
    ai_species         TEXT,              -- NULL = blank
    ai_conf            REAL,
    ai_det_conf        REAL,
    corrected_species  TEXT,
    corrected_by       TEXT,
    corrected_at       TEXT,
    display_species    TEXT NOT NULL,     -- corrected_species, else ai_species, else 'blank'
    count              INTEGER DEFAULT 0,
    notes              TEXT DEFAULT '',
    band_id            TEXT DEFAULT '',   -- reserved for banded-jay re-identification
    marked_for_review  INTEGER DEFAULT 1,
    metadata_edited    INTEGER DEFAULT 0,
    has_thumbnail      INTEGER DEFAULT 0,
    created_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_species  ON images(display_species);
CREATE INDEX IF NOT EXISTS idx_images_captured ON images(captured_at);
CREATE INDEX IF NOT EXISTS idx_images_position ON images(position, captured_at);

CREATE TABLE IF NOT EXISTS species (
    label TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS log_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    start      TEXT NOT NULL,             -- YYYY-MM-DD
    "end"      TEXT,                      -- YYYY-MM-DD, NULL for a single-day event
    position   INTEGER,                   -- NULL = site-wide
    equipment  TEXT,
    title      TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_events_start ON log_events(start);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_local = threading.local()
write_lock = threading.RLock()
_db_path = None


# Columns added after the first release, so databases created by an earlier
# version get them on startup. (CREATE TABLE IF NOT EXISTS doesn't add
# columns to a table that already exists.)
ADDED_COLUMNS = {
    "images": ["hardware_id TEXT", "battery TEXT", "battery_source TEXT", "trigger TEXT",
               "camera_model TEXT", "firmware TEXT", "exif_comment TEXT"],
}


def init(path):
    global _db_path
    _db_path = str(path)
    conn = connect()
    conn.executescript(SCHEMA)
    for table, cols in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    conn.commit()


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def query(sql, params=()):
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql, params=()):
    r = connect().execute(sql, params).fetchone()
    return dict(r) if r else None


@contextmanager
def transaction():
    """`with db.transaction() as conn:` — serialized, committed on success,
    rolled back on exception."""
    with write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def execute(sql, params=()):
    with transaction() as conn:
        return conn.execute(sql, params)
