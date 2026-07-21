import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "books.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    description TEXT,
    subjects TEXT,
    series TEXT,
    first_publish_year INTEGER,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS books_in_library (
    metadata_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    work_id TEXT REFERENCES works(work_id),
    title TEXT NOT NULL,
    subtitle TEXT,
    author TEXT,
    format TEXT,
    content_type TEXT,
    description TEXT,
    call_number TEXT,
    publication_year INTEGER,
    primary_language TEXT,
    isbn TEXT,
    subjects TEXT,
    composite_subjects TEXT,
    genres TEXT,
    series TEXT,
    super_formats TEXT,
    consumption_format TEXT,
    active INTEGER DEFAULT 1,
    first_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (metadata_id, library_id)
);

CREATE INDEX IF NOT EXISTS idx_bil_work ON books_in_library(work_id);
CREATE INDEX IF NOT EXISTS idx_bil_library ON books_in_library(library_id);
CREATE INDEX IF NOT EXISTS idx_bil_format ON books_in_library(format);
CREATE INDEX IF NOT EXISTS idx_bil_author ON books_in_library(author);
CREATE INDEX IF NOT EXISTS idx_bil_year ON books_in_library(publication_year);
CREATE INDEX IF NOT EXISTS idx_bil_lang ON books_in_library(primary_language);
CREATE INDEX IF NOT EXISTS idx_bil_content ON books_in_library(content_type);
CREATE INDEX IF NOT EXISTS idx_bil_isbn ON books_in_library(isbn);

CREATE TABLE IF NOT EXISTS borrow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metadata_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    checkout_date TEXT,
    source TEXT NOT NULL CHECK(source IN ('history', 'checkout')),
    library_entry_id TEXT UNIQUE,
    is_current INTEGER DEFAULT 0,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_be_meta ON borrow_events(metadata_id);
CREATE INDEX IF NOT EXISTS idx_be_source ON borrow_events(source);
CREATE INDEX IF NOT EXISTS idx_be_library ON borrow_events(library_id);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id TEXT NOT NULL,
    sync_type TEXT NOT NULL,
    query_params TEXT,
    pages_total INTEGER DEFAULT 0,
    pages_completed INTEGER DEFAULT 0,
    books_total INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendation_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metadata_id TEXT NOT NULL,
    score REAL,
    category TEXT NOT NULL,
    category_rank INTEGER NOT NULL,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rec_cat ON recommendation_cache(category);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


def schema_matches(conn):
    """True if all SCHEMA_SQL tables exist with matching columns and no extras."""
    ref = sqlite3.connect(":memory:")
    ref.executescript(SCHEMA_SQL)

    expected = set()
    for line in SCHEMA_SQL.splitlines():
        line = line.strip()
        if not line.upper().startswith("CREATE TABLE"):
            continue
        name = line.split("(")[0].split()[-1]
        expected.add(name)
        try:
            ref_cols = {r[1]: r[2] for r in ref.execute(f"PRAGMA table_info({name})").fetchall()}
            cur_cols = {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()}
        except Exception:
            ref.close()
            return False
        if ref_cols != cur_cols:
            ref.close()
            return False

    ref.close()

    actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if actual - expected:
        return False

    return True


