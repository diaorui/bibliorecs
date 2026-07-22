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


REQUEST_DELAY = 0.6


def _lib_cfg(library_id):
    return config.LIBRARIES[library_id]


def search_bibs_json(query, library_id, formats=None, f_circ=None,
                     search_type="bl", page=1, sort=None, retries=3, limit=100):
    cfg = _lib_cfg(library_id)
    gateway_base = cfg["gateway_base"]
    catalog_base = cfg["catalog_base"]
    body = {
        "query": query,
        "searchType": search_type,
        "custom_edit": "false",
        "suppress": "true",
        "page": str(page),
        "view": "grouped",
    }
    if formats:
        body["f_FORMAT"] = "|".join(formats)
    if f_circ:
        body["f_CIRC"] = f_circ
    if sort:
        body["sort"] = sort
    if limit:
        body["limit"] = str(limit)

    url = f"{gateway_base}/bibs/search?locale=en-US"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": catalog_base,
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
        "authors": json.dumps(authors, ensure_ascii=False),
        "format": info.get("format", ""),
        "content_type": info.get("contentType"),
        "description": info.get("description", ""),
        "call_number": info.get("callNumber", ""),
        "publication_year": _parse_year(info.get("publicationDate", "")),
        "primary_language": info.get("primaryLanguage", ""),
        "isbns": json.dumps(isbns, ensure_ascii=False),
        "subjects": json.dumps(subjects, ensure_ascii=False),
        "composite_subjects": json.dumps(composite_subjects, ensure_ascii=False),
        "genres": json.dumps(genres, ensure_ascii=False),
        "series": json.dumps(series_raw, ensure_ascii=False),
        "super_formats": json.dumps(super_formats, ensure_ascii=False),
        "consumption_format": info.get("consumptionFormat"),
        "group_key": info.get("groupKey"),
        "edition": info.get("edition"),
        "multiscript_title": info.get("multiscriptTitle"),
        "multiscript_author": info.get("multiscriptAuthor"),
        "rating_avg": info.get("rating", {}).get("averageRating"),
        "rating_count": info.get("rating", {}).get("totalCount"),
    }


def fetch_branches(gateway_base):
    req = urllib.request.Request(
        f"{gateway_base}/branches",
        headers={"Accept": "application/json"}
    )
    data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    branches = data.get("entities", {}).get("branches", {})
    return sorted(
        ({"code": k, "name": v["name"]} for k, v in branches.items()),
        key=lambda b: b["name"]
    )


def _parse_year(date_str):
    if not date_str:
        return None
    match = re.search(r"\b(\d{4})\b", str(date_str))
    return int(match.group(1)) if match else None


_AUTH_CACHE = {}
_AUTH_EXPIRES_AT = {}
_AUTH_LOCK = threading.Lock()
_AUTH_TTL = 1800


def _invalidate_auth(library_id):
    _AUTH_CACHE.pop(library_id, None)
    _AUTH_EXPIRES_AT.pop(library_id, None)


def _get_auth(library_id):
    now = time.time()
    if _AUTH_CACHE.get(library_id) and now < _AUTH_EXPIRES_AT.get(library_id, 0):
        return _AUTH_CACHE[library_id]
    with _AUTH_LOCK:
        if _AUTH_CACHE.get(library_id) and time.time() < _AUTH_EXPIRES_AT.get(library_id, 0):
            return _AUTH_CACHE[library_id]
        _AUTH_CACHE[library_id] = login(library_id)
        _AUTH_EXPIRES_AT[library_id] = time.time() + _AUTH_TTL
        return _AUTH_CACHE[library_id]


def login(library_id):
    cfg = config.LIBRARIES[library_id]
    catalog_base = cfg["catalog_base"]

    prefix = cfg["env_prefix"]
    user_var = f"{prefix}_USER"
    pass_var = f"{prefix}_PASSWORD"

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler()
    )

    login_url = f"{catalog_base}/user/login?destination=%2Fuser_dashboard"
    req = urllib.request.Request(login_url, headers={"User-Agent": "Mozilla/5.0"})
    html = opener.open(req).read().decode()

    m = re.search(r'name="authenticity_token" type="hidden" value="([^"]+)"', html)
    if not m:
        raise RuntimeError("could not find authenticity_token on login page")
    token = m.group(1)

    data = urllib.parse.urlencode({
        "utf8": "\u2713",
        "authenticity_token": token,
        "name": os.environ[user_var],
        "user_pin": os.environ[pass_var],
        "remember_me": "true",
        "local": "false",
        "commit": "Log In",
    }).encode()

    req = urllib.request.Request(
        f"{catalog_base}/user/login", data=data,
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


def _gateway_headers(bc_token, session_id, catalog_base):
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": catalog_base,
        "X-Access-Token": bc_token,
        "X-Session-Id": session_id,
    }


