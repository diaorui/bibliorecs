import threading
import time
import urllib.error

import api
import config
import vault
from api import dedup_items

_TTL_SEC = {
    "holds": config.HOLDS_TTL_MIN * 60,
    "checkouts": config.CHECKOUTS_TTL_MIN * 60,
    "history": config.HISTORY_TTL_MIN * 60,
}

_LOCK = threading.Lock()
_INFLIGHT = {}
_QUEUE = None
_SEM = threading.Semaphore(config.SYNC_MAX_CONCURRENCY)


def _ensure_worker():
    global _QUEUE
    with _LOCK:
        if _QUEUE is None:
            import queue
            _QUEUE = queue.Queue()
            for _ in range(config.SYNC_MAX_CONCURRENCY):
                threading.Thread(target=_worker, daemon=True).start()


def request(account_id, library_id, data_type, force=False):
    """Enqueue a background refresh job (deduped by key + TTL)."""
    _ensure_worker()
    key = (account_id, library_id, data_type)
    with _LOCK:
        if key in _INFLIGHT:
            return
        if not force and not _is_stale(account_id, library_id, data_type):
            return
        _INFLIGHT[key] = "queued"
        _QUEUE.put(key)


def sync_now(account_id, library_id, data_type):
    """Blocking refresh (used for first sync so callers get data immediately)."""
    key = (account_id, library_id, data_type)
    with _LOCK:
        if key in _INFLIGHT:
            return False
        _INFLIGHT[key] = "running"
    try:
        _run(account_id, library_id, data_type)
        return True
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)


def _is_stale(account_id, library_id, data_type):
    value, updated = vault.get_account_data(account_id, f"{data_type}:{library_id}")
    if value is None:
        return True
    return time.time() - updated >= _TTL_SEC.get(data_type, 3600)


def _worker():
    while True:
        key = _QUEUE.get()
        account_id, library_id, data_type = key
        with _LOCK:
            _INFLIGHT[key] = "running"
        try:
            _run(account_id, library_id, data_type)
        finally:
            with _LOCK:
                _INFLIGHT.pop(key, None)


def _run(account_id, library_id, data_type):
    with _SEM:
        try:
            creds = vault.get_creds(account_id, library_id)
            if not creds:
                return
            if data_type == "history":
                data = _fetch_history(account_id, library_id, creds)
                vault.set_account_data(account_id, f"history:{library_id}", data)
                _prewarm_search(library_id, data)
            elif data_type == "holds":
                data = _call_with_creds(account_id, library_id, creds,
                                        lambda c: api.proxy_fetch_holds(
                                            library_id, c["bc_token"], c["session_id"], c["account_id"]))
                vault.set_account_data(account_id, f"holds:{library_id}", data)
            elif data_type == "checkouts":
                data = _call_with_creds(account_id, library_id, creds,
                                        lambda c: api.proxy_fetch_checkouts(
                                            library_id, c["bc_token"], c["session_id"], c["account_id"]))
                vault.set_account_data(account_id, f"checkouts:{library_id}", data)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _renew_creds_and_retry(account_id, library_id, data_type)
            else:
                _schedule_retry(account_id, library_id, data_type)
        except Exception:
            _schedule_retry(account_id, library_id, data_type)


def _call_with_creds(account_id, library_id, creds, fn):
    try:
        return fn(creds)
    except urllib.error.HTTPError as e:
        if e.code == 401 and creds.get("user") and creds.get("password"):
            new_creds = renew_creds(account_id, library_id, creds)
            return fn(new_creds)
        raise


def renew_creds(account_id, library_id, creds):
    bc_token, session_id, account_id_bc = api.login(
        library_id, creds["user"], creds["password"])
    new_creds = {
        **creds,
        "bc_token": bc_token,
        "session_id": session_id,
        "account_id": account_id_bc,
    }
    vault.set_creds(account_id, library_id, new_creds)
    return new_creds


