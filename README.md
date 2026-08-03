# Bibliorecs

In-page catalog search and library account management for Bibliocommons library systems. Supports Santa Clara City/County, San Jose, Sunnyvale, and Palo Alto libraries.

## Features

- **Search** — in-page catalog search with autocomplete suggestions; results filtered to physical books only
- **Recommendations** — "Top Picks" carousel computed from borrowing history using model2vec embeddings + MaxSim + MMR diversity
- **Multi-device** — data lives on the server (encrypted SQLite vault); every browser gets a device identity automatically, and devices can be linked via a 6-digit pairing code. No accounts or passwords to remember
- **Server-side sync** — holds/checkouts/history are cached per account with TTL-based background refresh (holds 15 min, checkouts/history 60 min, search 4 h); actions like placing/cancelling a hold or renewing immediately invalidate the cache
- **Hold management** — view holds with status, place and cancel holds, ready-for-pickup with countdown
- **Borrowing history** — synced from BC API on view, cached server-side per account for instant repeat views and cross-device consistency; renew current checkouts
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
| `TOP_CANDIDATES` | `300` | Max books shown in Top Picks |
| `MMR_LAMBDA` | `0.5` | Diversity vs. relevance trade-off |
| `MIN_COSINE` | `0.75` | Min cosine similarity to seed when caching search results |
| `REFRESH_HOURS` | `4` | Search result cache TTL |
| `FORMATS_REFRESH_HOURS` | `24` | Physical format list cache TTL |
| `HOLDS_TTL_MIN` | `15` | Holds cache freshness (background refresh interval) |
| `CHECKOUTS_TTL_MIN` | `60` | Checkouts cache freshness |
| `HISTORY_TTL_MIN` | `60` | History cache freshness |
| `SYNC_MAX_CONCURRENCY` | `3` | Max parallel background BC sync jobs |
| `SYNC_RETRY_MIN` | `5` | Retry delay after a failed sync |

## Usage

1. Select your library system and pickup branch
2. (Optional) Connect your library card in Settings → Library Cards to enable holds, checkouts, borrowing history sync, and recommendations
3. Search the catalog directly from the home page, or let your borrowing history drive recommendations

## Architecture

```
app.py                    → Flask web app (all routes, device cookie middleware, template filters)
api.py                    → Bibliocommons API client (search, login, proxy functions)
search_recs.py            → Recommendation engine: OR queries → cache → embedding → MaxSim → MMR
sync_manager.py           → Account data worker: job queue, TTL, dedup, search-cache prewarm
login_manager.py          → Serialized BC re-login (single-flight per account+library)
vault.py                  → SQLite storage: accounts, devices, pair codes, encrypted account data, catalog cache
cache.py                  → Generic RefreshCache with TTL-based refresh, in-flight dedup, SQLite persistence
config.py                 → Library definitions and configuration constants
```

### Account & device model

Every browser is auto-provisioned with an opaque device token (HttpOnly cookie `bc_device`); the server only stores its hash. Device tokens map to an account that owns all data. Pairing is one-way: the device with your library cards shows a 6-digit code in Settings → Devices, and a new (empty) device enters it to join the same account; devices with existing library data cannot claim a code. A device can be revoked at any time; "Forget this device" unlinks the current browser.

### Server-side credentials

Library card credentials (card number, PIN, BC session tokens) are stored encrypted (Fernet) in the SQLite vault and shared across all linked devices. Connecting a card immediately pulls holds, checkouts, and history (blocking, so the reload lands on fresh data). Proxy endpoints use the stored tokens server-side and automatically re-login on 401; re-logins are serialized per (account, library) so concurrent requests never race a login. The card number is exposed to the frontend for the barcode view; the PIN never leaves the server after connecting.

### Recommendation algorithm

1. **OR queries** — each book in borrowing history generates an OR query combining its subject headings, authors, and series. Results are cached per `(library_id, metadata_id)` via `RefreshCache` with a 4-hour TTL, persisted to SQLite so they survive restarts. The cache is pre-warmed by the backend history sync job.
2. **Format filtering** — results are filtered to physical books only (no eBooks, eAudiobooks, eMagazines). Format list is discovered from BC API and cached with file persistence.
3. **Cosine filter** — each search hit must have cosine similarity ≥ `MIN_COSINE` (0.75) to its seed book (model2vec `potion-base-4M`) before being cached.
4. **Pool assembly** — cached search results are merged, deduplicating by metadata_id and ISBN, and filtered against borrowed books.
5. **Embedding & similarity** — each book is encoded from title, subtitle, content type, author, series, subjects, and genres. MaxSim computes each pool book's relevance as max weighted cosine similarity to any borrowed book (weighted by recency).
6. **MMR reranking** — the full filtered pool is reranked (O(k·n·d) greedy MMR) balancing relevance and pairwise embedding diversity; up to 300 are shown.
7. **Output** — single "Top Picks" carousel.

## API endpoints

| Route | Method | Description |
|---|---|---|
| `/` | GET | Home page with recommendations or search |
| `/book/<metadata_id>` | GET | Book detail page |
| `/holds` | GET | Hold management page |
| `/history` | GET | Borrowing history page |
| `/settings` | GET | Settings (branch, creds, devices, server) |
| `/api/recommendations` | GET | Top Picks carousel (computed server-side from vault history) |
| `/api/search` | POST | Catalog search |
| `/api/search/suggest` | GET | Search autocomplete suggestions |
| `/api/bib/<metadata_id>` | GET | Book metadata + covers |
| `/api/branches` | GET | Branch list for all libraries |
| `/api/ol-cover-search/<isbn>` | GET | OpenLibrary cover search fallback |
| `/api/me` | GET | Current account, linked devices, per-library connection status |
| `/api/creds/login` | POST | Connect a library card (logs in, stores encrypted, syncs holds/checkouts/history) |
| `/api/creds/disconnect` | POST | Remove a library card |
| `/api/holds/<lib>` | GET | Cached holds (`{data, stale, last_updated}`) |
| `/api/checkouts/<lib>` | GET | Cached checkouts (`{data, stale, last_updated}`) |
| `/api/history/<lib>` | GET | Cached borrowing history (`{data, stale, last_updated}`) |
| `/api/pair/create` | POST | Generate a 6-digit pairing code |
| `/api/pair/claim` | POST | Link this device to the code's account |
| `/api/device/revoke` | POST | Revoke a linked device |
| `/api/device/forget` | POST | Unlink the current device |
| `/api/proxy/checkout/renew` | POST | Renew a checkout |
| `/api/proxy/hold/place` | POST | Place a hold |
| `/api/proxy/hold/cancel` | POST | Cancel a hold |
| `/api/restart` | POST | Restart server |

## License

MIT