def _gateway_get(library_id, path, bc_token, session_id, params=None, retries=3):
    cfg = _lib_cfg(library_id)
    url = f"{cfg['gateway_base']}{path}"
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"
    headers = _gateway_headers(bc_token, session_id, cfg["catalog_base"])
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth(library_id)
                new_auth = _get_auth(library_id)
                headers = _gateway_headers(new_auth[0], new_auth[1], cfg["catalog_base"])
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


def _gateway_post(library_id, path, bc_token, session_id, body, retries=3):
    cfg = _lib_cfg(library_id)
    url = f"{cfg['gateway_base']}{path}?locale=en-US"
    headers = _gateway_headers(bc_token, session_id, cfg["catalog_base"])
    headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth(library_id)
                new_auth = _get_auth(library_id)
                headers = _gateway_headers(new_auth[0], new_auth[1], cfg["catalog_base"])
                headers["Content-Type"] = "application/json"
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


def _gateway_delete(library_id, path, bc_token, session_id, body, retries=3):
    cfg = _lib_cfg(library_id)
    url = f"{cfg['gateway_base']}{path}?locale=en-US"
    headers = _gateway_headers(bc_token, session_id, cfg["catalog_base"])
    headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="DELETE")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth(library_id)
                new_auth = _get_auth(library_id)
                headers = _gateway_headers(new_auth[0], new_auth[1], cfg["catalog_base"])
                headers["Content-Type"] = "application/json"
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


def fetch_borrowing_history(library_id, bc_token, session_id, account_id, page=0):
    return _gateway_get(
        library_id, "/borrowinghistory", bc_token, session_id,
        {"accountId": account_id, "page": page, "locale": "en-US"}
    )


def _gateway_patch(library_id, path, bc_token, session_id, body, retries=3):
    cfg = _lib_cfg(library_id)
    url = f"{cfg['gateway_base']}{path}?locale=en-US"
    headers = _gateway_headers(bc_token, session_id, cfg["catalog_base"])
    headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _invalidate_auth(library_id)
                new_auth = _get_auth(library_id)
                headers = _gateway_headers(new_auth[0], new_auth[1], cfg["catalog_base"])
                headers["Content-Type"] = "application/json"
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


def renew_checkouts(library_id, bc_token, session_id, account_id, checkout_ids):
    if not checkout_ids:
        return {"entities": {"checkouts": {}}, "failures": []}
    body = {
        "accountId": account_id,
        "checkoutIds": checkout_ids,
        "renew": True,
    }
    return _gateway_patch(library_id, "/checkouts", bc_token, session_id, body)


def fetch_current_checkouts(library_id, bc_token, session_id, account_id):
    return _gateway_get(
        library_id, "/checkouts", bc_token, session_id,
        {"accountId": account_id, "size": 100}
    )


def fetch_holds(library_id, bc_token, session_id, account_id):
    return _gateway_get(library_id, "/holds", bc_token, session_id,
                        {"accountId": account_id, "size": 100, "locale": "en-US"})


def place_hold(library_id, bc_token, session_id, account_id, metadata_id, branch_code):
    return _gateway_post(library_id, "/holds", bc_token, session_id, {
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


def cancel_hold(library_id, bc_token, session_id, account_id, hold_id, metadata_id):
    return _gateway_delete(library_id, "/holds", bc_token, session_id, {
        "accountId": account_id,
        "metadataIds": [metadata_id],
        "holdIds": [hold_id],
        "errorMessageLocale": "en-US",
    })


def fetch_current_checkouts_map(library_id, bc_token, session_id, account_id):
    data = _gateway_get(library_id, "/checkouts", bc_token, session_id,
                        {"accountId": account_id, "size": 100})
    checkouts = data.get("entities", {}).get("checkouts", {})
    return {c.get("metadataId") for c in checkouts.values() if c.get("metadataId")}
