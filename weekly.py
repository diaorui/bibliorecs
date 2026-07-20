#!/usr/bin/env python3
import time
import db
import sync
import process_ol
import generate_embeddings
import recommend


def main():
    db.init_db()

    t0 = time.time()

    print("=== Step 1/4: Incremental catalog sync ===")
    sync.run_incremental()

    print("\n=== Step 2/4: OpenLibrary data processing ===")
    conn = db.get_conn()
    process_ol.run(conn)
    conn.close()

    print("\n=== Step 3/4: Regenerating embeddings ===")
    generate_embeddings.main()

    print("\n=== Step 4/4: Computing recommendations ===")
    conn = db.get_conn()
    recommend.compute(conn)
    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
