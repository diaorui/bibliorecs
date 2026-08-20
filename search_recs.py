import json
import os
from datetime import date

import numpy as np
from model2vec import StaticModel

import config
import api
from api import dedup_items
from cache import RefreshCache

_EMBEDDER = None


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = StaticModel.from_pretrained("minishlab/potion-base-4M")
    return _EMBEDDER


_NON_PHYSICAL_BOOK_FORMATS = frozenset({"EBOOK", "EAUDIO", "EMAGAZINE", "GRAPHIC_NOVEL_DOWNLOAD", "EAUDIOBOOK"})


def _discover_physical_formats(library_id):
    try:
        data = api.search_bibs_json('*', library_id,
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


def _mmr(scores, embeddings, lambda_param, top_n):
    """Greedy MMR in O(k · n · d). embeddings must be L2-normalized rows."""
    n = len(scores)
    if n == 0 or top_n <= 0:
        return []
    scores = np.asarray(scores, dtype=np.float64)
    max_sim = np.full(n, -np.inf, dtype=np.float64)
    selected_mask = np.zeros(n, dtype=bool)
    selected = []

    for _ in range(min(top_n, n)):
        div = np.maximum(max_sim, 0.0)
        mmr_vals = lambda_param * scores - (1.0 - lambda_param) * div
        mmr_vals[selected_mask] = -np.inf
        best_idx = int(np.argmax(mmr_vals))
        if not np.isfinite(mmr_vals[best_idx]):
            break
        selected.append(best_idx)
        selected_mask[best_idx] = True
        sims_to_new = np.clip(embeddings @ embeddings[best_idx], 0.0, 1.0)
        np.maximum(max_sim, sims_to_new, out=max_sim)

    return selected


def _build_or_query_raw(subjects, authors, series_raw):
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


def _refresh_search(key, meta):
    library_id, metadata_id = key
    subjects = meta.get("subjects", []) or []
    authors = meta.get("authors", []) or []
    series_raw = meta.get("series", []) or []

    formats = formats_cache.get(library_id)
    if formats is None:
        formats = _discover_physical_formats(library_id)
        formats_cache.set(library_id, formats)

    query = _build_or_query_raw(subjects, authors, series_raw)
    if not query:
        return []

    try:
        seed_audiences = meta.get("audiences", []) or []
        f_audience = "|".join(seed_audiences) if seed_audiences else None
        seed_lang = meta.get("primary_language", "") or ""
        f_lang = seed_lang.lower() if seed_lang else None
        data = api.search_bibs_json(query, library_id, formats=formats,
                                     f_circ="CIRC", f_lang=f_lang,
                                     f_audience=f_audience,
                                     limit=config.POOL_LIMIT)
        bibs = api.parse_bib_entities(data)
        results = []
        for mid, bib in bibs.items():
            bi = bib.get("briefInfo", {})
            if not bi.get("isbns"):
                continue
            if seed_lang:
                lang = (bi.get("primaryLanguage") or "").lower()
                if lang and lang != seed_lang.lower():
                    continue
            if seed_audiences:
                bib_audiences = bi.get("audiences", []) or []
                if not any(a in bib_audiences for a in seed_audiences):
                    continue
            a = bib.get("availability", {})
            if a.get("bibType") != "PHYSICAL" or a.get("status") == "ON_ORDER" or a.get("circulationType") == "NON_CIRCULATING":
                continue
            results.append(api.extract_book_info(mid, bib))
        if not results:
            return []
        seed_text = _build_embedding_text(
            title=meta.get("title") or "",
            subtitle=meta.get("subtitle") or "",
            content_type=meta.get("content_type") or "",
            authors=authors,
            series=series_raw,
            subjects=subjects,
            genres=meta.get("genres") or [],
        )
        if not seed_text:
            return results
        seed_vec = _embed_texts([seed_text])[0]
        cand_vecs = _embed_texts([_build_pool_embed_text(info) for info in results])
        sims = cand_vecs @ seed_vec
        return [info for info, s in zip(results, sims) if float(s) >= config.MIN_COSINE]
    except Exception:
        raise


def _refresh_formats(key, meta=None):
    return _discover_physical_formats(key)


_formats_path = os.path.join(os.path.dirname(__file__), "formats.json")
formats_cache = RefreshCache(_refresh_formats, refresh_hours=config.FORMATS_REFRESH_HOURS,
                              failure_retry_minutes=5, name="formats",
                              persist_path=_formats_path)
search_cache = RefreshCache(_refresh_search, refresh_hours=config.REFRESH_HOURS,
                             failure_retry_minutes=5, name="search",
                             persist_table="search")


def _refresh_branches(key, meta=None):
    gateway = config.LIBRARIES[key]["gateway_base"]
    return api.fetch_branches(gateway)


_branches_path = os.path.join(os.path.dirname(__file__), "branches.json")
branches_cache = RefreshCache(
    _refresh_branches,
    refresh_hours=24,
    failure_retry_minutes=5,
    name="branches",
    persist_path=_branches_path,
    scanner_interval=60,
)


def _add_to_pool(info, pool, pool_mids, pool_isbns, borrowed_mids, borrowed_isbns):
    mid = info["metadata_id"]
    if mid in borrowed_mids:
        return False
    info_isbns = json.loads(info.get("isbns") or "[]")
    if any(i in borrowed_isbns for i in info_isbns):
        return False
    if mid in pool_mids:
        return False
    if any(i in pool_isbns for i in info_isbns):
        return False
    pool_mids.add(mid)
    for i_isbn in info_isbns:
        pool_isbns.add(i_isbn)
    pool.append(info)
    return True


def _build_pool_embed_text(info):
    title = info.get("title") or ""
    subtitle = info.get("subtitle") or ""
    content_type = info.get("content_type") or ""
    authors_raw = json.loads(info.get("authors") or "[]")
    series_raw = json.loads(info.get("series") or "[]")
    subjects_raw = json.loads(info.get("subjects") or "[]")
    genres_raw = json.loads(info.get("genres") or "[]")
    return _build_embedding_text(
        title=title, subtitle=subtitle, content_type=content_type,
        authors=authors_raw, series=series_raw,
        subjects=subjects_raw, genres=genres_raw)


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
            "_meta": {
                "subjects": subjects,
                "authors": authors,
                "series": series,
                "audiences": b.get("audiences") or [],
                "primary_language": b.get("primary_language") or "",
                "title": title,
                "subtitle": subtitle,
                "content_type": content_type,
                "genres": genres,
            },
        })

    if not books:
        return {"carousels": [], "has_profile": True}

    texts = [b["_text"] for b in books]
    emb_norm = _embed_texts(texts)
    weights = np.array([_time_weight(b["checkout_date"], b["is_current"]) for b in books], dtype=float)

    pool = []
    pool_mids = set()
    pool_isbns = set()

    for b in books:
        mid = b["metadata_id"]
        key = (library_id, mid)
        results, stale = search_cache.get_with_age(key)
        if results is not None:
            for info in results:
                _add_to_pool(info, pool, pool_mids, pool_isbns, borrowed_mids, borrowed_isbns)
            if stale:
                search_cache.ensure(key, meta=b["_meta"], wait=False)
        else:
            search_cache.ensure(key, meta=b["_meta"], wait=False)

    if not pool:
        return {"carousels": [], "has_profile": has_profile}

    pool_norm = _embed_texts([_build_pool_embed_text(info) for info in pool])

    sims = _maxsim_scores(pool_norm, emb_norm, weights)

    mmr_selected = _mmr(sims, pool_norm, config.MMR_LAMBDA, config.TOP_CANDIDATES)

    top_picks = []
    for rank, i in enumerate(mmr_selected, 1):
        info = dict(pool[i])
        info["score"] = float(sims[i])
        info["category_rank"] = rank
        top_picks.append(info)

    return {"carousels": [{"name": "Top Picks", "books": top_picks}], "has_profile": has_profile}
