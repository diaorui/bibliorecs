import json
import os
import sys
import re
import urllib.error
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import date, datetime

from flask import Flask, render_template, abort, jsonify, request, redirect
import api
import config
import db
import patron
import updater
import recommend as recmod
book_category = recmod.book_category

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["DEBUG_MODE"] = "--debug" in sys.argv or os.environ.get("BIBLIORECS_DEBUG") == "1"

LIBRARY_ID = config.LIBRARY_ID

OL_URL = "https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
SYN_URL = f"https://secure.syndetics.com/index.aspx?isbn={{isbn}}/{{size}}.GIF&client={config.SYNDETICS_CLIENT}&type=xw12&oclc="
PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='180' viewBox='0 0 120 180'%3E%3Crect width='120' height='180' fill='%23e8e8ed' rx='4'/%3E%3Cpath d='M45 55v70l15-8 15 8V55z' fill='%2386868b' opacity='.4'/%3E%3Crect x='48' y='65' width='24' height='2' fill='%2386868b' opacity='.3'/%3E%3C/svg%3E"


def _cover(isbn):
    if not isbn:
        return PLACEHOLDER, PLACEHOLDER
    return (
        SYN_URL.format(isbn=isbn, size="LC"),
        OL_URL.format(isbn=isbn, size="L"),
    )


def _cover_large(isbn):
    if not isbn:
        return PLACEHOLDER, PLACEHOLDER
    return (
        SYN_URL.format(isbn=isbn, size="LC"),
        OL_URL.format(isbn=isbn, size="L"),
    )


def _prefer(a, b):
    return a if a else b


@app.route("/")
def index():
    conn = db.get_conn()

    recs = recmod.get_recommendations(conn)
    by_cat = recs["by_cat"]
    has_profile = recs["has_profile"]

    if not by_cat:
        conn.close()
        return render_template("index.html", carousels=[])

    for items in by_cat.values():
        for r in items:
            _fmt_rec(r)

    call_counts = db.get_category_order(conn, LIBRARY_ID)
    cat_counts = defaultdict(float)
    for row in call_counts:
        cat = recmod.book_category(row["call_number"])
        cat_counts[cat] += recmod._time_weight(row["checkout_date"], row["is_current"])

    cat_order = sorted(
        (c for c in by_cat if c != "Other" and c != "Top Picks" and c != "New"),
        key=lambda c: -cat_counts.get(c, 0),
    )
    if "Other" in by_cat:
        cat_order.append("Other")

    ROW_DESCRIPTIONS = {
        "Top Picks": "Best matches across all categories",
        "Graphic Novels": "Comics and illustrated stories",
        "Picture Books": "Stories told with full-page art",
        "Easy Readers": "Beginning and early chapter books",
        "Fiction": "Chapter books and novels",
        "Board Books": "Sturdy books for the youngest readers",
        "Biography": "Real people and their stories",
        "Science": "Animals, space, earth & experiments",
        "History": "Countries, places & the past",
        "Technology": "Vehicles, pets, cooking & the human body",
        "Arts & Recreation": "Sports, drawing, crafts, games & music",
        "Social Sciences": "Folktales, holidays & how we live together",
        "Other": "Poetry, myths, coding & more",
    }

    new_year_cutoff = date.today().year - config.NEW_BOOK_MAX_AGE_YEARS
    new_desc = f"Published {new_year_cutoff}–{date.today().year}"

    carousels = []
    if "Top Picks" in by_cat:
        carousels.append({"name": "Top Picks", "books": by_cat["Top Picks"],
                          "description": ROW_DESCRIPTIONS["Top Picks"]})
    if "New" in by_cat:
        carousels.append({"name": "New", "books": by_cat["New"],
                          "description": new_desc})
    for c in cat_order:
        entry = {"name": c, "books": by_cat[c]}
        if c in ROW_DESCRIPTIONS:
            entry["description"] = ROW_DESCRIPTIONS[c]
        carousels.append(entry)

    conn.close()
    return render_template("index.html", carousels=carousels)


