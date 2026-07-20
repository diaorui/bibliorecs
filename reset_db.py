#!/usr/bin/env python3
import db

def reset():
    conn = db.get_conn()
    tables = [
        "recommendation_cache", "sync_log", "availability",
        "borrow_events", "books_in_library", "works", "books",
    ]
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(db.SCHEMA_SQL)
    conn.commit()
    conn.close()
    print("Database reset complete.")

if __name__ == "__main__":
    reset()
