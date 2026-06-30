import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "books.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS books (
    metadata_id TEXT PRIMARY KEY,
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
    first_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_books_format ON books(format);
CREATE INDEX IF NOT EXISTS idx_books_author ON books(author);
CREATE INDEX IF NOT EXISTS idx_books_year ON books(publication_year);
CREATE INDEX IF NOT EXISTS idx_books_lang ON books(primary_language);
CREATE INDEX IF NOT EXISTS idx_books_content_type ON books(content_type);

CREATE TABLE IF NOT EXISTS availability (
    metadata_id TEXT PRIMARY KEY,
    status TEXT,
    available_copies INTEGER DEFAULT 0,
    total_copies INTEGER DEFAULT 0,
    held_copies INTEGER DEFAULT 0,
    on_order_copies INTEGER DEFAULT 0,
    localised_status TEXT,
    status_type TEXT,
    at_home INTEGER DEFAULT 0,
    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS borrow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metadata_id TEXT NOT NULL,
    checkout_date TEXT,
    source TEXT NOT NULL CHECK(source IN ('history', 'checkout')),
    library_entry_id TEXT UNIQUE,
    is_current INTEGER DEFAULT 0,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_borrow_events_meta ON borrow_events(metadata_id);
CREATE INDEX IF NOT EXISTS idx_borrow_events_source ON borrow_events(source);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type TEXT NOT NULL,
    query_params TEXT,
    pages_total INTEGER DEFAULT 0,
    pages_completed INTEGER DEFAULT 0,
    books_total INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


def _migrate_schema(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(books)").fetchall()]
    if "cover_url" in cols:
        conn.execute("ALTER TABLE books DROP COLUMN cover_url")
        print("  Migration: dropped cover_url column from books")

    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "recommendation_cache" in tables:
        rec_cols = [r[1] for r in conn.execute("PRAGMA table_info(recommendation_cache)").fetchall()]
        if "owns_home" in rec_cols:
            conn.execute("DROP INDEX IF EXISTS idx_rec_owns")
            conn.execute("ALTER TABLE recommendation_cache DROP COLUMN owns_home")
            print("  Migration: dropped owns_home from recommendation_cache")
        if "rank" in rec_cols or "owns_central" in rec_cols:
            conn.execute("DROP TABLE recommendation_cache")
            print("  Migration: dropped old recommendation_cache table")
            _create_rec_cache(conn)
            print("  Migration: created new recommendation_cache table")
    else:
        _create_rec_cache(conn)

    idx_names = [r[2] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'").fetchall()
                 if r[2] is not None]
    if "idx_rec_cat" not in idx_names:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_cat ON recommendation_cache(category)")

    avail_cols = [r[1] for r in conn.execute("PRAGMA table_info(availability)").fetchall()]
    if "at_central" in avail_cols:
        conn.execute("ALTER TABLE availability RENAME COLUMN at_central TO at_home")
        print("  Migration: renamed at_central to at_home")
    elif "at_home" not in avail_cols:
        conn.execute("ALTER TABLE availability ADD COLUMN at_home INTEGER DEFAULT 0")
        print("  Migration: added at_home to availability")

def _create_rec_cache(conn):
    conn.execute("""
        CREATE TABLE recommendation_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metadata_id TEXT NOT NULL,
            score REAL,
            category TEXT NOT NULL,
            category_rank INTEGER NOT NULL,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    _migrate_schema(conn)
    conn.commit()
    conn.close()


def upsert_book(conn, metadata_id, title, subtitle=None, author=None,
                format=None, content_type=None, description=None,
                call_number=None, publication_year=None,
                primary_language=None, isbn=None, subjects=None,
                composite_subjects=None, genres=None, series=None,
                super_formats=None, consumption_format=None,
                active=1):
    conn.execute("""
        INSERT INTO books (metadata_id, title, subtitle, author, format,
                           content_type, description, call_number,
                           publication_year, primary_language, isbn,
                           subjects, composite_subjects, genres, series,
                           super_formats, consumption_format,
                           active, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?)
        ON CONFLICT(metadata_id) DO UPDATE SET
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
        metadata_id, title, subtitle, author, format,
        content_type, description, call_number,
        publication_year, primary_language, isbn,
        subjects, composite_subjects, genres, series,
        super_formats, consumption_format,
        active, datetime.utcnow().isoformat()
    ))


def upsert_availability(conn, metadata_id, status, available_copies,
                        total_copies, held_copies, on_order_copies=0,
                        localised_status=None, status_type=None,
                        at_home=0):
    conn.execute("""
        INSERT INTO availability (metadata_id, status, available_copies,
                                  total_copies, held_copies, on_order_copies,
                                  localised_status, status_type, at_home,
                                  last_checked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(metadata_id) DO UPDATE SET
            status=excluded.status,
            available_copies=excluded.available_copies,
            total_copies=excluded.total_copies,
            held_copies=excluded.held_copies,
            on_order_copies=excluded.on_order_copies,
            localised_status=excluded.localised_status,
            status_type=excluded.status_type,
            at_home=excluded.at_home,
            last_checked=excluded.last_checked
    """, (
        metadata_id, status, available_copies,
        total_copies, held_copies, on_order_copies,
        localised_status, status_type, int(at_home),
        datetime.utcnow().isoformat()
    ))


def upsert_borrow_event(conn, metadata_id, checkout_date, source, library_entry_id, is_current=0):
    conn.execute("""
        INSERT INTO borrow_events (metadata_id, checkout_date, source, library_entry_id, is_current)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(library_entry_id) DO NOTHING
    """, (metadata_id, checkout_date, source, library_entry_id, is_current))


def clear_current_checkouts(conn):
    conn.execute("DELETE FROM borrow_events WHERE source = 'checkout'")


def get_borrow_event_ids(conn):
    rows = conn.execute("SELECT library_entry_id FROM borrow_events").fetchall()
    return {r["library_entry_id"] for r in rows}


def get_borrow_events_for_recommendation(conn):
    rows = conn.execute("""
        SELECT b.metadata_id, b.checkout_date, b.is_current
        FROM borrow_events b
        INNER JOIN books bk ON bk.metadata_id = b.metadata_id
        WHERE bk.active = 1
    """).fetchall()
    return [dict(r) for r in rows]


def clear_recommendation_cache(conn):
    conn.execute("DELETE FROM recommendation_cache")


def upsert_recommendation(conn, metadata_id, score, category, category_rank):
    conn.execute("""
        INSERT INTO recommendation_cache (metadata_id, score, category, category_rank)
        VALUES (?, ?, ?, ?)
    """, (metadata_id, score, category, category_rank))


def get_category_order(conn):
    rows = conn.execute("""
        SELECT b.call_number, COUNT(*) as cnt
        FROM borrow_events e
        INNER JOIN books b ON b.metadata_id = e.metadata_id
        WHERE b.active = 1
        GROUP BY b.call_number
        ORDER BY cnt DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_recommendation_sync_time(conn):
    row = conn.execute("SELECT synced_at FROM recommendation_cache LIMIT 1").fetchone()
    if row and row["synced_at"]:
        return row["synced_at"] + "Z"
    return None


def start_sync_log(conn, sync_type, query_params, pages_total):
    cur = conn.execute("""
        INSERT INTO sync_log (sync_type, query_params, pages_total, status)
        VALUES (?, ?, ?, 'running')
    """, (sync_type, query_params, pages_total))
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
        FROM books WHERE active = 1
    """).fetchone()
    return dict(row)


def get_format_distribution(conn):
    rows = conn.execute("""
        SELECT format, COUNT(*) as count
        FROM books WHERE active = 1 GROUP BY format ORDER BY count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_content_type_distribution(conn):
    rows = conn.execute("""
        SELECT content_type, COUNT(*) as count
        FROM books WHERE active = 1 GROUP BY content_type ORDER BY count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_language_distribution(conn):
    rows = conn.execute("""
        SELECT primary_language, COUNT(*) as count
        FROM books WHERE active = 1 GROUP BY primary_language ORDER BY count DESC LIMIT 20
    """).fetchall()
    return [dict(r) for r in rows]


def get_year_distribution(conn):
    rows = conn.execute("""
        SELECT publication_year, COUNT(*) as count
        FROM books WHERE active = 1 AND publication_year IS NOT NULL
        GROUP BY publication_year ORDER BY publication_year DESC LIMIT 30
    """).fetchall()
    return [dict(r) for r in rows]


def get_sample_books(conn, n=10):
    rows = conn.execute("""
        SELECT metadata_id, title, subtitle, author, format,
               content_type, publication_year,
               subjects, genres, description
        FROM books ORDER BY RANDOM() LIMIT ?
    """, (n,)).fetchall()
    return [dict(r) for r in rows]


def get_book_count(conn):
    row = conn.execute("SELECT COUNT(*) as cnt FROM books").fetchone()
    return row['cnt']
