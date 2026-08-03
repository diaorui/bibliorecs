import json
import os
import sys
import re
import secrets
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Flask, render_template, jsonify, request, g
import api
from api import dedup_items
import config
import login_manager
import search_recs
import sync_manager
import vault

app = Flask(__name__)
app.config["DEBUG_MODE"] = "--debug" in sys.argv or os.environ.get("BIBLIORECS_DEBUG") == "1"

DEVICE_COOKIE = "bc_device"

OL_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='180' viewBox='0 0 120 180'%3E%3Crect width='120' height='180' fill='%23e8e8ed' rx='4'/%3E%3Cpath d='M45 55v70l15-8 15 8V55z' fill='%2386868b' opacity='.4'/%3E%3Crect x='48' y='65' width='24' height='2' fill='%2386868b' opacity='.3'/%3E%3C/svg%3E"


def _lib_from_cookies():
    lib = request.cookies.get("selected_library") or ""
    branch = request.cookies.get("selected_branch") or ""
    if lib not in config.LIBRARIES:
        return "", branch, None
    return lib, branch, config.LIBRARIES[lib]


def _secure_cookie():
    return request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure


def _device_name_from_ua(ua):
    ua = ua or ""
    if "Edg/" in ua:
        b = "Edge"
    elif "Chrome/" in ua:
        b = "Chrome"
    elif "Safari/" in ua:
        b = "Safari"
    elif "Firefox/" in ua:
        b = "Firefox"
    else:
        b = "Browser"
    if "iPhone" in ua or "iPad" in ua:
        plat = "iOS"
    elif "Android" in ua:
        plat = "Android"
    elif "Windows" in ua:
        plat = "Windows"
    elif "Mac OS" in ua:
        plat = "macOS"
    elif "Linux" in ua:
        plat = "Linux"
    else:
        plat = "device"
    return f"{b} on {plat}"


@app.before_request
def _attach_device():
    if request.path.startswith("/static/"):
        g.account_id = None
        g.device_token = None
        return
    token = request.cookies.get(DEVICE_COOKIE)
    if token:
        account_id = vault.account_for_token(token)
        if account_id:
            vault.touch_device(token)
            g.account_id = account_id
            g.device_token = token
            return
    token = secrets.token_urlsafe(32)
    device = vault.create_device(token,
                                 name=_device_name_from_ua(request.headers.get("User-Agent")))
    g.account_id = device["account_id"]
    g.device_token = token


@app.after_request
def _set_device_cookie(resp):
    if getattr(g, "clear_device_cookie", False):
        resp.set_cookie(DEVICE_COOKIE, "", httponly=True, samesite="Lax",
                        max_age=0, secure=_secure_cookie(), path="/")
    token = getattr(g, "device_token", None)
    if token:
        resp.set_cookie(DEVICE_COOKIE, token, httponly=True, samesite="Lax",
                        max_age=400 * 24 * 3600, secure=_secure_cookie(), path="/")
    return resp


def _first_isbn(isbns_json):
    if not isbns_json:
        return None
    try:
        lst = json.loads(isbns_json)
        return lst[0] if lst else None
    except (json.JSONDecodeError, TypeError, IndexError):
        return None


def _syn_url(isbn, size, syndetics_client):
    return f"https://secure.syndetics.com/index.aspx?isbn={isbn}/{size}.GIF&client={syndetics_client}&type=xw12&oclc="


def _cover(isbn, syndetics_client):
    if not isbn:
        return PLACEHOLDER, PLACEHOLDER
    return (
        _syn_url(isbn, "LC", syndetics_client),
        OL_URL.format(isbn=isbn, size="L"),
    )


def _cover_large(isbn, syndetics_client):
    if not isbn:
        return PLACEHOLDER, PLACEHOLDER
    return (
        _syn_url(isbn, "LC", syndetics_client),
        OL_URL.format(isbn=isbn, size="L"),
    )


def _prefer(a, b):
    return a if a else b


@app.route("/")
def index():
    lib_id, branch_code, lib_cfg = _lib_from_cookies()
    return render_template("index.html",
                           selected_library=lib_id, selected_branch=branch_code)


