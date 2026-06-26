#!/usr/bin/env python3
import json
import os
import time

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

import config
import db


def _load_model():
    kwargs = {"model_name_or_path": config.EMBEDDING_MODEL}
    try:
        import optimum  # noqa
        kwargs["backend"] = "onnx"
        print(f"  Using ONNX backend for {config.EMBEDDING_MODEL}")
    except ImportError:
        print(f"  Using PyTorch backend for {config.EMBEDDING_MODEL}")
    return SentenceTransformer(**kwargs)


def embed_text(b):
    parts = []
    if b.get("title"):
        parts.append("title: " + b["title"])
    if b.get("author"):
        parts.append("author: " + b["author"].replace(",", " ").strip())
    series = json.loads(b["series"]) if b.get("series") else []
    if series:
        parts.extend("series: " + s for s in series)
    subjects = json.loads(b["subjects"]) if b.get("subjects") else []
    if subjects:
        parts.append("subjects: " + " ".join(subjects))
    genres = json.loads(b["genres"]) if b.get("genres") else []
    if genres:
        parts.append("genres: " + " ".join("#" + g for g in genres))
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
    model = _load_model()
    load_t = time.time() - t0
    print(f"  Model loaded in {load_t:.1f}s")

    texts = [embed_text(b) for b in books]

    torch.set_num_threads(os.cpu_count())
    batch_size = 512 if not torch.cuda.is_available() else 256

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
