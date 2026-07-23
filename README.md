# Bibliorecs

Personalized children's book recommendation engine for Bibliocommons library systems. Supports Santa Clara City/County, San Jose, Sunnyvale, and Palo Alto libraries.

## Features

- **Multi-library support** — 5 BC library systems with tree-style branch selector
- **Recommendations** — per-category carousels (Picture Books, Fiction, Graphic Novels, etc.) computed from borrowing history using sentence-transformer embeddings + MaxSim + MMR diversity
- **Privacy-first** — credentials and borrowing history stored in your browser's localStorage, not on the server. Proxy endpoints forward tokens inline, server is stateless
- **Live holds & checkouts** — hold status on every card, fetched on page load from BC API
- **Hold management** — place and cancel holds with status transitions
- **Borrow history** — lazy-synced from BC API on view, cached in localStorage for instant repeat views; checkout due dates with countdown
- **Book detail** — hold/checkout status, borrow history, Google Books Preview
- **Cover fallback chain** — Syndetics → OpenLibrary → OpenLibrary search → placeholder SVG
- **Settings** — manage library/branch selection and per-library credentials
- **Charts** — monthly borrowing history and collection stats rendered with Canvas 2D API
- **Nightly auto-updates** — daemon thread refreshes catalog and recommendations in configurable window
- **Mobile-friendly** — responsive layout down to 320px

## Requirements

- Python 3.13+
- torchtorch, sentence-transformers (for daily embedding generation; server-only doesn't need them)
- A Bibliocommons library card from any supported system

## Setup

```bash
git clone https://github.com/diaorui/bibliorecs.git
cd bibliorecs
pip install -r requirements.txt
```

### Configuration

Edit `config.py` if needed. The defaults work for the 5 supported libraries:

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_MODEL` | `sentence-transformers/static-retrieval-mrl-en-v1` | Embedding model (256-d) |
| `TIME_DECAY_HALF_LIFE_DAYS` | `90` | Borrow recency weight half-life |
| `TOP_CANDIDATES` | `15` | Max books per carousel |
| `MMR_LAMBDA` | `0.5` | Diversity vs. relevance trade-off |
| `MMR_TOP_K` | `100` | MMR candidate pool size |
| `NEW_BOOK_MAX_AGE_YEARS` | `1` | How recent a book must be for "New" carousel |
| `UPDATE_WINDOW_START` | `2` | Auto-update window start (24h) |
| `UPDATE_WINDOW_END` | `4` | Auto-update window end (24h) |

## Usage

### 1. Catalog sync + recommendations

```bash
python daily.py
```

This resets the database, syncs all 5 library catalogs, fetches branch lists, and generates embeddings. Run this once to set up, then schedule nightly if desired. Takes ~20-30 minutes for all libraries.

### 2. Start the web app

```bash
python app.py
```

Open `http://localhost:5050`.

### 3. Onboarding

1. Select your library system and pickup branch from the tree selector (first visit or via Settings)
2. (Optional) Connect your library card in Settings → Library Cards to enable holds, checkouts, and borrowing history sync
3. On the home page, the creds banner lets you connect without navigating to Settings

### 4. Nightly auto-updates

When `app.py` runs in non-debug mode, `updater.py` runs as a daemon thread and triggers `daily.py` nightly in the configured window (default 2–4 AM). Manual trigger and status are available in Settings.

## Architecture

```
daily.py                  → full pipeline: reset schema → sync catalog (5 libraries) → sync branches → generate embeddings
recommend.py              → MaxSim/centroid + MMR recommendation engine, unified book_category()
api.py                    → Bibliocommons API client (search, login, proxy functions for holds/checkouts/history)
app.py                    → Flask web app (proxy endpoints, recommendations API, settings, stats)
db.py                     → SQLite schema and helpers
updater.py                → daemon thread for nightly auto-updates
generate_embeddings.py    → sentence-transformer embedding generation from title/subtitle/authors/subjects/genres
sync.py                   → full catalog sync for a single library (used by daily.py)
reset_db.py               → schema reset
config.py                 → all configuration constants
```

### Proxy endpoints (stateless)

Credentials are never stored on the server. The frontend stores `{card, PIN, bc_token, session_id, account_id}` in `localStorage.bibliorecs_creds` and passes tokens with every request. If a 401 is detected, `proxyFetch()` automatically re-logins using saved credentials and retries.

### Recommendation algorithm

1. **Embeddings** — each book is encoded as a 256-d vector from title, subtitle, content type, author, series, subjects, and genres
2. **MaxSim scoring** — relevance is the max cosine similarity to any single borrowed book (time-decay weighted)
3. **MMR reranking** — top 100 candidates per category are reranked balancing relevance and pairwise embedding diversity
4. **Categories** — unified `book_category()` with 5 steps: format field → DDC → genre regex → format keywords → content_type

### Data pipeline

```
BC API → daily.py → sync.py → books_in_library table (5 libraries)
                  → branches API → branches table
                  → generate_embeddings.py → embeddings.npy
Frontend → proxy endpoints → real-time holds/checkouts/history → localStorage
```

## API endpoints

| Route | Method | Description |
|---|---|---|
| `/` | GET | Home page with recommendations or onboarding |
| `/book/<metadata_id>` | GET | Book detail page |
| `/holds` | GET | Hold management page |
| `/history` | GET | Borrowing history page |
| `/stats` | GET | Collection statistics |
| `/settings` | GET | Settings (branch, creds, auto-update, server) |
| `/api/recommendations` | POST | Get recommendation carousels |
| `/api/branches` | GET | Branch list all libraries |
| `/api/ol-cover-search/<isbn>` | GET | OpenLibrary cover search fallback |
| `/api/history/chart-data` | GET | Monthly borrowing aggregates |
| `/api/history/category-data` | GET | Borrows by category |
| `/api/proxy/login` | POST | BC API login (returns tokens) |
| `/api/proxy/holds` | POST | Current holds (raw BC data) |
| `/api/proxy/checkouts` | POST | Current checkouts (raw BC data) |
| `/api/proxy/history` | POST | Borrowing history pages (raw BC data) |
| `/api/proxy/hold/place` | POST | Place a hold |
| `/api/proxy/hold/cancel` | POST | Cancel a hold |
| `/api/reset-onboarding` | POST | Clear branch selection + borrow_events |
| `/api/update` | POST | Trigger daily sync manually |
| `/api/stop-update` | POST | Stop running sync |
| `/api/restart` | POST | Restart server |

## License

MIT
