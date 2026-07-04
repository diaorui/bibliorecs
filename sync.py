#!/usr/bin/env python3
"""
Sync library children's paper books into SQLite.

Usage:
    python sync.py                    # full sync (all paper formats)
    python sync.py --format BK        # sync only one format
    python sync.py --pages 10         # sync first N pages only (test run)
    python sync.py --resume 42        # resume from page 42
    python sync.py --incremental      # recently added books
"""

import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
import api
import generate_embeddings

QUERY = 'audience:"children" branch:"' + config.HOME_BRANCH + '"'
PAPER_FORMATS = ["BK", "PICTURE_BOOK", "PAPERBACK", "BOARD_BK", "GRAPHIC_NOVEL"]

FORMAT_LABELS = {
    "BK": "Book",
    "PICTURE_BOOK": "Picture Book",
    "PAPERBACK": "Paperback",
    "BOARD_BK": "Board Book",
    "GRAPHIC_NOVEL": "Graphic Novel",
}

MAX_PAGE_RETRIES = 3
COMMIT_INTERVAL = 50

_t0 = 0


def run_sync(formats=None, max_pages=None, resume_from=None):
    global _t0
    _t0 = time.time()
    fmt_list = formats or PAPER_FORMATS
    fmt_label = ", ".join(FORMAT_LABELS.get(f, f) for f in fmt_list)
    print(f"Sync: query='{QUERY}' | formats: [{fmt_label}]")
    if max_pages:
        print(f"      max pages: {max_pages}")

    conn = db.get_conn()
    failed_pages = []
    synced_mids = set()

    if resume_from:
        page = resume_from
        log_id = _get_latest_sync_log_id(conn)
        data = api.search_bibs_json(QUERY, formats=fmt_list, f_circ="CIRC",
                                    page=page, sort="newly_acquired", limit=100)
        pagination = api.parse_pagination(data)
        total_pages = pagination.get("pages", 1)
        if max_pages:
            total_pages = min(page + max_pages - 1, total_pages)
        synced_mids.update(_process_page(conn, data))
        db.update_sync_progress(conn, log_id, page)
        print(f"  Page {page}/{total_pages} done — {db.get_book_count(conn):,} books")
        page += 1
    else:
        page = 1
        data = api.search_bibs_json(QUERY, formats=fmt_list, f_circ="CIRC",
                                    page=page, sort="newly_acquired", limit=100)
        pagination = api.parse_pagination(data)
        total_pages = pagination.get("pages", 1)
        total_count = pagination.get("count", 0)
        if max_pages:
            total_pages = min(max_pages, total_pages)
        print(f"      total: {total_count:,} books across {total_pages} pages")
        log_id = db.start_sync_log(conn, "full",
                                   f"query={QUERY}&formats={fmt_list}",
                                   total_pages)
        synced_mids.update(_process_page(conn, data))
        db.update_sync_progress(conn, log_id, page)
        count = db.get_book_count(conn)
        print(f"  Page 1/{total_pages} done — {count:,} books so far")
        page = 2

    processed = 1
    with ThreadPoolExecutor(max_workers=10) as pool:
        fut_map = {
            pool.submit(_fetch_page, p, fmt_list): p
            for p in range(page, total_pages + 1)
        }

        for f in as_completed(fut_map):
            p = fut_map[f]
            try:
                data = f.result()
                mids = _process_page(conn, data)
                synced_mids.update(mids)
                db.update_sync_progress(conn, log_id, p)
                processed += 1

                if p % COMMIT_INTERVAL == 0:
                    conn.commit()
                    elapsed = time.time() - _t0
                    rate = processed / elapsed * 60
                    eta_min = (total_pages - processed) / rate if rate > 0 else 0
                    print(f"  Page {p}/{total_pages} ({processed} done)"
                          f" — {db.get_book_count(conn):,} books"
                          f" — {rate:.0f} pg/min — ETA {eta_min:.0f} min")
            except Exception as e:
                failed_pages.append(p)
                print(f"  Page {p} FAILED: {e}")

        conn.commit()

    if not max_pages and not resume_from:
        _deactivate_stale_books(conn, synced_mids)

    conn.commit()
    total_books = db.get_book_count(conn)
    status = "completed" if not failed_pages else "completed_with_errors"
    db.complete_sync_log(conn, log_id, total_books, status)
    conn.close()

    print("\nRegenerating embeddings after catalog update...")
    generate_embeddings.main()

    if failed_pages:
        print(f"\nWARNING: {len(failed_pages)} page(s) failed: {failed_pages}")
    print(f"\nDONE! {total_books:,} books in database.")


def _fetch_page(page, formats):
    """Fetch a single page from the API (no DB writes). Safe to call from any thread."""
    for attempt in range(MAX_PAGE_RETRIES):
        try:
            return api.search_bibs_json(QUERY, formats=formats, f_circ="CIRC",
                                        page=page, sort="newly_acquired", limit=100)
        except Exception as e:
            if attempt < MAX_PAGE_RETRIES - 1:
                wait = 2 ** attempt
                print(f"  Page {page} error (attempt {attempt + 1}): {e}"
                      f" — retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Page {page} exhausted retries")


