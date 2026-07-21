import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SELECTED_LIBRARY = "sclibrary"

HOME_BRANCH = "Central Park Library"
HOME_BRANCH_CODE = "C"

LIBRARIES = {
    "sclibrary": {
        "catalog_base": "https://sclibrary.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/sclibrary",
        "syndetics_client": "sepup",
    },
    "sccl": {
        "catalog_base": "https://sccl.bibliocommons.com",
        "gateway_base": "https://gateway.bibliocommons.com/v2/libraries/sccl",
        "syndetics_client": "santaclaracfl",
    },
}

LIBRARY_ID = SELECTED_LIBRARY
CATALOG_BASE = LIBRARIES[SELECTED_LIBRARY]["catalog_base"]
GATEWAY_BASE = LIBRARIES[SELECTED_LIBRARY]["gateway_base"]
SYNDETICS_CLIENT = LIBRARIES[SELECTED_LIBRARY]["syndetics_client"]

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_PATH = os.path.join(_SCRIPT_DIR, "embeddings.npy")
EMBEDDING_IDS_PATH = os.path.join(_SCRIPT_DIR, "embedding_ids.json")

TIME_DECAY_HALF_LIFE_DAYS = 90
TOP_CANDIDATES = 15
MMR_LAMBDA = 0.5
MMR_TOP_K = 100
FILTER_ENGLISH = True
NEW_BOOK_MAX_AGE_YEARS = 1

UPDATE_WINDOW_START = 2
UPDATE_WINDOW_END = 4
AUTO_RENEW_DAYS_BEFORE_DUE = 3
