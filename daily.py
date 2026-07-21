#!/usr/bin/env python3
import os
import sys
import time
import urllib.request

import config
import db
import reset_db
import sync
import process_ol
import generate_embeddings
import api
import patron


OL_EDITIONS_URL = "https://openlibrary.org/data/ol_dump_editions_latest.txt.gz"
OL_WORKS_URL = "https://openlibrary.org/data/ol_dump_works_latest.txt.gz"


def _needs_download(url, dest):
    if not os.path.exists(dest):
        return True
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req) as resp:
            remote_size = int(resp.headers.get('Content-Length', 0))
    except Exception:
        return True
    return remote_size != os.path.getsize(dest)


def _download_dump(url, dest):
    print(f"  Downloading {os.path.basename(url)}...", end="", flush=True)
    t0 = time.time()
    try:
        urllib.request.urlretrieve(url, dest)
        size_mb = os.path.getsize(dest) / 1024 / 1024
        elapsed = time.time() - t0
        print(f" {size_mb:.0f} MB in {elapsed:.0f}s")
    except Exception as e:
        print(f" FAILED: {e}")
        print(f"  Download manually: {url}")
        print(f"  Place at: {dest}")
        sys.exit(1)


def main():
    t0 = time.time()

    print("=== Step 1/8: Downloading fresh OL dumps ===")
    if _needs_download(OL_EDITIONS_URL, config.OL_EDITIONS_DUMP):
        _download_dump(OL_EDITIONS_URL, config.OL_EDITIONS_DUMP)
    else:
        print("  Editions dump unchanged, skipping")
    if _needs_download(OL_WORKS_URL, config.OL_WORKS_DUMP):
        _download_dump(OL_WORKS_URL, config.OL_WORKS_DUMP)
    else:
        print("  Works dump unchanged, skipping")

    print("\n=== Step 2/8: Resetting database to latest schema ===")
    reset_db.reset()

    print("\n=== Step 3/8: Full catalog sync ===")
    sync.run_sync()

    try:
        import duckdb
        duckdb_ok = True
    except ImportError:
        duckdb_ok = False

    if duckdb_ok:
        print("\n=== Step 4/8: OpenLibrary data processing ===")
        conn = db.get_conn()
        process_ol.run(conn)
        conn.close()
    else:
        print("\n=== Step 4/8: OpenLibrary data processing ===")
        print("  duckdb not installed — skipping OL processing")
        print("  Install: pip install duckdb")

    print("\nLogging in...")
    try:
        bc_token, session_id, account_id, _ = api.login()
    except Exception as e:
        print(f"  Login failed: {e}")
        return

    print(f"  account_id={account_id}")

    conn = db.get_conn()

    print("\n=== Step 5/8: Syncing borrowing history ===")
    new_history, pages = patron.sync_history(conn, bc_token, session_id, account_id)
    print(f"  {pages} pages checked, {new_history} new entries")

    print("\n=== Step 6/8: Syncing current checkouts ===")
    checkout_count = patron.sync_checkouts(conn, bc_token, session_id, account_id)
    print(f"  {checkout_count} current checkouts")

    print("\n=== Step 7/8: Auto-renewing checkouts close to due ===")
    renewed = patron.auto_renew_checkouts(conn, bc_token, session_id, account_id)
    print(f"  {renewed} checkouts renewed")

    if duckdb_ok:
        print("\n=== Step 8/8: Regenerating embeddings ===")
        generate_embeddings.main()
    else:
        print("\n=== Step 8/8: Regenerating embeddings ===")
        print("  duckdb not installed — skipping embeddings")

    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
