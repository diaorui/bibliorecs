#!/usr/bin/env python3
"""
Analyze the synced book database.

Usage:
    python analyze.py              # summary stats
    python analyze.py --sample     # random 10 books
    python analyze.py --sample 20  # random 20 books
    python analyze.py --export     # export all to CSV
    python analyze.py --book <id>  # show one book by metadata_id
    python analyze.py --search <term>  # search by title/author
"""

import sys
import json
import csv
import os

import db


def print_stats():
    conn = db.get_conn()

    stats = db.get_stats(conn)
    print("=" * 60)
    print("  BOOK DATABASE STATISTICS")
    print("=" * 60)
    print(f"  Total books:         {stats['total']:>8,}")
    print(f"  With author:         {stats['with_author']:>8,}  ({_pct(stats['with_author'], stats['total'])})")
    print(f"  With subjects:       {stats['with_subjects']:>8,}  ({_pct(stats['with_subjects'], stats['total'])})")
    print()

    formats = db.get_format_distribution(conn)
    print("-" * 60)
    print("  FORMAT DISTRIBUTION")
    print("-" * 60)
    for f in formats:
        print(f"  {f['format']:<20s} {f['count']:>8,}  ({_pct(f['count'], stats['total'])})")
    print()

    langs = db.get_language_distribution(conn)
    print("-" * 60)
    print("  LANGUAGE DISTRIBUTION (top 20)")
    print("-" * 60)
    for l in langs:
        print(f"  {l['primary_language']:<20s} {l['count']:>8,}")
    print()

    years = db.get_year_distribution(conn)
    print("-" * 60)
    print("  RECENT YEAR DISTRIBUTION")
    print("-" * 60)
    for y in years:
        print(f"  {y['publication_year']:<10d} {y['count']:>8,}")

    conn.close()


def print_samples(n=10):
    conn = db.get_conn()
    books = db.get_sample_books(conn, n)
    print("=" * 60)
    print(f"  RANDOM SAMPLE ({n} books)")
    print("=" * 60)
    for i, b in enumerate(books, 1):
        subjects = json.loads(b["subjects"]) if b["subjects"] else []
        genres = json.loads(b["genres"]) if b["genres"] else []
        desc = (b["description"] or "")[:120]

        print(f"\n  [{i}] {b['title']}")
        print(f"      Author:    {b['author'] or 'N/A'}")
        print(f"      Format:    {b['format']} | Year: {b['publication_year'] or 'N/A'}")
        print(f"      Subjects:  {', '.join(subjects[:4])}")
        print(f"      Genres:    {', '.join(genres[:3])}")
        print(f"      Desc:      {desc}{'...' if len((b['description'] or '')) > 120 else ''}")
    conn.close()


def export_csv():
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT metadata_id, title, author, format, publication_year,
               primary_language, isbn,
               subjects, genres, series, call_number
        FROM books
        ORDER BY title
    """).fetchall()

    out_path = os.path.join(os.path.dirname(db.DB_PATH), "books_export.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metadata_id", "title", "author", "format",
                         "publication_year", "language", "isbn",
                         "subjects", "genres", "series", "call_number"])
        for row in rows:
            writer.writerow([
                row["metadata_id"], row["title"], row["author"],
                row["format"], row["publication_year"],
                row["primary_language"], row["isbn"],
                row["subjects"], row["genres"], row["series"],
                row["call_number"],
            ])

    print(f"Exported {len(rows)} books to {out_path}")
    conn.close()


def show_book(metadata_id):
    conn = db.get_conn()
    row = conn.execute("""
        SELECT * FROM books WHERE metadata_id = ?
    """, (metadata_id,)).fetchone()
    if not row:
        print(f"Book not found: {metadata_id}")
        conn.close()
        return

    row = dict(row)
    print("=" * 60)
    print(f"  BOOK DETAIL: {row['title']}")
    print("=" * 60)
    for key, val in row.items():
        if isinstance(val, str) and len(val) > 200:
            val = val[:200] + "..."
        print(f"  {key}: {val}")
    conn.close()


def search_books(term):
    conn = db.get_conn()
    pattern = f"%{term}%"
    rows = conn.execute("""
        SELECT metadata_id, title, author, format, publication_year
        FROM books
        WHERE title LIKE ? OR author LIKE ?
        ORDER BY title
        LIMIT 30
    """, (pattern, pattern)).fetchall()

    if not rows:
        print(f"No books found matching '{term}'")
        conn.close()
        return

    print(f"Found {len(rows)} books matching '{term}':")
    for r in rows:
        print(f"  {r['metadata_id']:25s} | {r['title'][:50]:50s} | {r['author'][:25]:25s} | {r['format']}")
    conn.close()


def _pct(val, total):
    return f"{val / total * 100:.1f}%" if total > 0 else "0%"


if __name__ == "__main__":
    if "--sample" in sys.argv:
        idx = sys.argv.index("--sample")
        n = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 10
        print_samples(n)
    elif "--export" in sys.argv:
        export_csv()
    elif "--book" in sys.argv:
        idx = sys.argv.index("--book")
        book_id = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        if book_id:
            show_book(book_id)
        else:
            print("Usage: python analyze.py --book <metadata_id>")
    elif "--search" in sys.argv:
        idx = sys.argv.index("--search")
        term = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        if term:
            search_books(term)
        else:
            print("Usage: python analyze.py --search <term>")
    else:
        print_stats()
