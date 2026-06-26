import json
import os
from datetime import datetime, date
from collections import defaultdict

import numpy as np
from sklearn.preprocessing import normalize

import config
import db


def book_category(call_number):
    if not call_number:
        return "Other"
    cn = call_number.strip()
    if cn.startswith("Juv Picture Book"):
        return "Picture Books"
    if cn.startswith("Juv Fiction"):
        return "Fiction"
    if cn.startswith("Juv Easy"):
        return "Easy Readers"
    if cn.startswith("Juv Graphic"):
        return "Graphic Novels"
    if cn.startswith("Juv Board"):
        return "Board Books"
    if cn.startswith("Juv 92"):
        return "Biography"
    if cn.startswith("Juv 5"):
        return "Science"
    if cn.startswith("Juv 9"):
        return "History"
    if cn.startswith("Juv 7"):
        return "Arts & Recreation"
    if cn.startswith("Juv 6"):
        return "Technology"
    if cn.startswith("Juv 3"):
        return "Social Sciences"
    return "Other"


def _time_weight(checkout_date, is_current=False):
    if is_current:
        return 1.0
    if not checkout_date:
        return 0.3
    try:
        d = datetime.strptime(checkout_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        d = date.today()
    days_ago = (date.today() - d).days
    if days_ago < 0:
        return 1.0
    return 2 ** (-days_ago / config.TIME_DECAY_HALF_LIFE_DAYS)


def _mmr(relevance, pairwise_sim, lambda_param, top_n):
    n = len(relevance)
    selected = []
    candidates = set(range(n))

    for _ in range(min(top_n, n)):
        if not candidates:
            break
        best_score = -float("inf")
        best_idx = -1

        for idx in candidates:
            rel = relevance[idx]
            div = max(pairwise_sim[idx, s] for s in selected) if selected else 0
            mmr_val = lambda_param * rel - (1 - lambda_param) * div
            if mmr_val > best_score:
                best_score = mmr_val
                best_idx = idx

        selected.append(best_idx)
        candidates.remove(best_idx)

    return selected


def _load_embeddings():
    if not os.path.exists(config.EMBEDDING_PATH):
        print(f"  Embeddings not found — generating with {config.EMBEDDING_MODEL}...")
        import generate_embeddings
        generate_embeddings.main()
    emb = np.load(config.EMBEDDING_PATH)
    with open(config.EMBEDDING_MIDS_PATH) as f:
        mids = json.load(f)
    return emb, mids


def compute(conn):
    print(f"  Loading embeddings ({config.EMBEDDING_PATH})...")
    emb, mid_list = _load_embeddings()
    emb_norm = normalize(emb, norm="l2", axis=1)
    mid_to_idx = {mid: i for i, mid in enumerate(mid_list)}
    idx_to_mid = {i: mid for i, mid in enumerate(mid_list)}

    print(f"  Loaded {len(mid_list):,} embeddings (dim={emb.shape[1]})")

    books = conn.execute("""
        SELECT metadata_id, call_number
        FROM books WHERE active = 1
    """).fetchall()
    books = [dict(r) for r in books]

    by_cat = defaultdict(list)
    for b in books:
        mid = b["metadata_id"]
        if mid not in mid_to_idx:
            continue
        cat = book_category(b.get("call_number"))
        by_cat[cat].append(mid_to_idx[mid])

    print(f"  {len(books):,} books across {len(by_cat)} categories")

    # Embedding MaxSim: for each candidate, max cosine similarity to any borrowed book
    borrows = db.get_borrow_events_for_recommendation(conn)
    has_profile = bool(borrows)

    if has_profile:
        valid = []
        borrowed_indices = set()
        for b in borrows:
            mid = b["metadata_id"]
            if mid in mid_to_idx:
                idx = mid_to_idx[mid]
                borrowed_indices.add(idx)
                valid.append((idx, _time_weight(b["checkout_date"], b["is_current"])))

        indices, weights = zip(*valid) if valid else ([], [])
        indices = list(indices)
        weights = np.array(weights, dtype=float)

        borrow_embs = emb_norm[indices]
        weighted = borrow_embs * weights[:, np.newaxis]
        weights_total = weights.sum()

        # MaxSim: max similarity to any single borrowed book
        maxsim = np.max(borrow_embs @ emb_norm.T, axis=0)

        # Blend with weighted profile for books that don't strongly match any single borrow
        avg_profile = np.sum(weighted, axis=0) / weights_total
        avg_norm = avg_profile / np.linalg.norm(avg_profile)
        profile_sim = emb_norm @ avg_norm
        maxsim = np.where(profile_sim > maxsim, profile_sim, maxsim)

        for i in borrowed_indices:
            maxsim[i] = -1
    else:
        print("  No borrow history — cold start")
        maxsim = np.ones(len(mid_list), dtype=float)

    db.clear_recommendation_cache(conn)

    for cat_name, cat_indices_list in by_cat.items():
        cat_indices = np.array(cat_indices_list)
        print(f"  [{cat_name}] {len(cat_indices):,} books...", end="")

        cat_sims = maxsim[cat_indices]
        top_n = min(config.TOP_CANDIDATES, len(cat_indices))

        if has_profile:
            sorted_idx = np.argsort(cat_sims)[::-1][:config.MMR_TOP_K]
            sorted_idx = sorted_idx[sorted_idx < len(cat_sims)]

            subset = emb_norm[cat_indices[sorted_idx]]
            pairwise_sim = subset @ subset.T

            mmr_selected = _mmr(
                cat_sims[sorted_idx],
                pairwise_sim,
                config.MMR_LAMBDA,
                top_n,
            )
            top_indices = cat_indices[sorted_idx[mmr_selected]].tolist()
        else:
            print(" (no profile — random)", end="")
            rng = np.random.default_rng()
            top_indices = rng.choice(cat_indices, size=top_n, replace=False).tolist()

        candidate_mids = [idx_to_mid[i] for i in top_indices]
        candidate_scores = [float(maxsim[i]) for i in top_indices]

        for rank, (mid, score) in enumerate(zip(candidate_mids, candidate_scores)):
            db.upsert_recommendation(conn, mid, score, cat_name, rank + 1)

        print(f" {top_n} recommended")

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM recommendation_cache").fetchone()[0]
    print(f"  Cached {total} recommendations total")
