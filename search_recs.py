import json
import threading
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from model2vec import StaticModel

import config
import api
from api import dedup_items

_EMBEDDER = None


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = StaticModel.from_pretrained("minishlab/potion-base-4M")
    return _EMBEDDER


_NON_PHYSICAL_BOOK_FORMATS = frozenset({"EBOOK", "EAUDIO", "EMAGAZINE"})

def _discover_physical_formats(library_id):
    try:
        data = api.search_bibs_json('audience:"children"', library_id,
                                     f_circ="CIRC", limit=1)
        for f in data.get("catalogSearch", {}).get("fields", []):
            if f.get("id") == "FORMAT":
                fmts = [
                    fil["value"] for fil in f.get("fieldFilters", [])
                    if "BOOKS" in fil.get("groupIds", [])
                    and fil["value"] not in _NON_PHYSICAL_BOOK_FORMATS
                ]
                return fmts if fmts else ["BK"]
    except Exception:
        pass
    return ["BK"]


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
        for s in series:
            name = s.get("name", s) if isinstance(s, dict) else s
            name = (name or "").strip()
            if name:
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
        raise ValueError(f"borrowing history entry missing checkout_date")
    try:
        d = date.fromisoformat(checkout_date[:10])
    except (ValueError, TypeError):
        d = date.today()
    days_ago = (date.today() - d).days
    if days_ago < 0:
        return 1.0
    return 2 ** (-days_ago / config.HALF_LIFE_DAYS)



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


def _build_or_query(book):
    subjects = book.get("subjects") or []
    authors = book.get("authors") or []
    series_raw = book.get("series") or []

    parts = []
    for s in subjects[:6]:
        s = s.strip()
        if s:
            parts.append(f'subject:({s})')
    for a in authors[:3]:
        a = a.strip().replace('"', "")
        if a:
            parts.append(f'author:"{a}"')
    for s in series_raw[:3]:
        name = s.get("name", "").strip() if isinstance(s, dict) else str(s).strip()
        name = name.replace('"', "")
        if name:
            parts.append(f'series:"{name}"')

    if not parts:
        return None
    return f"({' OR '.join(parts)})"


def _search_or(query, library_id, formats):
    try:
        data = api.search_bibs_json(query, library_id, formats=formats,
                                     f_circ="CIRC", f_lang="eng",
                                     f_audience="juvenile",
                                     limit=config.POOL_LIMIT)
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
            if "JUVENILE" not in bi.get("audiences", []):
                continue
            info = api.extract_book_info(metadata_id, bib)
            results.append(info)
        return results
    except Exception:
        return []


def get_recommendations(library_id, borrowing_history):
    if not borrowing_history:
        return {"carousels": [], "has_profile": False}

    has_profile = True

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
        authors = dedup_items(authors_raw if isinstance(authors_raw, list) else [])
        series_raw = b.get("series") or []
        series = dedup_items(series_raw, key=lambda s: s.get("name", "") if isinstance(s, dict) else str(s))
        title = (b.get("title") or "").strip()
        subtitle = b.get("subtitle") or ""
        content_type = b.get("content_type") or ""
        subjects = dedup_items(b.get("subjects") or [])
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

    # Select diverse seeds via MMR
    sim_matrix = emb_norm @ emb_norm.T
    np.clip(sim_matrix, 0, 1, out=sim_matrix)
    n_seeds = min(config.DISCOVERY_SEEDS, len(books))
    seed_indices = _mmr(weights, sim_matrix, config.MMR_LAMBDA, n_seeds)

    formats = _discover_physical_formats(library_id)

    pool = []
    pool_mids = set()
    pool_isbns = set()
    pool_lock = threading.Lock()

    def _add_to_pool(info):
        mid = info["metadata_id"]
        if mid in borrowed_mids:
            return False
        info_isbns = json.loads(info.get("isbns") or "[]")
        if any(i in borrowed_isbns for i in info_isbns):
            return False
        with pool_lock:
            if mid not in pool_mids and not any(i in pool_isbns for i in info_isbns):
                pool_mids.add(mid)
                for i_isbn in info_isbns:
                    pool_isbns.add(i_isbn)
                pool.append(info)
                return True
        return False

    # Build OR queries and run in parallel
    with ThreadPoolExecutor(max_workers=10) as executor:
        futs = {}
        for i in seed_indices:
            query = _build_or_query(books[i])
            if query:
                futs[executor.submit(_search_or, query, library_id, formats)] = books[i]["title"]

        for fut in as_completed(futs):
            results = fut.result()
            for info in results:
                _add_to_pool(info)

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

    # MaxSim
    sims = _maxsim_scores(pool_norm, emb_norm, weights)

    # Top Picks (global MMR)
    top_k = min(config.MMR_TOP_K, len(pool))
    global_order = np.argsort(sims)[::-1][:top_k]
    global_subset = pool_norm[global_order]
    global_pairwise = global_subset @ global_subset.T
    np.clip(global_pairwise, 0, 1, out=global_pairwise)
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

    carousels = [{"name": "Top Picks", "books": top_picks}]

    return {"carousels": carousels, "has_profile": has_profile}
