#!/usr/bin/env python3
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
import api

QUERY = 'audience:"children"'

MAX_PAGE_RETRIES = 3
COMMIT_INTERVAL = 50

_t0 = 0


_NON_PHYSICAL = {"EBOOK", "AUDIOBOOK", "MUSIC_CD", "PLAYAWAY_AUDIOBOOK",
                 "BOOK_CD", "VIDEO_GAME", "DVD", "BLURAY", "BR", "KIT", "MAG"}


def _discover_formats(library_id):
    print(f"  Discovering physical book formats...", end="", flush=True)
    data = api.search_bibs_json(QUERY, library_id=library_id,
                                f_circ="CIRC", page=1, limit=1)
    fields = api.parse_fields(data)
    formats = []
    for f in fields:
        if f.get("id") == "FORMAT":
            formats = [
                ff["value"] for ff in f.get("fieldFilters", [])
                if "BOOKS" in ff.get("groupIds", [])
                and ff["value"] not in _NON_PHYSICAL
            ]
            break
    if not formats:
        raise RuntimeError("no physical book formats discovered from BC API")
    sorted_fmts = sorted(formats)
    print(f" {len(sorted_fmts)} found: {', '.join(sorted_fmts)}")
    return sorted_fmts


# sort stability across runs (tested 2026-07):
#   title / author / published_date → 0.00% drift (fully stable)
#   newly_acquired                  → 0.40% drift (books shift pages)
#   relevancy                       → 1.04% drift (most unstable)
#   call_number                     → HTTP 500    (not supported)
SORT = "title"


def run_sync(library_id, gateway_base, formats=None, max_pages=None, resume_from=None):
    global _t0
    _t0 = time.time()
    if formats is None:
        formats = _discover_formats(library_id)
    fmt_list = formats
    fmt_label = ", ".join(fmt_list)
    print(f"Sync [{library_id}]: query='{QUERY}' | formats: [{fmt_label}]")
    if max_pages:
        print(f"      max pages: {max_pages}")

    conn = db.get_conn()
    failed_pages = []
    synced_mids = set()
    total_record_errors = 0

    if resume_from:
        page = resume_from
        log_id = _get_latest_sync_log_id(conn)
        data = api.search_bibs_json(QUERY, library_id=library_id,
                                    formats=fmt_list, f_circ="CIRC",
                                    page=page, sort=SORT, limit=100)
        pagination = api.parse_pagination(data)
        total_pages = pagination.get("pages", 1)
        if max_pages:
            total_pages = min(page + max_pages - 1, total_pages)
        mids, record_errors = _process_page(conn, data, library_id)
        total_record_errors += record_errors
        synced_mids.update(mids)
        db.update_sync_progress(conn, log_id, page)
        print(f"  Page {page}/{total_pages} done — {db.get_book_count(conn, library_id):,} books")
        page += 1
    else:
        page = 1
        data = api.search_bibs_json(QUERY, library_id=library_id,
                                    formats=fmt_list, f_circ="CIRC",
                                    page=page, sort=SORT, limit=100)
        pagination = api.parse_pagination(data)
        total_pages = pagination.get("pages", 1)
        total_count = pagination.get("count", 0)
        if max_pages:
            total_pages = min(max_pages, total_pages)
        print(f"      total: {total_count:,} books across {total_pages} pages")
        log_id = db.start_sync_log(conn, library_id, "full",
            f"query={QUERY}&formats={fmt_list}",
            total_pages)
        mids, record_errors = _process_page(conn, data, library_id)
        total_record_errors += record_errors
        synced_mids.update(mids)
        db.update_sync_progress(conn, log_id, page)
        count = db.get_book_count(conn, library_id)
        print(f"  Page 1/{total_pages} done — {count:,} books so far")
        page = 2

    processed = 1
    with ThreadPoolExecutor(max_workers=10) as pool:
        fut_map = {
            pool.submit(_fetch_page, p, fmt_list, library_id): p
            for p in range(page, total_pages + 1)
        }

        for f in as_completed(fut_map):
            p = fut_map[f]
            try:
                data = f.result()
                mids, page_errors = _process_page(conn, data, library_id)
                total_record_errors += page_errors
                synced_mids.update(mids)
                processed += 1
                db.update_sync_progress(conn, log_id, processed)

                if p % COMMIT_INTERVAL == 0:
                    conn.commit()
                    elapsed = time.time() - _t0
                    rate = processed / elapsed * 60
                    eta_min = (total_pages - processed) / rate if rate > 0 else 0
                    print(f"  Page {p}/{total_pages} ({processed} done)"
                          f" — {db.get_book_count(conn, library_id):,} books"
                          f" — {rate:.0f} pg/min — ETA {eta_min:.0f} min")
            except Exception as e:
                failed_pages.append(p)
                print(f"  Page {p} FAILED: {e}")

        conn.commit()

    if not max_pages and not resume_from:
        _deactivate_stale_books(conn, synced_mids, library_id)

    conn.commit()
    total_books = db.get_book_count(conn, library_id)
    status = "completed" if not failed_pages else "completed_with_errors"
    db.complete_sync_log(conn, log_id, total_books, status)
    conn.close()

    if failed_pages or total_record_errors:
        parts = []
        if failed_pages:
            parts.append(f"page errors: {len(failed_pages)} ({failed_pages})")
        if total_record_errors:
            parts.append(f"record errors: {total_record_errors}")
        print(f"\nSync warnings: {'; '.join(parts)}")
    print(f"\nDONE! {total_books:,} books in database.")


