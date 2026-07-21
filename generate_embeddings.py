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
        parts.append(f"title: {w['title']}.")
    if w.get("author"):
        parts.append(f"author: {w['author']}.")
    series = json.loads(w["series"]) if w.get("series") else []
    seen_s = set()
    for s in series:
        k = s.lower().strip()
        if k not in seen_s:
            seen_s.add(k)
            parts.append(f"series: {s}.")
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
            parts.append(f"subjects: {', '.join(deduped)}.")
    if w.get("description"):
        desc = w["description"]
        if len(desc) > 1000:
            desc = desc[:1000]
        parts.append(f"description: {desc}.")
    return " ".join(parts)


def main():
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT work_id, title, author, subjects, series, description
        FROM works
    """).fetchall()
    works = [dict(r) for r in rows]
    wids = [w["work_id"] for w in works]
    conn.close()

    if not wids:
        print("No works to embed.")
        return

    print(f"Generating embeddings for {len(works):,} works...")
    t0 = time.time()
    model = SentenceTransformer(config.EMBEDDING_MODEL)
    load_t = time.time() - t0
    print(f"  Model loaded in {load_t:.1f}s ({config.EMBEDDING_MODEL})")

    texts = [embed_text(w) for w in works]

    use_gpu = torch.cuda.is_available()
    if not use_gpu:
        torch.set_num_threads(os.cpu_count())
    batch_size = 256 if use_gpu else 64

    t0 = time.time()
    embeddings = model.encode(texts, batch_size=batch_size, show_progress_bar=True)
    encode_t = time.time() - t0
    print(f"  Encoded {len(embeddings):,} vectors in {encode_t:.1f}s (dim={embeddings.shape[1]})")

    with open(config.EMBEDDING_WIDS_PATH, "w") as f:
        json.dump(wids, f)
    np.save(config.EMBEDDING_PATH, embeddings)

    file_size = os.path.getsize(config.EMBEDDING_PATH)
    print(f"  Saved: {config.EMBEDDING_PATH} ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Saved: {config.EMBEDDING_WIDS_PATH} ({len(wids):,} work_ids)")
    print("Done.")


if __name__ == "__main__":
    main()