def _renew_creds_and_retry(account_id, library_id, data_type):
    try:
        creds = vault.get_creds(account_id, library_id)
        if not creds:
            return
        new_creds = renew_creds(account_id, library_id, creds)
        if data_type == "history":
            data = _fetch_history(account_id, library_id, new_creds)
            vault.set_account_data(account_id, f"history:{library_id}", data)
            _prewarm_search(library_id, data)
        elif data_type == "holds":
            data = api.proxy_fetch_holds(
                library_id, new_creds["bc_token"], new_creds["session_id"], new_creds["account_id"])
            vault.set_account_data(account_id, f"holds:{library_id}", data)
        elif data_type == "checkouts":
            data = api.proxy_fetch_checkouts(
                library_id, new_creds["bc_token"], new_creds["session_id"], new_creds["account_id"])
            vault.set_account_data(account_id, f"checkouts:{library_id}", data)
    except Exception:
        _schedule_retry(account_id, library_id, data_type)


def _schedule_retry(account_id, library_id, data_type):
    t = threading.Timer(config.SYNC_RETRY_MIN * 60,
                        request, args=(account_id, library_id, data_type, True))
    t.daemon = True
    t.start()


def _fetch_history(account_id, library_id, creds):
    bc_token = creds["bc_token"]
    session_id = creds["session_id"]
    account_id_bc = creds["account_id"]
    try:
        history_data = api.proxy_fetch_history(
            library_id, bc_token, session_id, account_id_bc, page=0)
        checkouts_data = api.proxy_fetch_checkouts(
            library_id, bc_token, session_id, account_id_bc)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            new_creds = renew_creds(account_id, library_id, creds)
            return _fetch_history(account_id, library_id, new_creds)
        raise
    return _build_history(history_data, checkouts_data)


def _build_history(history_data, checkouts_data):
    bibs = {}
    bibs.update((history_data.get("entities", {}).get("bibs") or {}))
    bibs.update((checkouts_data.get("entities", {}).get("bibs") or {}))
    entries = history_data.get("entities", {}).get("borrowingHistory") or {}
    history = []
    seen = set()
    for _eid, entry in entries.items():
        mid = entry.get("metadataId")
        if not mid:
            continue
        bib = (bibs.get(mid) or {}).get("briefInfo") or {}
        key = f"{mid}|{entry.get('checkedoutDate') or ''}"
        if key in seen:
            continue
        seen.add(key)
        history.append(_history_item(entry, bib, is_current=False))
    checkouts = checkouts_data.get("entities", {}).get("checkouts") or {}
    for cid, co in checkouts.items():
        mid = co.get("metadataId")
        if not mid:
            continue
        bib = (bibs.get(mid) or {}).get("briefInfo") or {}
        history.append(_history_item(co, bib, is_current=True, checkout_id=cid))
    return history


def _history_item(entry, bib, is_current, checkout_id=None):
    item = {
        "metadata_id": entry.get("metadataId"),
        "title": bib.get("title") or entry.get("bibTitle") or "",
        "subtitle": bib.get("subtitle") or "",
        "multiscript_title": bib.get("multiscriptTitle") or "",
        "multiscript_author": bib.get("multiscriptAuthor") or "",
        "authors": dedup_items(bib.get("authors") or []),
        "series": dedup_items(bib.get("series") or [],
                               key=lambda s: (s.get("name") if isinstance(s, dict) else str(s)) or ""),
        "isbns": bib.get("isbns") or [],
        "subjects": dedup_items(bib.get("subjectHeadings") or []),
        "genres": dedup_items(bib.get("genreForm") or []),
        "audiences": bib.get("audiences") or [],
        "primary_language": bib.get("primaryLanguage") or "",
        "content_type": bib.get("contentType") or "",
        "format": bib.get("format") or "",
        "checkout_date": entry.get("checkedoutDate") or entry.get("dueDate"),
        "is_current": is_current,
    }
    if checkout_id:
        item["checkout_id"] = checkout_id
        item["actions"] = entry.get("actions") or []
    return item


def _prewarm_search(library_id, history):
    import search_recs
    for item in history:
        mid = item.get("metadata_id")
        if not mid:
            continue
        meta = {
            "subjects": item.get("subjects") or [],
            "authors": item.get("authors") or [],
            "series": item.get("series") or [],
            "audiences": item.get("audiences") or [],
            "primary_language": item.get("primary_language") or "",
            "title": item.get("title") or "",
            "subtitle": item.get("subtitle") or "",
            "content_type": item.get("content_type") or "",
            "genres": item.get("genres") or [],
        }
        search_recs.search_cache.ensure((library_id, mid), meta=meta, wait=False)
