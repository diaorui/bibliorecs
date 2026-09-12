import logging
import threading
import urllib.error

import api
import login_manager
import vault
from api import dedup_items

logger = logging.getLogger(__name__)

_GUARD = threading.Lock()
_INFLIGHT = {}
_DIRTY = set()


def _claim(key, force=False):
    with _GUARD:
        if key in _INFLIGHT:
            if force:
                _DIRTY.add(key)
            return _INFLIGHT[key], False
        ev = threading.Event()
        _INFLIGHT[key] = ev
        return ev, True


def _finish(key):
    with _GUARD:
        if key in _DIRTY:
            _DIRTY.discard(key)
            return True
        ev = _INFLIGHT.pop(key, None)
    if ev:
        ev.set()
    return False


def _abort(key):
    with _GUARD:
        _DIRTY.discard(key)
        ev = _INFLIGHT.pop(key, None)
    if ev:
        ev.set()


def ensure_data(account_id, library_id, data_type):
    """Blocking guarantee: cached data exists whenever credentials allow it.

    Coalesces with any in-flight sync of the same key. Returns (value, state)
    where state is 'ok', 'no_creds', or 'failed'.
    """
    value, _ = vault.get_account_data(account_id, f"{data_type}:{library_id}")
    if value is not None:
        return value, "ok"
    if not vault.get_creds(account_id, library_id):
        return None, "no_creds"
    key = (account_id, library_id, data_type)
    ev, should_run = _claim(key)
    if should_run:
        _worker(account_id, library_id, data_type)
    else:
        ev.wait()
    value, _ = vault.get_account_data(account_id, f"{data_type}:{library_id}")
    return (value, "ok") if value is not None else (None, "failed")


def refresh_later(account_id, library_id, data_type, force=False):
    """Fire-and-forget background refresh.

    At most one sync per key is queued or running. A duplicate request is
    dropped; force=True marks the in-flight sync dirty so it runs once more
    after the current fetch finishes.
    """
    key = (account_id, library_id, data_type)
    _ev, should_run = _claim(key, force=force)
    if not should_run:
        return
    threading.Thread(target=_worker,
                     args=(account_id, library_id, data_type),
                     daemon=True).start()


def _worker(account_id, library_id, data_type):
    key = (account_id, library_id, data_type)
    try:
        while True:
            if vault.get_creds(account_id, library_id):
                _sync(account_id, library_id, data_type)
            if not _finish(key):
                break
    except Exception:
        logger.exception("sync worker failed: account=%s library=%s type=%s",
                         account_id, library_id, data_type)
        _abort(key)


def _sync(account_id, library_id, data_type):
    """Fetch from BC and write to vault."""
    try:
        creds = vault.get_creds(account_id, library_id)
        if not creds:
            return False
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
        return True
    except Exception:
        logger.exception("sync failed: account=%s library=%s type=%s",
                         account_id, library_id, data_type)
        return False


def _call_with_creds(account_id, library_id, creds, fn):
    try:
        return fn(creds)
    except urllib.error.HTTPError as e:
        if e.code == 401 and creds.get("user") and creds.get("password"):
            new_creds = login_manager.renew_creds(account_id, library_id, creds)
            return fn(new_creds)
        raise


def _fetch_history(account_id, library_id, creds, depth=0):
    bc_token = creds["bc_token"]
    session_id = creds["session_id"]
    account_id_bc = creds["account_id"]
    try:
        history_data = api.proxy_fetch_history(
            library_id, bc_token, session_id, account_id_bc, page=0)
        checkouts_data = api.proxy_fetch_checkouts(
            library_id, bc_token, session_id, account_id_bc)
    except urllib.error.HTTPError as e:
        if e.code == 401 and depth < 2:
            new_creds = login_manager.renew_creds(account_id, library_id, creds)
            return _fetch_history(account_id, library_id, new_creds, depth + 1)
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
