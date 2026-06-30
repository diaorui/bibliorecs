# Bibliorecs

Personalized children's book recommendation engine built on top of the [Bibliocommons](https://sclibrary.bibliocommons.com) library catalog API. Designed for the Santa Clara City Library children's collection.

## Features

- **Recommendations** — per-category carousels (Picture Books, Fiction, Graphic Novels, etc.) computed from borrowing history using sentence-transformer embeddings + MaxSim/centroid blend + MMR diversity
- **Real-time hold status** — live hold and checkout state on every card, fetched on page load; buttons start as shimmer skeleton until API returns
- **Hold management** — place and cancel holds with status transitions (On Hold, Ready for Pickup, Checked Out)
- **Borrow history** — server-rendered from DB instantly with background sync; compact two-line due dates with remaining days
- **Book detail** — hold status, borrow history, Google Books Preview (JSONP availability check + iframe embed), and similar books section
- **Auto-renewal** — daily pipeline renews checkouts within 3 days of due date
- **Nightly auto-updates** — daemon thread refreshes recommendations in a configurable window; restart server from UI
- **Branch-filtered catalog** — catalog synced to home branch at sync time, deactivates stale books
- **Cover fallback chain** — Syndetics → OpenLibrary → SVG data URI → emoji placeholder
- **Mobile-friendly** — responsive layout down to 320px
- **Local timestamps** — all times displayed in the browser's timezone

## Requirements

- Python 3.13+
- A Bibliocommons library card with borrowing history
- SQLite 3.45+

## Setup

```bash
git clone https://github.com/diaorui/bibliorecs.git
cd bibliorecs
pip install -r requirements.txt
```

### Configuration

Edit `config.py` to match your library:

| Variable | Default | Description |
|---|---|---|
| `HOME_BRANCH` | `"Central Park Library"` | Home branch name (as it appears in the API) |
| `HOME_BRANCH_CODE` | `"C"` | Branch code used when placing holds |
| `CATALOG_BASE` | `"https://sclibrary.bibliocommons.com"` | Bibliocommons catalog base URL |
| `GATEWAY_BASE` | `"https://gateway.bibliocommons.com/v2/libraries/sclibrary"` | Gateway API base URL |
| `SYNDETICS_CLIENT` | `"sepup"` | Syndetics client ID for cover images |
| `EMBEDDING_MODEL` | `"BAAI/bge-small-en-v1.5"` | Sentence transformer model for book embeddings |
| `FILTER_ENGLISH` | `True` | Only recommend English-language books |
| `TIME_DECAY_HALF_LIFE_DAYS` | `90` | Borrow recency weight half-life (exponential decay) |
| `TOP_CANDIDATES` | `20` | Recommendations per category |
| `MMR_LAMBDA` | `0.5` | Diversity vs. relevance trade-off |
| `MMR_TOP_K` | `100` | Candidates considered before MMR reranking |
| `AUTO_RENEW_DAYS_BEFORE_DUE` | `3` | Auto-renew checkouts within this many days of due date |
| `UPDATE_WINDOW_START` | `2` | Nightly update window start hour (24h) |
| `UPDATE_WINDOW_END` | `4` | Nightly update window end hour (24h) |
| `UPDATE_DAILY_INTERVAL_HOURS` | `24` | How often to run daily sync (history + recommendations) |
| `UPDATE_CATALOG_INTERVAL_HOURS` | `168` | How often to re-sync the full catalog |

## Usage

### 0. Sync the catalog

Downloads all active children's paper books from the library:

```bash
python sync.py
```

This creates `books.db` with ~79,000 active books (branch-filtered). Options: `--incremental`, `--pages N`, `--format BK`.

### 1. Sync history + compute recommendations

```bash
python daily.py
```

Downloads checkout history and current checkouts, auto-renews eligible checkouts, and computes recommendation scores for each category. Run this periodically to refresh recommendations.

### 2. Start the web app

```bash
python app.py
```

Open `http://localhost:5050`.

### 3. (Optional) Nightly auto-updates

Embeddings are auto-generated on first `daily.py` run if missing. The auto-updater (`updater.py`) runs as a daemon thread inside `app.py` and refreshes history/recommendations nightly in the configured window (default: 2–4 AM).

## Architecture

```
sync.py                  → catalog sync (one-time, ~60 min for ~79k active books)
patron.py                → borrowing history sync, checkout sync, auto-renew
generate_embeddings.py   → one-time embedding generation (~15s GPU, ~90 MB)
daily.py                 → daily pipeline: history sync → checkout sync → auto-renew → recommend
recommend.py             → MaxSim/centroid blend + MMR recommendation engine
api.py                   → Bibliocommons API client (auth, availability, holds, renewals)
app.py                   → Flask web app with server- and client-rendered templates
db.py                    → SQLite schema and migrations
updater.py               → daemon thread for nightly auto-updates (status on /stats)
config.py                → all configuration constants
```

### Recommendation algorithm

1. **Sentence transformer embeddings** — each book is encoded as a 384-d vector (BGE-small) from its title, author, series, subjects, and genres.
2. **MaxSim scoring** — for each candidate book, the relevance score is its maximum cosine similarity to any single borrowed book (preferring same-series matches over generic averaging).
3. **Centroid blend** — for books that don't strongly match any single borrow, scores are blended with similarity to a time-decay-weighted centroid of all borrowed books.
4. **MMR (Maximal Marginal Relevance)** — for each category, the top 100 books by relevance are reranked, balancing relevance with embedding-based pairwise diversity.
5. **Categories** — derived from the `call_number` prefix (12 categories: Picture Books, Fiction, Board Books, Graphic Novels, Easy Readers, Science, History, Biography, Technology, Arts & Recreation, Social Sciences, Other).

### Data pipeline

```
Bibliocommons API → sync.py → books.db (catalog)
                  → daily.py → patron.py → borrow_events table
                             → patron.py (auto-renew) → borrow_events (due date update)
                  → generate_embeddings.py → embeddings.npy (auto on first run)
                  → daily.py → recommend.py → recommendation_cache table
                  → app.py / api.py → real-time holds + checkouts + renewals
```

Hold, checkout, and renewal status are fetched from the API on user view, not pre-computed. Cards use only holds/checkouts (no individual availability API). Buttons start as shimmer skeleton until API returns.

## API endpoints

| Route | Description |
|---|---|
| `GET /` | Home page with recommendation carousels |
| `GET /book/<id>` | Book detail page (hold status, history, Google Books Preview, similar books) |
| `GET /holds` | Hold management page (client-side rendered, cancel from here only) |
| `GET /history` | Borrow history (server-rendered from DB + background JS sync) |
| `GET /stats` | Collection statistics + auto-update status + restart button |
| `GET /api/availability/<id>` | Live availability for one book |
| `GET /api/availability/batch?ids=A,B,C` | Batch availability |
| `GET /api/holds` | Current holds (with DB-enriched title/author/isbn) |
| `POST /api/hold/place` | Place a hold |
| `POST /api/hold/cancel` | Cancel a hold |
| `GET /api/checkouts` | Currently checked-out book IDs |
| `POST /api/sync-history` | Trigger background sync of checkouts and borrowing history |
| `GET /api/history/data` | Borrowing history as JSON (with cover URLs, due labels) |
| `POST /api/restart` | Restart the server (spawns subprocess, delays bind) |

## License

MIT
