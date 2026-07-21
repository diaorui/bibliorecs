#!/usr/bin/env python3
import json
import os
import time

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

import config
import db


BOILERPLATE = [
    " juvenile fiction", " juvenile literature", " comic books, strips, etc",
    " juvenile fiction comic books, strips, etc",
    " juvenile literature comic books, strips, etc",
]


def _clean_subj(s):
    s = s.lower()
    for bp in BOILERPLATE:
        s = s.replace(bp, "")
    return s.strip()


def embed_text(w):
    parts = []
    if w.get("title"):
        parts.append("title: " + w["title"])
    if w.get("author"):
        parts.append("author: " + w["author"])
    series = json.loads(w["series"]) if w.get("series") else []
    seen_s = set()
    for s in series:
        k = s.lower().strip()
        if k not in seen_s:
            seen_s.add(k)
            parts.append("series: " + s)
    subjects = json.loads(w["subjects"]) if w.get("subjects") else []
    if subjects:
        cleaned = [_clean_subj(s) for s in subjects]
        cleaned = [s for s in cleaned if s]
        seen = set()
        deduped = []
        for s in cleaned:
            k = s.lower()
            if k not in seen:
                seen.add(k)
                deduped.append(s)
        if deduped:
            parts.append("subjects: " + " ".join(deduped))
    genres = json.loads(w["genres"]) if w.get("genres") else []
    if genres:
        cleaned_g = []
        seen_g = set()
        for g in genres:
            g = g.replace("<delimit>", " ").replace("|", " ").strip()
            if g and g.lower() not in seen_g:
                seen_g.add(g.lower())
                cleaned_g.append(g)
        parts.append("genres: " + " ".join(cleaned_g))
    if w.get("description"):
        desc = w["description"]
        if len(desc) > 200:
            desc = desc[:200]  # longer desc hurts inference speed with little quality gain
        parts.append("description: " + desc)
    return " | ".join(parts)


def main():
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT metadata_id, title, author, subjects, series, genres, description
        FROM books_in_library
        WHERE active = 1
          AND isbn IS NOT NULL AND isbn != ''
    """).fetchall()
    books = [dict(r) for r in rows]
    mids = [b["metadata_id"] for b in books]
    conn.close()

    if not mids:
        print("No books to embed.")
        return

    print(f"Generating embeddings for {len(books):,} books...")
    t0 = time.time()
    model = SentenceTransformer(config.EMBEDDING_MODEL)
    load_t = time.time() - t0
    print(f"  Model loaded in {load_t:.1f}s ({config.EMBEDDING_MODEL})")

    texts = [embed_text(b) for b in books]

    # sort by length to minimize batch padding waste on CPU
    sort_idx = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    texts_sorted = [texts[i] for i in sort_idx]

    use_gpu = torch.cuda.is_available()
    if not use_gpu:
        torch.set_num_threads(os.cpu_count())
    batch_size = 256 if use_gpu else 64

    t0 = time.time()
    embeddings_sorted = model.encode(texts_sorted, batch_size=batch_size, show_progress_bar=True)

    embeddings = np.zeros_like(embeddings_sorted)
    for orig_i, sorted_i in enumerate(sort_idx):
        embeddings[orig_i] = embeddings_sorted[sorted_i]
    encode_t = time.time() - t0
    print(f"  Encoded {len(embeddings):,} vectors in {encode_t:.1f}s (dim={embeddings.shape[1]})")

    with open(config.EMBEDDING_IDS_PATH, "w") as f:
        json.dump(mids, f)
    np.save(config.EMBEDDING_PATH, embeddings)

    file_size = os.path.getsize(config.EMBEDDING_PATH)
    print(f"  Saved: {config.EMBEDDING_PATH} ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Saved: {config.EMBEDDING_IDS_PATH} ({len(mids):,} metadata_ids)")
    print("Done.")


if __name__ == "__main__":
    main()
