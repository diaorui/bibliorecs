import json
import math
from datetime import datetime, date
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

import config
import api
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


def _book_corpus(books):
    for b in books:
        parts = []
        ct = b.get("content_type") or ""
        if ct:
            parts.append(ct)
        subjects = json.loads(b["subjects"]) if b.get("subjects") else []
        parts.extend(subjects)
        genres = json.loads(b["genres"]) if b.get("genres") else []
        parts.extend(f"#{g}" for g in genres)
        author = (b.get("author") or "").replace(",", " ").strip()
        if author:
            parts.append(author)
        series = json.loads(b["series"]) if b.get("series") else []
        parts.extend(series)
        yield " ".join(parts)


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


def _mmr(similarities, pairwise_sim, lambda_param, top_n):
    n = len(similarities)
    selected = []
    candidates = set(range(n))

    for _ in range(min(top_n, n)):
        if not candidates:
            break
        best_score = -float("inf")
        best_idx = -1

        for idx in candidates:
            rel = similarities[idx]
            div = max(pairwise_sim[idx, s] for s in selected) if selected else 0
            mmr_val = lambda_param * rel - (1 - lambda_param) * div
            if mmr_val > best_score:
                best_score = mmr_val
                best_idx = idx

        selected.append(best_idx)
        candidates.remove(best_idx)

    return selected


def _compute_category(conn, cat_name, cat_books, mid_to_idx, tfidf_matrix, sim_array, has_profile, bc_token, session_id):
    print(f"  [{cat_name}] {len(cat_books):,} books...", end="")
    idx_to_mid = {v: k for k, v in mid_to_idx.items()}
    cat_mids = [b["metadata_id"] for b in cat_books]
    cat_indices = np.array([mid_to_idx[mid] for mid in cat_mids])
    cat_sims = sim_array[cat_indices]
    top_n = min(config.TOP_CANDIDATES, len(cat_books))

    if has_profile:
        sorted_idx = np.argsort(cat_sims)[::-1][:config.TOP_CANDIDATES * 3]
        sorted_idx = sorted_idx[sorted_idx < len(cat_sims)]
        subset = tfidf_matrix[cat_indices][sorted_idx]
        pairwise_sim = (subset @ subset.T).toarray()
        mmr_selected = _mmr(
            cat_sims[sorted_idx],
            pairwise_sim,
            config.MMR_LAMBDA,
            top_n
        )
        top_indices = cat_indices[sorted_idx[mmr_selected]].tolist()
    else:
        print(" (no profile — random)", end="")
        rng = np.random.default_rng()
        top_indices = rng.choice(cat_indices, size=top_n, replace=False).tolist()

    candidate_mids = [idx_to_mid[i] for i in top_indices]
    candidate_scores = [float(sim_array[i]) for i in top_indices]

    print(f" checking availability...", end="")

    owns_flags = []
    if bc_token:
        with ThreadPoolExecutor(max_workers=config.CHECKOUTS_PARALLEL_WORKERS) as pool:
            fut_map = {
                pool.submit(api.fetch_availability, bc_token, session_id, mid): j
                for j, mid in enumerate(candidate_mids)
            }
            results = {}
            for f in as_completed(fut_map):
                j = fut_map[f]
                try:
                    results[j] = api.bib_owns_home(f.result())
                except Exception:
                    results[j] = False
            owns_flags = [results.get(j, False) for j in range(len(candidate_mids))]
    else:
        owns_flags = [False] * len(candidate_mids)

    print(f" {sum(owns_flags)}/{len(candidate_mids)} at Central Park")

    for rank, (mid, score, owns) in enumerate(zip(candidate_mids, candidate_scores, owns_flags)):
        db.upsert_recommendation(conn, mid, score, cat_name, rank + 1, int(owns))


def compute(conn):
    books = conn.execute("""
        SELECT metadata_id, content_type, subjects, genres, author, series, call_number
        FROM books WHERE active = 1
    """).fetchall()
    books = [dict(r) for r in books]

    if not books:
        print("  No active books in catalog")
        return

    by_cat = defaultdict(list)
    for b in books:
        cat = book_category(b.get("call_number"))
        by_cat[cat].append(b)

    print(f"  Loaded {len(books):,} books across {len(by_cat)} categories:")

    # Build full corpus and TF-IDF matrix
    print(f"  Building corpus from {len(books):,} books...")
    corpus = list(_book_corpus(books))
    mid_list = [b["metadata_id"] for b in books]

    vec = TfidfVectorizer(
        max_features=config.TFIDF_MAX_FEATURES,
        analyzer="word",
        token_pattern=r"(?u)\S+",
    )
    tfidf_matrix = vec.fit_transform(corpus)
    print(f"  TF-IDF matrix: {tfidf_matrix.shape}")

    # Build user profile from ALL borrows
    borrows = db.get_borrow_events_for_recommendation(conn)
    borrowed_indices = set()

    if borrows:
        mid_to_idx = {mid: i for i, mid in enumerate(mid_list)}
        valid = [(mid_to_idx[b["metadata_id"]], _time_weight(b["checkout_date"], b["is_current"]))
                 for b in borrows if b["metadata_id"] in mid_to_idx]
        print(f"  Building profile from {len(valid)}/{len(borrows)} borrow events...")

        if valid:
            idx_list = [v[0] for v in valid]
            borrowed_indices = set(idx_list)
            weights = np.array([v[1] for v in valid], dtype=float)
            borrowed_vectors = tfidf_matrix[idx_list]
            profile = borrowed_vectors.T.dot(weights) / weights.sum()
            profile = np.asarray(profile).ravel()
            norm = np.linalg.norm(profile)
            profile = profile / norm if norm > 0 else None
    else:
        print("  No borrow history — cold start")
        profile = None

    # Score all books against user profile
    if profile is not None:
        similarities = tfidf_matrix.dot(profile)
        if hasattr(similarities, "toarray"):
            sim_array = similarities.toarray().ravel()
        else:
            sim_array = np.asarray(similarities).ravel()
        sim_array[list(borrowed_indices)] = -1
    else:
        sim_array = np.ones(len(books), dtype=float)

    # Login for availability check
    try:
        bc_token, session_id, account_id, _ = api.login()
    except Exception as e:
        print(f"  Login failed for ownership check: {e}")
        bc_token = session_id = None

    db.clear_recommendation_cache(conn)

    mid_to_idx = {mid: i for i, mid in enumerate(mid_list)}

    has_profile = profile is not None
    for cat_name, cat_books in by_cat.items():
        _compute_category(conn, cat_name, cat_books, mid_to_idx, tfidf_matrix, sim_array, has_profile, bc_token, session_id)

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM recommendation_cache").fetchone()[0]
    print(f"  Cached {total} recommendations total")
