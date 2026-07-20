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
import recommend


OL_EDITIONS_URL = "https://openlibrary.org/data/ol_dump_editions_latest.txt.gz"
OL_WORKS_URL = "https://openlibrary.org/data/ol_dump_works_latest.txt.gz"


def main():
    t0 = time.time()

    print("=== Step 1/5: Downloading fresh OL dumps ===")
    _download_dump(OL_EDITIONS_URL, config.OL_EDITIONS_DUMP)
    _download_dump(OL_WORKS_URL, config.OL_WORKS_DUMP)

    print("\n=== Step 2/5: Resetting database to latest schema ===")
    reset_db.reset()

    print("\n=== Step 3/5: Full catalog sync ===")
    sync.run_sync()

    print("\n=== Step 4/5: OpenLibrary data processing ===")
    conn = db.get_conn()
    process_ol.run(conn)
    conn.close()

    print("\n=== Step 5/5: Regenerating embeddings & recommendations ===")
    generate_embeddings.main()
    conn = db.get_conn()
    recommend.compute(conn)
    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


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


if __name__ == "__main__":
    main()
