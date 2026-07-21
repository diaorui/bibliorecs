#!/usr/bin/env python3
import time

import db
import reset_db
import sync
import generate_embeddings
import api
import patron

import config


def main():
    t0 = time.time()

    print("=== Step 1/4: Resetting database schema ===")
    reset_db.reset()

    print("\n=== Step 2/4: Full catalog sync ===")
    for lib_id, lib_cfg in config.LIBRARIES.items():
        print(f"\n--- {lib_id} ---")
        sync.run_sync(lib_id, lib_cfg["gateway_base"])

    print("\nLogging in...")
    try:
        bc_token, session_id, account_id, _ = api.login()
    except Exception as e:
        print(f"  Login failed: {e}")
        return

    print(f"  account_id={account_id}")

    conn = db.get_conn()

    print("\n=== Step 3/4: Syncing borrowing history ===")
    new_history, pages = patron.sync_history(conn, bc_token, session_id, account_id)
    print(f"  {pages} pages checked, {new_history} new entries")

    print("\n=== Step 4/4: Regenerating embeddings ===")
    generate_embeddings.main()

    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
