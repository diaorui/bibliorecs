import json
import os
from datetime import datetime, date
from collections import defaultdict

import numpy as np
from sklearn.preprocessing import normalize

import config
import db

LIBRARY_ID = config.LIBRARY_ID

_EMB_CACHE = None
_EMB_IDS_CACHE = None
_EMB_MTIME = 0


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
    global _EMB_CACHE, _EMB_IDS_CACHE, _EMB_MTIME
    mtime = os.path.getmtime(config.EMBEDDING_PATH)
    if _EMB_CACHE is None or mtime != _EMB_MTIME:
        _EMB_CACHE = np.load(config.EMBEDDING_PATH)
        with open(config.EMBEDDING_IDS_PATH) as f:
            _EMB_IDS_CACHE = json.load(f)
        _EMB_MTIME = mtime
    return _EMB_CACHE, _EMB_IDS_CACHE


def get_recommendations(conn):
    if not os.path.exists(config.EMBEDDING_PATH):
        return {"by_cat": {}, "has_profile": False}

    emb, mid_list = _load_embeddings()
    if len(mid_list) == 0:
        return {"by_cat": {}, "has_profile": False}

    emb_norm = normalize(emb, norm="l2", axis=1)
    mid_to_idx = {mid: i for i, mid in enumerate(mid_list)}

    books = conn.execute("""
        SELECT metadata_id, call_number, publication_year,
               title, subtitle, author, isbn, format,
               content_type, subjects, genres, description, series
        FROM books_in_library
        WHERE active = 1 AND library_id = ?
          AND primary_language = 'eng'
    """, (LIBRARY_ID,)).fetchall()

    min_year = date.today().year - config.NEW_BOOK_MAX_AGE_YEARS
    by_cat = defaultdict(list)
    new_indices = set()
    meta_info = {}

    for b in books:
        b = dict(b)
        mid = b["metadata_id"]
        if mid not in mid_to_idx:
            continue
        meta_info[mid] = b
        idx = mid_to_idx[mid]
        cat = book_category(b.get("call_number"))
        by_cat[cat].append(idx)
        if b["publication_year"] and b["publication_year"] >= min_year:
            new_indices.add(idx)

    borrows = db.get_borrow_events_for_recommendation(conn, LIBRARY_ID)
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
        weighted_embs = borrow_embs * weights[:, np.newaxis]
        maxsim = np.max(weighted_embs @ emb_norm.T, axis=0)

        for i in borrowed_indices:
            maxsim[i] = -1
    else:
        maxsim = np.ones(len(mid_list), dtype=float)

    result = {"by_cat": {}, "has_profile": has_profile}

    for cat_name, cat_indices_list in by_cat.items():
        cat_indices = np.array(cat_indices_list)
        top_n = min(config.TOP_CANDIDATES, len(cat_indices))

        if has_profile:
            cat_sims = maxsim[cat_indices]
            sorted_idx = np.argsort(cat_sims)[::-1][:config.MMR_TOP_K]
            sorted_idx = sorted_idx[sorted_idx < len(cat_sims)]

            subset = emb_norm[cat_indices[sorted_idx]]
            pairwise_sim = subset @ subset.T

            mmr_selected = _mmr(cat_sims[sorted_idx], pairwise_sim, config.MMR_LAMBDA, top_n)
            top_indices = cat_indices[sorted_idx[mmr_selected]].tolist()
        else:
            rng = np.random.default_rng()
            top_indices = rng.choice(cat_indices, size=top_n, replace=False).tolist()

        items = []
        rank = 1
        for i in top_indices:
            mid = mid_list[i]
            info = dict(meta_info[mid])
            info["score"] = float(maxsim[i])
            info["category_rank"] = rank
            items.append(info)
            rank += 1

        result["by_cat"][cat_name] = items

    if has_profile:
        all_book_indices = np.concatenate(list(by_cat.values()))
        global_sims = maxsim[all_book_indices]
        top_k = min(config.MMR_TOP_K, len(all_book_indices))
        sorted_idx = np.argsort(global_sims)[::-1][:top_k]
        subset = emb_norm[all_book_indices[sorted_idx]]
        pairwise_sim = subset @ subset.T
        mmr_selected = _mmr(global_sims[sorted_idx], pairwise_sim, config.MMR_LAMBDA, config.TOP_CANDIDATES)
        top_indices = all_book_indices[sorted_idx[mmr_selected]].tolist()

        items = []
        rank = 1
        for i in top_indices:
            mid = mid_list[i]
            info = dict(meta_info[mid])
            info["score"] = float(maxsim[i])
            info["category_rank"] = rank
            items.append(info)
            rank += 1
        result["by_cat"]["Top Picks"] = items

    if new_indices:
        new_arr = np.array(list(new_indices), dtype=int)
        new_sims = maxsim[new_arr]
        top_n = min(config.TOP_CANDIDATES, len(new_arr))

        if has_profile:
            sorted_idx = np.argsort(new_sims)[::-1][:config.MMR_TOP_K]
            sorted_idx = sorted_idx[sorted_idx < len(new_sims)]
            subset = emb_norm[new_arr[sorted_idx]]
            pairwise_sim = subset @ subset.T
            mmr_selected = _mmr(new_sims[sorted_idx], pairwise_sim, config.MMR_LAMBDA, top_n)
            top_indices = new_arr[sorted_idx[mmr_selected]].tolist()
        else:
            rng = np.random.default_rng()
            top_indices = rng.choice(new_arr, size=top_n, replace=False).tolist()

        items = []
        rank = 1
        for i in top_indices:
            mid = mid_list[i]
            info = dict(meta_info[mid])
            info["score"] = float(maxsim[i])
            info["category_rank"] = rank
            items.append(info)
            rank += 1
        result["by_cat"]["New"] = items

    return result
