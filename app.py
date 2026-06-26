import json
import os
import sys
import re
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, render_template, abort, jsonify, request
import api
import config
import db
import updater
from recommend import book_category

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["DEBUG_MODE"] = "--debug" in sys.argv or os.environ.get("BIBLIORECS_DEBUG") == "1"

OL_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
SYN_URL = "https://secure.syndetics.com/index.aspx?isbn={isbn}/{size}.GIF&client=sepup&type=xw12&oclc="


def _cover(isbn):
    if not isbn:
        return None, None
    return (
        SYN_URL.format(isbn=isbn, size="LC"),
        OL_URL.format(isbn=isbn, size="L"),
    )


def _cover_large(isbn):
    if not isbn:
        return None, None
    return (
        SYN_URL.format(isbn=isbn, size="LC"),
        OL_URL.format(isbn=isbn, size="L"),
    )


@app.route("/")
def index():
    conn = db.get_conn()

    english_where = "AND b.primary_language = 'eng'" if config.FILTER_ENGLISH else ""
    rows = conn.execute(f"""
        SELECT r.category, r.category_rank, r.metadata_id, r.score,
               b.title, b.subtitle, b.author, b.isbn, b.format,
               b.content_type, b.subjects, b.genres, b.description, b.series
        FROM recommendation_cache r
        INNER JOIN books b ON b.metadata_id = r.metadata_id
        WHERE 1=1 {english_where}
        ORDER BY r.category, r.category_rank
    """).fetchall()

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(_fmt_rec(dict(r)))

    call_counts = db.get_category_order(conn)
    cat_counts = defaultdict(int)
    for row in call_counts:
        cat = book_category(row["call_number"])
        cat_counts[cat] += row["cnt"]

    cat_order = sorted(
        (c for c in by_cat if c != "Other"),
        key=lambda c: -cat_counts.get(c, 0),
    )
    if "Other" in by_cat:
        cat_order.append("Other")

    SUB_LABELS = {
        "Science": ["Animals", "Dinosaurs", "Space", "Earth & Nature", "Insects & Bugs"],
        "History": ["US History", "World History", "Exploration & Travel", "Ancient Times"],
        "Technology": ["Vehicles", "Pets & Farms", "Cooking", "How Things Work"],
        "Arts & Recreation": ["Sports", "Games", "Drawing & Crafts", "Music"],
        "Social Sciences": ["Fairy Tales & Folklore", "Holidays & Traditions", "Community & Family", "Social Issues"],
    }

    carousels = []
    for c in cat_order:
        entry = {"name": c, "books": by_cat[c]}
        if c in SUB_LABELS:
            entry["subs"] = SUB_LABELS[c]
        carousels.append(entry)

    sync_time = db.get_recommendation_sync_time(conn)
    conn.close()
    return render_template("index.html", carousels=carousels, sync_time=sync_time)


