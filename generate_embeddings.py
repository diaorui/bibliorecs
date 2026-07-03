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


def embed_text(b):
    parts = []
    if b.get("title"):
        parts.append("title: " + b["title"])
    if b.get("author"):
        parts.append("author: " + b["author"])
    series = json.loads(b["series"]) if b.get("series") else []
    seen_s = set()
    for s in series:
        k = s.lower().strip()
        if k not in seen_s:
            seen_s.add(k)
            parts.append("series: " + s)
    subjects = json.loads(b["subjects"]) if b.get("subjects") else []
    if subjects:
        cleaned = [_clean_subj(s) for s in subjects]
        cleaned = [s for s in cleaned if s]
        seen_sub = set()
        deduped = []
        for s in cleaned:
            k = s.lower()
            if k not in seen_sub:
                seen_sub.add(k)
                deduped.append(s)
        if deduped:
            parts.append("subjects: " + " ".join(deduped))
    genres = json.loads(b["genres"]) if b.get("genres") else []
    if genres:
        seen_g = set()
        clean_g = []
        for g in genres:
            g2 = g.replace("<delimit>", " ").replace("|", " ").strip()
            k = g2.lower()
            if k not in seen_g:
                seen_g.add(k)
                clean_g.append(g2)
        if clean_g:
            parts.append("genres: " + " ".join(clean_g))
    return " | ".join(parts)


def main():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT metadata_id, title, author, subjects, genres, series FROM books WHERE active = 1"
    ).fetchall()
    books = [dict(r) for r in rows]
    mids = [b["metadata_id"] for b in books]
    conn.close()

    print(f"Generating embeddings for {len(books):,} books...")
    t0 = time.time()
    model = SentenceTransformer(config.EMBEDDING_MODEL)
    load_t = time.time() - t0
    print(f"  Model loaded in {load_t:.1f}s ({config.EMBEDDING_MODEL})")

    texts = [embed_text(b) for b in books]

    use_gpu = torch.cuda.is_available()
    if not use_gpu:
        torch.set_num_threads(os.cpu_count())
    batch_size = 256 if use_gpu else 64

    t0 = time.time()
    embeddings = model.encode(texts, batch_size=batch_size, show_progress_bar=True)
    encode_t = time.time() - t0
    print(f"  Encoded {len(embeddings):,} vectors in {encode_t:.1f}s (dim={embeddings.shape[1]})")

    with open(config.EMBEDDING_MIDS_PATH, "w") as f:
        json.dump(mids, f)
    np.save(config.EMBEDDING_PATH, embeddings)

    file_size = os.path.getsize(config.EMBEDDING_PATH)
    print(f"  Saved: {config.EMBEDDING_PATH} ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Saved: {config.EMBEDDING_MIDS_PATH} ({len(mids):,} mids)")
    print("Done.")


if __name__ == "__main__":
    main()