def _fetch_page(page, formats, library_id):
    for attempt in range(MAX_PAGE_RETRIES):
        try:
            return api.search_bibs_json(QUERY, library_id=library_id,
                                        formats=formats, f_circ="CIRC",
                                        page=page, sort=SORT, limit=100)
        except Exception as e:
            if attempt < MAX_PAGE_RETRIES - 1:
                wait = 2 ** attempt
                print(f"  Page {page} error (attempt {attempt + 1}): {e}"
                      f" — retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Page {page} exhausted retries")


def _process_page(conn, data, library_id):
    entities = api.parse_bib_entities(data)
    mids = set()
    errors = 0
    for metadata_id, bib in entities.items():
        a = bib.get("availability", {})
        if a.get("status") == "ON_ORDER" or a.get("circulationType") == "NON_CIRCULATING":
            continue
        mids.add(metadata_id)
        try:
            book = api.extract_book_info(metadata_id, bib)
            db.upsert_book_in_library(
                conn,
                library_id=library_id,
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
                isbns=book["isbns"],
                subjects=book["subjects"],
                composite_subjects=book["composite_subjects"],
                genres=book["genres"],
                series=book["series"],
                super_formats=book["super_formats"],
                consumption_format=book["consumption_format"],
                group_key=book["group_key"],
                edition=book["edition"],
                multiscript_title=book["multiscript_title"],
                multiscript_author=book["multiscript_author"],
                rating_avg=book["rating_avg"],
                rating_count=book["rating_count"],
            )
        except Exception:
            errors += 1
    return mids, errors


def _deactivate_stale_books(conn, active_mids, library_id):
    total = conn.execute(
        "SELECT COUNT(*) as c FROM books_in_library WHERE active = 1 AND library_id = ?",
        (library_id,)
    ).fetchone()[0]
    if total == 0:
        return

    mids_json = json.dumps(list(active_mids))
    would = conn.execute("""
        SELECT COUNT(*) as c FROM books_in_library
        WHERE active = 1 AND library_id = ? AND metadata_id NOT IN (
            SELECT value FROM json_each(?)
        )
    """, (library_id, mids_json)).fetchone()[0]

    ratio = would / total
    if ratio > config.DEACTIVATE_MAX_RATIO:
        print(f"  SAFETY: would deactivate {would:,}/{total:,} ({ratio:.0%})"
              f" — exceeds {config.DEACTIVATE_MAX_RATIO:.0%}, skipped")
        return

    conn.execute("""
        UPDATE books_in_library SET active = 0
        WHERE active = 1 AND library_id = ? AND metadata_id NOT IN (
            SELECT value FROM json_each(?)
        )
    """, (library_id, mids_json))
    deactivated = total - conn.execute(
        "SELECT COUNT(*) as c FROM books_in_library WHERE active = 1 AND library_id = ?",
        (library_id,)
    ).fetchone()[0]
    if deactivated:
        print(f"  Deactivated {deactivated:,} books no longer in branch catalog")


def _get_latest_sync_log_id(conn):
    row = conn.execute(
        "SELECT id FROM sync_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


if __name__ == "__main__":
    _t0 = time.time()

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    if "--library" not in sys.argv:
        print("Usage: python sync.py --library {lib_id} [--format ...] [--pages N] [--resume]")
        print(f"Libraries: {', '.join(config.LIBRARIES)}")
        sys.exit(1)

    lib_idx = sys.argv.index("--library")
    library_id = sys.argv[lib_idx + 1] if len(sys.argv) > lib_idx + 1 else ""
    if library_id not in config.LIBRARIES:
        print(f"Unknown library: {library_id}")
        sys.exit(1)

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

    gateway_base = config.LIBRARIES[library_id]["gateway_base"]
    if "--resume" in sys.argv:
        idx = sys.argv.index("--resume")
        resume_page = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else None
        run_sync(library_id, gateway_base, formats=fmts, resume_from=resume_page)
    else:
        run_sync(library_id, gateway_base, formats=fmts, max_pages=max_pages)
