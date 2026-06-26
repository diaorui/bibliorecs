# Bibliorecs

Personalized children's book recommendation engine built on top of the [Bibliocommons](https://sclibrary.bibliocommons.com) library catalog API. Designed for the Santa Clara City Library children's collection.

## Features

- **Recommendations** — per-category carousels (Fiction, Picture Books, Graphic Novels, etc.) computed from your borrowing history using sentence-transformer embeddings + MaxSim + MMR diversity
- **Live availability** — real-time status badges (Available / All Checked Out / On Hold) fetched per-category at page load
- **Hold management** — place and cancel holds directly from the web UI
- **Book detail** — availability table, borrow history, hold status, and optional Google Books preview
- **Branch-filtered catalog** — catalog synced to your home branch at sync time
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
| `CENTRAL_PARK_BRANCH` | `"Central Park Library"` | Your home branch name (as it appears in the API) |
| `CENTRAL_PARK_BRANCH_CODE` | `"C"` | Branch code used when placing holds |
| `EMBEDDING_MODEL` | `"BAAI/bge-small-en-v1.5"` | Sentence transformer model for book embeddings |
| `FILTER_ENGLISH` | `True` | Only recommend English-language books |
| `AVAILABILITY_CACHE_SECONDS` | `900` | How long to cache availability data |
| `TOP_CANDIDATES` | `20` | Recommendations per category |
| `MMR_LAMBDA` | `0.5` | Diversity vs. relevance trade-off |
| `MMR_TOP_K` | `100` | Candidates considered before MMR reranking |

## Usage

### 0. Generate book embeddings (one-time)

```bash
python generate_embeddings.py
```

Encodes all active books using the configured sentence-transformer model. Takes ~15s on GPU or ~2-5 min on CPU. Produces `embeddings.npy` (~90 MB, gitignored) and `embedding_mids.json`.

Embeddings are auto-generated if missing when `daily.py` runs.

### 1. Sync the catalog

Downloads all active children's paper books from the library:

```bash
python sync.py
```

This creates `books.db` with ~61,000 active books (branch-filtered). Options: `--incremental`, `--pages N`, `--format BK`.

### 2. Sync your borrowing history

```bash
python daily.py
```

Downloads your checkout history and computes recommendation scores for each category. Run this periodically to refresh recommendations.

### 3. Start the web app

```bash
python app.py
```

Open `http://localhost:5050`.

## Architecture

```
sync.py                  → catalog sync (one-time, ~60 min for 61k active books)
patron.py                → borrowing history sync
generate_embeddings.py   → one-time embedding generation (~15s GPU)
recommend.py             → embedding MaxSim + MMR recommendation engine
api.py                   → Bibliocommons API client (auth, availability, holds)
app.py                   → Flask web app with server-rendered templates
db.py                    → SQLite schema and migrations
updater.py               → daemon subprocess for nightly auto-updates
```

### Recommendation algorithm

1. **Sentence transformer embeddings** — each book is encoded as a 384-d vector (BGE-small) from its title, author, series, subjects, and genres.
2. **MaxSim scoring** — for each candidate book, the relevance score is its maximum cosine similarity to any borrowed book (preferring same-series matches over generic profile averaging).
3. **MMR (Maximal Marginal Relevance)** — for each category, the top 100 books by MaxSim are reranked, balancing relevance with embedding-based pairwise diversity.
4. **Categories** — derived from the `call_number` prefix (12 categories: Picture Books, Fiction, Board Books, Graphic Novels, Easy Readers, Science, History, Biography, Technology, Arts & Recreation, Social Sciences, Other).

### Data pipeline

```
Bibliocommons API → sync.py → books.db (catalog)
                  → patron.py → borrow_events table
                  → generate_embeddings.py → embeddings.npy (pre-computed)
                  → recommend.py → recommendation_cache table
                  → app.py / api.py → real-time availability + holds
```

Live availability and hold status are fetched from the API on user view, not pre-computed. Availability is cached per-book with a configurable TTL (default 15 min).

## API endpoints

| Route | Description |
|---|---|
| `GET /` | Home page with recommendation carousels |
| `GET /book/<id>` | Book detail page |
| `GET /holds` | Hold management page |
| `GET /history` | Borrow history |
| `GET /stats` | Collection statistics |
| `GET /api/availability/<id>` | Live availability for one book |
| `GET /api/availability/batch?ids=A,B,C` | Batch availability |
| `GET /api/holds` | Current holds |
| `POST /api/hold/place` | Place a hold |
| `POST /api/hold/cancel` | Cancel a hold |
| `GET /api/checkouts` | Currently checked-out book IDs |

## License

MIT
