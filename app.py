import json
import os
import sys
import re
import urllib.request
import urllib.parse

from flask import Flask, render_template, jsonify, request
import api
import config
import search_recs

app = Flask(__name__)
app.config["DEBUG_MODE"] = "--debug" in sys.argv or os.environ.get("BIBLIORECS_DEBUG") == "1"

OL_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='180' viewBox='0 0 120 180'%3E%3Crect width='120' height='180' fill='%23e8e8ed' rx='4'/%3E%3Cpath d='M45 55v70l15-8 15 8V55z' fill='%2386868b' opacity='.4'/%3E%3Crect x='48' y='65' width='24' height='2' fill='%2386868b' opacity='.3'/%3E%3C/svg%3E"


def _lib_from_cookies():
    lib = request.cookies.get("selected_library") or ""
    branch = request.cookies.get("selected_branch") or ""
    if lib not in config.LIBRARIES:
        return "", branch, None
    return lib, branch, config.LIBRARIES[lib]


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


@app.route("/api/recommendations", methods=["POST"])
def api_recommendations():
    body = request.get_json() or {}
    lib_id = body.get("library_id") or request.cookies.get("selected_library")
    if not lib_id:
        return jsonify({"carousels": [], "has_profile": False})

    lib_cfg = config.LIBRARIES.get(lib_id)
    if not lib_cfg:
        return jsonify({"carousels": [], "has_profile": False})

    borrowing_history = body.get("borrowing_history", [])

    try:
        result = search_recs.get_recommendations(lib_id, borrowing_history)
        carousels = result.get("carousels", [])
        has_profile = result.get("has_profile", False)

        syndetics = lib_cfg["syndetics_client"]
        for carousel in carousels:
            for r in carousel.get("books", []):
                _fmt_rec(r, syndetics, lib_id)

        return jsonify({"carousels": carousels, "has_profile": has_profile})
    except Exception as e:
        if app.config.get("DEBUG_MODE"):
            raise
        return jsonify({"carousels": [], "has_profile": False})


@app.route("/book/<metadata_id>")
def book_detail(metadata_id):
    lib_id, branch_code, lib_cfg = _lib_from_cookies()
    if not lib_id:
        return render_template("not_found.html", metadata_id=metadata_id), 404

    isbn = request.args.get("isbn") or ""

    book_data = None
    if isbn:
        try:
            book_data = api.fetch_bib_by_isbn(lib_id, isbn)
        except Exception:
            pass

    if not book_data:
        return render_template("not_found.html", metadata_id=metadata_id,
                               selected_library=lib_id, selected_branch=branch_code), 404

    book = dict(book_data)
    book["isbn"] = _first_isbn(book.get("isbns"))
    book["author"] = ", ".join(json.loads(book.get("authors") or "[]"))

    img, fallback = _cover_large(book["isbn"], lib_cfg["syndetics_client"])
    return render_template(
        "book.html",
        metadata_id=metadata_id,
        book=book,
        borrows=[],
        subjects=_json_list(book.get("subjects")),
        genres=_json_list(book.get("genres")),
        series=_json_list(book.get("series")),
        catalog_url=f"{lib_cfg['catalog_base']}/v2/record/{metadata_id}",
        img_url=img,
        fallback_url=fallback,
        selected_library=lib_id, selected_branch=branch_code,
    )


# ── branches / library config ──

