import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "books.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS books_in_library (
    metadata_id TEXT NOT NULL,
    library_id TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    authors TEXT,
    format TEXT,
    content_type TEXT,
    description TEXT,
    call_number TEXT,
    publication_year INTEGER,
    primary_language TEXT,
    isbns TEXT,
    subjects TEXT,
    composite_subjects TEXT,
    genres TEXT,
    series TEXT,
    super_formats TEXT,
    consumption_format TEXT,
    group_key TEXT,
    edition TEXT,
    multiscript_title TEXT,
    multiscript_author TEXT,
    rating_avg INTEGER,
    rating_count INTEGER,
    active INTEGER DEFAULT 1,
    first_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (metadata_id, library_id)
);

CREATE INDEX IF NOT EXISTS idx_bil_library ON books_in_library(library_id);
CREATE INDEX IF NOT EXISTS idx_bil_format ON books_in_library(format);
CREATE INDEX IF NOT EXISTS idx_bil_year ON books_in_library(publication_year);
CREATE INDEX IF NOT EXISTS idx_bil_lang ON books_in_library(primary_language);
CREATE INDEX IF NOT EXISTS idx_bil_content ON books_in_library(content_type);

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

CREATE TABLE IF NOT EXISTS branches (
    library_id TEXT NOT NULL,
    branch_code TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    PRIMARY KEY (library_id, branch_code)
);

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
                           authors=None, format=None, content_type=None,
                           description=None, call_number=None,
                           publication_year=None, primary_language=None,
                           isbns=None, subjects=None,
                           composite_subjects=None, genres=None, series=None,
                           super_formats=None, consumption_format=None,
                           group_key=None,
                           edition=None, multiscript_title=None,
                           multiscript_author=None, rating_avg=None,
                           rating_count=None, active=1):
    conn.execute("""
        INSERT INTO books_in_library (
            library_id, metadata_id, title, subtitle, authors, format,
            content_type, description, call_number,
            publication_year, primary_language, isbns,
            subjects, composite_subjects, genres, series,
            super_formats, consumption_format, group_key,
            edition, multiscript_title, multiscript_author,
            rating_avg, rating_count,
            active, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(metadata_id, library_id) DO UPDATE SET
            title=excluded.title, subtitle=excluded.subtitle,
            authors=excluded.authors, format=excluded.format,
            content_type=excluded.content_type,
            description=excluded.description,
            call_number=excluded.call_number,
            publication_year=excluded.publication_year,
            primary_language=excluded.primary_language,
            isbns=excluded.isbns, subjects=excluded.subjects,
            composite_subjects=excluded.composite_subjects,
            genres=excluded.genres, series=excluded.series,
            super_formats=excluded.super_formats,
            consumption_format=excluded.consumption_format,
            group_key=excluded.group_key,
            edition=excluded.edition,
            multiscript_title=excluded.multiscript_title,
            multiscript_author=excluded.multiscript_author,
            rating_avg=excluded.rating_avg,
            rating_count=excluded.rating_count,
            first_synced=COALESCE(first_synced, excluded.first_synced),
            last_updated=excluded.last_updated,
            active=excluded.active
    """, (
        library_id, metadata_id, title, subtitle, authors, format,
        content_type, description, call_number,
        publication_year, primary_language, isbns,
        subjects, composite_subjects, genres, series,
        super_formats, consumption_format, group_key,
        edition, multiscript_title, multiscript_author,
        rating_avg, rating_count,
        active, datetime.utcnow().isoformat()
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


def get_borrow_events_for_recommendation(conn, library_id=None):
    if library_id:
        rows = conn.execute("""
            SELECT b.metadata_id, b.checkout_date, b.is_current, bk.group_key, bk.isbns
            FROM borrow_events b
            INNER JOIN books_in_library bk
                ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
            WHERE bk.active = 1 AND b.library_id = ?
        """, (library_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT b.metadata_id, b.checkout_date, b.is_current, bk.group_key, bk.isbns
            FROM borrow_events b
            INNER JOIN books_in_library bk
                ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
            WHERE bk.active = 1
        """).fetchall()
    return [dict(r) for r in rows]


def get_category_order(conn, library_id):
    rows = conn.execute("""
        SELECT b.call_number, b.format, b.content_type, b.genres,
               e.checkout_date, e.is_current
        FROM borrow_events e
        INNER JOIN books_in_library b
            ON b.metadata_id = e.metadata_id AND b.library_id = e.library_id
        WHERE b.active = 1 AND e.checkout_date IS NOT NULL AND e.library_id = ?
    """, (library_id,)).fetchall()
    return [dict(r) for r in rows]


def get_active_books(conn, library_id):
    rows = conn.execute("""
        SELECT metadata_id, call_number, publication_year
        FROM books_in_library
        WHERE active = 1 AND library_id = ?
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


def get_stats(conn, library_id=None):
    where = "WHERE active = 1"
    params = []
    if library_id:
        where += " AND library_id = ?"
        params.append(library_id)
    row = conn.execute(f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN authors IS NOT NULL AND authors != '[]' THEN 1 ELSE 0 END) as with_author,
            SUM(CASE WHEN subjects IS NOT NULL AND subjects != '[]' THEN 1 ELSE 0 END) as with_subjects,
            SUM(CASE WHEN description IS NOT NULL AND description != '' THEN 1 ELSE 0 END) as with_description,
            SUM(CASE WHEN content_type IS NOT NULL THEN 1 ELSE 0 END) as with_content_type,
            SUM(CASE WHEN series IS NOT NULL AND series != '[]' THEN 1 ELSE 0 END) as with_series
        FROM books_in_library {where}
    """, params).fetchone()
    return dict(row)


def get_format_distribution(conn, library_id=None):
    where = "WHERE active = 1"
    params = []
    if library_id:
        where += " AND library_id = ?"
        params.append(library_id)
    rows = conn.execute(f"""
        SELECT format, COUNT(*) as count
        FROM books_in_library {where} GROUP BY format ORDER BY count DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_content_type_distribution(conn):
    rows = conn.execute("""
        SELECT content_type, COUNT(*) as count
        FROM books_in_library WHERE active = 1 GROUP BY content_type ORDER BY count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_language_distribution(conn, library_id=None):
    where = "WHERE active = 1"
    params = []
    if library_id:
        where += " AND library_id = ?"
        params.append(library_id)
    rows = conn.execute(f"""
        SELECT primary_language, COUNT(*) as count
        FROM books_in_library {where} GROUP BY primary_language
        ORDER BY count DESC LIMIT 20
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_year_distribution(conn, library_id=None):
    where = "WHERE active = 1 AND publication_year IS NOT NULL"
    params = []
    if library_id:
        where += " AND library_id = ?"
        params.append(library_id)
    rows = conn.execute(f"""
        SELECT publication_year, COUNT(*) as count
        FROM books_in_library {where}
        GROUP BY publication_year ORDER BY publication_year DESC LIMIT 30
    """, params).fetchall()
    return [dict(r) for r in rows]


def get_sample_books(conn, n=10):
    rows = conn.execute("""
        SELECT metadata_id, library_id, title, subtitle, authors, format,
               content_type, publication_year,
               subjects, genres, description
        FROM books_in_library ORDER BY RANDOM() LIMIT ?
    """, (n,)).fetchall()
    return [dict(r) for r in rows]


def get_book_count(conn, library_id=None):
    if library_id:
        row = conn.execute("SELECT COUNT(*) as cnt FROM books_in_library WHERE library_id = ?", (library_id,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as cnt FROM books_in_library").fetchone()
    return row['cnt']


def replace_branches(conn, library_id, branches):
    conn.execute("DELETE FROM branches WHERE library_id = ?", (library_id,))
    for b in branches:
        conn.execute(
            "INSERT INTO branches (library_id, branch_code, branch_name) VALUES (?, ?, ?)",
            (library_id, b["code"], b["name"])
        )
    conn.commit()


def validate_branch(conn, library_id, branch_code):
    if not library_id or not branch_code:
        return False
    row = conn.execute(
        "SELECT 1 FROM branches WHERE library_id = ? AND branch_code = ?",
        (library_id, branch_code)
    ).fetchone()
    return row is not None