@app.route("/api/recommendations", methods=["GET"])
def api_recommendations():
    lib_id = request.args.get("library_id") or request.cookies.get("selected_library")
    if not lib_id:
        return jsonify({"carousels": [], "has_profile": False})

    lib_cfg = config.LIBRARIES.get(lib_id)
    if not lib_cfg:
        return jsonify({"carousels": [], "has_profile": False})

    history = []
    account_id = getattr(g, "account_id", None)
    if account_id:
        value, _ = vault.get_account_data(account_id, f"history:{lib_id}")
        if value is None and vault.get_creds(account_id, lib_id):
            sync_manager.sync_now(account_id, lib_id, "history")
            value, _ = vault.get_account_data(account_id, f"history:{lib_id}")
        if value:
            history = value

    try:
        result = search_recs.get_recommendations(lib_id, history)
        carousels = result.get("carousels", [])
        has_profile = result.get("has_profile", False)

        syndetics = lib_cfg["syndetics_client"]
        for carousel in carousels:
            for r in carousel.get("books", []):
                _fmt_rec(r, syndetics, lib_id)

        return jsonify({"carousels": carousels, "has_profile": has_profile})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        if app.config.get("DEBUG_MODE"):
            raise
        return jsonify({"carousels": [], "has_profile": False})


@app.route("/api/search/suggest", methods=["GET"])
def api_search_suggest():
    lib_id = request.args.get("library_id") or request.cookies.get("selected_library")
    query = request.args.get("q", "").strip()
    if not lib_id or len(query) < 2:
        return jsonify([])
    return jsonify(api.suggest(lib_id, query))


@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json() or {}
    lib_id = body.get("library_id") or request.cookies.get("selected_library")
    query = body.get("query", "").strip()
    if not lib_id or not query:
        return jsonify({"books": []})

    lib_cfg = config.LIBRARIES.get(lib_id)
    if not lib_cfg:
        return jsonify({"error": "Invalid library"}), 400

    try:
        formats = search_recs.formats_cache.get(lib_id)
        if formats is None:
            formats = search_recs._discover_physical_formats(lib_id)
            search_recs.formats_cache.set(lib_id, formats)
        data = api.search_bibs_json(query, lib_id, formats=formats, f_circ="CIRC", limit=100)
        bibs = api.parse_bib_entities(data)

        books = []
        for mid, bib in bibs.items():
            bi = bib.get("briefInfo", {})
            if "BOOKS" not in bi.get("superFormats", []):
                continue
            a = bib.get("availability", {})
            if a.get("bibType") != "PHYSICAL" or a.get("status") == "ON_ORDER" or a.get("circulationType") == "NON_CIRCULATING":
                continue
            if not bi.get("isbns"):
                continue
            info = api.extract_book_info(mid, bib)
            _fmt_rec(info, lib_cfg["syndetics_client"], lib_id)
            books.append(info)

        return jsonify({"books": books})
    except Exception as e:
        if app.config.get("DEBUG_MODE"):
            raise
        return jsonify({"error": str(e)}), 500


@app.route("/book/<metadata_id>")
def book_detail(metadata_id):
    lib_id, branch_code, lib_cfg = _lib_from_cookies()
    if not lib_id:
        return render_template("not_found.html", metadata_id=metadata_id), 404

    isbn = request.args.get("isbn") or ""
    img, fallback = _cover_large(isbn, lib_cfg["syndetics_client"])

    return render_template(
        "book.html",
        metadata_id=metadata_id,
        isbn=isbn,
        catalog_url=f"{lib_cfg['catalog_base']}/v2/record/{metadata_id}",
        img_url=img,
        fallback_url=fallback,
    )


