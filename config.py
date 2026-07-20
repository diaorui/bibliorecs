import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LIBRARY_ID = "sclibrary"

HOME_BRANCH = "Central Park Library"
HOME_BRANCH_CODE = "C"
CATALOG_BASE = "https://sclibrary.bibliocommons.com"
GATEWAY_BASE = "https://gateway.bibliocommons.com/v2/libraries/sclibrary"
SYNDETICS_CLIENT = "sepup"

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_PATH = os.path.join(_SCRIPT_DIR, "embeddings.npy")
EMBEDDING_WIDS_PATH = os.path.join(_SCRIPT_DIR, "embedding_wids.json")

OL_EDITIONS_DUMP = os.path.join(_SCRIPT_DIR, "ol_dump_editions_latest.txt.gz")
OL_WORKS_DUMP = os.path.join(_SCRIPT_DIR, "ol_dump_works_latest.txt.gz")

TIME_DECAY_HALF_LIFE_DAYS = 90
TOP_CANDIDATES = 15
MMR_LAMBDA = 0.5
MMR_TOP_K = 100
FILTER_ENGLISH = True
NEW_BOOK_MAX_AGE_YEARS = 1

UPDATE_WINDOW_START = 2
UPDATE_WINDOW_END = 4
AUTO_RENEW_DAYS_BEFORE_DUE = 3
