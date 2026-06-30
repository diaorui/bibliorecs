import time
import json
import os
import threading
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar
import re
import config


CATALOG_BASE = "https://sclibrary.bibliocommons.com"
GATEWAY_BASE = "https://gateway.bibliocommons.com/v2/libraries/sclibrary"
PUBLIC_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://sclibrary.bibliocommons.com",
}
REQUEST_DELAY = 0.6

# ─────────────────────────── public catalog API ───────────────────────────


def search_bibs_json(query, formats=None,
                     search_type="bl", page=1, sort=None, retries=3):
    if formats:
        fmt_clause = " OR ".join(formats)
        query = f'{query} formatcode:({fmt_clause})'

    body = {
        "query": query,
        "searchType": search_type,
        "custom_edit": "false",
        "suppress": "true",
        "page": str(page),
        "view": "small",
    }
    if sort:
        body["sortBy"] = sort

    url = f"{GATEWAY_BASE}/bibs/search?locale=en-US"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": CATALOG_BASE,
    }

    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def parse_bib_entities(data):
    return data.get("entities", {}).get("bibs", {})


def parse_pagination(data):
    return data.get("catalogSearch", {}).get("pagination", {})


def parse_results(data):
    return data.get("catalogSearch", {}).get("results", [])


def parse_fields(data):
    return data.get("catalogSearch", {}).get("fields", [])


def extract_book_info(metadata_id, bib):
    info = bib.get("briefInfo", {})
    subjects = info.get("subjectHeadings", [])
    composite_subjects = info.get("compositeSubjectHeadings", [])
    genres = info.get("genreForm", [])
    series_raw = info.get("series", [])
    isbns = info.get("isbns", [])
    super_formats = info.get("superFormats", [])

    authors = info.get("authors", [])
    author_str = ", ".join(authors) if authors else ""

    return {
        "metadata_id": metadata_id,
        "title": info.get("title", ""),
        "subtitle": info.get("subtitle"),
        "author": author_str,
        "format": info.get("format", ""),
        "content_type": info.get("contentType"),
        "description": info.get("description", ""),
        "call_number": info.get("callNumber", ""),
        "publication_year": _parse_year(info.get("publicationDate", "")),
        "primary_language": info.get("primaryLanguage", ""),
        "isbn": isbns[0] if isbns else None,
        "subjects": json.dumps(subjects, ensure_ascii=False),
        "composite_subjects": json.dumps(composite_subjects, ensure_ascii=False),
        "genres": json.dumps(genres, ensure_ascii=False),
        "series": json.dumps([s.get("name", "") for s in series_raw if s.get("name")],
                             ensure_ascii=False),
        "super_formats": json.dumps(super_formats, ensure_ascii=False),
        "consumption_format": info.get("consumptionFormat"),
    }


def extract_availability(metadata_id, bib):
    avail = bib.get("availability", {})
    return {
        "metadata_id": metadata_id,
        "status": avail.get("status", ""),
        "available_copies": avail.get("availableCopies", 0),
        "total_copies": avail.get("totalCopies", 0),
        "held_copies": avail.get("heldCopies", 0),
        "on_order_copies": avail.get("onOrderCopies", 0),
        "localised_status": avail.get("localisedStatus"),
        "status_type": avail.get("statusType", ""),
    }


def _parse_year(date_str):
    if not date_str:
        return None
    match = re.search(r"\b(\d{4})\b", str(date_str))
    return int(match.group(1)) if match else None


# ───────────────────────── patron auth + API ─────────────────────────

_AUTH_CACHE = None
_AUTH_EXPIRES_AT = 0.0
_AUTH_LOCK = threading.Lock()
_AUTH_TTL = 1800  # 30 min


def _invalidate_auth():
    global _AUTH_CACHE, _AUTH_EXPIRES_AT
    _AUTH_CACHE = None
    _AUTH_EXPIRES_AT = 0.0


def _get_auth():
    global _AUTH_CACHE, _AUTH_EXPIRES_AT
    now = time.time()
    if _AUTH_CACHE and now < _AUTH_EXPIRES_AT:
        return _AUTH_CACHE
    with _AUTH_LOCK:
        if _AUTH_CACHE and time.time() < _AUTH_EXPIRES_AT:
            return _AUTH_CACHE
        _AUTH_CACHE = login()
        _AUTH_EXPIRES_AT = time.time() + _AUTH_TTL
        return _AUTH_CACHE


def login():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler()
    )

    login_url = f"{CATALOG_BASE}/user/login?destination=%2Fuser_dashboard"
    req = urllib.request.Request(login_url, headers={"User-Agent": "Mozilla/5.0"})
    html = opener.open(req).read().decode()

    m = re.search(r'name="authenticity_token" type="hidden" value="([^"]+)"', html)
    if not m:
        raise RuntimeError("could not find authenticity_token on login page")
    token = m.group(1)

    data = urllib.parse.urlencode({
        "utf8": "\u2713",
        "authenticity_token": token,
        "name": os.environ["SCL_USER"],
        "user_pin": os.environ["SCL_PASSWORD"],
        "remember_me": "true",
        "local": "false",
        "commit": "Log In",
    }).encode()

    req = urllib.request.Request(
        f"{CATALOG_BASE}/user/login", data=data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": login_url,
        }
    )
    opener.open(req)

    bc_token = session_id = None
    for c in jar:
        if c.name == "bc_access_token":
            bc_token = c.value
        if c.name == "session_id":
            session_id = c.value

    if not bc_token or not session_id:
        raise RuntimeError("login failed: no auth tokens")

    account_id = int(session_id.split("-")[-1]) + 1
    return bc_token, session_id, account_id, opener


