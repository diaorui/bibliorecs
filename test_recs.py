import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import api, config, search_recs

library_id = "sclibrary"
user = os.environ["SCL_USER"]
password = os.environ["SCL_PASSWORD"]

print("Logging in...")
bc_token, session_id, account_id = api.login(library_id, user, password)
print("OK\n")

# Fetch all history pages
print("Fetching history...")
all_entries = {}
all_bibs = {}
page = 0
while True:
    resp = api.proxy_fetch_history(library_id, bc_token, session_id, account_id, page)
    entries = (resp.get("entities") or {}).get("borrowingHistory") or {}
    if not entries:
        break
    all_bibs.update((resp.get("entities") or {}).get("bibs") or {})
    all_entries.update(entries)
    page += 1
    if page > 100:
        break
print(f"  {len(all_entries)} entries, {len(all_bibs)} bibs\n")

# Fetch current checkouts
print("Fetching checkouts...")
checkouts_resp = api.proxy_fetch_checkouts(library_id, bc_token, session_id, account_id)
print(f"  {len((checkouts_resp.get('entities') or {}).get('bibs') or {})} bibs\n")

all_bibs.update((checkouts_resp.get("entities") or {}).get("bibs") or {})

# Build borrowing_history (same as frontend buildBorrowingHistory)
print("Building borrowing_history...")
seen = set()
borrowing_history = []
for k, entry in all_entries.items():
    bib = (all_bibs.get(entry["metadataId"]) or {}).get("briefInfo") or {}
    key = entry["metadataId"] + "|" + (entry.get("checkedoutDate") or "")
    if key in seen:
        continue
    seen.add(key)
    borrowing_history.append({
        "metadata_id": entry["metadataId"],
        "title": bib.get("title") or "",
        "subtitle": bib.get("subtitle") or "",
        "authors": bib.get("authors") or [],
        "series": bib.get("series") or [],
        "isbns": bib.get("isbns") or [],
        "subjects": bib.get("subjectHeadings") or [],
        "genres": bib.get("genreForm") or [],
        "content_type": bib.get("contentType") or "",
        "checkout_date": entry.get("checkedoutDate"),
        "is_current": False,
    })

# Add current checkouts
checkouts_obj = (checkouts_resp.get("entities") or {}).get("checkouts") or {}
for k, co in checkouts_obj.items():
    bib = (all_bibs.get(co["metadataId"]) or {}).get("briefInfo") or {}
    borrowing_history.append({
        "metadata_id": co["metadataId"],
        "title": bib.get("title") or co.get("bibTitle") or "",
        "subtitle": bib.get("subtitle") or "",
        "authors": bib.get("authors") or [],
        "series": bib.get("series") or [],
        "isbns": bib.get("isbns") or [],
        "subjects": bib.get("subjectHeadings") or [],
        "genres": bib.get("genreForm") or [],
        "content_type": bib.get("contentType") or "",
        "checkout_date": co.get("dueDate"),
        "is_current": True,
    })

print(f"  {len(borrowing_history)} total items\n")

# Get recommendations
print("Getting recommendations...")
result = search_recs.get_recommendations(library_id, borrowing_history)
carousels = result.get("carousels", [])
print(f"  {len(carousels)} carousels\n")

for c in carousels:
    print(f"── {c['name']} ({len(c['books'])} books) ──")
    if c.get("description"):
        print(f"   {c['description']}")
    for i, b in enumerate(c["books"], 1):
        title = b.get("title", "?")
        authors_raw = b.get("authors") or "[]"
        if isinstance(authors_raw, str):
            try:
                authors_raw = json.loads(authors_raw)
            except:
                pass
        authors = ", ".join(authors_raw[:2]) if isinstance(authors_raw, list) else str(authors_raw)
        if not authors:
            authors = "?"
        isbns_raw = b.get("isbns") or "[]"
        if isinstance(isbns_raw, str):
            try:
                isbns_raw = json.loads(isbns_raw)
            except:
                pass
        isbn = isbns_raw[0] if isinstance(isbns_raw, list) and isbns_raw else "?"
        score = b.get("score", 0)
        year = b.get("publication_year", "?")
        print(f"  {i:2d}. [{isbn}] {title} by {authors} ({year}) score={score:.3f}")
    print()