def _fetch_and_process_page(conn, page, formats, sort=None):
    for attempt in range(MAX_PAGE_RETRIES):
        try:
            data = api.search_bibs_json(QUERY, formats=formats, f_circ="CIRC",
                                        page=page, sort=sort, limit=100)
        except Exception as e:
            if attempt < MAX_PAGE_RETRIES - 1:
                wait = 2 ** attempt
                print(f"  Page {page} error (attempt {attempt + 1}): {e}"
                      f" — retrying in {wait}s")
                time.sleep(wait)
                continue
            print(f"  Page {page} FAILED after {MAX_PAGE_RETRIES} attempts: {e}")
            return False, set()

        mids = _process_page(conn, data)
        return True, mids

    return False, set()


def _process_page(conn, data):
    entities = api.parse_bib_entities(data)
    mids = set()
    for metadata_id, bib in entities.items():
        mids.add(metadata_id)
        book = api.extract_book_info(metadata_id, bib)
        db.upsert_book(
            conn,
            metadata_id=book["metadata_id"],
            title=book["title"],
            subtitle=book["subtitle"],
            author=book["author"],
            format=book["format"],
            content_type=book["content_type"],
            description=book["description"],
            call_number=book["call_number"],
            publication_year=book["publication_year"],
            primary_language=book["primary_language"],
            isbn=book["isbn"],
            subjects=book["subjects"],
            composite_subjects=book["composite_subjects"],
            genres=book["genres"],
            series=book["series"],
            super_formats=book["super_formats"],
            consumption_format=book["consumption_format"],
        )

        avail = api.extract_availability(metadata_id, bib)
        db.upsert_availability(
            conn,
            metadata_id=avail["metadata_id"],
            status=avail["status"],
            available_copies=avail["available_copies"],
            total_copies=avail["total_copies"],
            held_copies=avail["held_copies"],
            on_order_copies=avail["on_order_copies"],
            localised_status=avail["localised_status"],
            status_type=avail["status_type"],
        )
    return mids


def _deactivate_stale_books(conn, active_mids):
    total = conn.execute("SELECT COUNT(*) as c FROM books WHERE active = 1").fetchone()[0]
    mids_json = json.dumps(list(active_mids))
    conn.execute("""
        UPDATE books SET active = 0
        WHERE active = 1 AND metadata_id NOT IN (
            SELECT value FROM json_each(?)
        )
    """, (mids_json,))
    deactivated = total - conn.execute("SELECT COUNT(*) as c FROM books WHERE active = 1").fetchone()[0]
    if deactivated:
        print(f"  Deactivated {deactivated:,} books no longer in branch catalog")
        conn.execute("""
            DELETE FROM availability
            WHERE metadata_id NOT IN (SELECT metadata_id FROM books WHERE active = 1)
        """)
        print(f"  Cleaned up availability for deactivated books")


def _get_latest_sync_log_id(conn):
    row = conn.execute(
        "SELECT id FROM sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def run_incremental(formats=None):
    fmt_list = formats or PAPER_FORMATS
    print(f"Fetching recently added children's paper books...")

    conn = db.get_conn()

    data = api.search_bibs_json(QUERY, formats=fmt_list, f_circ="CIRC",
                                page=1, sort="newly_acquired", limit=100)
    pagination = api.parse_pagination(data)
    total_pages = pagination.get("pages", 1)
    print(f"  Recent: {pagination.get('count', 0):,} books, {total_pages} pages")

    log_id = db.start_sync_log(conn, "incremental",
                               f"query={QUERY}&formatcode=({', '.join(fmt_list)})",
                               total_pages)

    failed_pages = []
    for page in range(1, min(total_pages + 1, 10)):
        if page > 1:
            ok = _fetch_and_process_page(conn, page, fmt_list, sort="newly_acquired")
            if not ok:
                failed_pages.append(page)
                continue
        else:
            _process_page(conn, data)

        db.update_sync_progress(conn, log_id, page)
        if page % 5 == 0:
            conn.commit()
        print(f"  Page {page}/{total_pages} done")

    conn.commit()
    total_books = db.get_book_count(conn)
    status = "completed" if not failed_pages else "completed_with_errors"
    db.complete_sync_log(conn, log_id, total_books, status)
    conn.close()

    if failed_pages:
        print(f"  WARNING: {len(failed_pages)} page(s) failed: {failed_pages}")
    print(f"Incremental done. Total in DB: {total_books:,}")


if __name__ == "__main__":
    _t0 = time.time()

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    fmts = None
    if "--format" in sys.argv:
        fmts = []
        for i, arg in enumerate(sys.argv):
            if arg == "--format" and i + 1 < len(sys.argv):
                fmts.append(sys.argv[i + 1])

    max_pages = None
    if "--pages" in sys.argv:
        idx = sys.argv.index("--pages")
        max_pages = int(sys.argv[idx + 1])

    if "--incremental" in sys.argv:
        run_incremental(formats=fmts)
    elif "--resume" in sys.argv:
        idx = sys.argv.index("--resume")
        resume_page = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else None
        run_sync(formats=fmts, resume_from=resume_page)
    else:
        run_sync(formats=fmts, max_pages=max_pages)
