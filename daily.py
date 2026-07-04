#!/usr/bin/env python3
import time
import api
import db
import patron
import recommend
import sync


def main():
    db.init_db()

    t0 = time.time()

    print("=== Step 1/5: Full catalog sync ===")
    sync.run_sync()

    conn = db.get_conn()

    print("\nLogging in...")
    try:
        bc_token, session_id, account_id, _ = api.login()
    except Exception as e:
        print(f"  Login failed: {e}")
        conn.close()
        return

    print(f"  account_id={account_id}")

    print("\n=== Step 2/5: Syncing borrowing history ===")
    new_history, pages = patron.sync_history(conn, bc_token, session_id, account_id)
    print(f"  {pages} pages checked, {new_history} new entries")

    print("\n=== Step 3/5: Syncing current checkouts ===")
    checkout_count = patron.sync_checkouts(conn, bc_token, session_id, account_id)
    print(f"  {checkout_count} current checkouts")

    print("\n=== Step 4/5: Auto-renewing checkouts close to due ===")
    renewed = patron.auto_renew_checkouts(conn, bc_token, session_id, account_id)
    print(f"  {renewed} checkouts renewed")

    print("\n=== Step 5/5: Computing recommendations ===")
    recommend.compute(conn)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
