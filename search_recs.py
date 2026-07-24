import re
import json
import threading
from collections import defaultdict
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from model2vec import StaticModel

import config
import api

_EMBEDDER = None


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = StaticModel.from_pretrained("minishlab/potion-base-4M")
    return _EMBEDDER


def _discover_physical_formats(library_id):
    formats = set()
    try:
        data = api.search_bibs_json('audience:"children"', library_id, f_circ="CIRC", limit=100)
        bibs = api.parse_bib_entities(data)
        for mid, bib in bibs.items():
            bi = bib.get("briefInfo", {})
            fmt = bi.get("format", "")
            isbns = bi.get("isbns", [])
            super_fmts = bi.get("superFormats", [])
            if not fmt or not isbns:
                continue
            if "BOOKS" in super_fmts and "ELECTRONIC_FORMATS" not in super_fmts:
                formats.add(fmt)
    except Exception:
        pass

    return list(formats) if formats else ["BK"]


def _embed_texts(texts):
    embedder = _get_embedder()
    embs = embedder.encode(texts, show_progress_bar=False)
    if embs.ndim == 1:
        embs = embs.reshape(1, -1)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return embs / norms


def _build_embedding_text(title="", subtitle="", content_type="",
                          authors=None, series=None,
                          subjects=None, genres=None):
    parts = []
    if title:
        parts.append(f"title: {title}")
    if subtitle:
        parts.append(f"subtitle: {subtitle}")
    if content_type:
        parts.append(f"type: {content_type.lower()}")
    if authors:
        parts.append(f"author: {'; '.join(authors)}")
    if series:
        seen = set()
        for s in series:
            name = s.get("name", s) if isinstance(s, dict) else s
            name = (name or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                parts.append(f"series: {name}")
    if subjects:
        parts.append(f"subjects: {' '.join(subjects)}")
    if genres:
        parts.append(f"genres: {' '.join(genres)}")
    return " | ".join(parts)


def _time_weight(checkout_date, is_current=False):
    if is_current:
        return 1.0
    if not checkout_date:
        return 0.3
    try:
        d = date.fromisoformat(checkout_date[:10])
    except (ValueError, TypeError):
        d = date.today()
    days_ago = (date.today() - d).days
    if days_ago < 0:
        return 1.0
    return 2 ** (-days_ago / config.HALF_LIFE_DAYS)


def _build_query(qtype, value):
    if qtype == "author" and value:
        return f'contributor:"{value}" AND audience:"children"'
    elif qtype == "series" and value:
        return f'series:"{value}" AND audience:"children"'
    elif qtype == "title" and value:
        return f'title:"{value}" AND audience:"children"'
    return None


def _continuous_greedy(books, emb_norm, weights, max_queries=10):
    if not books or len(emb_norm) == 0:
        return []
    n = len(books)
    sim_matrix = emb_norm @ emb_norm.T
    sim_matrix = np.clip(sim_matrix, 0, 1)

    seen_q = set()
    candidates = []

    for i, b in enumerate(books):
        for a in (b.get("authors") or []):
            key = ("author", a.strip().lower())
            if not a.strip() or key in seen_q:
                continue
            seen_q.add(key)
            exact = {j for j, bb in enumerate(books)
                     if any(aa.strip().lower() == a.strip().lower()
                            for aa in (bb.get("authors") or []))}
            candidates.append(("author", a.strip(), exact))

        for s in (b.get("series") or []):
            name = s.get("name", s) if isinstance(s, dict) else s
            name = (name or "").strip()
            if not name:
                continue
            key = ("series", name.lower())
            if key in seen_q:
                continue
            seen_q.add(key)
            exact = {j for j, bb in enumerate(books)
                     if any(sn.lower() == name.lower()
                            for sn in _get_series_names(bb))}
            candidates.append(("series", name, exact))

    title_counts = {}
    for b in books:
        t = (b.get("title") or "").strip().lower()
        if t:
            title_counts[t] = title_counts.get(t, 0) + 1

    seen_titles = set()
    for i, b in enumerate(books):
        t = (b.get("title") or "").strip().lower()
        if t and title_counts.get(t, 0) >= 2 and t not in seen_titles:
            seen_titles.add(t)
            candidates.append(("title", b.get("title", "").strip(),
                               {j for j, bb in enumerate(books)
                                if (bb.get("title") or "").strip().lower() == t}))

    valid = []
    for qt, qv, exact in candidates:
        if not exact:
            continue
        exact_arr = np.array(list(exact))
        cov_vec = np.max(sim_matrix[:, exact_arr], axis=1)
        valid.append((qt, qv, exact, cov_vec))

    selected = []
    cur_cov = np.zeros(n)
    remaining = list(enumerate(valid))

    for _ in range(max_queries):
        best_m = -1.0
        best_i = -1

        for ri, (orig_idx, (qt, qv, exact, cov_vec)) in enumerate(remaining):
            delta = np.maximum(0, cov_vec - cur_cov)
            marginal = float((weights * delta).sum())
            if marginal > best_m:
                best_m = marginal
                best_i = ri

        if best_i < 0 or best_m <= 0:
            break

        qt, qv, exact, cov_vec = remaining[best_i][1]
        q = _build_query(qt, qv)
        if not q:
            del remaining[best_i]
            continue
        selected.append((qt, qv, q))
        cur_cov = np.maximum(cur_cov, cov_vec)
        del remaining[best_i]

    return selected


def _get_series_names(book):
    seen = set()
    names = []
    for s in (book.get("series") or []):
        name = s.get("name", s) if isinstance(s, dict) else s
        name = (name or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _search_one(query, library_id, formats):
    try:
        data = api.search_bibs_json(query, library_id, formats=formats,
                                     f_circ="CIRC", f_lang="eng", limit=config.POOL_LIMIT)
        bibs = api.parse_bib_entities(data)
        results = []
        for metadata_id, bib in bibs.items():
            bi = bib.get("briefInfo", {})
            isbns = bi.get("isbns", [])
            a = bib.get("availability", {})
            if a.get("status") == "ON_ORDER" or a.get("circulationType") == "NON_CIRCULATING":
                continue
            if not isbns:
                continue
            lang = (bi.get("primaryLanguage") or "").lower()
            if lang and lang != "eng":
                continue
            info = api.extract_book_info(metadata_id, bib)
            results.append(info)
        return results, query
    except Exception:
        return [], query


def _maxsim_scores(pool_norm, borrowed_norm, borrowed_weights):
    weighted = borrowed_norm * borrowed_weights[:, np.newaxis]
    sims = weighted @ pool_norm.T
    return np.max(sims, axis=0)


def _mmr(scores, pairwise_sim, lambda_param, top_n):
    n = len(scores)
    selected = []
    candidates = set(range(n))

    for _ in range(min(top_n, n)):
        if not candidates:
            break
        best_score = -float("inf")
        best_idx = -1

        for idx in candidates:
            rel = scores[idx]
            div = max(pairwise_sim[idx, s] for s in selected) if selected else 0
            mmr_val = lambda_param * rel - (1 - lambda_param) * div
            if mmr_val > best_score:
                best_score = mmr_val
                best_idx = idx

        selected.append(best_idx)
        candidates.remove(best_idx)

    return selected


def _dedup_books(books):
    seen = set()
    result = []
    for b in books:
        mid = b.get("metadata_id")
        if mid and mid not in seen:
            seen.add(mid)
            result.append(b)
    return result


def _carousel_name(qtype, query_str):
    m = re.search(r'[a-z]+:"([^"]+)"', query_str)
    val = m.group(1) if m else None
    if not val:
        return None
    if qtype == "author":
        return f"More by {val}"
    elif qtype == "series":
        return f"More {val}"
    elif qtype == "title":
        return f"If you liked {val}"
    return None


def get_recommendations(library_id, borrowing_history):
    if not borrowing_history:
        return {"carousels": [], "has_profile": False}

    has_profile = True
    pool_lock = threading.Lock()

    borrowed_mids = set()
    borrowed_isbns = set()
    for b in borrowing_history:
        mid = b.get("metadata_id")
        if mid:
            borrowed_mids.add(mid)
        for isbn in (b.get("isbns") or []):
            borrowed_isbns.add(isbn)

    books = []
    for b in borrowing_history:
        authors_raw = b.get("authors") or b.get("author") or ""
        authors = authors_raw if isinstance(authors_raw, list) else []
        series = b.get("series") or []
        title = (b.get("title") or "").strip()
        subtitle = b.get("subtitle") or ""
        content_type = b.get("content_type") or ""
        subjects = b.get("subjects") or []
        genres = b.get("genres") or []

        text_for_emb = _build_embedding_text(
            title=title, subtitle=subtitle, content_type=content_type,
            authors=authors, series=series, subjects=subjects, genres=genres)
        if not text_for_emb:
            continue
        books.append({
            "metadata_id": b.get("metadata_id"),
            "title": title,
            "authors": authors,
            "series": series,
            "subjects": subjects,
            "isbns": b.get("isbns") or [],
            "checkout_date": b.get("checkout_date"),
            "is_current": b.get("is_current", False),
            "_text": text_for_emb,
        })

    if not books:
        return {"carousels": [], "has_profile": True}

    texts = [b["_text"] for b in books]
    emb_norm = _embed_texts(texts)
    weights = np.array([_time_weight(b["checkout_date"], b["is_current"]) for b in books], dtype=float)

    fmt_queries = _continuous_greedy(books, emb_norm, weights, config.MAX_SEARCH_QUERIES)
    if not fmt_queries:
        return {"carousels": [], "has_profile": has_profile}

    formats = _discover_physical_formats(library_id)

    pool = []
    pool_mids = set()
    pool_isbns = set()
    query_to_results = defaultdict(list)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_search_one, q, library_id, formats): q
                   for _, _, q in fmt_queries}
        for fut in as_completed(futures):
            results, query = fut.result()
            for info in results:
                mid = info["metadata_id"]
                if mid in borrowed_mids:
                    continue
                info_isbns = json.loads(info.get("isbns") or "[]")
                if any(i in borrowed_isbns for i in info_isbns):
                    continue
                with pool_lock:
                    if mid not in pool_mids and not any(i in pool_isbns for i in info_isbns):
                        pool_mids.add(mid)
                        for i_isbn in info_isbns:
                            pool_isbns.add(i_isbn)
                        pool.append(info)
                        query_to_results[query].append(info)

    if not pool:
        return {"carousels": [], "has_profile": has_profile}

    # Embed pool
    pool_texts = []
    for info in pool:
        title = info.get("title") or ""
        subtitle = info.get("subtitle") or ""
        content_type = info.get("content_type") or ""
        authors_raw = json.loads(info.get("authors") or "[]")
        series_raw = json.loads(info.get("series") or "[]")
        subjects_raw = json.loads(info.get("subjects") or "[]")
        genres_raw = json.loads(info.get("genres") or "[]")
        pool_texts.append(_build_embedding_text(
            title=title, subtitle=subtitle, content_type=content_type,
            authors=authors_raw, series=series_raw,
            subjects=subjects_raw, genres=genres_raw))

    pool_norm = _embed_texts(pool_texts)
    pool_mid_to_idx = {}
    for i, info in enumerate(pool):
        pool_mid_to_idx[info["metadata_id"]] = i

    # MaxSim
    sims = _maxsim_scores(pool_norm, emb_norm, weights)

    # Top Picks (global MMR)
    top_k = min(config.MMR_TOP_K, len(pool))
    global_order = np.argsort(sims)[::-1][:top_k]
    global_subset = pool_norm[global_order]
    global_pairwise = global_subset @ global_subset.T
    mmr_selected = _mmr(sims[global_order], global_pairwise, config.MMR_LAMBDA, config.TOP_CANDIDATES)
    top_picks_indices = global_order[mmr_selected]

    top_picks = []
    rank = 1
    for i in top_picks_indices:
        info = dict(pool[i])
        info["score"] = float(sims[i])
        info["category_rank"] = rank
        rank += 1
        top_picks.append(info)

    # Per-query carousels
    per_query_carousels = []

    for qt, qv, q in fmt_queries:
        results = query_to_results.get(q, [])
        if len(results) < config.RECS_PER_CAROUSEL:
            continue
        name = _carousel_name(qt, q)
        if not name:
            continue

        results.sort(key=lambda r: -sims[pool_mid_to_idx.get(r["metadata_id"], 0)])
        top_n = min(config.TOP_CANDIDATES, len(results))
        selected = []
        for info in results[:top_n]:
            mid = info["metadata_id"]
            idx = pool_mid_to_idx.get(mid)
            if idx is None:
                continue
            r = dict(info)
            r["score"] = float(sims[idx])
            selected.append(r)

        if selected:
            per_query_carousels.append({"name": name, "books": _dedup_books(selected)})

    carousels = []

    carousels.append({"name": "Top Picks", "books": top_picks})

    carousels.extend(per_query_carousels)

    return {"carousels": carousels, "has_profile": has_profile}
