#!/usr/bin/env python3
import time
import db
import process_ol
import generate_embeddings
import recommend


def main():
    db.init_db()

    t0 = time.time()

    print("=== Step 1/3: OpenLibrary data processing ===")
    conn = db.get_conn()
    process_ol.run(conn)
    conn.close()

    print("\n=== Step 2/3: Regenerating embeddings ===")
    generate_embeddings.main()

    print("\n=== Step 3/3: Computing recommendations ===")
    conn = db.get_conn()
    recommend.compute(conn)
    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