@app.route("/api/bib/<metadata_id>")
def api_bib(metadata_id):
    lib_id, _, lib_cfg = _lib_from_cookies()
    if not lib_id:
        return jsonify({"error": "no library"}), 400
    isbn = request.args.get("isbn") or ""
    if not isbn:
        return jsonify({"error": "no isbn"}), 400
    try:
        book_data = api.fetch_bib_by_isbn(lib_id, isbn)
        if not book_data:
            return jsonify({"error": "not found"}), 404
        full_desc = book_data.get("description", "")
        book_data = _fmt_rec(book_data, lib_cfg["syndetics_client"], lib_id)
        book_data["description"] = full_desc
        return jsonify(book_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── branches / library config ──

@app.route("/api/branches")
def api_branches():
    result = {}
    for lib_id, cfg in config.LIBRARIES.items():
        result[lib_id] = {
            "catalog_base": cfg["catalog_base"],
            "syndetics_client": cfg["syndetics_client"],
            "branches": search_recs.branches_cache.get(lib_id) or [],
        }
    return jsonify(result)


# ── holds ──

# ── misc ──

# ── sync history ──

@app.route("/settings")
def settings():
    lib_id, branch_code, lib_cfg = _lib_from_cookies()
    return render_template("settings.html",
                           selected_library=lib_id, selected_branch=branch_code)


_ol_cover_cache = {}


@app.route("/api/ol-cover-search/<isbn>")
def api_ol_cover_search(isbn):
    if not isbn:
        return jsonify({"cover_url": None})
    cached = _ol_cover_cache.get(isbn)
    if cached is not None:
        return jsonify({"cover_url": cached})
    try:
        url = "https://openlibrary.org/search.json?q=" + urllib.parse.quote(isbn)
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        docs = data.get("docs", [])
        cover_url = None
        if docs and docs[0].get("cover_i") is not None:
            cover_url = f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-L.jpg"
        _ol_cover_cache[isbn] = cover_url
        return jsonify({"cover_url": cover_url})
    except Exception:
        _ol_cover_cache[isbn] = None
        return jsonify({"cover_url": None})


@app.route("/holds")
def holds_page():
    lib_id, branch_code, _ = _lib_from_cookies()
    return render_template("holds.html",
                           selected_library=lib_id, selected_branch=branch_code)


@app.route("/history")
def history():
    lib_id, branch_code, lib_cfg = _lib_from_cookies()
    return render_template("history.html",
                           selected_library=lib_id, selected_branch=branch_code)


# ── account / creds ──

@app.route("/api/me")
def api_me():
    account_id = getattr(g, "account_id", None)
    if not account_id:
        return jsonify({"account_id": None, "device_id": None, "devices": [], "libs": {}})
    dev = vault.current_device(g.device_token)
    devices = vault.list_devices(account_id)
    libs = {}
    for lib_id in config.LIBRARIES:
        creds = vault.get_creds(account_id, lib_id)
        libs[lib_id] = {
            "connected": bool(creds),
            "user": creds.get("user") if creds else None,
        }
    return jsonify({
        "account_id": account_id,
        "device_id": dev["id"] if dev else None,
        "devices": devices,
        "libs": libs,
    })


@app.route("/api/creds/login", methods=["POST"])
def api_creds_login():
    body = request.get_json() or {}
    lib = body.get("library_id")
    user = (body.get("user") or "").strip()
    password = body.get("password") or ""
    if not lib or not user or not password:
        return jsonify({"success": False, "error": "library_id, user, password required"}), 400
    try:
        bc_token, session_id, account_id_bc = api.login(lib, user, password)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    vault.set_creds(g.account_id, lib, {
        "user": user,
        "password": password,
        "bc_token": bc_token,
        "session_id": session_id,
        "account_id": account_id_bc,
    })
    sync_manager.sync_now(g.account_id, lib, "holds")
    sync_manager.sync_now(g.account_id, lib, "checkouts")
    sync_manager.sync_now(g.account_id, lib, "history")
    return jsonify({"success": True})


@app.route("/api/creds/disconnect", methods=["POST"])
def api_creds_disconnect():
    body = request.get_json() or {}
    lib = body.get("library_id")
    if not lib:
        return jsonify({"error": "library_id required"}), 400
    vault.delete_creds(g.account_id, lib)
    return jsonify({"success": True})


# ── account data (cached, TTL-driven background refresh) ──

_TTL_SEC = {
    "holds": config.HOLDS_TTL_MIN * 60,
    "checkouts": config.CHECKOUTS_TTL_MIN * 60,
    "history": config.HISTORY_TTL_MIN * 60,
}


def _account_data_endpoint(data_type, library_id):
    if library_id not in config.LIBRARIES:
        return jsonify({"data": None, "stale": True, "last_updated": 0}), 400
    account_id = getattr(g, "account_id", None)
    if not account_id:
        return jsonify({"data": None, "stale": True, "last_updated": 0})
    key = f"{data_type}:{library_id}"
    value, updated = vault.get_account_data(account_id, key)
    if value is None:
        if vault.get_creds(account_id, library_id):
            sync_manager.sync_now(account_id, library_id, data_type)
            value, updated = vault.get_account_data(account_id, key)
        return jsonify({"data": value, "stale": value is None, "last_updated": updated})
    stale = time.time() - updated > _TTL_SEC[data_type]
    if stale:
        sync_manager.request(account_id, library_id, data_type)
    return jsonify({"data": value, "stale": stale, "last_updated": updated})


@app.route("/api/holds/<library_id>")
def api_holds_data(library_id):
    return _account_data_endpoint("holds", library_id)


@app.route("/api/checkouts/<library_id>")
def api_checkouts_data(library_id):
    return _account_data_endpoint("checkouts", library_id)


@app.route("/api/history/<library_id>")
def api_history_data(library_id):
    return _account_data_endpoint("history", library_id)


# ── device pairing ──

@app.route("/api/pair/create", methods=["POST"])
def api_pair_create():
    code = vault.create_pair_code(g.account_id)
    return jsonify({"code": code, "expires_at": time.time() + 600,
                    "has_data": vault.has_creds(g.account_id)})


@app.route("/api/pair/claim", methods=["POST"])
def api_pair_claim():
    body = request.get_json() or {}
    code = (body.get("code") or "").strip()
    dev = vault.current_device(g.device_token)
    if not dev:
        return jsonify({"success": False, "error": "no device"})
    return jsonify(vault.claim_pair_code(code, dev["id"]))


@app.route("/api/device/revoke", methods=["POST"])
def api_device_revoke():
    body = request.get_json() or {}
    device_id = body.get("device_id")
    dev = vault.current_device(g.device_token)
    if dev and device_id == dev["id"]:
        return jsonify({"success": False, "error": "cannot revoke current device"}), 400
    ok = vault.revoke_device(device_id, g.account_id)
    return jsonify({"success": ok})


@app.route("/api/device/forget", methods=["POST"])
def api_device_forget():
    dev = vault.current_device(g.device_token)
    if dev:
        vault.forget_device(dev["id"])
    g.device_token = None
    g.clear_device_cookie = True
    return jsonify({"success": True})


# ── Proxy actions (server holds tokens, 401 auto-relogin) ──

def _call_bc(library_id, fn):
    account_id = getattr(g, "account_id", None)
    creds = vault.get_creds(account_id, library_id) if account_id else None
    if not creds:
        return {"error": "no credentials"}, None
    try:
        return None, fn(creds)
    except urllib.error.HTTPError as e:
        if e.code == 401 and creds.get("user") and creds.get("password"):
            try:
                new_creds = login_manager.renew_creds(account_id, library_id, creds)
            except Exception:
                return {"error": str(e)}, None
            try:
                return None, fn(new_creds)
            except Exception as e2:
                return {"error": str(e2)}, None
        return {"error": str(e)}, None
    except Exception as e:
        return {"error": str(e)}, None


@app.route("/api/proxy/checkout/renew", methods=["POST"])
def api_proxy_checkout_renew():
    body = request.get_json() or {}
    lib = body.get("library_id")
    checkout_id = body.get("checkout_id")
    if not lib or not checkout_id:
        return jsonify({"error": "library_id, checkout_id required"}), 400

    def do(creds):
        return api.proxy_renew_checkout(lib, creds["bc_token"], creds["session_id"],
                                        creds["account_id"], [checkout_id])

    err, data = _call_bc(lib, do)
    if err:
        return jsonify({"success": False, "error": err.get("error", "failed")})
    failures = data.get("failures")
    if failures:
        if isinstance(failures, dict) and checkout_id in failures:
            return jsonify({"success": False, "error": str(failures[checkout_id])})
        if isinstance(failures, list):
            for f in failures:
                if f.get("id") == checkout_id or f.get("checkoutId") == checkout_id:
                    msg = f.get("message") or f.get("error") or str(f)
                    return jsonify({"success": False, "error": msg})
    co = (data.get("entities", {}).get("checkouts", {}) or {}).get(checkout_id, {})
    sync_manager.request(g.account_id, lib, "checkouts", force=True)
    sync_manager.request(g.account_id, lib, "history", force=True)
    return jsonify({"success": True, "due_date": co.get("dueDate")})


@app.route("/api/proxy/hold/place", methods=["POST"])
def api_proxy_hold_place():
    body = request.get_json() or {}
    lib = body.get("library_id")
    metadata_id = body.get("metadata_id")
    branch_code = body.get("branch_code")
    if not lib or not metadata_id or not branch_code:
        return jsonify({"error": "library_id, metadata_id, branch_code required"}), 400

    def do(creds):
        return api.proxy_place_hold(lib, creds["bc_token"], creds["session_id"],
                                    creds["account_id"], metadata_id, branch_code)

    err, data = _call_bc(lib, do)
    if err:
        return jsonify({"success": False, "error": err.get("error", "failed")})
    holds = data.get("entities", {}).get("holds", {})
    if holds:
        hid = next(iter(holds))
        h = holds[hid]
        sync_manager.request(g.account_id, lib, "holds", force=True)
        return jsonify({"success": True, "hold_id": hid,
                        "position": h.get("holdsPosition"),
                        "status": h.get("status")})
    return jsonify({"success": False, "error": "no hold in response"})


@app.route("/api/proxy/hold/cancel", methods=["POST"])
def api_proxy_hold_cancel():
    body = request.get_json() or {}
    lib = body.get("library_id")
    hold_id = body.get("hold_id")
    metadata_id = body.get("metadata_id")
    if not lib or not hold_id or not metadata_id:
        return jsonify({"error": "library_id, hold_id, metadata_id required"}), 400

    def do(creds):
        return api.proxy_cancel_hold(lib, creds["bc_token"], creds["session_id"],
                                     creds["account_id"], hold_id, metadata_id)

    err, data = _call_bc(lib, do)
    if err:
        return jsonify({"success": False, "error": err.get("error", "failed")})
    sync_manager.request(g.account_id, lib, "holds", force=True)
    return jsonify({"success": True})




# ── template filters ──

@app.template_filter("fmt_label")
def _fmt_label_filter(val):
    if not val:
        return ""
    return val.replace("_", " ").title().strip()


@app.template_filter("content_type_label")
def _content_type_label(val):
    if not val:
        return ""
    return val.title()


@app.template_filter("lang_label")
def _lang_label(val):
    if not val:
        return ""
    return val.capitalize()


_SUBJECT_SUFFIXES = [
    " Juvenile fiction", " Juvenile literature",
    " Comic books, strips, etc",
]


@app.template_filter("clean_subject")
def _clean_subject(val):
    if not val:
        return ""
    for s in _SUBJECT_SUFFIXES:
        if val.lower().endswith(s.lower()):
            return val[:-len(s)]
    return val


@app.template_filter("dedup_genres")
def _dedup_genres(val):
    if not val:
        return []
    seen = set()
    result = []
    for g in val:
        key = g.lower()
        if key not in seen:
            seen.add(key)
            result.append(_clean_genre(g))
    return result


@app.template_filter("dedup")
def _dedup(val):
    if not val:
        return []
    seen = set()
    result = []
    for item in val:
        key = item.lower() if isinstance(item, str) else item
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


@app.template_filter("duration_format")
def _duration_format(secs):
    if secs is None:
        return "\u2014"
    try:
        s = int(secs)
    except (ValueError, TypeError):
        return "\u2014"
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


@app.template_filter("best_series")
def _best_series(series_list, title=None):
    if not series_list:
        return []

    def strip_parens(text):
        text = text or ""
        prev = None
        while prev != text:
            prev = text
            text = re.sub(r"\s*\([^()]*\)", "", text)
        return text.strip()

    def clean_name(s):
        s = strip_parens(s)
        s = re.sub(r"[\s,;:]+$", "", s).strip()
        s = re.sub(r"\s+books?$", "", s, flags=re.IGNORECASE).strip()
        return s

    items = []
    seen = set()
    for s in series_list:
        if isinstance(s, dict):
            s = s.get("name", "")
        if not isinstance(s, str):
            continue
        c = clean_name(s)
        if not c:
            continue
        k = c.lower()
        if k in seen:
            continue
        seen.add(k)
        items.append((len(c), c))

    if not items:
        return []

    items.sort()

    kept = []
    for _, name in items:
        lname = name.lower()
        if any(lname != kk.lower() and kk.lower() in lname for kk in kept):
            continue
        replaced = False
        new_kept = []
        for k in kept:
            if lname in k.lower() and lname != k.lower():
                replaced = True
                continue
            new_kept.append(k)
        kept = new_kept
        if replaced or not any(lname != kk.lower() and lname in kk.lower() for kk in kept):
            kept.append(name)
        if len(kept) >= 2:
            break

    SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "with"}

    def nice(s):
        words = s.split()
        out = []
        for i, w in enumerate(words):
            lw = w.lower()
            if i == 0:
                if w and w[0].islower():
                    w = w[0].upper() + w[1:]
                out.append(w)
            elif lw in SMALL:
                out.append(lw)
            else:
                if any(c.isupper() for c in w[1:]):
                    out.append(w)
                else:
                    out.append(w[0].upper() + w[1:] if w else w)
        return " ".join(out)

    return [nice(k) for k in kept[:2]]


