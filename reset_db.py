#!/usr/bin/env python3
import db


def reset():
    conn = db.get_conn()
    if db.schema_matches(conn):
        print("  Schema up to date — preserving tables")
        conn.executescript(db.SCHEMA_SQL)
        conn.commit()
        conn.close()
        return
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (t,) in rows:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(db.SCHEMA_SQL)
    conn.commit()
    conn.close()
    print("Database reset complete.")


if __name__ == "__main__":
    reset()
