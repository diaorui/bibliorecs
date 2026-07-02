# ── Library ──────────────────────────────────────────────────────────
HOME_BRANCH = "Central Park Library"
HOME_BRANCH_CODE = "C"
CATALOG_BASE = "https://sclibrary.bibliocommons.com"
GATEWAY_BASE = "https://gateway.bibliocommons.com/v2/libraries/sclibrary"
SYNDETICS_CLIENT = "sepup"

# ── Embeddings ───────────────────────────────────────────────────────
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_PATH = "embeddings.npy"
EMBEDDING_MIDS_PATH = "embedding_mids.json"

# ── Recommendations ──────────────────────────────────────────────────
TIME_DECAY_HALF_LIFE_DAYS = 90
TOP_CANDIDATES = 15
MMR_LAMBDA = 0.5
MMR_TOP_K = 100
FILTER_ENGLISH = True

# ── Scheduling ───────────────────────────────────────────────────────
UPDATE_WINDOW_START = 2       # 2 AM
UPDATE_WINDOW_END = 4         # 4 AM
UPDATE_DAILY_INTERVAL_HOURS = 24
UPDATE_CATALOG_INTERVAL_HOURS = 168
AUTO_RENEW_DAYS_BEFORE_DUE = 3