@app.route("/book/<metadata_id>")
def book_detail(metadata_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM books WHERE metadata_id = ?",
                       (metadata_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    book = dict(row)

    borrows = conn.execute("""
        SELECT * FROM borrow_events WHERE metadata_id = ?
        ORDER BY checkout_date DESC
    """, (metadata_id,)).fetchall()

    img, fallback = _cover_large(book.get("isbn"))
    conn.close()
    return render_template(
        "book.html",
        metadata_id=metadata_id,
        book=book,
        borrows=[dict(b) for b in borrows],
        subjects=_json_list(book.get("subjects")),
        genres=_json_list(book.get("genres")),
        series=_json_list(book.get("series")),
        catalog_url=f"https://sclibrary.bibliocommons.com/v2/record/{metadata_id}",
        img_url=img,
        fallback_url=fallback,
    )


@app.route("/api/availability/<metadata_id>")
def api_availability(metadata_id):
    conn = db.get_conn()
    try:
        result = _resolve_availability(conn, [metadata_id])[metadata_id]
    except Exception as e:
        result = {"error": str(e)}
    conn.close()
    return jsonify(result)


@app.route("/api/availability/batch")
def api_batch_availability():
    """Fetch live availability for multiple books. Pass ?ids=A,B,C"""
    ids = request.args.get("ids", "")
    metadata_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not metadata_ids:
        return jsonify({})
    conn = db.get_conn()
    try:
        result = _resolve_availability(conn, metadata_ids)
    except Exception as e:
        result = {mid: {"error": str(e)} for mid in metadata_ids}
    conn.close()
    return jsonify(result)


def _resolve_availability(conn, metadata_ids):
    """Return dict of metadata_id → {at_home, available_copies, ...}.
    Checks cache first, fetches fresh from API only for stale or missing entries."""
    cutoff = (datetime.utcnow() - timedelta(seconds=config.AVAILABILITY_CACHE_SECONDS)).isoformat()

    rows = conn.execute("""
        SELECT metadata_id, at_home, status, available_copies, total_copies, held_copies, last_checked
        FROM availability
        WHERE metadata_id IN ({})
    """.format(",".join("?" * len(metadata_ids))), metadata_ids).fetchall()

    cached = {r["metadata_id"]: r for r in rows}
    result = {}
    stale = []

    for mid in metadata_ids:
        r = cached.get(mid)
        if r and r["last_checked"] and r["last_checked"] >= cutoff:
            result[mid] = {
                "owns_home": r["total_copies"] > 0,
                "at_home": bool(r["at_home"]),
                "status": r["status"] or "",
                "available_copies": r["available_copies"],
                "total_copies": r["total_copies"],
                "held_copies": r["held_copies"],
            }
        else:
            stale.append(mid)

    if stale:
        try:
            fresh = api.fetch_batch_availability(stale)
            for mid in stale:
                info = fresh.get(mid)
                if info:
                    result[mid] = {
                        "owns_home": info.get("owns_home", False),
                        "at_home": info.get("at_home", False),
                        "status": info.get("status", ""),
                        "available_copies": info.get("available_copies", 0),
                        "total_copies": info.get("total_copies", 0),
                        "held_copies": info.get("held_copies", 0),
                    }
                    db.upsert_availability(conn, metadata_id=mid, status=info.get("status", ""),
                                           available_copies=info.get("available_copies", 0),
                                           total_copies=info.get("total_copies", 0),
                                           held_copies=info.get("held_copies", 0),
                                           at_home=info.get("at_home", False))
                else:
                    result[mid] = {"at_home": False}
            conn.commit()
        except Exception as e:
            for mid in stale:
                r = cached.get(mid)
                result[mid] = {"at_home": bool(r["at_home"]) if r else False,
                               "_stale": True}

    return result


# ─────────────────────────────── holds ───────────────────────────────


@app.route("/api/holds")
def api_holds():
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        data = api.fetch_holds(bc_token, session_id, account_id)
        holds_ents = data.get("entities", {}).get("holds", {})
        holds = [
            {
                "hold_id": hid,
                "metadata_id": h.get("metadataId"),
                "title": h.get("bibTitle"),
                "status": h.get("status"),
                "position": h.get("holdsPosition"),
                "pickup_branch": (h.get("pickupLocation") or {}).get("code"),
                "placed_date": h.get("holdPlacedDate"),
                "expiry_date": h.get("expiryDate"),
            }
            for hid, h in holds_ents.items()
        ]
        quotas = data.get("borrowing", {}).get("summaries", {}).get("holds", {}).get("quotas", [])
        return jsonify({"holds": holds, "quotas": quotas})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/hold/place", methods=["POST"])
def api_hold_place():
    body = request.get_json()
    if not body or "metadata_id" not in body:
        return jsonify({"error": "metadata_id required"}), 400
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        data = api.place_hold(bc_token, session_id, account_id,
                              body["metadata_id"], config.CENTRAL_PARK_BRANCH_CODE)
        holds = data.get("entities", {}).get("holds", {})
        if holds:
            hid = next(iter(holds))
            h = holds[hid]
            return jsonify({
                "success": True,
                "hold_id": hid,
                "position": h.get("holdsPosition"),
                "status": h.get("status"),
            })
        return jsonify({"success": False, "error": "no hold in response"})
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            err = json.loads(e.read().decode())
            msg = err.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return jsonify({"success": False, "error": msg}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/hold/cancel", methods=["POST"])
def api_hold_cancel():
    body = request.get_json()
    if not body or "hold_id" not in body or "metadata_id" not in body:
        return jsonify({"error": "hold_id and metadata_id required"}), 400
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        data = api.cancel_hold(bc_token, session_id, account_id,
                               body["hold_id"], body["metadata_id"])
        failures = data.get("failures") or {}
        if body["hold_id"] in failures:
            return jsonify({"success": False, "error": failures[body["hold_id"]]})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/checkouts")
def api_checkouts():
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        mids = list(api.fetch_current_checkouts_map(bc_token, session_id, account_id))
        return jsonify({"checked_out": mids})
    except Exception as e:
        return jsonify({"checked_out": [], "error": str(e)})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    def _do_restart():
        import time, subprocess

        # Let the 200 response reach the browser first
        time.sleep(0.5)

        # Launch a fresh server process.
        # It will see BIBLIORECS_RESTARTING and sleep before trying to bind.
        env = os.environ.copy()
        env["BIBLIORECS_RESTARTING"] = "1"
        subprocess.Popen(
            [sys.executable, __file__] + sys.argv[1:],
            cwd=_SCRIPT_DIR,
            start_new_session=True,
            env=env,
            stdout=open(os.devnull, 'w'),
            stderr=open(os.devnull, 'w'),
        )

        # Old process waits longer so the OS releases port 5050
        time.sleep(2.5)
        os._exit(0)

    import threading
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/holds")
def holds_page():
    """Show all current holds."""
    conn = db.get_conn()
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        data = api.fetch_holds(bc_token, session_id, account_id)
        holds_ents = data.get("entities", {}).get("holds", {})
        holds = []
        for hid, h in holds_ents.items():
            mid = h.get("metadataId")
            row = conn.execute("SELECT title, author, isbn FROM books WHERE metadata_id = ?", (mid,)).fetchone() if mid else None
            holds.append({
                "hold_id": hid,
                "metadata_id": mid,
                "title": h.get("bibTitle") or (row["title"] if row else ""),
                "author": row["author"] if row else "",
                "status": h.get("status"),
                "position": h.get("holdsPosition"),
                "pickup_branch": (h.get("pickupLocation") or {}).get("code"),
                "placed_date": h.get("holdPlacedDate"),
                "expiry_date": h.get("expiryDate"),
                "isbn": row["isbn"] if row else None,
            })
        quotas = data.get("borrowing", {}).get("summaries", {}).get("holds", {}).get("quotas", [])
        ils_quota = next((q for q in quotas if q.get("type") == "ILS"), {})
        od_quota = next((q for q in quotas if q.get("type") == "OverDriveAPI"), {})
        conn.close()
        return render_template("holds.html", holds=holds,
                               ils_used=ils_quota.get("total", 0),
                               ils_total=ils_quota.get("total", 0) + ils_quota.get("remaining", 0) if ils_quota.get("remaining") is not None else None,
                               od_used=od_quota.get("total", 0),
                               od_total=od_quota.get("total", 0) + od_quota.get("remaining", 0) if od_quota.get("remaining") is not None else None)
    except Exception as e:
        conn.close()
        return render_template("holds.html", holds=[], error=str(e))


@app.route("/history")
def history():
    conn = db.get_conn()
    current = conn.execute("""
        SELECT b.*, bk.title, bk.author, bk.isbn, bk.metadata_id
        FROM borrow_events b
        LEFT JOIN books bk ON bk.metadata_id = b.metadata_id
        WHERE b.is_current = 1
        ORDER BY COALESCE(b.checkout_date, '9999-12-31') ASC
    """).fetchall()

    past = conn.execute("""
        SELECT b.*, bk.title, bk.author, bk.isbn, bk.metadata_id
        FROM borrow_events b
        LEFT JOIN books bk ON bk.metadata_id = b.metadata_id
        WHERE b.source = 'history'
        ORDER BY b.checkout_date DESC
    """).fetchall()

    current_list = []
    for c in current:
        c = dict(c)
        img, fallback = _cover(c.get("isbn"))
        c["img_url"] = img
        c["fallback_url"] = fallback
        current_list.append(c)

    past_list = []
    for p in past:
        p = dict(p)
        img, fallback = _cover(p.get("isbn"))
        p["img_url"] = img
        p["fallback_url"] = fallback
        past_list.append(p)

    conn.close()
    return render_template("history.html",
                           current=current_list,
                           past=past_list)


@app.route("/stats")
def stats():
    conn = db.get_conn()
    s = db.get_stats(conn)
    formats = db.get_format_distribution(conn)
    content_types = db.get_content_type_distribution(conn)
    languages = db.get_language_distribution(conn)
    years = db.get_year_distribution(conn)
    sync_time = db.get_recommendation_sync_time(conn)
    total_borrows = conn.execute("SELECT COUNT(*) FROM borrow_events").fetchone()[0]
    conn.close()
    return render_template("stats.html", stats=s, formats=formats,
                           content_types=content_types, languages=languages,
                           years=years, sync_time=sync_time,
                           total_borrows=total_borrows,
                           update_status=updater.status(),
                           update_window_start=config.UPDATE_WINDOW_START,
                           update_window_end=config.UPDATE_WINDOW_END)


@app.template_filter("fmt_label")
def _fmt_label_filter(val):
    if not val:
        return ""
    return _FORMAT_LABELS.get(val, val.replace("_", " ").title().strip())


@app.template_filter("content_type_label")
def _content_type_label(val):
    if not val:
        return ""
    return val.title()


@app.template_filter("lang_label")
def _lang_label(val):
    if not val:
        return ""
    labels = {"eng": "English"}
    return labels.get(val, val)


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


@app.template_filter("best_series")
def _best_series(series_list, title=None):
    """Pick the best 1-2 series names to show on the detail page.
    Strategy:
    - Clean parentheses and trailing junk like " book"/" series"
    - Prefer shorter, core names over longer qualified ones
    - When names overlap (one is substring of another), keep the shorter core
    - Never drop a series just because it matches the book title (Dog Man etc.)
    - Return at most 2, nicely capitalized
    """
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
        # drop trailing " book" / " books" for display (common noise in series names)
        s = re.sub(r"\s+books?$", "", s, flags=re.IGNORECASE).strip()
        return s

    # Normalize and dedup
    items = []
    seen = set()
    for s in series_list:
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

    # Process shortest first
    items.sort()

    kept = []
    for _, name in items:
        lname = name.lower()
        # Skip if we already have a shorter core that this extends
        if any(lname != kk.lower() and kk.lower() in lname for kk in kept):
            continue
        # If this is shorter and contained in an existing, replace the longer one
        replaced = False
        new_kept = []
        for k in kept:
            if lname in k.lower() and lname != k.lower():
                replaced = True
                continue  # drop the longer
            new_kept.append(k)
        kept = new_kept
        if replaced or not any(lname != kk.lower() and lname in kk.lower() for kk in kept):
            kept.append(name)
        if len(kept) >= 2:
            break

    # Nice display casing
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


_FORMAT_LABELS = {
    "GRAPHIC_NOVEL": "Graphic Novel",
    "PICTURE_BOOK": "Picture Book",
    "BOOK": "Book",
    "BK": "Book",
    "BOARD_BK": "Board Book",
    "PAPERBACK": "Paperback",
    "EBOOK": "eBook",
    "AUDIOBOOK": "Audiobook",
    "LARGE_PRINT": "Large Print",
}

_GENRE_LABELS = {
    "Comics (Graphic Works)": "Comics",
    "Humorous Comics": "Humor",
    "School Comics": "School",
    "Action and Adventure Comics": "Adventure",
    "Graphic Novels": "Graphic Novel",
    "Fantasy comic books, strips, etc": "Fantasy",
    "Animal comics": "Animals",
}


def _clean_genre(raw):
    cleaned = _GENRE_LABELS.get(raw)
    if cleaned:
        return cleaned
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
    return best[:25] if len(best) > 25 else best


def _fmt_rec(r):
    desc = r.get("description") or ""
    if len(desc) > 200:
        r["description"] = desc[:200] + "\u2026"
    img, fallback = _cover(r.get("isbn"))
    r["img_url"] = img
    r["fallback_url"] = fallback

    fmt = r.get("format") or ""
    r["format_label"] = _FORMAT_LABELS.get(fmt, fmt.replace("_", " ").title().strip())

    genres = _json_list(r.get("genres"))
    r["genre_tag"] = _clean_genre(genres[0]) if genres else None

    series = _json_list(r.get("series"))
    r["series_name"] = series[0] if series else None

    return r


@app.context_processor
def inject_debug():
    return {"debug": app.config.get("DEBUG_MODE", False)}


if __name__ == "__main__":
    # When the restart button was used, the previous process may still hold port 5050
    # (TCP TIME_WAIT). Sleep long enough before trying to bind.
    if os.environ.get("BIBLIORECS_RESTARTING"):
        import time
        print("Restart: waiting for previous server to release port 5050...")
        time.sleep(5)

    debug_mode = app.config["DEBUG_MODE"]
    if not debug_mode:
        updater.start()
    app.run(host="0.0.0.0", port=5050, debug=debug_mode)