@app.route("/book/<metadata_id>")
def book_detail(metadata_id):
    conn = db.get_conn()
    row = conn.execute("""
        SELECT *
        FROM books_in_library
        WHERE metadata_id = ? AND library_id = ?
    """, (metadata_id, LIBRARY_ID)).fetchone()
    if not row:
        conn.close()
        return redirect(f"{config.CATALOG_BASE}/v2/record/{metadata_id}")
    book = dict(row)

    borrows = conn.execute("""
        SELECT * FROM borrow_events
        WHERE metadata_id = ? AND library_id = ?
        ORDER BY checkout_date DESC
    """, (metadata_id, LIBRARY_ID)).fetchall()

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
        catalog_url=f"{config.CATALOG_BASE}/v2/record/{metadata_id}",
        img_url=img,
        fallback_url=fallback,
    )


# ── holds ──

@app.route("/api/holds")
def api_holds():
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        data = api.fetch_holds(bc_token, session_id, account_id)
        holds_ents = data.get("entities", {}).get("holds", {})
        conn = db.get_conn()
        holds = []
        for hid, h in holds_ents.items():
            mid = h.get("metadataId")
            row = None
            if mid:
                row = conn.execute(
                    "SELECT title, subtitle, author, isbn FROM books_in_library WHERE metadata_id = ? AND library_id = ?",
                    (mid, LIBRARY_ID)
                ).fetchone()
            holds.append({
                "hold_id": hid,
                "metadata_id": mid,
                "title": row["title"] if row else (h.get("bibTitle") or ""),
                "subtitle": row["subtitle"] if row else None,
                "author": row["author"] if row else "",
                "isbn": row["isbn"] if row else None,
                "status": h.get("status"),
                "position": h.get("holdsPosition"),
                "pickup_branch": (h.get("pickupLocation") or {}).get("code"),
                "placed_date": h.get("holdPlacedDate"),
                "expiry_date": h.get("pickupByDate"),
            })
        conn.close()
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
                              body["metadata_id"], config.HOME_BRANCH_CODE)
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


# ── checkouts ──

@app.route("/api/checkouts")
def api_checkouts():
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        mids = list(api.fetch_current_checkouts_map(bc_token, session_id, account_id))
        return jsonify({"checked_out": mids})
    except Exception as e:
        return jsonify({"checked_out": [], "error": str(e)})


# ── misc ──

@app.route("/api/restart", methods=["POST"])
def api_restart():
    def _do_restart():
        import time, subprocess

        time.sleep(0.5)

        env = os.environ.copy()
        env["BIBLIORECS_RESTARTING"] = "1"
        subprocess.Popen(
            [sys.executable, __file__] + sys.argv[1:],
            cwd=SCRIPT_DIR,
            start_new_session=True,
            env=env,
            stdout=open(os.devnull, 'w'),
            stderr=open(os.devnull, 'w'),
        )

        time.sleep(2.5)
        os._exit(0)

    import threading
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True})


# ── sync history ──

@app.route("/api/sync-history", methods=["POST"])
def api_sync_history():
    try:
        bc_token, session_id, account_id, _ = api._get_auth()
        conn = db.get_conn()
        co = patron.sync_checkouts(conn, bc_token, session_id, account_id)
        hi = patron.sync_history(conn, bc_token, session_id, account_id)
        conn.close()
        return jsonify({"synced": True, "checkouts": co, "history_new": hi})
    except Exception as e:
        return jsonify({"synced": False, "error": str(e)})