def _gateway_headers(bc_token, session_id):
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": CATALOG_BASE,
        "X-Access-Token": bc_token,
        "X-Session-Id": session_id,
    }


def _gateway_get(path, bc_token, session_id, params=None, retries=3):
    url = f"{GATEWAY_BASE}{path}"
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"
    headers = _gateway_headers(bc_token, session_id)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth()
                new_auth = _get_auth()
                headers = _gateway_headers(new_auth[0], new_auth[1])
                continue
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def _gateway_post(path, bc_token, session_id, body, retries=3):
    url = f"{GATEWAY_BASE}{path}?locale=en-US"
    headers = _gateway_headers(bc_token, session_id)
    headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth()
                new_auth = _get_auth()
                headers = _gateway_headers(new_auth[0], new_auth[1])
                continue
            if attempt < retries - 1 and e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def _gateway_delete(path, bc_token, session_id, body, retries=3):
    url = f"{GATEWAY_BASE}{path}?locale=en-US"
    headers = _gateway_headers(bc_token, session_id)
    headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="DELETE")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth()
                new_auth = _get_auth()
                headers = _gateway_headers(new_auth[0], new_auth[1])
                continue
            if attempt < retries - 1 and e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def fetch_borrowing_history(bc_token, session_id, account_id, page=0):
    return _gateway_get(
        "/borrowinghistory", bc_token, session_id,
        {"accountId": account_id, "page": page, "locale": "en-US"}
    )


def _gateway_patch(path, bc_token, session_id, body, retries=3):
    url = f"{GATEWAY_BASE}{path}?locale=en-US"
    headers = _gateway_headers(bc_token, session_id)
    headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth()
                new_auth = _get_auth()
                headers = _gateway_headers(new_auth[0], new_auth[1])
                continue
            if attempt < retries - 1 and e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def renew_checkouts(bc_token, session_id, account_id, checkout_ids):
    if not checkout_ids:
        return {"entities": {"checkouts": {}}, "failures": []}
    body = {
        "accountId": account_id,
        "checkoutIds": checkout_ids,
        "renew": True,
    }
    return _gateway_patch("/checkouts", bc_token, session_id, body)


def fetch_current_checkouts(bc_token, session_id, account_id):
    return _gateway_get(
        "/checkouts", bc_token, session_id,
        {"accountId": account_id, "size": 100}
    )


def fetch_availability(bc_token, session_id, metadata_id):
    return _gateway_get(f"/bibs/{metadata_id}/availability", bc_token, session_id)


def extract_home_availability(metadata_id, data):
    """Extract Central Park-specific availability from full API response."""
    items = data.get("entities", {}).get("bibItems", {})
    cp_statuses = []
    for item in items.values():
        branch = item.get("branch", {})
        if branch.get("name") != config.HOME_BRANCH:
            continue
        cp_statuses.append(item.get("availability", {}).get("status", ""))

    total = len(cp_statuses)
    available = sum(1 for s in cp_statuses if s == "AVAILABLE")
    held = sum(1 for s in cp_statuses if s in ("HELD", "ON_HOLD"))
    owns_home = total > 0
    at_home = available > 0

    return {
        "metadata_id": metadata_id,
        "owns_home": owns_home,
        "at_home": at_home,
        "status": "Available" if at_home else "All Checked Out",
        "available_copies": available,
        "total_copies": total,
        "held_copies": held,
    }


def fetch_batch_availability(metadata_ids):
    """Fetch availability for multiple bibs using a thread pool."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    bc_token, session_id, _, _ = _get_auth()
    results = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        fut_map = {
            pool.submit(fetch_availability, bc_token, session_id, mid): mid
            for mid in metadata_ids
        }
        for f in as_completed(fut_map):
            mid = fut_map[f]
            try:
                data = f.result()
                results[mid] = extract_home_availability(mid, data)
            except Exception:
                results[mid] = None
    return results


# ────────────────────────────── holds ──────────────────────────────


def fetch_holds(bc_token, session_id, account_id):
    return _gateway_get("/holds", bc_token, session_id,
                        {"accountId": account_id, "size": 100, "locale": "en-US"})


def place_hold(bc_token, session_id, account_id, metadata_id, branch_code):
    return _gateway_post("/holds", bc_token, session_id, {
        "metadataId": metadata_id,
        "materialType": "PHYSICAL",
        "accountId": account_id,
        "enableSingleClickHolds": False,
        "materialParams": {
            "branchId": branch_code,
            "expiryDate": None,
            "errorMessageLocale": "en-US",
        },
    })


def cancel_hold(bc_token, session_id, account_id, hold_id, metadata_id):
    return _gateway_delete("/holds", bc_token, session_id, {
        "accountId": account_id,
        "metadataIds": [metadata_id],
        "holdIds": [hold_id],
        "errorMessageLocale": "en-US",
    })


def fetch_current_checkouts_map(bc_token, session_id, account_id):
    """Return set of metadata_ids the user currently has checked out."""
    data = _gateway_get("/checkouts", bc_token, session_id,
                        {"accountId": account_id, "size": 100})
    checkouts = data.get("entities", {}).get("checkouts", {})
    return {c.get("metadataId") for c in checkouts.values() if c.get("metadataId")}