def upsert_book_in_library(conn, library_id, metadata_id, title, subtitle=None,
                           author=None, format=None, content_type=None,
                           description=None, call_number=None,
                           publication_year=None, primary_language=None,
                           isbn=None, subjects=None,
                           composite_subjects=None, genres=None, series=None,
                           super_formats=None, consumption_format=None,
                           active=1):
    conn.execute("""
        INSERT INTO books_in_library (
            library_id, metadata_id, title, subtitle, author, format,
            content_type, description, call_number,
            publication_year, primary_language, isbn,
            subjects, composite_subjects, genres, series,
            super_formats, consumption_format,
            active, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(metadata_id, library_id) DO UPDATE SET
            title=excluded.title, subtitle=excluded.subtitle,
            author=excluded.author, format=excluded.format,
            content_type=excluded.content_type,
            description=excluded.description,
            call_number=excluded.call_number,
            publication_year=excluded.publication_year,
            primary_language=excluded.primary_language,
            isbn=excluded.isbn, subjects=excluded.subjects,
            composite_subjects=excluded.composite_subjects,
            genres=excluded.genres, series=excluded.series,
            super_formats=excluded.super_formats,
            consumption_format=excluded.consumption_format,
            first_synced=COALESCE(first_synced, excluded.first_synced),
            last_updated=excluded.last_updated,
            active=excluded.active
    """, (
        library_id, metadata_id, title, subtitle, author, format,
        content_type, description, call_number,
        publication_year, primary_language, isbn,
        subjects, composite_subjects, genres, series,
        super_formats, consumption_format,
        active, datetime.utcnow().isoformat()
    ))


def update_work_id(conn, library_id, metadata_id, work_id):
    conn.execute(
        "UPDATE books_in_library SET work_id = ? WHERE metadata_id = ? AND library_id = ?",
        (work_id, metadata_id, library_id)
    )


def upsert_work(conn, work_id, title, author=None, description=None,
                subjects=None, series=None, first_publish_year=None):
    conn.execute("""
        INSERT INTO works (work_id, title, author, description, subjects,
                           series, first_publish_year, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
            title=excluded.title, author=excluded.author,
            description=excluded.description, subjects=excluded.subjects,
            series=excluded.series,
            first_publish_year=excluded.first_publish_year,
            last_updated=excluded.last_updated
    """, (
        work_id, title, author, description, subjects,
        series, first_publish_year,
        datetime.utcnow().isoformat()
    ))


def upsert_borrow_event(conn, library_id, metadata_id, checkout_date, source,
                        library_entry_id, is_current=0):
    conn.execute("""
        INSERT INTO borrow_events (library_id, metadata_id, checkout_date,
                                   source, library_entry_id, is_current)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(library_entry_id) DO NOTHING
    """, (library_id, metadata_id, checkout_date, source, library_entry_id, is_current))


def clear_current_checkouts(conn):
    conn.execute("DELETE FROM borrow_events WHERE source = 'checkout'")


def get_borrow_event_ids(conn):
    rows = conn.execute("SELECT library_entry_id FROM borrow_events").fetchall()
    return {r["library_entry_id"] for r in rows}


def get_borrow_events_for_recommendation(conn, library_id):
    rows = conn.execute("""
        SELECT b.metadata_id, b.checkout_date, b.is_current
        FROM borrow_events b
        INNER JOIN books_in_library bk
            ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
        WHERE bk.active = 1 AND b.library_id = ?
    """, (library_id,)).fetchall()
    return [dict(r) for r in rows]


def clear_recommendation_cache(conn):
    conn.execute("DELETE FROM recommendation_cache")


def upsert_recommendation(conn, metadata_id, score, category, category_rank):
    conn.execute("""
        INSERT INTO recommendation_cache (metadata_id, score, category, category_rank)
        VALUES (?, ?, ?, ?)
    """, (metadata_id, score, category, category_rank))


def get_category_order(conn, library_id):
    rows = conn.execute("""
        SELECT b.call_number, e.checkout_date, e.is_current
        FROM borrow_events e
        INNER JOIN books_in_library b
            ON b.metadata_id = e.metadata_id AND b.library_id = e.library_id
        WHERE b.active = 1 AND e.checkout_date IS NOT NULL AND e.library_id = ?
    """, (library_id,)).fetchall()
    return [dict(r) for r in rows]


def get_recommendation_sync_time(conn):
    row = conn.execute("SELECT synced_at FROM recommendation_cache LIMIT 1").fetchone()
    if row and row["synced_at"]:
        return row["synced_at"] + "Z"
    return None