@app.route("/api/history/data")
def api_history_data():
    conn = db.get_conn()
    try:
        current = conn.execute("""
            SELECT b.*, bk.title, bk.subtitle, bk.author, bk.isbn, bk.metadata_id
            FROM borrow_events b
            LEFT JOIN books_in_library bk
                ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
            WHERE b.is_current = 1 AND b.library_id = ?
            ORDER BY COALESCE(b.checkout_date, '9999-12-31') ASC
        """, (LIBRARY_ID,)).fetchall()

        past = conn.execute("""
            SELECT b.*, bk.title, bk.subtitle, bk.author, bk.isbn, bk.metadata_id
            FROM borrow_events b
            LEFT JOIN books_in_library bk
                ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
            WHERE b.source = 'history' AND b.library_id = ?
            ORDER BY b.checkout_date DESC
        """, (LIBRARY_ID,)).fetchall()

        current_list = []
        for c in current:
            c = dict(c)
            img, fallback = _cover(c.get("isbn"))
            c["img_url"] = img
            c["fallback_url"] = fallback
            c["due_label"] = due_info(c.get("checkout_date"), True)
            c["due_label_compact"] = due_label_compact(c.get("checkout_date"), True)
            c["due_remaining"] = due_remaining(c.get("checkout_date"), True)
            current_list.append(c)

        past_list = []
        for p in past:
            p = dict(p)
            img, fallback = _cover(p.get("isbn"))
            p["img_url"] = img
            p["fallback_url"] = fallback
            past_list.append(p)

        return jsonify({"current": current_list, "past": past_list})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()


@app.route("/api/history/chart-data")
def api_history_chart_data():
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT strftime('%Y-%m', checkout_date) AS month,
                   COUNT(*) AS borrowed
            FROM borrow_events
            WHERE checkout_date IS NOT NULL
              AND source != 'checkout'
              AND library_id = ?
            GROUP BY month
            ORDER BY month
        """, (LIBRARY_ID,)).fetchall()
        cumulative = 0
        result = []
        for r in rows:
            cumulative += r["borrowed"]
            result.append({"m": r["month"], "b": r["borrowed"], "c": cumulative})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()


@app.route("/api/history/category-data")
def api_history_category_data():
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT bk.call_number
            FROM borrow_events be
            LEFT JOIN books_in_library bk
                ON bk.metadata_id = be.metadata_id AND bk.library_id = be.library_id
            WHERE be.checkout_date IS NOT NULL
              AND be.source != 'checkout'
              AND be.library_id = ?
        """, (LIBRARY_ID,)).fetchall()
        cat_counts = defaultdict(int)
        for r in rows:
            cat_counts[book_category(r["call_number"])] += 1
        result = [{"label": k, "count": v}
                  for k, v in sorted(cat_counts.items(), key=lambda x: (x[0] == "Other", -x[1]))]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()


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
    return render_template("holds.html")


@app.route("/history")
def history():
    conn = db.get_conn()

    current = conn.execute("""
        SELECT b.*, bk.title, bk.subtitle, bk.author, bk.isbn, bk.metadata_id
        FROM borrow_events b
        LEFT JOIN books_in_library bk
            ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
        WHERE b.is_current = 1 AND b.library_id = ?
        ORDER BY COALESCE(b.checkout_date, '9999-12-31') ASC
    """, (LIBRARY_ID,)).fetchall()

    past = conn.execute("""
        SELECT b.*, bk.title, bk.subtitle, bk.author, bk.isbn, bk.metadata_id
        FROM borrow_events b
        LEFT JOIN books_in_library bk
            ON bk.metadata_id = b.metadata_id AND bk.library_id = b.library_id
        WHERE b.source = 'history' AND b.library_id = ?
        ORDER BY b.checkout_date DESC
    """, (LIBRARY_ID,)).fetchall()

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

    chart_data = conn.execute("""
        SELECT strftime('%Y-%m', checkout_date) AS month,
               COUNT(*) AS borrowed
        FROM borrow_events
        WHERE checkout_date IS NOT NULL
          AND source != 'checkout'
          AND library_id = ?
        GROUP BY month
        ORDER BY month
    """, (LIBRARY_ID,)).fetchall()

    cumulative = 0
    chart_months = []
    for row in chart_data:
        cumulative += row["borrowed"]
        chart_months.append({
            "m": row["month"],
            "b": row["borrowed"],
            "c": cumulative,
        })

    cat_rows = conn.execute("""
        SELECT bk.call_number
        FROM borrow_events be
        LEFT JOIN books_in_library bk
            ON bk.metadata_id = be.metadata_id AND bk.library_id = be.library_id
        WHERE be.checkout_date IS NOT NULL
          AND be.source != 'checkout'
          AND be.library_id = ?
    """, (LIBRARY_ID,)).fetchall()
    cat_counts = defaultdict(int)
    for r in cat_rows:
        cat_counts[book_category(r["call_number"])] += 1
    chart_cats = [{"label": k, "count": v}
                  for k, v in sorted(cat_counts.items(), key=lambda x: (x[0] == "Other", -x[1]))]

    conn.close()
    return render_template("history.html",
                           current=current_list,
                           past=past_list,
                           chart_data=chart_months,
                           chart_cats=chart_cats)


