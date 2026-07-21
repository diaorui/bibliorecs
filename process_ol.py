#!/usr/bin/env python3
import json
import os
import time
import config
import db

try:
    import duckdb
except ImportError:
    duckdb = None


def _extract_description(work_json):
    raw = work_json.get("description")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("value") or None
    return None


def _extract_year(date_str):
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, TypeError):
        return None


def _author_key_to_id(author_key):
    return author_key.replace("/authors/", "")


def run(conn):
    if duckdb is None:
        print("  duckdb not installed — skipping OL processing")
        print("  Install: pip install duckdb")
        return

    print("  Rebuilding works table from fresh dumps...")
    conn.execute("DELETE FROM works")
    conn.commit()

    editions_path = config.OL_EDITIONS_DUMP
    works_path = config.OL_WORKS_DUMP

    if not os.path.exists(editions_path) or not os.path.exists(works_path):
        print(f"  OL dumps not found — skipping OL processing")
        print(f"  Expected: {editions_path}")
        print(f"            {works_path}")
        print(f"  Download from: https://openlibrary.org/developers/dumps")
        return

    t0 = time.time()

    isbns = [
        r["isbn"] for r in
        conn.execute("SELECT DISTINCT isbn FROM books_in_library WHERE isbn IS NOT NULL AND isbn != ''").fetchall()
    ]
    if not isbns:
        print("  No ISBNs in database — skipping OL processing")
        return

    isbn_set = set(isbns)
    print(f"  Matching {len(isbn_set):,} unique ISBNs against OL editions dump...")

    con = duckdb.connect()

    _map_isbns_to_works(con, conn, editions_path, isbn_set)
    _populate_works(con, conn, works_path)

    con.close()

    elapsed = time.time() - t0
    print(f"  OL processing done in {elapsed:.1f}s")

    work_count = conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    bil_with_work = conn.execute(
        "SELECT COUNT(*) FROM books_in_library WHERE work_id IS NOT NULL"
    ).fetchone()[0]
    bil_total = conn.execute("SELECT COUNT(*) FROM books_in_library").fetchone()[0]
    print(f"  {work_count:,} works in DB | {bil_with_work:,}/{bil_total:,} books resolved")


def _map_isbns_to_works(con, conn, editions_path, isbn_set):
    t0 = time.time()

    print(f"    Loading editions dump and extracting ISBNs...", end="", flush=True)
    con.execute(f"""
        CREATE TEMP TABLE raw_eds AS
        SELECT
            json_extract_string(j, '$.isbn_13[0]') AS isbn13,
            json_extract_string(j, '$.isbn_10[0]') AS isbn10,
            json_extract_string(j, '$.works[0].key') AS work_key
        FROM (
            SELECT json AS j
            FROM read_csv('{editions_path}', delim='\t', header=false,
                          columns={{'type': 'VARCHAR', 'key': 'VARCHAR',
                                    'revision': 'INT', 'last_modified': 'VARCHAR',
                                    'json': 'VARCHAR'}},
                          auto_detect=false, max_line_size=100000000)
            WHERE json IS NOT NULL
        )
        WHERE json_extract_string(j, '$.works[0].key') IS NOT NULL
    """)
    row_count = con.execute("SELECT COUNT(*) FROM raw_eds").fetchone()[0]
    t1 = time.time()
    print(f" raw_eds loaded: {row_count:,} rows ({t1-t0:.1f}s)")

    print(f"    Scanning raw_eds for ISBN matches...", end="", flush=True)
    isbn_to_work_id = {}
    total_rows = 0
    result = con.execute("SELECT isbn13, isbn10, work_key FROM raw_eds")
    while True:
        chunk = result.fetchmany(500000)
        if not chunk:
            break
        for isbn13, isbn10, work_key in chunk:
            total_rows += 1
            if isbn13 and isbn13 in isbn_set:
                isbn_to_work_id[isbn13] = work_key.replace("/works/", "")
            if isbn10 and isbn10 in isbn_set and isbn10 not in isbn_to_work_id:
                isbn_to_work_id[isbn10] = work_key.replace("/works/", "")
    t2 = time.time()
    print(f" scanned {total_rows:,} rows, {len(isbn_to_work_id):,} matches ({t2-t1:.1f}s)")

    print(f"    Updating books_in_library...", end="", flush=True)
    conn.executemany(
        "UPDATE books_in_library SET work_id = ? WHERE isbn = ? AND work_id IS NULL",
        [(work_id, isbn) for isbn, work_id in isbn_to_work_id.items()]
    )
    t3 = time.time()
    print(f" {len(isbn_to_work_id):,} rows updated ({t3-t2:.1f}s)")

    con.execute("DROP TABLE raw_eds")
    conn.commit()
    t4 = time.time()
    print(f"    ISBN → work_id mapping done ({t4-t0:.1f}s)")