@app.route("/api/branches")
def api_branches():
    result = {}
    for lib_id, cfg in config.LIBRARIES.items():
        try:
            branches = api.fetch_branches(cfg["gateway_base"])
        except Exception:
            branches = []
        result[lib_id] = {
            "catalog_base": cfg["catalog_base"],
            "syndetics_client": cfg["syndetics_client"],
            "branches": branches,
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


# ── Proxy endpoints (stateless — tokens from frontend) ──

@app.route("/api/proxy/login", methods=["POST"])
def api_proxy_login():
    body = request.get_json()
    if not body or "library_id" not in body or "user" not in body or "password" not in body:
        return jsonify({"error": "library_id, user, password required"}), 400
    try:
        bc_token, session_id, account_id = api.login(
            body["library_id"], body["user"], body["password"]
        )
        return jsonify({"success": True, "bc_token": bc_token,
                        "session_id": session_id, "account_id": account_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/proxy/holds", methods=["POST"])
def api_proxy_holds():
    body = request.get_json() or {}
    for k in ("library_id", "bc_token", "session_id", "account_id"):
        if k not in body:
            return jsonify({"error": f"{k} required"}), 400
    try:
        data = api.proxy_fetch_holds(body["library_id"], body["bc_token"],
                                      body["session_id"], body["account_id"])
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/proxy/checkouts", methods=["POST"])
def api_proxy_checkouts():
    body = request.get_json() or {}
    for k in ("library_id", "bc_token", "session_id", "account_id"):
        if k not in body:
            return jsonify({"error": f"{k} required"}), 400
    try:
        data = api.proxy_fetch_checkouts(body["library_id"], body["bc_token"],
                                          body["session_id"], body["account_id"])
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/proxy/history", methods=["POST"])
def api_proxy_history():
    body = request.get_json() or {}
    for k in ("library_id", "bc_token", "session_id", "account_id"):
        if k not in body:
            return jsonify({"error": f"{k} required"}), 400
    page = body.get("page", 0)
    try:
        data = api.proxy_fetch_history(body["library_id"], body["bc_token"],
                                        body["session_id"], body["account_id"], page)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/proxy/bib/<metadata_id>", methods=["POST"])
def api_proxy_bib(metadata_id):
    body = request.get_json() or {}
    for k in ("library_id", "bc_token", "session_id"):
        if k not in body:
            return jsonify({"error": f"{k} required"}), 400
    try:
        data = api.proxy_fetch_bib(body["library_id"], body["bc_token"],
                                    body["session_id"], metadata_id)
        bib = data.get("entities", {}).get("bibs", {}).get(metadata_id)
        if not bib:
            return jsonify({"error": "not found"}), 404
        bi = bib.get("briefInfo", {})
        isbns = bi.get("isbns", [])
        isbn = isbns[0] if isbns else ""
        syndetics = config.LIBRARIES.get(body["library_id"], {}).get("syndetics_client", "sepup")
        img, fallback = _cover(isbn, syndetics)
        return jsonify({
            "metadata_id": metadata_id,
            "title": bi.get("title"),
            "subtitle": bi.get("subtitle"),
            "author": ", ".join(bi.get("authors") or []),
            "isbn": isbn,
            "isbns": isbns,
            "img_url": img,
            "fallback_url": fallback,
            "format": bi.get("format"),
            "description": bi.get("description"),
            "publication_year": api._parse_year(bi.get("publicationDate", "")),
            "series": bi.get("series", []),
            "subjects": bi.get("subjectHeadings", []),
            "genres": bi.get("genreForm", []),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/proxy/hold/place", methods=["POST"])
def api_proxy_hold_place():
    body = request.get_json() or {}
    for k in ("library_id", "bc_token", "session_id", "account_id", "metadata_id", "branch_code"):
        if k not in body:
            return jsonify({"error": f"{k} required"}), 400
    try:
        data = api.proxy_place_hold(body["library_id"], body["bc_token"],
                                     body["session_id"], body["account_id"],
                                     body["metadata_id"], body["branch_code"])
        holds = data.get("entities", {}).get("holds", {})
        if holds:
            hid = next(iter(holds))
            h = holds[hid]
            return jsonify({"success": True, "hold_id": hid,
                            "position": h.get("holdsPosition"),
                            "status": h.get("status")})
        return jsonify({"success": False, "error": "no hold in response"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/proxy/hold/cancel", methods=["POST"])
def api_proxy_hold_cancel():
    body = request.get_json() or {}
    for k in ("library_id", "bc_token", "session_id", "account_id", "hold_id", "metadata_id"):
        if k not in body:
            return jsonify({"error": f"{k} required"}), 400
    try:
        data = api.proxy_cancel_hold(body["library_id"], body["bc_token"],
                                      body["session_id"], body["account_id"],
                                      body["hold_id"], body["metadata_id"])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


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
    branches = {}
    for lid, cfg in config.LIBRARIES.items():
        try:
            branches[lid] = api.fetch_branches(cfg["gateway_base"])
        except Exception:
            branches[lid] = []
    branch_name = ""
    if lib_id in branches:
        for b in branches[lib_id]:
            if b["code"] == branch_code:
                branch_name = b["name"]
                break
    lib_name = config.LIBRARIES[lib_id]["name"] if lib_id in config.LIBRARIES else ""
    return {
        "debug": app.config.get("DEBUG_MODE", False),
        "selected_library": lib_id,
        "selected_branch": branch_code,
        "selected_library_name": lib_name,
        "selected_branch_name": branch_name,
        "libraries": config.LIBRARIES,
        "branches": branches,
    }


if __name__ == "__main__":
    debug_mode = app.config["DEBUG_MODE"]
    app.run(host="0.0.0.0", port=5050, debug=debug_mode)