# ── update ──

@app.route("/api/update", methods=["POST"])
def trigger_update():
    ok = updater.run_manual()
    return jsonify({"success": ok})





@app.route("/api/stop-update", methods=["POST"])
def trigger_stop_update():
    updater.stop()
    return jsonify({"success": True})


@app.route("/stats")
def stats():
    conn = db.get_conn()
    s = db.get_stats(conn)
    formats = db.get_format_distribution(conn)
    languages = db.get_language_distribution(conn)
    years = db.get_year_distribution(conn)
    cat_rows = conn.execute("""
        SELECT call_number FROM books_in_library WHERE active = 1 AND library_id = ?
    """, (LIBRARY_ID,)).fetchall()
    cat_counts = defaultdict(int)
    for r in cat_rows:
        cat_counts[book_category(r["call_number"])] += 1
    chart_cats = [{"label": k, "count": v}
                  for k, v in sorted(cat_counts.items(), key=lambda x: (x[0] == "Other", -x[1]))]

    conn.close()

    chart_formats = [{"label": _FORMAT_LABELS.get(f["format"], f["format"].replace("_", " ").title().strip()),
                       "count": f["count"]} for f in formats]

    top_langs = languages[:10]
    other_count = sum(l["count"] for l in languages[10:])
    chart_langs = [{"label": l["primary_language"], "count": l["count"]} for l in top_langs]
    if other_count > 0:
        chart_langs.append({"label": "Other", "count": other_count})

    chart_years = [{"label": str(y["publication_year"]), "count": y["count"]} for y in reversed(years)]

    return render_template("stats.html", stats=s, formats=formats,
                           languages=languages,
                           years=years,
                           chart_formats=chart_formats,
                           chart_langs=chart_langs,
                           chart_years=chart_years,
                           chart_cats=chart_cats,
                           home_branch=config.HOME_BRANCH,
                           update_status=updater.status(),
                           update_window_start=config.UPDATE_WINDOW_START,
                           update_window_end=config.UPDATE_WINDOW_END)


# ── template filters ──

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


@app.template_filter("due_label_compact")
def due_label_compact(checkout_date, is_current):
    if not checkout_date or not is_current:
        return None
    from datetime import date as date_cls
    try:
        due = date_cls.fromisoformat(checkout_date[:10])
        today = date_cls.today()
        delta = (due - today).days
        formatted = due.strftime("%b %-d")
        if delta < 0:
            return f"Overdue {formatted}"
        elif delta == 0:
            return "Due today"
        else:
            return f"Due {formatted}"
    except (ValueError, TypeError):
        return None


@app.template_filter("due_remaining")
def due_remaining(checkout_date, is_current):
    if not checkout_date or not is_current:
        return None
    from datetime import date as date_cls
    try:
        due = date_cls.fromisoformat(checkout_date[:10])
        today = date_cls.today()
        delta = (due - today).days
        if delta < 0:
            return f"{-delta} days ago"
        elif delta == 0:
            return None
        elif delta == 1:
            return "1 day left"
        else:
            return f"{delta} days left"
    except (ValueError, TypeError):
        return None


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
    "UK": "Unknown",
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

    r["book_category"] = book_category(r.get("call_number"))

    return r


@app.context_processor
def inject_debug():
    return {"debug": app.config.get("DEBUG_MODE", False)}


if __name__ == "__main__":
    if os.environ.get("BIBLIORECS_RESTARTING"):
        import time
        print("Restart: waiting for previous server to release port 5050...")
        time.sleep(5)

    debug_mode = app.config["DEBUG_MODE"]
    if not debug_mode:
        updater.start()
    app.run(host="0.0.0.0", port=5050, debug=debug_mode)