@app.template_filter("due_info")
def due_info(checkout_date, is_current):
    if not checkout_date or not is_current:
        return None
    from datetime import date as date_cls
    try:
        due = date_cls.fromisoformat(checkout_date[:10])
        today = date_cls.today()
        delta = (due - today).days
        formatted = due.strftime("%B %-d, %Y")
        if delta < 0:
            return f"Overdue {formatted} ({-delta} days ago)"
        elif delta == 0:
            return f"Due today {formatted}"
        elif delta <= 3:
            return f"Due {formatted} ({delta} days left)"
        else:
            return f"Due {formatted} ({delta} days left)"
    except (ValueError, TypeError):
        return f"Due {checkout_date}"


@app.template_filter("parse_json")
def parse_json(val):
    if not val:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


def _json_list(val):
    if not val:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


def _clean_genre(raw):
    parts = re.split(r"<delimit>\s*", raw)
    candidates = []
    for p in parts:
        p = p.strip().rstrip(".")
        low = p.lower()
        if low.startswith("juvenile fiction") or low.startswith("juvenile literature"):
            continue
        if low in ("specimens", "pictorial works", "fiction"):
            continue
        candidates.append(p)
    best = min(candidates, key=len) if candidates else parts[0].strip().rstrip(".")
    best = best.split(",")[0].split("(")[0].strip()
    return best.split(",")[0].split("(")[0].strip()


