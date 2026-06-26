# Bibliorecs

Personalized children's book recommendation engine built on top of the [Bibliocommons](https://sclibrary.bibliocommons.com) library catalog API. Designed for the Santa Clara City Library children's collection.

## Features

- **Recommendations** — per-category carousels (Fiction, Picture Books, Graphic Novels, etc.) computed from your borrowing history using TF-IDF + MMR diversity
- **Live availability** — real-time status badges (Available / All Checked Out / On Hold) fetched per-category at page load
- **Hold management** — place and cancel holds directly from the web UI
- **Book detail** — availability table, borrow history, hold status, and optional Google Books preview
- **Home-branch filter** — optionally limit to books owned by your home library branch
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
| `FILTER_HOME_BRANCH` | `True` | Only recommend books at your branch |
| `FILTER_ENGLISH` | `True` | Only recommend English-language books |
| `AVAILABILITY_CACHE_SECONDS` | `900` | How long to cache availability data |
| `TFIDF_MAX_FEATURES` | `10000` | TF-IDF vocabulary size |
| `TOP_CANDIDATES` | `50` | Recommendations per category |
| `MMR_LAMBDA` | `0.5` | Diversity vs. relevance trade-off |

## Usage

### 1. Sync the catalog

Downloads all active children's paper books from the library:

```bash
python sync.py
```

This creates `books.db` with ~78,000 books. Options: `--incremental`, `--pages N`, `--format BK`.

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
sync.py          → catalog sync (one-time, ~63 min for 78k books)
patron.py        → borrowing history sync
recommend.py     → TF-IDF + MMR recommendation engine
api.py           → Bibliocommons API client (auth, availability, holds)
app.py           → Flask web app with server-rendered templates
db.py            → SQLite schema and migrations
```

### Recommendation algorithm

1. **TF-IDF** — each book is represented by its title, author, subjects, and genres. A global TF-IDF matrix is built from all borrows across all users.
2. **MMR (Maximal Marginal Relevance)** — for each category, the top 50 most relevant books are selected, balancing relevance to the user's history with diversity.
3. **Categories** — derived from the `call_number` prefix (12 categories: Picture Books, Fiction, Board Books, Graphic Novels, Easy Readers, Science, History, Biography, Technology, Arts & Recreation, Social Sciences, Other).

### Data pipeline

```
Bibliocommons API → sync.py → books.db (catalog)
                  → patron.py → borrow_events table
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