def get_work_ids_with_isbns(conn):
    rows = conn.execute("""
        SELECT DISTINCT work_id FROM books_in_library
        WHERE work_id IS NOT NULL
    """).fetchall()
    return [r["work_id"] for r in rows]


def get_isbns_without_work(conn):
    rows = conn.execute("""
        SELECT DISTINCT isbn FROM books_in_library
        WHERE isbn IS NOT NULL AND isbn != '' AND work_id IS NULL
    """).fetchall()
    return [r["isbn"] for r in rows]


def get_active_books(conn, library_id):
    rows = conn.execute("""
        SELECT b.metadata_id, b.call_number, b.publication_year, w.work_id
        FROM books_in_library b
        LEFT JOIN works w ON w.work_id = b.work_id
        WHERE b.active = 1 AND b.library_id = ?
    """, (library_id,)).fetchall()
    return [dict(r) for r in rows]


def start_sync_log(conn, library_id, sync_type, query_params, pages_total):
    cur = conn.execute("""
        INSERT INTO sync_log (library_id, sync_type, query_params, pages_total, status)
        VALUES (?, ?, ?, ?, 'running')
    """, (library_id, sync_type, query_params, pages_total))
    conn.commit()
    return cur.lastrowid


def update_sync_progress(conn, log_id, pages_completed):
    conn.execute("""
        UPDATE sync_log SET pages_completed = ?
        WHERE id = ?
    """, (pages_completed, log_id))
    conn.commit()


def complete_sync_log(conn, log_id, books_total, status='completed'):
    conn.execute("""
        UPDATE sync_log SET books_total = ?, status = ?,
                            completed_at = ?
        WHERE id = ?
    """, (books_total, status, datetime.utcnow().isoformat(), log_id))
    conn.commit()


def get_stats(conn):
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN author IS NOT NULL AND author != '' THEN 1 ELSE 0 END) as with_author,
            SUM(CASE WHEN subjects IS NOT NULL AND subjects != '[]' THEN 1 ELSE 0 END) as with_subjects,
            SUM(CASE WHEN description IS NOT NULL AND description != '' THEN 1 ELSE 0 END) as with_description,
            SUM(CASE WHEN content_type IS NOT NULL THEN 1 ELSE 0 END) as with_content_type,
            SUM(CASE WHEN series IS NOT NULL AND series != '[]' THEN 1 ELSE 0 END) as with_series
        FROM books_in_library WHERE active = 1
    """).fetchone()
    return dict(row)


def get_format_distribution(conn):
    rows = conn.execute("""
        SELECT format, COUNT(*) as count
        FROM books_in_library WHERE active = 1 GROUP BY format ORDER BY count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_content_type_distribution(conn):
    rows = conn.execute("""
        SELECT content_type, COUNT(*) as count
        FROM books_in_library WHERE active = 1 GROUP BY content_type ORDER BY count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_language_distribution(conn):
    rows = conn.execute("""
        SELECT primary_language, COUNT(*) as count
        FROM books_in_library WHERE active = 1 GROUP BY primary_language
        ORDER BY count DESC LIMIT 20
    """).fetchall()
    return [dict(r) for r in rows]


def get_year_distribution(conn):
    rows = conn.execute("""
        SELECT publication_year, COUNT(*) as count
        FROM books_in_library WHERE active = 1 AND publication_year IS NOT NULL
        GROUP BY publication_year ORDER BY publication_year DESC LIMIT 30
    """).fetchall()
    return [dict(r) for r in rows]


def get_sample_books(conn, n=10):
    rows = conn.execute("""
        SELECT metadata_id, library_id, title, subtitle, author, format,
               content_type, publication_year,
               subjects, genres, description
        FROM books_in_library ORDER BY RANDOM() LIMIT ?
    """, (n,)).fetchall()
    return [dict(r) for r in rows]


def get_book_count(conn):
    row = conn.execute("SELECT COUNT(*) as cnt FROM books_in_library").fetchone()
    return row['cnt']