def _populate_works(con, conn, works_path):
    work_ids = [
        r["work_id"] for r in
        conn.execute("SELECT DISTINCT work_id FROM books_in_library WHERE work_id IS NOT NULL").fetchall()
    ]
    if not work_ids:
        print("    No work_ids to process")
        return

    print(f"    Loading works dump into memory...", end="", flush=True)
    con.execute(f"""
        CREATE TEMP TABLE raw_works AS
        SELECT json AS j, key
        FROM read_csv('{works_path}', delim='\t', header=false,
                      columns={{'type': 'VARCHAR', 'key': 'VARCHAR',
                                'revision': 'INT', 'last_modified': 'VARCHAR',
                                'json': 'VARCHAR'}},
                      auto_detect=false, max_line_size=100000000)
    """)
    row_count = con.execute("SELECT COUNT(*) FROM raw_works").fetchone()[0]
    print(f" {row_count:,} rows loaded, fetching {len(work_ids):,} works...")

    work_key_list = [f"/works/{w}" for w in work_ids]
    con.execute("CREATE TEMP TABLE lookup_work_keys AS SELECT unnest($1)::VARCHAR AS wk", [work_key_list])

    rows = con.execute("""
        SELECT j FROM raw_works r
        INNER JOIN lookup_work_keys l ON r.key = l.wk
    """).fetchall()

    con.execute("DROP TABLE lookup_work_keys")

    populated = 0
    for (j,) in rows:
        try:
            w = json.loads(j)
        except (json.JSONDecodeError, TypeError):
            continue
        work_id = w.get("key", "").replace("/works/", "")
        if not work_id:
            continue

        title = w.get("title", "")
        subjects = w.get("subjects")
        series_raw = w.get("series")
        description = _extract_description(w)
        year = _extract_year(w.get("first_publish_date"))

        subjects_json = json.dumps(subjects, ensure_ascii=False) if subjects else None
        series_json = None
        if series_raw:
            names = [s.get("series", {}).get("key", "") for s in series_raw
                     if isinstance(s, dict)]
            series_json = json.dumps(names, ensure_ascii=False) if names else None

        db.upsert_work(
            conn, work_id, title=title,
            description=description, subjects=subjects_json,
            series=series_json, first_publish_year=year,
        )

        author_rows = conn.execute("""
            SELECT DISTINCT author FROM books_in_library
            WHERE work_id = ? AND author IS NOT NULL AND author != ''
        """, (work_id,)).fetchall()
        if author_rows:
            authors_list = [r["author"] for r in author_rows]
            best_author = max(set(authors_list), key=authors_list.count)
            conn.execute(
                "UPDATE works SET author = ? WHERE work_id = ? AND (author IS NULL OR author = '')",
                (best_author, work_id)
            )

        series_rows = conn.execute("""
            SELECT series FROM books_in_library
            WHERE work_id = ? AND series IS NOT NULL AND series != '[]'
        """, (work_id,)).fetchall()
        if series_rows:
            candidates = []
            for r in series_rows:
                try:
                    parsed = json.loads(r["series"])
                    if parsed:
                        candidates.extend(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            if candidates:
                best_series = max(set(candidates), key=candidates.count)
                conn.execute(
                    "UPDATE works SET series = ? WHERE work_id = ? AND (series IS NULL OR series = '[]')",
                    (json.dumps([best_series], ensure_ascii=False), work_id)
                )

        populated += 1

    con.execute("DROP TABLE raw_works")
    conn.commit()
    print(f"    {populated:,} works populated")


def main():
    conn = db.get_conn()
    run(conn)
    conn.close()


if __name__ == "__main__":
    main()
