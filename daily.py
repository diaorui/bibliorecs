#!/usr/bin/env python3
import time

import db
import reset_db
import sync
import generate_embeddings
import api

import config


def main():
    t0 = time.time()

    print("=== Step 1/4: Resetting database schema ===")
    reset_db.reset()

    print("\n=== Step 2/4: Full catalog sync ===")
    for lib_id, lib_cfg in config.LIBRARIES.items():
        print(f"\n--- {lib_id} ---")
        sync.run_sync(lib_id, lib_cfg["gateway_base"])

    conn = db.get_conn()

    print("\n=== Step 3/4: Syncing branches ===")
    for lib_id, lib_cfg in config.LIBRARIES.items():
        try:
            branches = api.fetch_branches(lib_cfg["gateway_base"])
            if branches:
                db.replace_branches(conn, lib_id, branches)
                print(f"  {lib_id}: {len(branches)} branches synced")
            else:
                print(f"  {lib_id}: empty response, keeping existing branches")
        except Exception as e:
            print(f"  {lib_id}: fetch failed ({e}), keeping existing branches")

    print("\n=== Step 4/4: Regenerating embeddings ===")
    generate_embeddings.main()

    conn.close()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