def _fmt_rec(r, syndetics_client, library_id=None):
    desc = r.get("description") or ""
    if len(desc) > 200:
        r["description"] = desc[:200] + "\u2026"
    isbn = _first_isbn(r.get("isbns"))
    r["isbn"] = isbn
    r["author"] = ", ".join(json.loads(r.get("authors") or "[]"))
    img, fallback = _cover(isbn, syndetics_client)
    r["img_url"] = img
    r["fallback_url"] = fallback

    fmt = r.get("format") or ""
    r["format_label"] = fmt.replace("_", " ").title().strip()

    genres = _json_list(r.get("genres"))
    r["genre_tag"] = _clean_genre(genres[0]) if genres else None

    series = _json_list(r.get("series"))
    if series:
        first = series[0]
        r["series_name"] = first.get("name", first) if isinstance(first, dict) else first
    else:
        r["series_name"] = None

    return r


@app.context_processor
def inject_globals():
    lib_id = request.cookies.get("selected_library") or ""
    branch_code = request.cookies.get("selected_branch") or ""
    branch_name = ""
    lib_branches = search_recs.branches_cache.get(lib_id) or []
    for b in lib_branches:
        if b["code"] == branch_code:
            branch_name = b["name"]
            break
    lib_name = config.LIBRARIES[lib_id]["name"] if lib_id in config.LIBRARIES else ""
    branches_dict = {}
    for lid in config.LIBRARIES:
        b = search_recs.branches_cache.get(lid)
        branches_dict[lid] = b if b is not None else []
    return {
        "debug": app.config.get("DEBUG_MODE", False),
        "selected_library": lib_id,
        "selected_branch": branch_code,
        "selected_library_name": lib_name,
        "selected_branch_name": branch_name,
        "libraries": config.LIBRARIES,
        "branches": branches_dict,
    }


@app.route("/api/restart", methods=["POST"])
def api_restart():
    import threading

    def _do_restart():
        import subprocess, time
        cmd = f"(sleep 2 && exec {sys.executable} '{__file__}' {' '.join(sys.argv[1:])})"
        subprocess.Popen(['/bin/bash', '-c', cmd], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=False).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    for lid in config.LIBRARIES:
        search_recs.formats_cache.ensure(lid, wait=False)
        search_recs.branches_cache.ensure(lid, wait=False)
    debug_mode = app.config["DEBUG_MODE"]
    app.run(host="0.0.0.0", port=5050, debug=debug_mode)
