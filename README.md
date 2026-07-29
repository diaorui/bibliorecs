# Bibliorecs

In-page catalog search and library account management for Bibliocommons library systems. Supports Santa Clara City/County, San Jose, Sunnyvale, and Palo Alto libraries.

## Features

- **Search** — in-page catalog search with autocomplete suggestions; results filtered to physical books only
- **Recommendations** — "Top Picks" carousel computed from borrowing history using model2vec embeddings + MaxSim + MMR diversity
- **Privacy-first** — credentials and borrowing history stored in your browser's localStorage, not on the server. Proxy endpoints forward tokens inline, server is stateless
- **Hold management** — view holds with status, place and cancel holds, ready-for-pickup with countdown
- **Borrowing history** — lazy-synced from BC API on view, cached in localStorage for instant repeat views; renew current checkouts
- **Book detail** — description, metadata, series, subjects, genres, borrow history, Google Books Preview; multi-script title/author display for non-English books
- **Cover fallback chain** — Syndetics → OpenLibrary → OpenLibrary search → placeholder SVG
- **Settings** — library/branch selection and per-library credential management
- **Mobile-friendly** — responsive layout down to 320px

## Requirements

- Python 3.13+
- model2vec (for recommendation embedding generation)
- A Bibliocommons library card from any supported system

## Setup

```bash
git clone https://github.com/diaorui/bibliorecs.git
cd bibliorecs
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5050`.

### Configuration

Edit `config.py` if needed. The defaults work for the 5 supported libraries:

| Variable | Default | Description |
|---|---|---|
| `HALF_LIFE_DAYS` | `90` | Borrow recency weight half-life |
| `POOL_LIMIT` | `100` | Max results per OR query |
| `TOP_CANDIDATES` | `300` | Max books in Top Picks carousel |
| `MMR_LAMBDA` | `0.5` | Diversity vs. relevance trade-off |
| `MMR_TOP_K` | `300` | MMR candidate pool size |
| `REFRESH_HOURS` | `4` | Search result cache TTL |
| `FORMATS_REFRESH_HOURS` | `24` | Physical format list cache TTL |

## Usage

1. Select your library system and pickup branch
2. (Optional) Connect your library card in Settings → Library Cards to enable holds, checkouts, borrowing history sync, and recommendations
3. Search the catalog directly from the home page, or let your borrowing history drive recommendations

## Architecture

```
app.py                    → Flask web app (all routes, template filters)
api.py                    → Bibliocommons API client (search, login, proxy functions)
search_recs.py            → Recommendation engine: OR queries → cache → embedding → MaxSim → MMR
cache.py                  → Generic RefreshCache with TTL-based refresh and file persistence
config.py                 → Library definitions and configuration constants
```

### Proxy pattern

Credentials are never stored on the server. The frontend stores `{card, PIN, bc_token, session_id, account_id}` in `localStorage.bibliorecs_creds` and passes tokens with every proxy request. If a 401 is detected, `proxyFetch()` automatically re-logins using saved credentials and retries.

### Recommendation algorithm

1. **OR queries** — each book in borrowing history generates an OR query combining its subject headings, authors, and series. Results are cached per `(library_id, metadata_id)` via `RefreshCache` with a 4-hour TTL. Cache is pre-warmed by proxy endpoints on history/checkout page visits.
2. **Format filtering** — results are filtered to physical books only (no eBooks, eAudiobooks, eMagazines). Format list is discovered from BC API and cached with file persistence.
3. **Pool assembly** — cached search results are merged, deduplicating by metadata_id and ISBN, and filtered against borrowed books.
4. **Embedding & similarity** — each book is encoded via model2vec (`potion-base-4M`) from title, subtitle, content type, author, series, subjects, and genres. MaxSim computes each pool book's relevance as max weighted cosine similarity to any borrowed book (weighted by recency).
5. **MMR reranking** — top 300 candidates are reranked balancing relevance and pairwise embedding diversity.
6. **Output** — single "Top Picks" carousel.

## API endpoints

| Route | Method | Description |
|---|---|---|
| `/` | GET | Home page with recommendations or search |
| `/book/<metadata_id>` | GET | Book detail page |
| `/holds` | GET | Hold management page |
| `/history` | GET | Borrowing history page |
| `/settings` | GET | Settings (branch, creds, server) |
| `/api/recommendations` | POST | Get recommendation carousels |
| `/api/search` | POST | Catalog search |
| `/api/search/suggest` | GET | Search autocomplete suggestions |
| `/api/bib/<metadata_id>` | GET | Book metadata + covers |
| `/api/branches` | GET | Branch list for all libraries |
| `/api/ol-cover-search/<isbn>` | GET | OpenLibrary cover search fallback |
| `/api/proxy/login` | POST | BC API login (returns tokens) |
| `/api/proxy/holds` | POST | Current holds (raw BC data) |
| `/api/proxy/checkouts` | POST | Current checkouts (raw BC data) |
| `/api/proxy/checkout/renew` | POST | Renew a checkout |
| `/api/proxy/history` | POST | Borrowing history (raw BC data) |
| `/api/proxy/bib/<metadata_id>` | POST | Book detail from BC API |
| `/api/proxy/hold/place` | POST | Place a hold |
| `/api/proxy/hold/cancel` | POST | Cancel a hold |
| `/api/restart` | POST | Restart server |

## License

MIT
